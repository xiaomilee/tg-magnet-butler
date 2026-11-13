"""迅雷「建任务次数达到上限」：识别、止损、说人话。

真机现场（1.0.36，2026-11-13）：

    HTTP 403 {"error":"task_create_count_limit","error_code":11,
              "error_description":"任务创建次数达到上限"}

旧代码把 401/403 一律归成「令牌问题」，于是

* 报错说「迅雷鉴权失败……被拒的是建任务的参数」，把人往重抓 pan_auth、换目录上带；
* 还顺手做了「改用最简请求体重试一次」，再加上层的「重扫令牌后再投一次」——
  同一次投递对着同一个额度撞了三到四次，额度越用越少。

这里锁四件事：

1. 限流能从 403 响应体里认出来，且不会被误判成鉴权/目录问题。
2. 文案说清「这是迅雷侧的额度，不是你的配置坏了」，并给出能做的事。
3. 认出限流后**一次都不重试**（换目录、减字段、重扫令牌全跳过）。
4. 限流挡在「取设备空间」那一步时，也别报成「没取到设备空间，检查 pan_auth」。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import util                                              # noqa: E402
from tmb.downloaders.base import DownloadResult                    # noqa: E402
from tmb.downloaders.xunlei import (                               # noqa: E402
    XunleiDownloader, classify_quota, classify_task_error, explain_quota)

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
QUOTA_BODY = ('{"error":"task_create_count_limit","error_code":11,'
              '"error_description":"任务创建次数达到上限",'
              '"error_details":[{"@type":"type.googleapis.com/google.rpc.LocalizedMessage",'
              '"locale":"zh-cn","message":"任务创建次数达到上限"}],"HttpStatus":403}')
CFG = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
       "pan_auth": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.x",
       "fnos_token": "tok"}


def make_dl():
    return XunleiDownloader(dict(CFG), proxy="")


class TestClassifyQuota(unittest.TestCase):
    def test_real_payload(self):
        self.assertTrue(classify_quota(QUOTA_BODY))
        self.assertTrue(classify_quota("任务创建次数达到上限"))
        self.assertTrue(classify_quota("too many requests"))
        self.assertTrue(classify_quota("rate limit exceeded"))

    def test_other_refusals_are_not_quota(self):
        self.assertFalse(classify_quota("invalid token"))
        self.assertFalse(classify_quota("parent folder not found"))
        self.assertFalse(classify_quota(""))
        self.assertFalse(classify_quota(None))

    def test_classify_task_error_puts_quota_first(self):
        self.assertEqual(classify_task_error({"error": "task_create_count_limit"}),
                         "quota")
        # 限流文案里顺带出现 unauthorized 也别退回「鉴权失败」——那是另一个方向
        self.assertEqual(classify_task_error(
            {"_raw_text": "task_create_count_limit unauthorized"}), "quota")
        # 真鉴权问题仍旧照旧
        self.assertEqual(classify_task_error({"_raw_text": "invalid token"}), "token")
        self.assertEqual(classify_task_error({"_raw_text": "parent folder not found"}),
                         "folder")

    def test_explain_is_actionable(self):
        text = explain_quota()
        self.assertIn("次数", text)
        self.assertIn("不是令牌", text)
        self.assertIn("没有重试", text)
        self.assertIn("手动加", text)       # 给了当下能做的事


class TestHttp403Path(unittest.TestCase):
    def test_quota_403_is_not_auth(self):
        """403 带 task_create_count_limit：不能挂 _auth_failed，否则上层会去重扫令牌。"""
        dl = make_dl()
        err = util.HttpError("HTTP 403", 403, QUOTA_BODY.encode())
        dl._open = mock.Mock(side_effect=err)
        resp = dl._request("/drive/v1/task", body={"type": "user#download-url"})
        self.assertTrue(resp.get("_quota"))
        self.assertNotIn("_auth_failed", resp)
        self.assertEqual(resp.get("_http_status"), 403)
        self.assertEqual(resp.get("error"), "task_create_count_limit")

    def test_plain_403_still_auth(self):
        """普通 403（真的是令牌问题）还得按鉴权处理，别被这次改动牵连。"""
        dl = make_dl()
        err = util.HttpError("HTTP 403", 403, b"invalid token")
        dl._open = mock.Mock(side_effect=err)
        resp = dl._request("/drive/v1/task", body={})
        self.assertEqual(resp.get("_auth_failed"), "token")
        self.assertFalse(resp.get("_quota"))


class TestNoRetry(unittest.TestCase):
    def _dl_ready(self):
        dl = make_dl()
        dl.resolve_target = mock.Mock(return_value="device#1")
        dl.list_files = mock.Mock(return_value=(
            [{"name": "a.mkv", "file_size": 1024}], {}))
        dl._folder_params = mock.Mock(return_value=({"parent_folder_id": "F1"}, ""))
        dl._post_task = mock.Mock(return_value=DownloadResult(
            False, "xunlei", "", explain_quota(),
            {"_stage": "task", "_quota": True, "_error_code": "11"}))
        return dl

    def test_add_stops_after_the_first_refusal(self):
        """认出限流就收手：不换目录、不减字段、不重投——每试一次都在扣额度。"""
        dl = self._dl_ready()
        res = dl.add(MAGNET, "片子")
        self.assertFalse(res.ok)
        self.assertEqual(dl._post_task.call_count, 1)
        self.assertTrue(res.raw.get("_quota"))
        self.assertIn("次数", res.message)
        self.assertIn("没有重试", res.message)

    def test_target_step_says_quota_not_pan_auth(self):
        """限流挡在「取设备空间」时，别报成「检查 pan_auth」那种指错方向的提示。"""
        dl = make_dl()
        dl._request = mock.Mock(return_value={"_raw_text": "task_create_count_limit",
                                              "_quota": True, "_http_status": 403})
        self.assertEqual(dl.resolve_target(), "")
        self.assertTrue(dl._quota_hit)
        res = dl.add(MAGNET, "片子")
        self.assertFalse(res.ok)
        self.assertIn("次数", res.message)
        self.assertNotIn("pan_auth", res.message)
        self.assertNotIn("设备空间", res.message)


if __name__ == "__main__":
    unittest.main()
