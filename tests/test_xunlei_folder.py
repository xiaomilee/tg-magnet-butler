"""迅雷「parent folder not found」的诊断与降级。

背景：``POST /drive/v1/task`` 里带的 ``parent_folder_id`` 一旦在当前迅雷里对不上
（换设备、重装迅雷、目录被删，或把路径当成 id 填进面板），迅雷只回一行纯文本
``parent folder not found``。旧代码把它原样拼成
「迅雷拒绝任务: {"_raw_text": "parent folder not found"}」，既没说是哪个设置，
也不会自己绕过去，整条投递链路就此卡死。这里锁四件事：

1. 目录错误能被单独识别，且不会被误判成令牌问题。
2. 报错能说清「是哪个设置、两种改法」。
3. 目录 id 无效时改用默认目录再投一次（可关），并标记「这次是降级成功的」。
4. 上层据此把坏 id 从配置里清掉，下次不再重演。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb.config import Config                                  # noqa: E402
from tmb.downloaders.base import DownloadResult                # noqa: E402
from tmb.downloaders.xunlei import (                           # noqa: E402
    XunleiDownloader, classify_task_error, explain_folder, looks_like_path)
from tmb.pipeline import Pipeline                              # noqa: E402
from tmb.store import Store                                    # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
FOLDER_ERR = {"_raw_text": "parent folder not found"}


class TestClassify(unittest.TestCase):
    def test_pure_text_folder_error(self):
        self.assertEqual(classify_task_error(FOLDER_ERR), "folder")

    def test_json_wrapped_folder_error(self):
        self.assertEqual(classify_task_error({"error": "Parent Folder Not Found"}),
                         "folder")
        self.assertEqual(classify_task_error(
            {"error_description": "invalid parent folder"}), "folder")

    def test_folder_hyphen_and_underscore_forms(self):
        for text in ("folder not found", "folder_not_found",
                     "parent_folder_id is invalid", "no such folder"):
            self.assertEqual(classify_task_error({"_raw_text": text}), "folder", text)

    def test_auth_wins_when_both_present(self):
        # 令牌坏的时候先修令牌：此时去改目录是白费功夫
        self.assertEqual(classify_task_error(
            {"_raw_text": "invalid token (parent_folder_id)"}), "token")

    def test_normal_payload_is_not_an_error(self):
        self.assertEqual(classify_task_error({"tasks": []}), "")
        self.assertEqual(classify_task_error({"task": {"id": "1"}}), "")


class TestExplain(unittest.TestCase):
    def test_mentions_both_ways_out(self):
        msg = explain_folder({})
        self.assertIn("留空", msg)
        self.assertIn("parent_folder_id", msg)
        self.assertIn("默认目录", msg)

    def test_path_instead_of_id(self):
        self.assertTrue(looks_like_path("/vol1/1000/Downloads"))
        self.assertTrue(looks_like_path("Downloads/FC2"))
        self.assertTrue(looks_like_path("C:\\Downloads"))
        self.assertFalse(looks_like_path(""))
        self.assertFalse(looks_like_path("bd0f6d4f2b4a4a1d8f6e1c2b3a4d5e6f"))


class TestAddFallback(unittest.TestCase):
    def _dl(self, **kw):
        cfg = {"mode": "panel", "pan_auth": "OK", "target": "device#1",
               "select_all_files": True, "timeout": 5}
        cfg.update(kw)
        return XunleiDownloader(cfg, "")

    @staticmethod
    def _hook(dl, reject_with_folder=True, always_fail=False):
        """记录每次请求；带 parent_folder_id 的任务被拒，不带的成功。"""
        calls = []

        def fake(path, body=None, method="POST", extra=None):
            calls.append({"path": path, "params": (body or {}).get("params") or {}})
            if path == "/drive/v1/resource/list":
                return {"resources": [{"name": "作品.mkv", "file_size": "1024"}]}
            params = (body or {}).get("params") or {}
            if always_fail or "parent_folder_id" in params:
                return dict(FOLDER_ERR)
            return {"task": {"id": "777"}}

        dl._request = fake
        return calls

    def test_falls_back_to_default_folder(self):
        dl = self._dl(parent_folder_id="BAD_ID")
        calls = self._hook(dl)
        res = dl.add(MAGNET, "某作品")
        self.assertTrue(res.ok)
        self.assertEqual(res.task_id, "777")
        self.assertIn("默认目录", res.message)
        self.assertTrue(res.raw.get("_folder_fallback"))
        self.assertEqual(res.raw.get("_bad_folder_id"), "BAD_ID")
        # 第一次带 id 失败、第二次不带 id 成功
        posts = [c for c in calls if c["path"] == "/drive/v1/task"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["params"].get("parent_folder_id"), "BAD_ID")
        self.assertNotIn("parent_folder_id", posts[1]["params"])

    def test_switch_off_keeps_hard_failure(self):
        dl = self._dl(parent_folder_id="BAD_ID", auto_fallback_folder=False)
        calls = self._hook(dl)
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("下载目录无效", res.message)
        self.assertEqual(res.raw.get("_error_kind"), "folder")
        self.assertEqual(len([c for c in calls if c["path"] == "/drive/v1/task"]), 1)

    def test_both_attempts_failing_says_so(self):
        dl = self._dl(parent_folder_id="BAD_ID")
        self._hook(dl, always_fail=True)
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("下载目录无效", res.message)
        self.assertIn("改用默认目录重试", res.message)
        # 第二次没带 id 还被拒：不能再让用户去改 id，得说「迅雷里没有可写目录」
        self.assertIn("没找到可用的下载目录", res.message)

    def test_no_folder_configured_but_xunlei_still_says_folder_error(self):
        # 没填 id、走默认目录仍被拒：这时「id 填错了」的提示就是误导
        dl = self._dl()
        self._hook(dl, always_fail=True)
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("没找到可用的下载目录", res.message)
        self.assertNotIn("两种改法", res.message)

    def test_no_folder_configured_means_single_request(self):
        dl = self._dl()
        calls = self._hook(dl)
        res = dl.add(MAGNET, "某作品")
        self.assertTrue(res.ok)
        self.assertEqual(len([c for c in calls if c["path"] == "/drive/v1/task"]), 1)
        self.assertFalse(res.raw.get("_folder_fallback"))

    def test_path_typed_id_is_dropped_before_request(self):
        dl = self._dl(parent_folder_id="/vol1/1000/Downloads")
        calls = self._hook(dl)
        res = dl.add(MAGNET, "某作品")
        self.assertTrue(res.ok)
        posts = [c for c in calls if c["path"] == "/drive/v1/task"]
        self.assertEqual(len(posts), 1)                       # 本地就拦了，不白打请求
        self.assertNotIn("parent_folder_id", posts[0]["params"])
        self.assertIn("路径", res.message)
        self.assertTrue(res.raw.get("_folder_fallback"))

    def test_non_folder_error_does_not_retry(self):
        dl = self._dl(parent_folder_id="OK_ID")
        calls = []

        def fake(path, body=None, method="POST", extra=None):
            calls.append(path)
            if path == "/drive/v1/resource/list":
                return {"resources": []}
            return {"_raw_text": "task quota exceeded"}

        dl._request = fake
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("task quota exceeded", res.message)     # 原话能看到了
        self.assertNotIn("{'_raw_text'", res.message)
        self.assertEqual(calls.count("/drive/v1/task"), 1)

    def test_probe_flags_path_typed_id(self):
        dl = self._dl(parent_folder_id="Downloads/FC2")

        def fake(path, body=None, method="POST", extra=None):
            return {"tasks": [{"target": "device#9"}]}

        dl._request = fake
        info = dl.probe()
        self.assertTrue(info["ok"])
        self.assertEqual(info["folder"], "invalid")
        self.assertIn("路径", info["hint"])


class TestPipelineClearsBadFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.data["download"]["backend"] = "xunlei"
        self.cfg.data["download"]["fallback"] = []
        self.cfg.data["app"]["dedupe_days"] = 0
        self.cfg.data["download"]["xunlei"].update(
            {"mode": "panel", "pan_auth": "OK", "host": "127.0.0.1", "port": 2345,
             "parent_folder_id": "BAD_ID"})
        self.store = Store(os.path.join(self.tmp, "var", "t.db"))
        self.pipe = Pipeline(self.cfg, self.store)

    def _item(self):
        return {"title": "某作品", "magnet": MAGNET, "infohash": "b" * 40}

    @staticmethod
    def _fake(res):
        m = mock.Mock()
        m.name = "xunlei"
        m.add = mock.Mock(return_value=res)
        return m

    def _ok(self, **raw):
        return DownloadResult(True, "xunlei", "42", "已投递迅雷（1 个文件）", raw)

    def test_fallback_does_not_touch_config(self):
        # 降级成功只提示，不替用户改设置：失败时动配置太危险（1.0.9 清空过 id，反而更糟）
        res = self._ok(_folder_fallback=True, _bad_folder_id="BAD_ID")
        with mock.patch("tmb.downloaders.chain", return_value=[self._fake(res)]):
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"], out)
        self.assertIn("建议到面板", out["message"])
        self.assertNotIn("已清空", out["message"])
        self.assertEqual(self.cfg.data["download"]["xunlei"]["parent_folder_id"],
                         "BAD_ID")

    def test_recovered_folder_is_saved(self):
        res = self._ok(_folder_recovered={"parent_folder_id": "GOOD_ID",
                                          "parent_folder_path": "/vol1/Downloads"})
        with mock.patch("tmb.downloaders.chain", return_value=[self._fake(res)]):
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"], out)
        self.assertIn("已把这个目录写进设置", out["message"])
        x = self.cfg.data["download"]["xunlei"]
        self.assertEqual(x["parent_folder_id"], "GOOD_ID")
        self.assertEqual(x["parent_folder_path"], "/vol1/Downloads")

    def test_user_changed_id_in_meantime_is_kept(self):
        self.cfg.data["download"]["xunlei"]["parent_folder_id"] = "NEW_ID"
        res = self._ok(_folder_fallback=True, _bad_folder_id="BAD_ID")
        with mock.patch("tmb.downloaders.chain", return_value=[self._fake(res)]):
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"], out)
        self.assertNotIn("已清空", out["message"])
        self.assertEqual(self.cfg.data["download"]["xunlei"]["parent_folder_id"],
                         "NEW_ID")

    def test_normal_success_keeps_folder_setting(self):
        with mock.patch("tmb.downloaders.chain",
                        return_value=[self._fake(self._ok())]):
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.cfg.data["download"]["xunlei"]["parent_folder_id"],
                         "BAD_ID")


if __name__ == "__main__":
    unittest.main()
