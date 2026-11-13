"""迅雷令牌失效的诊断与自愈。

背景：迅雷接口鉴权失败时不回 JSON，body 就是一行 `invalid token`。
以前这条会被原样抛成「迅雷拒绝任务: {"_raw_text": "invalid token"}」——
既没说是哪个令牌，也没说该去哪换。这里锁四件事：

1. 纯文本鉴权错误能被识别，并翻成「哪个令牌、怎么修」。
2. 失败信息里带模式与令牌现状，不再是一句英文原文。
3. probe 能把「令牌坏了」和「空间没激活」分开。
4. 失效时能本机重抓一次令牌、写回配置并立刻重试同一个后端。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb.config import Config                              # noqa: E402
from tmb.downloaders.base import DownloadResult            # noqa: E402
from tmb.downloaders.xunlei import (                       # noqa: E402
    XunleiDownloader, classify_auth, explain_auth)
from tmb.pipeline import Pipeline                          # noqa: E402
from tmb.store import Store                                # noqa: E402


class TestClassify(unittest.TestCase):
    def test_raw_text_invalid_token(self):
        self.assertEqual(classify_auth("invalid token"), "token")
        self.assertEqual(classify_auth("Invalid Token"), "token")

    def test_json_wrapped_token_error(self):
        self.assertEqual(classify_auth('{"error": "invalid token"}'), "token")

    def test_account_level_errors_win(self):
        # 账号没登录 / 空间没激活：这不是「换个令牌」能解决的
        self.assertEqual(classify_auth("refresh token not found"), "account")
        self.assertEqual(classify_auth("device_space_not_active"), "account")

    def test_normal_payload_not_auth(self):
        self.assertEqual(classify_auth('{"tasks": []}'), "")
        self.assertEqual(classify_auth(""), "")


class TestExplain(unittest.TestCase):
    def test_panel_without_pan_auth(self):
        msg = explain_auth("token", "panel", {})
        self.assertIn("没填 pan_auth", msg)

    def test_panel_with_expired_pan_auth(self):
        msg = explain_auth("token", "panel", {"pan_auth": "abc"})
        self.assertIn("pan_auth 失效", msg)

    def test_gateway_without_fnos_token(self):
        msg = explain_auth("token", "fnos_gateway", {"pan_auth": "abc"})
        self.assertIn("fnos-token", msg)
        self.assertIn("飞牛", msg)

    def test_gateway_with_expired_fnos_token(self):
        msg = explain_auth("token", "fnos_gateway",
                           {"pan_auth": "abc", "fnos_token": "t"})
        self.assertIn("fnos-token 失效", msg)

    def test_account_message_mentions_login(self):
        self.assertIn("扫码登录", explain_auth("account", "panel", {}))


class TestDownloaderMessages(unittest.TestCase):
    def _dl(self, **kw):
        cfg = {"mode": "panel", "pan_auth": "OLD", "target": "device#1"}
        cfg.update(kw)
        return XunleiDownloader(cfg, "")

    @staticmethod
    def _raw(text, status=200):
        """模拟服务端：HTTP 200、body 是一句纯文本。"""
        return mock.patch("tmb.downloaders.xunlei.util.http_request",
                          return_value=(status, text, text.encode()))

    def test_task_rejected_with_invalid_token(self):
        dl = self._dl()
        with self._raw("invalid token"):
            res = dl.add("magnet:?xt=urn:btih:" + "a" * 40, "t")
        self.assertFalse(res.ok)
        self.assertIn("鉴权失败", res.message)
        self.assertIn("pan_auth 失效", res.message)
        self.assertIn("invalid token", res.message)      # 保留原话，方便检索
        self.assertEqual(res.raw.get("_auth_failed"), "token")

    def test_no_target_and_missing_pan_auth_says_so(self):
        dl = self._dl(pan_auth="", target="")
        with self._raw("invalid token"):
            res = dl.add("magnet:?xt=urn:btih:" + "a" * 40, "t")
        self.assertFalse(res.ok)
        self.assertIn("没填 pan_auth", res.message)
        self.assertEqual(res.raw.get("_auth_failed"), "token")

    def test_http_401_also_reads_as_token_problem(self):
        dl = self._dl()
        from tmb import util
        with mock.patch("tmb.downloaders.xunlei.util.http_request",
                        side_effect=util.HttpError("HTTP 401", 401, b"")):
            res = dl.add("magnet:?xt=urn:btih:" + "a" * 40, "t")
        self.assertFalse(res.ok)
        self.assertIn("鉴权失败", res.message)
        self.assertEqual(res.raw.get("_auth_failed"), "token")

    def test_probe_reports_auth_not_just_false(self):
        dl = self._dl()
        with self._raw("invalid token"):
            info = dl.probe()
        self.assertFalse(info["ok"])
        self.assertEqual(info["auth"], "token")
        self.assertIn("pan_auth 失效", info["hint"])

    def test_probe_ok_when_target_present(self):
        dl = self._dl()
        with self._raw('{"tasks": [{"target": "device#9"}]}'):
            info = dl.probe()
        self.assertTrue(info["ok"])
        self.assertEqual(info["target"], "device#9")
        self.assertEqual(info["auth"], "")

    def test_gateway_message_points_at_fnos_token(self):
        dl = self._dl(mode="fnos_gateway", fnos_token="", target="")
        with self._raw("invalid token"):
            res = dl.add("magnet:?xt=urn:btih:" + "a" * 40, "t")
        self.assertIn("fnos-token", res.message)
        self.assertIn("飞牛", res.message)


class TestPipelineHeal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.data["download"]["backend"] = "xunlei"
        self.cfg.data["download"]["fallback"] = []
        self.cfg.data["app"]["dedupe_days"] = 0
        self.cfg.data["download"]["xunlei"].update(
            {"mode": "fnos_gateway", "pan_auth": "OLD", "host": "127.0.0.1",
             "fnos_port": 5666})
        self.store = Store(os.path.join(self.tmp, "var", "t.db"))
        self.pipe = Pipeline(self.cfg, self.store)

    def _item(self):
        return {"title": "某作品", "magnet": "magnet:?xt=urn:btih:" + "b" * 40,
                "infohash": "b" * 40}

    @staticmethod
    def _fail():
        return DownloadResult(False, "xunlei", "", "迅雷鉴权失败：pan_auth 失效了",
                              {"_auth_failed": "token", "_stage": "task"})

    @staticmethod
    def _ok():
        return DownloadResult(True, "xunlei", "42", "已投递迅雷（1 个文件）")

    @staticmethod
    def _fake(res):
        """假下载器：name 必须真写成字符串，Mock 的 name 属性是自动生成的子 mock。"""
        m = mock.Mock()
        m.name = "xunlei"
        m.add = mock.Mock(return_value=res)
        return m

    def test_heal_refreshes_token_and_retries(self):
        rep = {"best": {"state": "ok", "mode": "fnos_gateway",
                        "host": "127.0.0.1", "port": 5666},
               "_secrets": {"pan_auth": "NEW", "fnos_token": ""}}
        first, second = self._fake(self._fail()), self._fake(self._ok())
        with mock.patch("tmb.downloaders.chain", return_value=[first]), \
                mock.patch("tmb.downloaders.make", return_value=second), \
                mock.patch("tmb.xunlei_detect.scan", return_value=rep):
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["task_id"], "42")
        self.assertIn("令牌已自动更新", out["message"])
        self.assertEqual(self.cfg.data["download"]["xunlei"]["pan_auth"], "NEW")

    def test_switch_off_means_no_scan(self):
        self.cfg.data["download"]["xunlei"]["auto_refresh_token"] = False
        first = self._fake(self._fail())
        with mock.patch("tmb.downloaders.chain", return_value=[first]), \
                mock.patch("tmb.xunlei_detect.scan") as scan:
            out = self.pipe.submit(self._item())
        self.assertFalse(out["ok"])
        self.assertIn("鉴权失败", out["message"])
        scan.assert_not_called()

    def test_no_ok_candidate_keeps_old_config(self):
        rep = {"best": {"state": "no_space"}, "_secrets": {}}
        first = self._fake(self._fail())
        with mock.patch("tmb.downloaders.chain", return_value=[first]), \
                mock.patch("tmb.xunlei_detect.scan", return_value=rep), \
                mock.patch("tmb.downloaders.make") as make:
            out = self.pipe.submit(self._item())
        self.assertFalse(out["ok"])
        make.assert_not_called()
        self.assertEqual(self.cfg.data["download"]["xunlei"]["pan_auth"], "OLD")

    def test_same_token_does_not_retry(self):
        rep = {"best": {"state": "ok", "mode": "fnos_gateway",
                        "host": "127.0.0.1", "port": 5666},
               "_secrets": {"pan_auth": "OLD", "fnos_token": ""}}
        first = self._fake(self._fail())
        with mock.patch("tmb.downloaders.chain", return_value=[first]), \
                mock.patch("tmb.xunlei_detect.scan", return_value=rep), \
                mock.patch("tmb.downloaders.make") as make:
            out = self.pipe.submit(self._item())
        self.assertFalse(out["ok"])
        make.assert_not_called()

    def test_heal_only_once(self):
        rep = {"best": {"state": "ok", "mode": "fnos_gateway",
                        "host": "127.0.0.1", "port": 5666},
               "_secrets": {"pan_auth": "NEW", "fnos_token": ""}}
        first, second = self._fake(self._fail()), self._fake(self._fail())
        with mock.patch("tmb.downloaders.chain", return_value=[first]), \
                mock.patch("tmb.downloaders.make", return_value=second), \
                mock.patch("tmb.xunlei_detect.scan", return_value=rep) as scan:
            out = self.pipe.submit(self._item())
        self.assertFalse(out["ok"])
        self.assertEqual(scan.call_count, 1)             # 不反复重抓
        self.assertEqual(second.add.call_count, 1)


if __name__ == "__main__":
    unittest.main()
