"""令牌寿命：快到期时提前换新、换不到就提醒，而不是等投递失败。

用户问「还有 2 天后到期，会自动续期吗」。答案是没有真正的续期接口——能做的只是：
迅雷套件自己把 pan_auth 换新之后，从本机把它捞回来。这些测试锁的就是这套行为。
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb.config import Config  # noqa: E402
from tmb.downloaders.base import DownloadResult  # noqa: E402
from tmb.pipeline import Pipeline  # noqa: E402
from tmb.store import Store  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "c" * 40


def make_jwt(payload):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.%s" % (seg({"alg": "HS256", "typ": "JWT"}), seg(payload), "sig")


def token_in(hours):
    """造一个还有 hours 小时到期的 pan_auth（负数表示已过期）。"""
    now = int(time.time())
    return make_jwt({"sub": "u1", "iat": now - 86400, "exp": now + int(hours * 3600)})


def scan_rep(pan_auth, mode="fnos_gateway", port=5666):
    return {"best": {"state": "ok", "mode": mode, "host": "127.0.0.1", "port": port},
            "_secrets": {"pan_auth": pan_auth, "fnos_token": ""}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.data["download"]["backend"] = "xunlei"
        self.cfg.data["download"]["fallback"] = []
        self.cfg.data["app"]["dedupe_days"] = 0
        self.xcfg = self.cfg.data["download"]["xunlei"]
        self.xcfg.update({"mode": "fnos_gateway", "host": "127.0.0.1",
                          "fnos_port": 5666, "pan_auth": token_in(24 * 10),
                          "fnos_token": "FNOS-1", "auto_refresh_token": True})
        self.store = Store(os.path.join(self.tmp, "var", "t.db"))
        self.pipe = Pipeline(self.cfg, self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _set_left(self, hours):
        self.xcfg["pan_auth"] = token_in(hours)
        self.cfg.data["download"]["xunlei"]["pan_auth"] = self.xcfg["pan_auth"]


class TestTokenState(_Base):
    def test_far_token_is_ok(self):
        st = self.pipe.xunlei_token_state()
        self.assertTrue(st["ok"])
        self.assertEqual(st["level"], "ok")
        self.assertIn("还有", st["text"])

    def test_two_days_left_is_soon(self):
        # 用户的实际情况：还剩 2 天，落在 72 小时提醒窗口内
        self._set_left(48)
        st = self.pipe.xunlei_token_state()
        self.assertEqual(st["level"], "soon")
        self.assertIn("还有", st["text"])

    def test_expired(self):
        self._set_left(-1)
        st = self.pipe.xunlei_token_state()
        self.assertEqual(st["level"], "expired")
        self.assertTrue(st["expired"])

    def test_no_token_is_not_error(self):
        self.xcfg["pan_auth"] = ""
        st = self.pipe.xunlei_token_state()
        self.assertFalse(st["ok"])
        self.assertEqual(st["level"], "")


class TestRefresh(_Base):
    def test_far_token_does_not_scan(self):
        with mock.patch("tmb.xunlei_detect.scan") as scan:
            changed, note = self.pipe.refresh_xunlei_token()
        self.assertFalse(changed)
        self.assertEqual(note, "")
        scan.assert_not_called()

    def test_soon_token_swaps_in_newer(self):
        self._set_left(30)
        newer = token_in(24 * 20)
        with mock.patch("tmb.xunlei_detect.scan", return_value=scan_rep(newer)) as scan:
            changed, note = self.pipe.refresh_xunlei_token()
        self.assertTrue(changed)
        self.assertIn("已换用本机找到的令牌", note)
        scan.assert_called_once()
        self.assertEqual(self.cfg.data["download"]["xunlei"]["pan_auth"], newer)

    def test_target_not_cleared_by_refresh(self):
        # patch_for 会带出空 target；主动换令牌不该顺手清掉用户填的设备空间 id
        self._set_left(30)
        self.cfg.data["download"]["xunlei"]["target"] = "device#7"
        with mock.patch("tmb.xunlei_detect.scan", return_value=scan_rep(token_in(48))):
            self.pipe.refresh_xunlei_token()
        self.assertEqual(self.cfg.data["download"]["xunlei"]["target"], "device#7")

    def test_older_token_is_ignored(self):
        # 本机扫到的是旧的（比如浏览器里留着上次的 Cookie）：不能把新的换回旧的
        self._set_left(2)
        with mock.patch("tmb.xunlei_detect.scan", return_value=scan_rep(token_in(1))):
            changed, note = self.pipe.refresh_xunlei_token()
        self.assertFalse(changed)
        self.assertIn("不比现在的新", note)

    def test_expired_token_also_tries(self):
        self._set_left(-3)
        with mock.patch("tmb.xunlei_detect.scan", return_value=scan_rep(token_in(20))):
            changed, _ = self.pipe.refresh_xunlei_token()
        self.assertTrue(changed)

    def test_rate_limited(self):
        self._set_left(30)
        with mock.patch("tmb.xunlei_detect.scan",
                        return_value=scan_rep(token_in(24))):
            self.pipe.refresh_xunlei_token()
            self.pipe.refresh_xunlei_token()
            self.pipe.refresh_xunlei_token()
        # 每 token_retry_hours 才试一次，不能每次投递都扫一遍文件
        self.assertEqual(self.pipe._token_next_try > time.time(), True)

    def test_force_ignores_window_but_still_needs_token(self):
        # 手动点「重抓令牌」不受剩余天数限制，但仍然不认更旧的令牌
        with mock.patch("tmb.xunlei_detect.scan",
                        return_value=scan_rep(token_in(24 * 30))) as scan:
            changed, _ = self.pipe.refresh_xunlei_token(force=True)
        self.assertTrue(changed)
        scan.assert_called_once()

    def test_scan_failure_keeps_config(self):
        self._set_left(1)
        before = self.cfg.data["download"]["xunlei"]["pan_auth"]
        rep = {"best": {"state": "down", "mode": "fnos_gateway", "host": "127.0.0.1",
                        "port": 5666}, "_secrets": {}}
        with mock.patch("tmb.xunlei_detect.scan", return_value=rep):
            changed, note = self.pipe.refresh_xunlei_token()
        self.assertFalse(changed)
        self.assertIn("没扫到", note)
        self.assertEqual(self.cfg.data["download"]["xunlei"]["pan_auth"], before)


class TestTokenWatch(_Base):
    def _events(self, kind):
        return [e for e in self.store.recent_events(50) if e["kind"] == kind]

    def test_warns_once_when_cannot_refresh(self):
        self._set_left(20)
        rep = {"best": {"state": "down", "mode": "fnos_gateway", "host": "127.0.0.1",
                        "port": 5666}, "_secrets": {}}
        with mock.patch("tmb.xunlei_detect.scan", return_value=rep):
            self.pipe.token_watch()
            self.pipe._token_watch_at = 0            # 假装又过了半小时
            self.pipe.token_watch()
        self.assertEqual(len(self._events("xunlei_token_soon")), 1)

    def test_records_event_when_refreshed(self):
        self._set_left(20)
        with mock.patch("tmb.xunlei_detect.scan", return_value=scan_rep(token_in(48))):
            self.pipe.token_watch()
        self.assertEqual(len(self._events("xunlei_token_refreshed")), 1)
        self.assertEqual(self._events("xunlei_token_soon"), [])

    def test_quiet_when_token_is_fine(self):
        with mock.patch("tmb.xunlei_detect.scan") as scan:
            self.pipe.token_watch()
        scan.assert_not_called()
        self.assertEqual(self.store.recent_events(20), [])


class TestPreflight(_Base):
    def _fake(self, ok=True):
        m = mock.Mock()
        m.name = "xunlei"
        m.add = mock.Mock(return_value=DownloadResult(
            True, "xunlei", "9", "已投递迅雷（1 个文件）") if ok else
            DownloadResult(False, "xunlei", "", "失败"))
        return m

    def _item(self):
        return {"title": "某作品", "magnet": MAGNET, "infohash": "c" * 40}

    def test_uses_xunlei_detection(self):
        self.assertTrue(self.pipe._xunlei_in_use())
        self.assertTrue(self.pipe._xunlei_in_use("xunlei"))
        self.cfg.data["download"]["backend"] = "aria2"
        self.cfg.data["download"]["fallback"] = []
        self.assertFalse(self.pipe._xunlei_in_use())
        self.assertFalse(self.pipe._xunlei_in_use("qbittorrent"))

    def test_soon_token_refreshed_before_submit(self):
        # 投递前先换新：第一条消息就不会因为令牌失效而失败
        self._set_left(2)
        newer = token_in(30)
        with mock.patch("tmb.downloaders.chain", return_value=[self._fake()]), \
                mock.patch("tmb.xunlei_detect.scan", return_value=scan_rep(newer)):
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"])
        self.assertIn("已换用本机找到的令牌", out["message"])
        self.assertEqual(self.cfg.data["download"]["xunlei"]["pan_auth"], newer)

    def test_far_token_no_scan_before_submit(self):
        with mock.patch("tmb.downloaders.chain", return_value=[self._fake()]), \
                mock.patch("tmb.xunlei_detect.scan") as scan:
            out = self.pipe.submit(self._item())
        self.assertTrue(out["ok"])
        self.assertNotIn("已换用", out["message"])
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
