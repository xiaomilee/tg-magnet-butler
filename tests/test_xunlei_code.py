"""迅雷「只回一个数字错误码」的处理与目录找回。

背景：把「下载目录 id」清空后，迅雷不再回 `parent folder not found`，而是只丢一个
数字码（用户遇到的是 `9126`）。旧代码会把它拼成
`迅雷拒绝任务: {"error_code": 9126}` —— 一个数字，没有任何可操作信息。

这里锁四件事：

1. 能从各种响应形状里挖出数字错误码。
2. 公开文档里查得到的码（107/131/108/112/202/2）翻成人话；查不到的**不瞎编**，
   只说「它没给原因」并给三条自检方向。
3. 能从任务列表里捞出「迅雷自己用过的下载目录」——那是迅雷亲口承认能用的 id。
4. 没带目录被拒时，自动用捞到的目录重投一次；成功则把这个 id 记进配置。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb.downloaders.xunlei import (                      # noqa: E402
    XunleiDownloader, explain_code, extract_code, harvest_folders)

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
TASKS_WITH_FOLDER = {
    "tasks": [
        {"target": "device#1",
         "params": {"parent_folder_id": "GOOD", "parent_folder_path": "/vol1/dl"}},
        {"target": "device#1",
         "params": {"parent_folder_id": "GOOD", "parent_folder_path": "/vol1/dl"}},
    ]
}


class TestExtractCode(unittest.TestCase):
    def test_json_error_code(self):
        self.assertEqual(extract_code({"error_code": 9126}), "9126")
        self.assertEqual(extract_code({"error_code": "9126"}), "9126")

    def test_numeric_body(self):
        self.assertEqual(extract_code({"_raw_text": "9126"}), "9126")
        self.assertEqual(extract_code("202"), "202")

    def test_success_code_ignored(self):
        self.assertEqual(extract_code({"error_code": 0}), "")
        self.assertEqual(extract_code({"error_code": "0"}), "")

    def test_text_body_is_not_a_code(self):
        self.assertEqual(extract_code({"_raw_text": "invalid token"}), "")
        self.assertEqual(extract_code({"_raw_text": "parent folder not found"}), "")
        self.assertEqual(extract_code({}), "")


class TestExplainCode(unittest.TestCase):
    def test_documented_codes(self):
        self.assertEqual(explain_code("202")[0], "任务已存在")
        self.assertIn("写权限", explain_code("107")[1])
        self.assertIn("磁盘空间", explain_code("108")[0])
        self.assertIn("磁盘空间", explain_code("112")[0])
        self.assertIn("服务器", explain_code("131")[0])

    def test_unknown_code_is_not_invented(self):
        # 9126 官方没有公开对照表，不许编一个说法糊弄人
        self.assertEqual(explain_code("9126"), ())
        self.assertEqual(explain_code(""), ())


class TestAddWithCode(unittest.TestCase):
    def _dl(self, **kw):
        cfg = {"mode": "panel", "pan_auth": "OK", "target": "device#1",
               "select_all_files": True, "timeout": 5}
        cfg.update(kw)
        return XunleiDownloader(cfg, "")

    def _hook(self, dl, reject, tasks=None):
        """tasks 为 /drive/v1/tasks 的响应；reject(params) 决定任务是否被拒。"""
        calls = []

        def fake(path, body=None, method="POST", extra=None):
            calls.append(path)
            if path == "/drive/v1/resource/list":
                return {"resources": [{"name": "作品.mkv"}]}
            if path == "/drive/v1/tasks":
                return tasks if tasks is not None else {"tasks": []}
            params = (body or {}).get("params") or {}
            return reject(params)

        dl._request = fake
        return calls

    def test_known_code_is_translated(self):
        dl = self._dl()
        self._hook(dl, lambda p: {"error_code": 202})
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("错误码 202", res.message)
        self.assertIn("任务已存在", res.message)
        self.assertEqual(res.raw.get("_error_code"), "202")

    def test_unknown_code_lists_what_to_check(self):
        dl = self._dl()
        self._hook(dl, lambda p: {"error_code": 9126})
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("错误码 9126", res.message)
        self.assertIn("没给文字说明", res.message)
        self.assertIn("下载目录", res.message)      # 自检方向里有它能对上的
        self.assertEqual(res.raw.get("_error_code"), "9126")

    def test_rejects_then_recovers_with_harvested_folder(self):
        dl = self._dl()          # 没配下载目录
        self._hook(dl, lambda p: {"task": {"id": "333"}} if p.get("parent_folder_id")
                   else {"error_code": 9126}, tasks=TASKS_WITH_FOLDER)
        res = dl.add(MAGNET, "某作品")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.task_id, "333")
        self.assertIn("已有任务里的下载目录", res.message)
        self.assertEqual(res.raw["_folder_recovered"]["parent_folder_id"], "GOOD")

    def test_no_harvest_means_single_attempt(self):
        dl = self._dl()
        calls = self._hook(dl, lambda p: {"error_code": 9126}, tasks={"tasks": []})
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertEqual(calls.count("/drive/v1/task"), 1)
        self.assertIn("9126", res.message)

    def test_known_code_with_folder_does_not_retry(self):
        # 已知与目录无关的码（202 任务已存在）就别折腾目录了，直接如实报
        dl = self._dl(parent_folder_id="MY_ID")
        calls = self._hook(dl, lambda p: {"error_code": 202}, tasks=TASKS_WITH_FOLDER)
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertEqual(calls.count("/drive/v1/task"), 1)
        self.assertIn("任务已存在", res.message)

    def test_unknown_code_with_folder_runs_the_ladder(self):
        # 9126 这种查不到含义的码：带目录也值得试一遍「默认目录 → 已有任务的目录」
        dl = self._dl(parent_folder_id="MY_ID")
        calls = self._hook(dl, lambda p: {"task": {"id": "9"}}
                           if p.get("parent_folder_id") == "GOOD"
                           else {"error_code": 9126}, tasks=TASKS_WITH_FOLDER)
        res = dl.add(MAGNET, "某作品")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(calls.count("/drive/v1/task"), 3)   # MY_ID → 不带 → GOOD
        self.assertEqual(res.raw["_folder_recovered"]["parent_folder_id"], "GOOD")

    def test_repeated_failure_text_is_not_duplicated(self):
        # 三种投法撞同一句话时，别把同一段报错贴三遍
        dl = self._dl(parent_folder_id="MY_ID")
        self._hook(dl, lambda p: {"error_code": 9126}, tasks=TASKS_WITH_FOLDER)
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertEqual(res.message.count("常见三类原因"), 1)
        self.assertIn("初投 / 改用默认目录重试 / 改用已有任务的目录重试", res.message)

    def test_probe_lists_folders(self):
        dl = self._dl()
        self._hook(dl, lambda p: {"task": {"id": "1"}}, tasks=TASKS_WITH_FOLDER)
        info = dl.probe()
        self.assertEqual(info["folders"][0]["parent_folder_id"], "GOOD")
        self.assertEqual(info["folders"][0]["parent_folder_path"], "/vol1/dl")


class TestHarvest(unittest.TestCase):
    def test_nested_payload(self):
        data = {"tasks": [{"params": {"parent_folder_id": "A",
                                      "parent_folder_path": "/x"}},
                          {"params": {"parent_folder_id": "A"}},
                          {"params": {"parent_folder_id": "B",
                                      "parent_folder_path": "/y"}}]}
        out = harvest_folders(data)
        self.assertEqual(out[0]["parent_folder_id"], "A")
        self.assertEqual(out[0]["hits"], 2)
        self.assertEqual(out[0]["parent_folder_path"], "/x")
        self.assertEqual([x["parent_folder_id"] for x in out], ["A", "B"])

    def test_ignores_empty_and_non_string(self):
        data = {"tasks": [{"params": {"parent_folder_id": ""}},
                          {"params": {"parent_folder_id": None}},
                          {"params": {"parent_folder_id": 123}}]}
        self.assertEqual(harvest_folders(data), [])


if __name__ == "__main__":
    unittest.main()
