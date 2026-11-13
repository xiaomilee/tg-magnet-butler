"""HTTP 409 轮询冲突。

用户日志里那条：``轮询异常: getUpdates 请求失败: HTTP 409 .../getUpdates``。
裸 409 什么也说明不了，实际含义是「这串 Bot Token 还有别处也在 getUpdates」——
要么设过 webhook，要么另一个进程/设备在跑同一个机器人。这组测试锁住：

1. 报错要带上 Telegram 的原话，不再只是一个 409；
2. 面板重启（旧的长轮询还没退）造成的撞车要当自己人，短退避静默自愈；
3. 连着撞两次才认定是外部冲突，先查 webhook（是它就直接清掉），否则长退避 + 一次告警。
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb import util  # noqa: E402
from tmb.bot import CONFLICT_BACKOFF, CONFLICT_RETRY, BotService  # noqa: E402
from tmb.config import Config  # noqa: E402
from tmb.pipeline import Pipeline  # noqa: E402
from tmb.store import Store  # noqa: E402
from tmb.tg import Telegram, TelegramError, _http_message  # noqa: E402

TOKEN = "123456:AAbbCCddEEffGG"
CONFLICT_BODY = json.dumps({
    "ok": False, "error_code": 409,
    "description": "Conflict: terminated by other getUpdates request; "
                   "make sure that only one bot instance is running",
}).encode("utf-8")


class _LogWatch(object):
    """抓一段日志：用临时 handler，避免 assertLogs 把 logger 抬到 DEBUG。"""

    def __init__(self):
        self.records = []

    def __enter__(self):
        self.h = logging.Handler()
        self.h.emit = self.records.append
        self.log = logging.getLogger("tmb.bot")
        self.old_level = self.log.level
        self.log.addHandler(self.h)
        self.log.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.log.removeHandler(self.h)
        self.log.setLevel(self.old_level)
        return False

    def text(self, level=None):
        return "\n".join(r.getMessage() for r in self.records
                         if level is None or r.levelno == level)


class TestErrorMessage(unittest.TestCase):
    def test_conflict_carries_server_text(self):
        err = util.HttpError("HTTP 409 https://api.telegram.org/bot***/getUpdates", 409,
                             CONFLICT_BODY)
        msg = _http_message("getUpdates", err)
        self.assertIn("HTTP 409", msg)
        self.assertIn("服务端原话", msg)
        self.assertIn("terminated by other getUpdates", msg)

    def test_conflict_flag(self):
        self.assertTrue(TelegramError("x", 409).conflict)
        self.assertFalse(TelegramError("x", 400).conflict)
        self.assertFalse(TelegramError("x").conflict)

    def test_call_passes_code_through(self):
        tg = Telegram(TOKEN, proxy="")
        with mock.patch("tmb.util.http_request",
                        side_effect=util.HttpError("HTTP 409 url", 409, CONFLICT_BODY)):
            with self.assertRaises(TelegramError) as ctx:
                tg.get_updates(offset=0, timeout=0)
        self.assertEqual(ctx.exception.code, 409)
        self.assertTrue(ctx.exception.conflict)
        self.assertIn("服务端原话", str(ctx.exception))


class TestWebhookApi(unittest.TestCase):
    def setUp(self):
        self.tg = Telegram(TOKEN, proxy="")

    def test_get_webhook_info(self):
        with mock.patch("tmb.util.http_request",
                        return_value=(200, json.dumps({"ok": True, "result": {
                            "url": "", "pending_update_count": 0}}), b"")) as req:
            info = self.tg.get_webhook_info()
        self.assertEqual(info.get("url"), "")
        self.assertIn("/getWebhookInfo", req.call_args[0][0])

    def test_delete_webhook_keeps_pending(self):
        with mock.patch("tmb.util.http_request",
                        return_value=(200, json.dumps({"ok": True, "result": True}), b"")) as req:
            self.tg.delete_webhook()
        self.assertIn("/deleteWebhook", req.call_args[0][0])
        body = req.call_args[1]["data"]
        self.assertIs(body["drop_pending_updates"], False)


class _BotBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.data["telegram"]["bot_token"] = TOKEN
        self.store = Store(os.path.join(self.tmp, "var", "t.db"))
        self.pipe = Pipeline(self.cfg, self.store)
        self.bot = BotService(self.cfg, self.store, self.pipe)
        self.tg = mock.Mock()
        self.bot.tg = self.tg
        self.tg.get_webhook_info.return_value = {"url": ""}

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def err(self):
        return TelegramError("getUpdates 请求失败: HTTP 409（服务端原话：Conflict）", 409)


class TestConflictHandling(_BotBase):
    def test_first_conflict_is_self_inflicted_retry(self):
        """面板点「重连」：旧的长轮询还挂着，新请求把它顶掉——这是自己人打架，别报警。"""
        with _LogWatch() as w:
            delay = self.bot._on_conflict(self.err())
        self.assertEqual(delay, CONFLICT_RETRY)
        self.assertEqual(self.bot.poll_state, "conflict")
        self.assertEqual(w.text(logging.WARNING), "")     # 第一次不告警
        self.tg.get_webhook_info.assert_not_called()

    def test_second_conflict_clears_webhook(self):
        """连着撞两次：先看是不是被 webhook 占着——是它就直接清掉，改回长轮询。"""
        self.tg.get_webhook_info.return_value = {"url": "https://example.com/hook"}
        self.bot._on_conflict(self.err())
        with _LogWatch() as w:
            delay = self.bot._on_conflict(self.err())
        self.assertEqual(delay, CONFLICT_RETRY)
        self.tg.delete_webhook.assert_called_once_with()
        self.assertEqual(self.bot.poll_state, "webhook_cleared")
        self.assertIn("webhook", w.text(logging.INFO))

    def test_second_conflict_without_webhook_backs_off_long(self):
        self.bot._on_conflict(self.err())
        with _LogWatch() as w:
            delay = self.bot._on_conflict(self.err())
        self.assertEqual(delay, CONFLICT_BACKOFF)
        self.assertIn("还有别处在收消息", self.bot.last_error)
        warn = w.text(logging.WARNING)
        self.assertIn("409", warn)
        # 告警要给出可执行的排查方向，不能只说「冲突」
        self.assertIn("旧进程", warn)
        self.assertIn("webhook", warn)

    def test_conflict_warned_once(self):
        self.bot._on_conflict(self.err())
        with _LogWatch() as w:
            for _ in range(3):
                self.bot._on_conflict(self.err())
        self.assertEqual(w.text(logging.WARNING).count("轮询冲突"), 1)

    def test_webhook_not_cleared_when_disabled(self):
        self.cfg.data["telegram"]["auto_clear_webhook"] = False
        self.tg.get_webhook_info.return_value = {"url": "https://example.com/hook"}
        self.bot._on_conflict(self.err())
        delay = self.bot._on_conflict(self.err())
        self.tg.delete_webhook.assert_not_called()
        self.assertEqual(delay, CONFLICT_BACKOFF)

    def test_recovery_resets_state(self):
        self.bot._on_conflict(self.err())
        self.bot._on_conflict(self.err())
        self.assertEqual(self.bot.poll_state, "conflict")
        with _LogWatch() as w:
            self.bot._poll_ok()
        self.assertEqual(self.bot.poll_state, "ok")
        self.assertEqual(self.bot.last_error, "")
        self.assertEqual(self.bot._conflict_streak, 0)
        self.assertIn("恢复正常", w.text(logging.INFO))

    def test_poll_hint_is_short_and_actionable(self):
        self.bot.poll_state = "conflict"
        self.assertIn("Token", self.bot.poll_hint())
        self.assertLessEqual(len(self.bot.poll_hint()), 40)   # 侧栏那一行放得下
        self.bot.poll_state = "ok"
        self.assertEqual(self.bot.poll_hint(), "")


class TestStaleThreads(_BotBase):
    def test_stop_invalidates_old_generation(self):
        """重连时旧线程可能还挂在 getUpdates 里：代际一变，它回来后不能再抢一轮。"""
        gen = self.bot._gen + 1
        self.bot._gen = gen
        self.assertFalse(self.bot._stale(gen))
        self.bot.stop()
        self.assertTrue(self.bot._stale(gen))

    def test_stale_thread_stays_quiet_on_conflict(self):
        bot = BotService(self.cfg, self.store, self.pipe)
        bot.tg = self.tg
        self.tg.get_updates.side_effect = self.err()
        bot._gen = 7
        with _LogWatch() as w:
            bot._loop(gen=6)          # 老线程：一进来就该退
        self.assertEqual(w.text(logging.WARNING), "")
        self.tg.get_updates.assert_not_called()


class TestPortGuard(unittest.TestCase):
    """抢不到面板端口就别起第二份——这份不占端口的实例照跑，就会和人家抢同一个 bot token。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "tmb_entry", os.path.join(root, "app", "main.py"))
        cls.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.entry)

    def test_linux_eaddrinuse(self):
        import errno
        e = OSError(errno.EADDRINUSE, "Address already in use")
        self.assertTrue(self.entry.App._port_taken(e))

    def test_windows_wsaecacces(self):
        """Windows 上占端口报的是 10013（消息写着「访问权限不允许」），也得算占用。"""
        e = OSError(13, "[WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。")
        self.assertTrue(self.entry.App._port_taken(e))

    def test_privileged_port_is_not_conflict(self):
        """Linux 上绑 1024 以下端口没权限是配置错了，不是「有人已经在跑」。"""
        import errno
        e = OSError(errno.EACCES, "Permission denied")
        self.assertFalse(self.entry.App._port_taken(e))


if __name__ == "__main__":
    unittest.main()
