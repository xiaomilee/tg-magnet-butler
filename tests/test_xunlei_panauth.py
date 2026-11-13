"""pan_auth 的过期体检，以及「到底怪谁」的判定。

用户报的原始现象：面板一直说 fnos-token 失效，重登飞牛多少次都没用。
真实原因是 1.0.11 把「带着和去掉 fnos-token 回答一样」判成了 fnos-token 的错，
而本机访问网关时飞牛压根不校验 Cookie——拒绝来自迅雷云的 pan_auth。
"""
import base64
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb.downloaders.xunlei import (XunleiDownloader, _looks_like_pan_error,  # noqa: E402
                                    explain_auth, pan_auth_life)
from tmb.webui import AppContext  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def make_jwt(payload):
    """造一个只有 payload 有意义的 JWT（签名随便写，解析不看它）。"""
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.%s" % (seg({"alg": "HS256", "typ": "JWT"}), seg(payload), "sig")


def gateway_cfg(**kw):
    cfg = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
           "pan_auth": make_jwt({"sub": "u1", "iat": int(time.time()) - 86400,
                                 "exp": int(time.time()) + 86400}),
           "fnos_token": "FNOS-TOKEN-123456", "target": "device#1"}
    cfg.update(kw)
    return cfg


class TestPanAuthLife(unittest.TestCase):
    def test_expired_token_says_so(self):
        token = make_jwt({"exp": int(time.time()) - 3600 * 5})
        life = pan_auth_life(token)
        self.assertTrue(life["expired"])
        self.assertIn("过期", life["text"])
        self.assertIn("小时前", life["text"])

    def test_fresh_token_shows_remaining(self):
        token = make_jwt({"exp": int(time.time()) + 86400 * 3})
        life = pan_auth_life(token)
        self.assertFalse(life["expired"])
        self.assertIn("还有", life["text"])
        self.assertIn("天后", life["text"])

    def test_millisecond_exp_is_normalised(self):
        # 迅雷有的版本给的是毫秒时间戳，按秒读会得出「几万年后过期」的鬼话
        token = make_jwt({"exp": int((time.time() + 3600) * 1000)})
        life = pan_auth_life(token)
        self.assertFalse(life["expired"])
        self.assertLess(life["left"], 7200)          # 还剩不到 2 小时，说明单位归对了

    def test_not_a_jwt(self):
        self.assertEqual(pan_auth_life("opaque-token"), {})
        self.assertEqual(pan_auth_life(""), {})
        self.assertEqual(pan_auth_life(None), {})

    def test_jwt_without_exp(self):
        life = pan_auth_life(make_jwt({"sub": "u1"}))
        self.assertIsNone(life["expired"])
        self.assertEqual(life["text"], "")


class TestWhoToBlame(unittest.TestCase):
    def test_pan_error_phrases(self):
        for text in ("invalid token", "token expired", "Invalid Token", "令牌无效"):
            self.assertTrue(_looks_like_pan_error(text), text)

    def test_gateway_refusal_phrases(self):
        for text in ("<html><body>登录</body></html>", "need login", "未登录", "请登录"):
            self.assertFalse(_looks_like_pan_error(text), text)
        self.assertFalse(_looks_like_pan_error(""))


class TestExplain(unittest.TestCase):
    def test_pan_auth_blames_the_expired_jwt(self):
        cfg = gateway_cfg(pan_auth=make_jwt({"exp": int(time.time()) - 7200}))
        msg = explain_auth("token", "fnos_gateway", cfg, "pan_auth")
        self.assertIn("pan_auth", msg)
        self.assertIn("过期", msg)
        self.assertNotIn("fnos-token 失效", msg)
        self.assertIn("迅雷套件", msg)          # 给出可执行的下一步

    def test_expired_jwt_reported_even_without_culprit(self):
        # 对比做不成（网关整个不通）时也不该让人去重登飞牛：JWT 自己写着过期了
        cfg = gateway_cfg(pan_auth=make_jwt({"exp": int(time.time()) - 86400}))
        msg = explain_auth("token", "fnos_gateway", cfg, "")
        self.assertIn("pan_auth", msg)
        self.assertNotIn("fnos-token 失效", msg)

    def test_still_talks_about_fnos_token_when_jwt_is_fine(self):
        cfg = gateway_cfg()                    # pan_auth 未过期
        msg = explain_auth("token", "fnos_gateway", cfg, "")
        self.assertIn("fnos-token", msg)


class TestProbe(unittest.TestCase):
    def test_probe_reports_token_expiry(self):
        cfg = gateway_cfg(pan_auth=make_jwt({"exp": int(time.time()) - 3600}))
        dl = XunleiDownloader(cfg)
        dl._request = lambda *a, **k: {"_raw_text": "invalid token",
                                       "_auth_failed": "token"}
        info = dl.probe()
        self.assertFalse(info["ok"])
        self.assertTrue(info["pan_auth_expired"])
        self.assertIn("过期", info["pan_auth_exp"])

    def test_add_message_points_at_pan_auth(self):
        cfg = gateway_cfg(pan_auth=make_jwt({"exp": int(time.time()) - 3600}))
        dl = XunleiDownloader(cfg)

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            if path == "/drive/v1/tasks":
                return {"_raw_text": "invalid token", "_auth_failed": "token"}
            return {"_raw_text": "invalid token", "_auth_failed": "token"}

        dl._request = fake
        res = dl.add(MAGNET, "标题", {"target": "device#1", "_no_folder": True})
        self.assertFalse(res.ok)
        self.assertIn("pan_auth", res.message)
        self.assertNotIn("fnos-token 失效", res.message)


class TestDetectClassify(unittest.TestCase):
    def test_invalid_token_is_not_missing_interface(self):
        # 网关转给迅雷、迅雷回 invalid token：接口是通的，别报成「这个端口不是迅雷接口」
        from tmb import xunlei_detect
        state, detail = xunlei_detect.classify(200, "invalid token", "fnos_gateway")
        self.assertEqual(state, "need_token")
        self.assertIn("令牌被拒", detail)

    def test_html_still_means_wrong_port(self):
        from tmb import xunlei_detect
        state, _ = xunlei_detect.classify(200, "<html>hi</html>", "fnos_gateway")
        self.assertEqual(state, "absent")


class TestFnosPortGuess(unittest.TestCase):
    """面板端口不能当成飞牛网关端口——直接访问 8470 时 Host 里就是它。"""

    def _ctx(self, web_port):
        class Fake(object):
            fnos_token = ""
            fnos_token_seen = False
            fnos_port = 0

            def _remember_fnos_token(self, tok):
                pass

        Fake.web_port = web_port
        return Fake()

    def test_panel_port_is_not_fnos_port(self):
        ctx = self._ctx(8470)
        AppContext.note_fnos_token(ctx, {"Host": "192.168.1.2:8470"})
        self.assertEqual(ctx.fnos_port, 0)

    def test_other_port_is_remembered(self):
        ctx = self._ctx(8470)
        AppContext.note_fnos_token(ctx, {"Host": "192.168.1.2:5666"})
        self.assertEqual(ctx.fnos_port, 5666)


if __name__ == "__main__":
    unittest.main()
