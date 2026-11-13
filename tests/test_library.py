"""历史字典回归测试。

重点锁三件事：
1. 扫描只对比、不删记录；文件消失时写「发现时间」，记录留着。
2. 目录整体读不到 / 条目骤减时**不**误标删除。
3. 命中「已存在」时不再检索投递，`/force` 能绕过。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import library                                # noqa: E402
from tmb.config import Config                          # noqa: E402
from tmb.pipeline import Pipeline                      # noqa: E402
from tmb.store import Store                            # noqa: E402


def write(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "Downloads")
        os.makedirs(self.root, exist_ok=True)
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.data["library"]["roots"] = [self.root]
        self.store = Store(os.path.join(self.tmp, "var", "t.sqlite3"))

    def tearDown(self):
        self.store.close()

    def scan(self, roots=None):
        return library.scan(self.cfg, self.store, roots=roots or [self.root])

    def path(self, *parts):
        return os.path.join(self.root, *parts)


class TestNameHelpers(unittest.TestCase):
    def test_normalize_strips_punct_and_case(self):
        self.assertEqual(library.normalize("ABC-123 甲.MKV"), "abc123甲mkv")
        self.assertEqual(library.normalize("ＦＣ２ＰＰＶ－４９９９９４８"), "fc2ppv4999948")
        self.assertEqual(library.normalize("  某 作品_1080p  "), "某作品1080p")

    def test_base_name_drops_ext_and_copy_mark(self):
        cases = {
            "ABC-123 甲.mkv": "abc123甲",
            "ABC-123 甲 (1).mkv": "abc123甲",
            "ABC-123 甲 - 副本": "abc123甲",
            "ABC-123 甲[2].mp4": "abc123甲",
            "ABC-123 甲 copy": "abc123甲",
        }
        for name, want in cases.items():
            kind = library.KIND_DIR if "." not in os.path.splitext(name)[1] else library.KIND_FILE
            with self.subTest(name=name):
                self.assertEqual(library.base_name(name, kind), want)

    def test_base_name_keeps_version_numbers(self):
        # 「Vol.2」这种数字是名字的一部分，不能当成副本标记削掉
        self.assertEqual(library.base_name("某作品 Vol.2.mkv"), "某作品vol2")
        self.assertEqual(library.base_name("作品 1080p"), "作品1080p")


class TestWalk(EnvCase):
    def test_collects_dirs_and_files_and_skips_junk(self):
        write(self.path("A", "a.mkv"), 5000)
        write(self.path("A", "cover.jpg"), 100)
        write(self.path("B", "半截.mkv.part"), 10)     # 下载中间态，不算入库
        write(self.path(".hidden", "x.mkv"), 10)        # 隐藏目录默认跳过
        rows, stats = library.walk([self.root], max_depth=3)
        names = {r["name"] for r in rows}
        self.assertIn("A", names)
        self.assertIn("a.mkv", names)
        self.assertNotIn("半截.mkv.part", names)
        self.assertNotIn(".hidden", names)
        self.assertFalse(stats["truncated"])
        # 目录体积由子文件累加
        dir_a = [r for r in rows if r["name"] == "A"][0]
        self.assertEqual(dir_a["size"], 5100)

    def test_max_depth_limits_descent(self):
        write(self.path("L1", "L2", "L3", "deep.mkv"), 10)
        rows, _ = library.walk([self.root], max_depth=1)
        names = {r["name"] for r in rows}
        self.assertIn("L1", names)
        self.assertNotIn("deep.mkv", names)

    def test_max_items_truncates(self):
        for i in range(10):
            write(self.path("d%02d" % i, "f.mkv"), 10)
        rows, stats = library.walk([self.root], max_depth=2, max_items=5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(stats["truncated"])

    def test_missing_root_reported(self):
        rows, stats = library.walk([os.path.join(self.tmp, "nope")])
        self.assertEqual(rows, [])
        self.assertEqual(len(stats["roots_missing"]), 1)


class TestScanLifecycle(EnvCase):
    def test_add_then_delete_keeps_record(self):
        write(self.path("FC2PPV-4999948 某作品", "片.mp4"), 3000)
        rep = self.scan()
        self.assertEqual(rep["added"], 2)                 # 目录 + 文件
        self.assertEqual(rep["present"], 2)

        # 删掉整个目录，再扫
        import shutil
        shutil.rmtree(self.path("FC2PPV-4999948 某作品"))
        rep = self.scan()
        self.assertEqual(rep["removed"], 2)
        self.assertEqual(rep["present"], 0)
        self.assertEqual(rep["deleted"], 2)               # 记录没被删，只是标记

        rows = self.store.library_map([self.root])
        victim = [r for r in rows.values() if r["name"] == "FC2PPV-4999948 某作品"][0]
        self.assertEqual(victim["status"], library.STATUS_DELETED)
        self.assertAlmostEqual(victim["deleted_ts"], time.time(), delta=120)
        self.assertEqual(victim["deleted_count"], 1)

    def test_reappear_marks_present_but_keeps_deletion_time(self):
        write(self.path("A", "a.mkv"), 100)
        self.scan()
        deleted_at = time.time() - 100
        self.store.library_mark_deleted(self.path("A", "a.mkv"), deleted_at)

        rep = self.scan()
        self.assertEqual(rep["restored"], 1)
        row = self.store.library_map([self.root])[self.path("A", "a.mkv")]
        self.assertEqual(row["status"], library.STATUS_PRESENT)
        # 「曾经什么时间没的」这条信息仍然在
        self.assertAlmostEqual(row["deleted_ts"], deleted_at, delta=1)

    def test_unmounted_root_does_not_wipe_records(self):
        write(self.path("A", "a.mkv"), 100)
        self.scan()
        import shutil
        shutil.rmtree(self.root)                          # 模拟卷没挂上
        rep = self.scan()
        self.assertEqual(rep["removed"], 0)
        self.assertTrue(rep["skipped_roots"])
        self.assertTrue(any("目录不存在" in w for w in rep["warnings"]))
        self.assertEqual(self.store.library_counts()["deleted"], 0)

    def test_sudden_shrink_is_not_treated_as_deletion(self):
        for i in range(60):
            write(self.path("bulk", "f%02d.mkv" % i), 10)
        self.scan()
        for i in range(50):
            os.remove(self.path("bulk", "f%02d.mkv" % i))
        rep = self.scan()
        self.assertEqual(rep["removed"], 0)               # 骤减按「读不到」处理
        self.assertTrue(any("骤减" in w for w in rep["warnings"]))

    def test_scan_records_run(self):
        write(self.path("A", "a.mkv"), 100)
        self.scan()
        runs = self.store.recent_library_scans(3)
        self.assertTrue(runs)
        self.assertEqual(runs[0]["added"], 2)

    def test_no_roots_gives_actionable_error(self):
        self.cfg.data["library"]["roots"] = []
        rep = library.scan(self.cfg, self.store, roots=[])
        self.assertFalse(rep["ok"])
        self.assertIn("监控目录", rep["text"])


class TestLookup(EnvCase):
    def setUp(self):
        super().setUp()
        write(self.path("ABC-123 甲", "ABC-123 甲.mkv"), 5000)
        write(self.path("旧片子", "旧片子.mp4"), 900)
        self.scan()
        self.store.library_mark_deleted(self.path("旧片子"), time.time() - 3600)
        self.store.library_mark_deleted(self.path("旧片子", "旧片子.mp4"), time.time() - 3600)

    def test_present_hit_is_strong(self):
        res = library.lookup(self.store, ["ABC-123 甲"], self.cfg.data)
        self.assertEqual(res["state"], "present")
        self.assertTrue(res["strong"])
        self.assertIn("库里已有", res["verdict"])
        # 目录与里面的文件折叠成一条，不重复报
        self.assertEqual(len(res["matches"]), 1)
        self.assertEqual(res["matches"][0]["kind"], "dir")

    def test_deleted_hit_reports_time(self):
        res = library.lookup(self.store, ["旧片子"], self.cfg.data)
        self.assertEqual(res["state"], "deleted")
        self.assertIn("发现已删除", res["text"])
        self.assertTrue(res["matches"][0]["deleted_ts"])

    def test_partial_code_still_matches(self):
        res = library.lookup(self.store, ["abc123"], self.cfg.data)
        self.assertEqual(res["state"], "present")

    def test_unrelated_query_misses(self):
        res = library.lookup(self.store, ["完全不相关的东西"], self.cfg.data)
        self.assertEqual(res["state"], "none")

    def test_short_terms_are_ignored(self):
        res = library.lookup(self.store, ["a", "1"], self.cfg.data)
        self.assertEqual(res["state"], "none")
        self.assertEqual(res["checked_keys"], 0)

    def test_suggest_terms_collects_fc2_and_title(self):
        plan = {"text": "FC2PPV-4999948\n某标题\n", "fc2": {"code": "4999948",
                                                           "raw": "FC2PPV-4999948"}}
        match = {"query": "4999948", "keywords": ["4999948"]}
        terms = library.suggest_terms(plan, match)
        self.assertIn("4999948", terms)
        self.assertIn("FC2PPV-4999948", terms)
        self.assertIn("FC2PPV-4999948", terms)


class TestDuplicates(EnvCase):
    def test_same_name_same_size_flagged(self):
        write(self.path("A", "ABC-123.mkv"), 4000)
        write(self.path("B", "ABC-123 甲 - 副本", "ABC-123.mkv"), 4000)
        self.scan()
        res = library.duplicates(self.store, self.cfg.data)
        self.assertEqual(res["total"], 1)
        g = res["groups"][0]
        self.assertEqual(g["count"], 2)          # 两个目录
        self.assertTrue(g["same_size"])
        self.assertIn("同名同体积", g["reason"])

    def test_different_size_is_soft_hint(self):
        write(self.path("A", "ABC-123.mkv"), 4000)
        write(self.path("B", "ABC-123.mkv"), 8000)
        self.scan()
        res = library.duplicates(self.store, self.cfg.data)
        self.assertEqual(res["total"], 1)
        self.assertFalse(res["groups"][0]["same_size"])
        self.assertIn("不同版本", res["groups"][0]["reason"])

    def test_single_copy_not_flagged(self):
        write(self.path("A", "唯一.mkv"), 4000)
        self.scan()
        self.assertEqual(library.duplicates(self.store, self.cfg.data)["total"], 0)

    def test_dir_and_child_file_not_duplicate(self):
        # 目录名与里面文件同名是正常结构，不能算重复
        write(self.path("作品 X", "作品 X.mkv"), 4000)
        self.scan()
        self.assertEqual(library.duplicates(self.store, self.cfg.data)["total"], 0)


class TestPipelineGuard(EnvCase):
    def test_present_hit_skips_search_and_download(self):
        write(self.path("ABC-123 甲", "ABC-123 甲.mkv"), 100)
        self.scan()
        pipe = Pipeline(self.cfg, self.store)
        with mock.patch.object(pipe, "search", side_effect=AssertionError("不该检索")), \
                mock.patch.object(pipe, "submit", side_effect=AssertionError("不该投递")):
            bundle = pipe.handle_text("ABC-123 甲", 0)
        self.assertTrue(bundle.get("skipped_library"))
        self.assertEqual(bundle["library"]["state"], "present")
        text = pipe.format_bundle(bundle)
        self.assertIn("库里已有", text)
        self.assertIn("/force", text)

    def test_deleted_hit_also_skips(self):
        # 「曾存在已删」与「已存在」同样处理：默认拦下，要 /force 才继续
        write(self.path("旧片子", "旧片子.mp4"), 100)
        self.scan()
        self.store.library_mark_deleted(self.path("旧片子"), time.time() - 60)
        self.store.library_mark_deleted(self.path("旧片子", "旧片子.mp4"), time.time() - 60)
        pipe = Pipeline(self.cfg, self.store)
        with mock.patch.object(pipe, "search", side_effect=AssertionError("不该检索")), \
                mock.patch.object(pipe, "submit", side_effect=AssertionError("不该投递")):
            bundle = pipe.handle_text("旧片子", 0)
        self.assertTrue(bundle.get("skipped_library"))
        self.assertEqual(bundle["library"]["state"], "deleted")
        text = pipe.format_bundle(bundle)
        self.assertIn("曾经有", text)
        self.assertIn("发现已删除", text)
        self.assertIn("/force", text)

    def test_deleted_hit_force_continues(self):
        write(self.path("旧片子", "旧片子.mp4"), 100)
        self.scan()
        self.store.library_mark_deleted(self.path("旧片子"), time.time() - 60)
        self.store.library_mark_deleted(self.path("旧片子", "旧片子.mp4"), time.time() - 60)
        pipe = Pipeline(self.cfg, self.store)
        calls = {"search": 0}

        def fake_search(*a, **kw):
            calls["search"] += 1
            return {"items": [], "relaxed": False, "total": 0, "dropped": {}}

        with mock.patch.object(pipe, "search", side_effect=fake_search):
            bundle = pipe.handle_text("旧片子", 0, force=True)
        self.assertEqual(calls["search"], 1)
        self.assertFalse(bundle.get("skipped_library"))

    def test_skip_deleted_off_only_warns(self):
        write(self.path("旧片子", "旧片子.mp4"), 100)
        self.scan()
        self.store.library_mark_deleted(self.path("旧片子"), time.time() - 60)
        self.store.library_mark_deleted(self.path("旧片子", "旧片子.mp4"), time.time() - 60)
        self.cfg.data["library"]["skip_deleted"] = False
        pipe = Pipeline(self.cfg, self.store)
        with mock.patch.object(pipe, "search",
                               return_value={"items": [], "relaxed": False,
                                             "total": 0, "dropped": {}}) as s:
            bundle = pipe.handle_text("旧片子", 0)
        self.assertEqual(s.call_count, 1)
        self.assertEqual(bundle["library"]["state"], "deleted")
        self.assertFalse(bundle.get("skipped_library"))
        self.assertIn("曾经有", pipe.format_bundle(bundle))

    def test_present_and_deleted_both_counted(self):
        pipe = Pipeline(self.cfg, self.store)
        self.assertEqual(pipe.library_skip_state(
            {"state": "present", "strong_present": True, "strong_deleted": True}),
            "present,deleted")
        self.assertIsNone(pipe.library_skip_state(None))
        self.assertIsNone(pipe.library_skip_state(
            {"state": "deleted", "strong_present": False, "strong_deleted": False}))

    def test_force_hint_keeps_fc2_prefix(self):
        # 照抄 /force 要能复现同一条判定，不能只剩一串数字
        pipe = Pipeline(self.cfg, self.store)
        self.assertEqual(
            pipe.force_hint({"mode": "fc2", "plan": {"fc2": {"code": "4999948"}},
                             "query": "4999948"}), "FC2 4999948")
        self.assertEqual(pipe.force_hint({"query": "ABC-123 甲"}), "ABC-123 甲")
        self.assertEqual(pipe.force_hint({"text": "   "}), "关键词")

    def test_force_bypasses_library(self):
        write(self.path("ABC-123 甲", "ABC-123 甲.mkv"), 100)
        self.scan()
        pipe = Pipeline(self.cfg, self.store)
        calls = {"n": 0}

        def fake_search(*a, **kw):
            calls["n"] += 1
            return {"items": [], "relaxed": False, "total": 0, "dropped": {}}

        with mock.patch.object(pipe, "search", side_effect=fake_search):
            bundle = pipe.handle_text("ABC-123 甲", 0, force=True)
        self.assertEqual(calls["n"], 1)
        self.assertFalse(bundle.get("skipped_library"))

    def test_check_switch_off_disables_lookup(self):
        write(self.path("ABC-123 甲", "ABC-123 甲.mkv"), 100)
        self.scan()
        self.cfg.data["library"]["check_search"] = False
        pipe = Pipeline(self.cfg, self.store)
        with mock.patch.object(pipe, "search",
                               return_value={"items": [], "relaxed": False,
                                             "total": 0, "dropped": {}}):
            bundle = pipe.handle_text("ABC-123 甲", 0)
        self.assertNotIn("library", bundle)

    def test_skip_download_off_warns_but_downloads(self):
        write(self.path("ABC-123 甲", "ABC-123 甲.mkv"), 100)
        self.scan()
        self.cfg.data["library"]["skip_download"] = False
        pipe = Pipeline(self.cfg, self.store)
        items = [{"title": "ABC-123 甲", "magnet": "magnet:?xt=urn:btih:" + "A" * 40,
                  "infohash": "a" * 40, "seeders": 9, "downloads": 1,
                  "size_human": "1 MB", "age_human": "1 天", "trusted": False}]
        with mock.patch.object(pipe, "search",
                               return_value={"items": items, "relaxed": False,
                                             "total": 1, "dropped": {}}), \
                mock.patch.object(pipe, "submit",
                                  return_value={"ok": True, "title": "ABC-123 甲",
                                                "infohash": "a" * 40, "magnet": "",
                                                "backend": "xunlei", "task_id": "1",
                                                "message": "", "deduped": False}):
            bundle = pipe.handle_text("ABC-123 甲", 0)
        self.assertEqual(bundle["library"]["state"], "present")
        self.assertFalse(bundle.get("skipped_library"))
        self.assertTrue(bundle["submitted"])


class TestWebApi(EnvCase):
    def test_scan_lookup_and_dups_endpoints(self):
        from tmb.webui import AppContext, WebUI

        write(self.path("ABC-123 甲", "ABC-123 甲.mkv"), 4000)
        write(self.path("B", "ABC-123 甲 - 副本", "ABC-123 甲.mkv"), 4000)
        self.cfg.data["app"]["port"] = _free_port()
        self.cfg.data["app"]["bind"] = "127.0.0.1"
        ctx = AppContext(self.cfg, self.store, Pipeline(self.cfg, self.store), None,
                         os.path.join(ROOT, "app", "ui"))
        ui = WebUI(ctx)
        ui.start()
        time.sleep(0.3)
        base = "http://127.0.0.1:%d" % self.cfg.data["app"]["port"]

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())

        try:
            rep = post("/api/library/scan", {"roots": [self.root],
                                             "max_depth": 3, "include_files": True})
            self.assertTrue(rep["ok"], rep.get("error"))
            self.assertGreaterEqual(rep["added"], 3)
            # 面板传的目录会记住
            self.assertEqual(self.cfg.get("library.roots"), [self.root])

            res = post("/api/library/lookup", {"text": "ABC-123 甲"})
            self.assertEqual(res["state"], "present")

            dups = post("/api/library/dups", {})
            self.assertEqual(dups["total"], 1)

            with urllib.request.urlopen(base + "/api/library/items?limit=5", timeout=10) as r:
                items = json.loads(r.read().decode())
            self.assertTrue(items["ok"])
            self.assertGreaterEqual(items["counts"]["total"], 3)
        finally:
            ui.stop()


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    unittest.main()
