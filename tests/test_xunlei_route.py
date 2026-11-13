# -*- coding: utf-8 -*-
"""代理 / WireGuard 环境下的线路判定，以及「令牌有效但建任务被拒」的最后一招。

两个背景：
1. 用户用 Clash + WireGuard 连回家，问是不是代理把请求带歪了。内网地址本来
   强制直连，但 WireGuard / Tailscale 常用的 100.64.0.0/10（CGNAT）Python 不
   当成 private，之前会被丢给代理——那当然连不上家里的 NAS。
2. 磁力能解析出来，说明 pan_auth 对迅雷云端是有效的，这时再说「令牌失效」就是
   误诊；真正的雷常在请求体的可选字段（mime_type / sub_file_index / file_size）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb import util                                                    # noqa: E402
from tmb.downloaders.xunlei import XunleiDownloader, explain_auth       # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "d" * 40


def gateway_cfg(**kw):
    cfg = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
           "pan_auth": "PAN", "fnos_token": "FNTOKEN", "target": "device#1"}
    cfg.update(kw)
    return cfg


class TestRoute(unittest.TestCase):
    def test_cgnat_is_local(self):
        # WireGuard / Tailscale 默认就用这一段，必须直连
        self.assertTrue(util.is_local_url("http://100.64.0.7:5666/x"))
        self.assertTrue(util.is_local_url("http://100.100.1.1:5666/x"))
        self.assertFalse(util.is_local_url("http://100.128.0.1:5666/x"))   # 出了 /10

    def test_lan_and_mdns_still_local(self):
        for u in ("http://192.168.1.2:5666/x", "http://10.0.0.5:2345/x",
                  "http://fnos.local:5666/x"):
            self.assertTrue(util.is_local_url(u), u)

    def test_route_ignores_proxy_for_lan(self):
        r = util.route_of("http://192.168.1.2:5666/x", "127.0.0.1:7890")
        self.assertTrue(r["direct"])
        self.assertEqual(r["proxy"], "")          # 配了代理也不该用
        self.assertIn("忽略代理", r["why"])

    def test_route_uses_proxy_for_public(self):
        r = util.route_of("http://example.com/x", "127.0.0.1:7890")
        self.assertFalse(r["direct"])
        self.assertEqual(r["proxy"], "127.0.0.1:7890")

    def test_route_direct_when_no_proxy(self):
        r = util.route_of("http://example.com/x", "")
        self.assertTrue(r["direct"])

    def test_probe_hints_proxy_when_target_not_local(self):
        # 域名访问 + 配了代理：失败时必须提一句「这条走了代理」，否则人会去瞎改令牌
        dl = XunleiDownloader(gateway_cfg(host="nas.example.com"), "127.0.0.1:7890")
        dl._request = mock.Mock(return_value={"_raw_text": "invalid token",
                                              "_auth_failed": "token"})
        info = dl.probe()
        self.assertFalse(info["route"]["local"])
        self.assertIn("经代理", info["hint"])

    def test_probe_no_proxy_note_for_lan(self):
        dl = XunleiDownloader(gateway_cfg(host="192.168.1.2"), "127.0.0.1:7890")
        dl._request = mock.Mock(return_value={"_raw_text": "invalid token",
                                              "_auth_failed": "token"})
        info = dl.probe()
        self.assertNotIn("经代理", info["hint"])

    def test_probe_reports_route(self):
        dl = XunleiDownloader(gateway_cfg(host="100.64.0.7", proxy=""))
        dl._request = mock.Mock(return_value={"tasks": [{"target": "device#1"}]})
        info = dl.probe()
        self.assertTrue(info["route"]["local"])
        self.assertTrue(info["route"]["direct"])


class TestMinimalBody(unittest.TestCase):
    def _body(self, minimal):
        # 这条用例只看请求体字段，与文件筛选无关：显式全选，别让筛选把两个文件拆开
        dl = XunleiDownloader(gateway_cfg(select_all_files=True))
        got = {}

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            got.update(body or {})
            return {"task": {"id": "1"}}

        dl._request = fake
        files = [{"name": "a.mkv", "file_size": 100, "mime_type": "video/x-matroska"},
                 {"name": "b.srt", "file_size": 10}]
        dl._post_task(MAGNET, "标题", "device#1", files, {}, minimal=minimal)
        return got

    def test_full_body_has_optional_fields(self):
        b = self._body(False)
        self.assertIn("file_name", b)
        self.assertIn("file_size", b)
        self.assertEqual(b["params"]["sub_file_index"], "-1")   # 全选就是 -1
        self.assertIn("mime_type", b["params"])
        self.assertEqual(b["params"]["total_file_count"], "2")

    def test_minimal_body_drops_only_optional(self):
        # 只砍「各版本认法不一」的两个；file_name 删掉网关会 400（实测），必须留
        b = self._body(True)
        self.assertEqual(b["type"], "user#download-url")
        self.assertEqual(b["space"], "device#1")
        self.assertEqual(b["params"],
                         {"url": MAGNET, "target": "device#1", "total_file_count": "2"})
        self.assertIn("file_name", b)
        for k in ("mime_type", "sub_file_index"):
            self.assertNotIn(k, b["params"])


class TestCloudOk(unittest.TestCase):
    def test_message_stops_blaming_pan_auth(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "token_ok",
                           600, cloud_ok=True)
        self.assertIn("pan_auth", msg)
        self.assertIn("解析", msg)
        self.assertNotIn("先点「自动侦测并连接」", msg)   # 令牌没问题，别让人瞎重抓

    def test_message_without_cloud_ok_keeps_old_advice(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "token_ok", 600)
        self.assertIn("自动侦测并连接", msg)

    def test_parse_success_sets_flag(self):
        dl = XunleiDownloader(gateway_cfg())
        dl._request = mock.Mock(return_value={"resources": [{"name": "a.mkv"}]})
        dl.parse_url(MAGNET)
        self.assertTrue(dl._cloud_ok)

    def test_parse_empty_keeps_flag_false(self):
        dl = XunleiDownloader(gateway_cfg())
        dl._request = mock.Mock(return_value={"resources": []})
        dl.parse_url(MAGNET)
        self.assertFalse(dl._cloud_ok)


class TestMinimalRetry(unittest.TestCase):
    def _dl(self):
        dl = XunleiDownloader(gateway_cfg())
        self.calls = []

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            self.calls.append((path, body))
            if path == "/drive/v1/tasks":
                return {"tasks": [{"target": "device#1"}]}
            if path == "/drive/v1/resource/list":
                return {"resources": [{"name": "a.mkv", "file_size": 1},
                                      {"name": "b.srt", "file_size": 2}]}
            if path == "/drive/v1/task":
                return {"error": "invalid token"}
            return {}

        dl._request = fake
        return dl

    def test_retries_with_minimal_body_when_cloud_ok(self):
        dl = self._dl()
        res = dl.add(MAGNET, "标题", {"target": "device#1", "_no_folder": True})
        self.assertFalse(res.ok)
        posted = [b for p, b in self.calls if p == "/drive/v1/task"]
        self.assertEqual(len(posted), 2)                 # 正常一次 + 最简一次
        self.assertIn("sub_file_index", posted[0]["params"])   # 正常投递带全字段
        self.assertNotIn("sub_file_index", posted[1]["params"])
        self.assertNotIn("mime_type", posted[1]["params"])
        self.assertIn("file_name", posted[1])                  # 必填，不能砍
        self.assertIn("最简参数重试", res.message)

    def test_no_minimal_retry_when_cloud_never_parsed(self):
        # 解析都失败 → 无从判断云端令牌好坏，别乱试最简体（省得掩盖真问题）
        dl = XunleiDownloader(gateway_cfg())
        calls = []

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            calls.append(path)
            if path == "/drive/v1/tasks":
                return {"tasks": [{"target": "device#1"}]}
            if path == "/drive/v1/resource/list":
                return {"_raw_text": "boom"}
            return {"error": "invalid token"}

        dl._request = fake
        dl.add(MAGNET, "标题", {"target": "device#1", "_no_folder": True})
        self.assertEqual(calls.count("/drive/v1/task"), 1)

    def test_switch_off(self):
        dl = self._dl()
        dl.cfg["auto_minimal_body"] = False
        dl.add(MAGNET, "标题", {"target": "device#1", "_no_folder": True})
        posted = [b for p, b in self.calls if p == "/drive/v1/task"]
        self.assertEqual(len(posted), 1)


if __name__ == "__main__":
    unittest.main()
