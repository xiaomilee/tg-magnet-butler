"""pan_auth 的形状：整条 watch 地址 vs JWT。

现场（1.0.40，2026-11-14）：面板上写着「迅雷连不上」，probe 回来的却是

    permission_deny: checkAuth failed:
    token contains an invalid number of segments
    token:http://192.168.1.2:5666/cgi/ThirdParty/xunlei/index.cgi/device/info/watch
          ?pan_auth=eyJhbGciOi…&device_space=&space=device_id%231fef9844

看着像「令牌过期」，其实是**形状**不对：配置里那串 pan_auth 不是 JWT，
而是从浏览器里连地址一起复制下来的整条 watch URL。网关把它当 token 转给
迅雷云，云只说「段数不对」——从这句话里没人能猜出是复制多了。

把里面那段 JWT 掐出来立刻就通了（同一台机器、同一份 fnos-token）。
所以这层清理要放在三个入口：读配置、存配置、构造请求。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb import util, xunlei_detect  # noqa: E402
from tmb.config import Config  # noqa: E402
from tmb.downloaders.xunlei import (XunleiDownloader, explain_auth,  # noqa: E402
                                    pan_auth_life)

WATCH = ("http://192.168.1.2:5666/cgi/ThirdParty/xunlei/index.cgi/device/info/watch"
         "?pan_auth=%s&device_space=&space=device_id%%231fef9844")


def make_jwt(payload=None):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    now = int(time.time())
    payload = payload or {"key": "UIAuth", "iat": now - 3600, "exp": now + 259200}
    return "%s.%s.%s" % (seg({"alg": "HS256", "typ": "JWT"}), seg(payload),
                         "hoo2_2qqY2ng7MUYoXI9B1O8BL7rXw8F3KZkyYKzX28")


class TestCleanPanAuth(unittest.TestCase):
    def test_plain_jwt_is_left_alone(self):
        tok = make_jwt()
        self.assertEqual(util.clean_pan_auth(tok), tok)
        self.assertTrue(util.pan_auth_looks_valid(tok))

    def test_strips_whole_watch_url(self):
        """用户粘的就是这个：整条 device/info/watch 地址。"""
        tok = make_jwt()
        self.assertEqual(util.clean_pan_auth(WATCH % tok), tok)

    def test_handles_url_encoded_separator(self):
        tok = make_jwt()
        text = ("http://192.168.1.2:5666/x/device/info/watch?pan_auth%3D"
                + tok + "&device_space=")
        self.assertEqual(util.clean_pan_auth(text), tok)

    def test_handles_json_snippet(self):
        """从响应里抄的 ``"pan_auth":"eyJ…"`` 也认。"""
        tok = make_jwt()
        self.assertEqual(util.clean_pan_auth('"pan_auth": "%s"' % tok), tok)
        self.assertEqual(util.clean_pan_auth("pan_auth=%s" % tok), tok)

    def test_handles_bearer_and_quotes(self):
        tok = make_jwt()
        self.assertEqual(util.clean_pan_auth('  "Bearer %s"  ' % tok), tok)

    def test_does_not_invent_values(self):
        """认不出形状就原样返回：宁可让用户看到原值，也不要猜一个假的。"""
        self.assertEqual(util.clean_pan_auth("OLD"), "OLD")
        self.assertEqual(util.clean_pan_auth(""), "")
        self.assertEqual(util.clean_pan_auth(None), "")
        self.assertFalse(util.pan_auth_looks_valid("OLD"))
        self.assertFalse(util.pan_auth_looks_valid("http://a/b?pan_auth=x"))


class TestConfigRepairsToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.etc = os.path.join(self.tmp, "etc")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, pan_auth):
        os.makedirs(self.etc, exist_ok=True)
        with open(os.path.join(self.etc, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"download": {"xunlei": {"pan_auth": pan_auth}}}, f)

    def test_load_repairs_and_persists(self):
        """读到脏值就地修好并且落盘：下次启动不该又变回那条地址。"""
        tok = make_jwt()
        self._write(WATCH % tok)
        cfg = Config(self.etc, os.path.join(self.tmp, "var")).load()
        self.assertEqual(cfg.get("download.xunlei.pan_auth"), tok)
        # 落盘过了：直接读文件确认，不靠内存
        with open(os.path.join(self.etc, "config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["download"]["xunlei"]["pan_auth"], tok)
        self.assertTrue(pan_auth_life(tok).get("text"))

    def test_apply_patch_repairs_before_saving(self):
        """面板上贴整条地址再保存，存进去的应该已经是 JWT。"""
        tok = make_jwt()
        cfg = Config(self.etc, os.path.join(self.tmp, "var")).load()
        cfg.apply_patch({"download": {"xunlei": {"pan_auth": WATCH % tok}}})
        self.assertEqual(cfg.get("download.xunlei.pan_auth"), tok)

    def test_mask_write_does_not_touch_real_value(self):
        """脱敏值回写仍然只是「保持原值」，清理逻辑不能把它当令牌改了。"""
        tok = make_jwt()
        cfg = Config(self.etc, os.path.join(self.tmp, "var")).load()
        cfg.apply_patch({"download": {"xunlei": {"pan_auth": tok}}})
        cfg.apply_patch({"download": {"xunlei": {"pan_auth": "******"}}})
        self.assertEqual(cfg.get("download.xunlei.pan_auth"), tok)


class TestDownloaderRepairsToken(unittest.TestCase):
    def test_constructor_fixes_shared_cfg(self):
        """请求前兜一层：即使配置是别处塞进来的脏值，也不会照原样发出去。"""
        tok = make_jwt()
        cfg = {"pan_auth": WATCH % tok, "host": "127.0.0.1", "fnos_port": 5666}
        XunleiDownloader(cfg)
        self.assertEqual(cfg["pan_auth"], tok)

    def test_explain_auth_names_the_shape_problem(self):
        """形状不对时别再说「令牌过期」——那会让人反复重抓、反复重登。"""
        msg = explain_auth("token", "fnos_gateway", {"pan_auth": WATCH % make_jwt()})
        self.assertIn("不是 JWT", msg)
        self.assertIn("pan_auth=", msg)
        self.assertNotIn("重登飞牛", msg)


class TestDetectWritesCleanToken(unittest.TestCase):
    def test_patch_for_writes_jwt_only(self):
        tok = make_jwt()
        rep = {"best": {"host": "127.0.0.1", "port": 5666, "mode": "fnos_gateway",
                        "state": "ok"},
               "_secrets": {"pan_auth": WATCH % tok, "fnos_token": "tok123456789"}}
        patch = xunlei_detect.patch_for(rep)
        self.assertEqual(patch["pan_auth"], tok)


if __name__ == "__main__":
    unittest.main()
