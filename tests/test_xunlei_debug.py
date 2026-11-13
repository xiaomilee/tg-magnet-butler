# -*- coding: utf-8 -*-
"""「把日志调到 DEBUG 看原始响应」这句话得是真的。

背景：1.0.11 的报错文案让人把日志级别调成 DEBUG 看原始响应，但代码里一条
debug 都没有——调了也什么都看不到。这里锁住两件事：
  * DEBUG 级别下，请求体与响应体真的会打出来，且令牌/磁力哈希已打码；
  * 投递被拒时，INFO 级别就留一条原始响应（不用改配置也能查）。
"""
import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb.downloaders.xunlei import XunleiDownloader, explain_auth   # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=test"


def gateway_cfg(**kw):
    cfg = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
           "pan_auth": "PANTOKEN123456", "fnos_token": "FNOS1234567890",
           "target": "device#1"}
    cfg.update(kw)
    return cfg


class TestDebugTraffic(unittest.TestCase):
    def test_debug_prints_request_and_response(self):
        dl = XunleiDownloader(gateway_cfg())
        with mock.patch("tmb.util.http_request",
                        return_value=(200, '{"tasks": []}', b"")), \
                self.assertLogs("tmb.xunlei", "DEBUG") as cm:
            dl._request("/drive/v1/tasks", method="GET", extra={"limit": "1"})
        text = "\n".join(cm.output)
        self.assertIn("迅雷请求 → GET", text)
        self.assertIn("迅雷响应 ← GET", text)
        self.assertIn("tasks", text)          # 响应体真的带出来了

    def test_debug_masks_secrets(self):
        dl = XunleiDownloader(gateway_cfg())
        body = ('{"error": "invalid token", "echo": "FNOS1234567890",'
                ' "pan": "PANTOKEN123456"}')
        with mock.patch("tmb.util.http_request", return_value=(200, body, b"")), \
                self.assertLogs("tmb.xunlei", "DEBUG") as cm:
            dl._request("/drive/v1/task", {"url": MAGNET})
        text = "\n".join(cm.output)
        self.assertNotIn("FNOS1234567890", text)      # 令牌不能进日志
        self.assertNotIn("PANTOKEN123456", text)
        self.assertNotIn("b" * 40, text)              # 磁力哈希打掉中段
        self.assertIn("btih:bbbbbb", text)            # 但仍看得出是哪条

    def test_debug_off_has_no_noise(self):
        # 正常情况下（INFO）不该有 DEBUG 行，否则日志会被刷爆。
        # 注意不能用 assertNoLogs：它自己会把级别抬到 DEBUG，等于自己喂自己。
        dl = XunleiDownloader(gateway_cfg())
        lg = logging.getLogger("tmb.xunlei")
        old, records = lg.level, []
        handler = logging.Handler()
        handler.emit = records.append
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)
        try:
            with mock.patch("tmb.util.http_request", return_value=(200, "{}", b"")):
                dl._request("/drive/v1/tasks", method="GET")
        finally:
            lg.removeHandler(handler)
            lg.setLevel(old)
        self.assertEqual([r.levelname for r in records if r.levelno < logging.INFO], [])

    def test_401_body_is_logged_too(self):
        from tmb import util
        dl = XunleiDownloader(gateway_cfg())
        err = util.HttpError("HTTP 401 http://x/y", 401, b"invalid token")
        with mock.patch("tmb.util.http_request", side_effect=err), \
                self.assertLogs("tmb.xunlei", "DEBUG") as cm:
            data = dl._request("/drive/v1/task", {"url": MAGNET})
        self.assertEqual(data["_http_status"], 401)
        self.assertIn("HTTP 401", "\n".join(cm.output))


class TestRejectLog(unittest.TestCase):
    def test_rejected_task_leaves_raw_response_in_log(self):
        dl = XunleiDownloader(gateway_cfg())
        dl._request = mock.Mock(return_value={"error": "invalid token"})
        with self.assertLogs("tmb.xunlei", "INFO") as cm:
            dl._post_task(MAGNET, "title", "device#1", [], {})
        text = "\n".join(cm.output)
        self.assertIn("建任务被拒", text)
        self.assertIn("invalid token", text)

    def test_rejected_log_shows_which_folder(self):
        dl = XunleiDownloader(gateway_cfg())
        dl._request = mock.Mock(return_value={"error": "parent folder not found"})
        with self.assertLogs("tmb.xunlei", "INFO") as cm:
            dl._post_task(MAGNET, "title", "device#1", [],
                          {"parent_folder_id": "F123"})
        text = "\n".join(cm.output)
        self.assertIn("F123", text)          # 三种投法撞同一句时能分清是哪次


class TestTokenOkMessage(unittest.TestCase):
    def test_points_to_pan_auth_not_relogin(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "token_ok", 600)
        self.assertIn("pan_auth", msg)
        self.assertNotIn("重登飞牛", msg)
        self.assertIn("磁力", msg)            # 也要给出「换条资源」这条二分法

    def test_mentions_where_to_see_raw_response(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "token_ok")
        self.assertIn("DEBUG", msg)
        self.assertIn("日志", msg)


class TestMaskUrl(unittest.TestCase):
    def test_query_token_is_masked(self):
        from tmb.util import mask_url
        masked = mask_url("http://h:5666/cgi/x/drive/v1/task"
                          "?pan_auth=ABCDEF123456&limit=1")
        self.assertNotIn("ABCDEF123456", masked)
        self.assertIn("limit=1", masked)          # 别的参数照旧，方便定位
        self.assertIn("pan_auth=ABCD***56", masked)

    def test_plain_url_untouched(self):
        from tmb.util import mask_url
        url = "http://h:2345/drive/v1/tasks?type=user%23runner"
        self.assertEqual(mask_url(url), url)

    def test_short_value_not_mangled(self):
        from tmb.util import mask_url
        self.assertIn("pan_auth=1", mask_url("http://h/x?pan_auth=1"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
