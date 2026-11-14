# -*- coding: utf-8 -*-
"""飞牛登录 Cookie 的清洗与「从套件首页换令牌」。

2026-11-14 现场：迅雷一直登录着，pan_auth 也没过期，程序却一直 ``invalid token``。
逐项剥离后确认：新版飞牛网关（迅雷套件 3.23.5）改认 Cookie 里的
``fnwg_token`` + ``ost``；而 **只要 Cookie 里出现 fnos-token 这个名字，网关就整条拒**，
值和位置都不看。这些用例把这两条钉住。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb import util                                                  # noqa: E402
from tmb.config import Config                                          # noqa: E402
from tmb.downloaders.xunlei import XunleiDownloader                    # noqa: E402

REAL = ("osrt=AAA/BBB=; fnwg_token=afa4a46bd3a5f1bc295343accc8084c084c56a2211ff23c40a3f9d203da189d7; "
        "language=zh-CN; XLA_CI=00bb6db2d77976ef95e902e108382ee8; ost=+Wxv1icKtdQ7sg2fpB+0CXpWSsPiqIDgUWiSsKwepN4=")

PAGE = '''<html><head><script>
function uiauth(value){ return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJrZXkiOiJVSUF1dGgiLCJleHAiOjE3OTQ4NDIxMTl9.hoo2_2qqY2ng7MUYoXI9B1O8BL7rXw8F3KZkyYKzX28"; }
</script></head></html>'''


def gateway_cfg(**kw):
    cfg = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
           "pan_auth": "OLD", "fnos_cookie": "", "fnos_token": "", "target": "device#1"}
    cfg.update(kw)
    return cfg


class TestCleanCookie(unittest.TestCase):
    def test_keeps_what_fnos_wants(self):
        got = util.clean_fnos_cookie(REAL)
        self.assertIn("fnwg_token=afa4", got)
        self.assertIn("ost=+Wxv", got)

    def test_drops_fnos_token_when_fnwg_present(self):
        """硬规则：两者同时出现时，fnos-token 必须被剔掉，否则整条被拒。"""
        got = util.clean_fnos_cookie("fnos-token=OLD; fnwg_token=W; ost=O")
        self.assertNotIn("fnos-token", got)
        self.assertIn("fnwg_token=W", got)

    def test_keeps_fn_token_when_old_fnos(self):
        """老版飞牛只有 fnos-token，那就得留着——不能一刀切删了。"""
        got = util.clean_fnos_cookie("fnos-token=OLD; language=zh-CN")
        self.assertIn("fnos-token=OLD", got)

    def test_strips_cookie_prefix_and_quotes(self):
        got = util.clean_fnos_cookie('Cookie: "fnwg_token=W; ost=O"')
        self.assertEqual(got, "fnwg_token=W; ost=O")

    def test_handles_multiline_paste(self):
        got = util.clean_fnos_cookie("fnwg_token=W;\n  ost=O\n")
        self.assertEqual(got, "fnwg_token=W; ost=O")

    def test_dedupes_same_name(self):
        got = util.clean_fnos_cookie("ost=A; ost=B; fnwg_token=W")
        self.assertEqual(got.count("ost="), 1)

    def test_empty_stays_empty(self):
        for junk in ("", None, "   ", ";;"):
            self.assertEqual(util.clean_fnos_cookie(junk), "")

    def test_has_fnwg(self):
        self.assertTrue(util.fnos_cookie_has_fnwg("XLA_CI=x; fnwg_token=W"))
        self.assertFalse(util.fnos_cookie_has_fnwg("fnos-token=OLD"))


class TestFindJwt(unittest.TestCase):
    def test_reads_uiauth_from_page(self):
        got = util.find_jwt_in_page(PAGE)
        self.assertTrue(got.startswith("eyJ"))
        self.assertTrue(util.pan_auth_looks_valid(got))

    def test_no_jwt_returns_blank(self):
        self.assertEqual(util.find_jwt_in_page("<html>nothing</html>"), "")


class TestFetchPanAuth(unittest.TestCase):
    def _dl(self, page, ev=None):
        dl = XunleiDownloader(gateway_cfg())
        dl._fetch_page = lambda: (page, ev or {"status": 200, "body": ""})
        return dl

    def test_returns_inline_token(self):
        token, note = self._dl(PAGE).fetch_pan_auth()
        self.assertTrue(util.pan_auth_looks_valid(token))
        self.assertIn("首页", note)

    def test_page_unreachable_explains(self):
        token, note = self._dl("", {"status": 0, "body": "invalid token"}).fetch_pan_auth()
        self.assertEqual(token, "")
        self.assertIn("打不开", note)

    def test_page_without_token_explains(self):
        token, note = self._dl("<html>no token</html>").fetch_pan_auth()
        self.assertEqual(token, "")
        self.assertIn("没找到", note)

    def test_probe_swaps_token_and_retries(self):
        """Cookie 好、pan_auth 废：首页读到新的 → 换上去重试 → 直接成功。"""
        dl = XunleiDownloader(gateway_cfg(
            pan_auth="OLD", fnos_cookie="fnwg_token=W; ost=O"))
        calls = []

        def fake(path, body=None, method="POST", extra=None, with_token=True):
            calls.append(dl.cfg.get("pan_auth"))
            if len(calls) == 1:
                return {"_raw_text": "invalid token", "_auth_failed": "token"}
            return {"tasks": [{"target": "device#9", "name": "任务"}]}

        dl._request = fake
        dl._fetch_page = lambda: (PAGE, {"status": 200, "body": ""})
        info = dl.probe()
        self.assertTrue(info["ok"], info)
        self.assertIn("pan_auth_new", info)
        self.assertEqual(dl.cfg["pan_auth"], info["pan_auth_new"])


class TestConfigMigration(unittest.TestCase):
    """从 1.0.41 升上来时，配置要自动摆正，不能让人手工改 JSON。"""

    def _load_after(self, mutate):
        tmp = tempfile.mkdtemp()
        etc, var = os.path.join(tmp, "etc"), os.path.join(tmp, "var")
        cfg = Config(etc, var).load()
        mutate(cfg.data["download"]["xunlei"])
        cfg.save()
        return Config(etc, var).load().data["download"]["xunlei"]

    def test_dirty_xla_ci_is_split_into_cookie(self):
        # 1.0.41 的救急写法：整条 Cookie 塞在 xla_ci 里
        def dirty(x):
            x["xla_ci"] = "CI0; fnwg_token=W; ost=O"
            x["fnos_cookie"] = ""

        got = self._load_after(dirty)
        self.assertEqual(got["xla_ci"], "CI0")
        self.assertIn("fnwg_token=W", got["fnos_cookie"])
        self.assertIn("ost=O", got["fnos_cookie"])

    def test_stored_cookie_never_keeps_fnos_token(self):
        def dirty(x):
            x["fnos_cookie"] = "fnos-token=OLD; fnwg_token=W; ost=O"

        got = self._load_after(dirty)
        self.assertNotIn("fnos-token", got["fnos_cookie"])
        self.assertIn("fnwg_token=W", got["fnos_cookie"])


if __name__ == "__main__":
    unittest.main()
