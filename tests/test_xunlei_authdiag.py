# -*- coding: utf-8 -*-
"""「到底哪个令牌失效了」的判定与落地。

背景：网关模式下 fnos-token 和 pan_auth 是一起发出去的，迅雷只回一句
``invalid token``，光看文本分不清是谁的错。1.0.10 之前一律按模式怪 fnos-token，
用户重登飞牛多少次都没用。这里锁住「对比判断」的行为。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb import xunlei_detect as xd                                  # noqa: E402
from tmb.config import Config                                       # noqa: E402
from tmb.downloaders.xunlei import (                                # noqa: E402
    XunleiDownloader, explain_auth, _age_text)

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def gateway_cfg(**kw):
    cfg = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
           "pan_auth": "PAN", "fnos_token": "FNTOKEN", "target": "device#1"}
    cfg.update(kw)
    return cfg


class TestHeaders(unittest.TestCase):
    def test_legacy_token_still_uses_fnos_token(self):
        """老版飞牛：没配 fnos_cookie 时仍按 fnos-token 发，自定义头也照旧。"""
        dl = XunleiDownloader(gateway_cfg())
        h = dl._headers()
        self.assertIn("fnos-token=FNTOKEN", h["Cookie"])
        self.assertEqual(h["X-Trim-Token"], "FNTOKEN")
        self.assertEqual(h["X-Fnos-Token"], "FNTOKEN")

    def test_new_cookie_is_sent_verbatim(self):
        """新版飞牛：整条 Cookie 原样发，不套前缀、也不补自定义头。"""
        dl = XunleiDownloader(gateway_cfg(
            fnos_token="", fnos_cookie="XLA_CI=CI1; fnwg_token=W; ost=O"))
        h = dl._headers()
        self.assertIn("fnwg_token=W", h["Cookie"])
        self.assertIn("ost=O", h["Cookie"])
        self.assertNotIn("X-Trim-Token", h)

    def test_fnos_token_never_leaks_into_new_cookie(self):
        """硬规则：Cookie 里出现 fnos-token 就被整条拒，必须剔掉。"""
        dl = XunleiDownloader(gateway_cfg(
            fnos_token="", fnos_cookie="fnos-token=OLD; fnwg_token=W; ost=O"))
        cookie = dl._headers()["Cookie"]
        self.assertNotIn("fnos-token=OLD", cookie)
        self.assertIn("fnwg_token=W", cookie)

    def test_no_empty_cookie_junk(self):
        dl = XunleiDownloader(gateway_cfg(fnos_token="", xla_ci=""))
        h = dl._headers()
        self.assertNotIn("Cookie", h)
        self.assertNotIn("X-Trim-Token", h)

    def test_dirty_xla_ci_is_not_sent(self):
        """1.0.41 的救急法把整条 Cookie 塞进 xla_ci，别再当 XLA_CI 发出去。"""
        dl = XunleiDownloader(gateway_cfg(fnos_token="", xla_ci="fnwg_token=W; ost=O"))
        self.assertNotIn("Cookie", dl._headers())

    def test_with_token_false_drops_everything(self):
        dl = XunleiDownloader(gateway_cfg(xla_ci="CI1"))
        h = dl._headers(with_token=False)
        self.assertNotIn("Cookie", h)
        self.assertNotIn("X-Trim-Token", h)


class TestCulprit(unittest.TestCase):
    """归因靠「套件首页」探针：打得开 = 登录 Cookie 是好的，剩下的只可能是 pan_auth。

    1.0.41 及以前是拿同一个接口连问两次（带/不带 fnos-token）比回答，
    在新版飞牛上必然判反——网关见到 fnos-token 就整条拒，两次当然一样。
    """

    def _dl(self, page="", body="", error="", **kw):
        dl = XunleiDownloader(gateway_cfg(**kw))
        dl._fetch_page = lambda: (page, {"body": body, "error": error, "status": 200})
        return dl

    def test_page_opens_means_pan_auth(self):
        dl = self._dl(page="<html><body>ok</body></html>")
        culprit, ev = dl.auth_culprit("invalid token")
        self.assertEqual(culprit, "pan_auth")
        self.assertIn("Cookie 有效", ev["reason"])

    def test_page_refused_means_login_cookie(self):
        dl = self._dl(body="invalid token", fnos_token="",
                      fnos_cookie="fnwg_token=W; ost=O")
        culprit, ev = dl.auth_culprit("invalid token")
        self.assertEqual(culprit, "fnos_token")
        self.assertIn("失效", ev["reason"])

    def test_no_cookie_at_all(self):
        dl = self._dl(body="invalid token", fnos_token="", fnos_cookie="")
        culprit, ev = dl.auth_culprit("invalid token")
        self.assertEqual(culprit, "fnos_token")
        self.assertIn("一个飞牛登录 Cookie 都没配", ev["reason"])

    def test_legacy_only_cookie_is_named_as_the_problem(self):
        dl = self._dl(body="invalid token", fnos_token="old", fnos_cookie="")
        culprit, ev = dl.auth_culprit("invalid token")
        self.assertEqual(culprit, "fnos_token")
        self.assertIn("fnwg_token", ev["reason"])

    def test_panel_mode_never_probes(self):
        dl = XunleiDownloader({"mode": "panel", "pan_auth": "PAN"})
        dl._fetch_page = mock.Mock(side_effect=AssertionError("不该发请求"))
        self.assertEqual(dl.auth_culprit("invalid token"), ("", {}))

    def test_network_error_blames_nobody(self):
        dl = self._dl(error="timed out")
        culprit, ev = dl.auth_culprit("")
        self.assertEqual(culprit, "")
        self.assertIn("没发出去", ev["reason"])

    def test_unexpected_body_blames_nobody(self):
        dl = self._dl(body="502 Bad Gateway")
        culprit, _ = dl.auth_culprit("")
        self.assertEqual(culprit, "")


class TestExplain(unittest.TestCase):
    def test_pan_auth_culprit_stops_blaming_fnos(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "pan_auth", 86400 * 3)
        self.assertIn("pan_auth", msg)
        self.assertNotIn("飞牛登录 Cookie 失效", msg)      # 别再让用户去重登飞牛

    def test_drop_token_culprit(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "drop_token")
        self.assertIn("清空", msg)

    def test_old_token_shows_age(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "", 86400 * 3)
        self.assertIn("飞牛登录 Cookie 失效", msg)
        self.assertIn("3 天前", msg)

    def test_fresh_token_checks_port_not_relogin(self):
        msg = explain_auth("token", "fnos_gateway", gateway_cfg(), "", 60)
        self.assertIn("5666", msg)
        self.assertNotIn("重登飞牛不会", msg)

    def test_age_text(self):
        self.assertEqual(_age_text(None), "")
        self.assertEqual(_age_text(30), "刚更新不到两分钟")
        self.assertEqual(_age_text(300), "5 分钟前")
        self.assertEqual(_age_text(7200), "2 小时前")
        self.assertEqual(_age_text(86400 * 2), "2 天前")


class TestHtmlBody(unittest.TestCase):
    """网关没认登录态时直接甩回登录页，以前会被当成「连通了」"""

    def test_login_page_counts_as_auth_failure(self):
        dl = XunleiDownloader(gateway_cfg())
        html = "<!DOCTYPE html><html><body>请先登录</body></html>"
        with mock.patch("tmb.downloaders.xunlei.util.http_request",
                        return_value=(200, html, {})):
            data = dl._request("/drive/v1/tasks", method="GET")
        self.assertEqual(data.get("_auth_failed"), "token")

    def test_probe_says_auth_not_connected(self):
        dl = XunleiDownloader(gateway_cfg())
        html = "<!DOCTYPE html><html><body>登录</body></html>"

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            return {"_raw_text": html, "_auth_failed": "token"}

        dl._request = fake
        info = dl.probe()
        self.assertFalse(info["ok"])
        self.assertEqual(info["auth"], "token")
        self.assertNotIn("任务列表", info.get("error", ""))

    def test_json_body_still_ok(self):
        dl = XunleiDownloader(gateway_cfg())
        with mock.patch("tmb.downloaders.xunlei.util.http_request",
                        return_value=(200, '{"tasks": [{"target": "d#1"}]}', {})):
            data = dl._request("/drive/v1/tasks", method="GET")
        self.assertNotIn("_auth_failed", data)


class TestTokenAge(unittest.TestCase):
    def test_age_from_ts(self):
        dl = XunleiDownloader(gateway_cfg(fnos_token_ts=time.time() - 7200))
        self.assertAlmostEqual(dl.token_age(), 7200, delta=5)

    def test_missing_ts(self):
        self.assertIsNone(XunleiDownloader(gateway_cfg()).token_age())
        self.assertIsNone(XunleiDownloader(gateway_cfg(fnos_token_ts="bad")).token_age())


class TestMessagesAndProbe(unittest.TestCase):
    def test_add_message_names_pan_auth(self):
        dl = XunleiDownloader(gateway_cfg(fnos_token_ts=time.time() - 86400 * 5))

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            return {"_raw_text": "invalid token", "_auth_failed": "token"}

        dl._request = fake
        # 首页打得开 = 登录 Cookie 是好的，锅只能在 pan_auth 上
        dl._fetch_page = lambda: ("<html>ok</html>", {"status": 200, "body": ""})
        res = dl.add(MAGNET, "某作品")
        self.assertFalse(res.ok)
        self.assertIn("pan_auth", res.message)
        self.assertNotIn("飞牛登录 Cookie 失效", res.message)

    def test_probe_exposes_culprit_and_age(self):
        dl = XunleiDownloader(gateway_cfg(fnos_token_ts=time.time() - 86400))

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            return {"_raw_text": "invalid token", "_auth_failed": "token"}

        dl._request = fake
        dl._fetch_page = lambda: ("<html>ok</html>", {"status": 200, "body": ""})
        info = dl.probe()
        self.assertFalse(info["ok"])
        self.assertEqual(info["auth"], "token")
        self.assertEqual(info["culprit"], "pan_auth")
        self.assertIn("1 天前", info["token_age"])
        self.assertIn("Cookie 有效", info["auth_evidence"])

    def test_probe_panel_has_no_culprit(self):
        dl = XunleiDownloader({"mode": "panel", "pan_auth": "PAN"})
        dl._request = mock.Mock(return_value={"_raw_text": "invalid token",
                                              "_auth_failed": "token"})
        info = dl.probe()
        self.assertNotIn("culprit", info)
        self.assertEqual(info["token_age"], "")


class TestWebuiPersistence(unittest.TestCase):
    def _ctx(self, mode):
        from tmb.webui import AppContext
        tmp = tempfile.mkdtemp()
        etc, var = os.path.join(tmp, "etc"), os.path.join(tmp, "var")
        os.makedirs(etc)
        os.makedirs(var)
        cfg = Config(etc, var).load()
        cfg.data["download"]["xunlei"]["mode"] = mode
        return cfg, AppContext(cfg, None, None, None, "")

    @staticmethod
    def _headers(token):
        return {"Cookie": "a=1; fnos-token=%s; b=2" % token, "Host": "nas:5666"}

    def test_gateway_mode_writes_token_to_config(self):
        cfg, ctx = self._ctx("fnos_gateway")
        ctx.note_fnos_token(self._headers("NEWTOKEN"))
        self.assertEqual(ctx.fnos_token, "NEWTOKEN")
        self.assertTrue(ctx.fnos_token_seen)
        self.assertEqual(cfg.get("download.xunlei.fnos_token"), "NEWTOKEN")
        self.assertEqual(cfg.get("download.xunlei.fnos_token_source"), "browser")
        self.assertGreater(cfg.get("download.xunlei.fnos_token_ts"), 0)
        self.assertEqual(ctx.fnos_port, 5666)          # 端口取自 Host
        self.assertEqual(ctx.auth_view()["port"], 5666)

    def test_panel_mode_does_not_touch_config(self):
        cfg, ctx = self._ctx("panel")
        ctx.note_fnos_token(self._headers("NEWTOKEN"))
        self.assertEqual(ctx.fnos_token, "NEWTOKEN")
        self.assertEqual(cfg.get("download.xunlei.fnos_token"), "")

    def test_same_token_not_rewritten(self):
        cfg, ctx = self._ctx("fnos_gateway")
        cfg.data["download"]["xunlei"]["fnos_token"] = "SAME"
        with mock.patch.object(cfg, "apply_patch") as ap:
            ctx.note_fnos_token(self._headers("SAME"))
        ap.assert_not_called()

    def test_auth_view(self):
        cfg, ctx = self._ctx("fnos_gateway")
        view = ctx.auth_view()
        self.assertFalse(view["has_token"])
        self.assertIsNone(view["age"])
        ctx.note_fnos_token(self._headers("T1"))
        view = ctx.auth_view()
        self.assertTrue(view["has_token"])
        self.assertEqual(view["source"], "browser")
        self.assertTrue(view["browser_cookie_seen"])
        self.assertIsNotNone(view["age"])
        self.assertEqual(view["mode"], "fnos_gateway")

    def test_empty_cookie_marks_seen_false(self):
        _, ctx = self._ctx("fnos_gateway")
        ctx.note_fnos_token({"Host": "nas:5666"})
        self.assertFalse(ctx.fnos_token_seen)
        self.assertFalse(ctx.auth_view()["browser_cookie_seen"])


class TestScanAlternates(unittest.TestCase):
    def _scan(self, tok_map, **kw):
        cfg = {"download": {"xunlei": {"mode": "fnos_gateway", "fnos_port": 5666,
                                       "pan_auth": "PAN", "fnos_token": "OLD"}}}

        def fake_fp(host, port, pan_auth="", fnos_token="", timeout=3):
            state = tok_map.get(fnos_token, "need_token")
            return {"mode": "fnos_gateway", "host": host, "port": port, "url": "u",
                    "status": 200, "state": state, "detail": "", "sample": ""}

        with mock.patch.object(xd, "fingerprint", side_effect=fake_fp), \
                mock.patch.object(xd, "processes", return_value=[]), \
                mock.patch.object(xd, "listening_ports", return_value=[]):
            return xd.scan(ports=[], dirs=[], cfg=cfg, fnos_port=5666, **kw)

    def test_alternate_token_wins(self):
        rep = self._scan({"OLD": "ok"}, fnos_token="BAD")
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["alternates"])
        self.assertEqual(rep["_secrets"]["fnos_token"], "OLD")
        self.assertTrue(any("备用令牌" in n for n in rep["notes"]))

    def test_no_alternate_keeps_first(self):
        rep = self._scan({}, fnos_token="NEW")
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["alternates"])
        self.assertEqual(rep["_secrets"]["fnos_token"], "NEW")

    def test_patch_records_source_and_ts(self):
        rep = self._scan({"OLD": "ok"}, fnos_token="BAD")
        patch = xd.patch_for(rep)
        self.assertEqual(patch["fnos_token"], "OLD")
        self.assertEqual(patch["fnos_token_source"], "候选备用令牌")
        self.assertGreater(patch["fnos_token_ts"], 0)

    def test_dedup(self):
        self.assertEqual(xd._dedup(["a", "", " a ", None, "b"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
