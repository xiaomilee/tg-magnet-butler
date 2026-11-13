"""迅雷自动侦测的回归测试：假网关、假 2345 面板、/proc 解析、令牌打捞。"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import xunlei_detect as xd                       # noqa: E402

GOOD_TOKEN = "fnos-token-value-1234567890"
GOOD_PAN = "pan-auth-value-abcdef123456"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeXunlei(object):
    """假网关：只认 ``/cgi/ThirdParty/xunlei/index.cgi`` 那条路径（1.0.31 起只有这一种接法）。"""

    def __init__(self, kind="gateway", mode="ok"):
        self.kind = kind
        self.mode = mode
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass

            def _send(self, code, body="", ctype="application/json"):
                data = (body or "").encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                cookie = self.headers.get("Cookie") or ""
                if not parsed.path.endswith("/cgi/ThirdParty/xunlei/index.cgi/drive/v1/tasks"):
                    return self._send(404, "nope")
                if outer.mode == "html":
                    return self._send(200, "<html><body>xunlei panel</body></html>",
                                      "text/html")
                del query
                authed = GOOD_TOKEN in cookie
                if outer.mode == "no_auth":
                    authed = True
                if not authed:
                    return self._send(401, "")
                return self._send(200, json.dumps({
                    "tasks": [], "params": {"target": "device#demo"}}))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.port

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


class TestProcParsing(unittest.TestCase):
    SAMPLE = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0929 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345
   1: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12346
   2: 0100007F:162E 0100007F:0929 01 00000000:00000000 00:00000000 00000000     0        0 12347
"""

    def test_listen_only(self):
        ports = xd.parse_proc_net(self.SAMPLE)
        self.assertIn(2345, ports)      # 0x0929
        self.assertIn(8080, ports)      # 0x1F90
        self.assertNotIn(5678, ports)   # 0x162E 是 ESTABLISHED，不算监听

    def test_empty_input(self):
        self.assertEqual(xd.parse_proc_net(""), [])


class TestHarvest(unittest.TestCase):
    def test_finds_pan_auth_in_app_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "data", "config.json")
            os.makedirs(os.path.dirname(cfg))
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({"pan_auth": GOOD_PAN, "other": 1}, f)
            hits = xd.harvest([{"path": tmp}])
            values = [h["value"] for h in hits["pan_auth"]]
            self.assertIn(GOOD_PAN, values)
            self.assertTrue(hits["pan_auth"][0]["source"].endswith("config.json"))

    def test_ignores_other_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.txt"), "w", encoding="utf-8") as f:
                f.write("nothing here")
            hits = xd.harvest([{"path": tmp}])
            self.assertEqual(hits["pan_auth"], [])

    def test_mask(self):
        self.assertEqual(xd.mask(GOOD_PAN), "pan-…3456")
        self.assertEqual(xd.mask("short"), "***")


class TestGatewayDetection(unittest.TestCase):
    def test_ok_with_reused_fnos_token(self):
        with FakeXunlei("gateway", "ok") as port:
            rep = xd.scan(fnos_token=GOOD_TOKEN, ports=[], fnos_port=port, dirs=[])
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["best"]["mode"], "fnos_gateway")
            self.assertEqual(rep["best"]["port"], port)
            patch = xd.patch_for(rep)
            self.assertEqual(patch["mode"], "fnos_gateway")
            self.assertEqual(patch["fnos_port"], port)
            self.assertEqual(patch["fnos_token"], GOOD_TOKEN)
            self.assertEqual(patch["target"], "")

    def test_missing_token_is_named(self):
        with FakeXunlei("gateway", "ok") as port:
            rep = xd.scan(fnos_token="", ports=[], fnos_port=port, dirs=[])
            self.assertFalse(rep["ok"])
            self.assertEqual(rep["best"]["state"], "need_token")
            self.assertIn("fnos-token", rep["notes"][0])

    def test_token_not_leaked_in_url(self):
        with FakeXunlei("gateway", "ok") as port:
            rep = xd.scan(fnos_token=GOOD_TOKEN, ports=[], fnos_port=port, dirs=[])
            self.assertNotIn(GOOD_TOKEN, json.dumps({"candidates": rep["candidates"],
                                                     "tokens": rep["tokens"]},
                                                    ensure_ascii=False))


class TestGatewayFalsePositive(unittest.TestCase):
    def test_html_on_gateway_port_is_not_xunlei(self):
        with FakeXunlei("gateway", "html") as port:
            rep = xd.scan(fnos_token="", ports=[], fnos_port=port, dirs=[])
            self.assertIsNone(rep["best"])
            self.assertFalse(rep["ok"])


class TestPortDiscovery(unittest.TestCase):
    """端口被改过也能找到：候选端口是「配置 + 常见值 + 本机在监听的 56xx」。"""

    def test_finds_gateway_on_non_default_port(self):
        with FakeXunlei("gateway", "ok") as port:
            rep = xd.scan(fnos_token=GOOD_TOKEN, ports=[port], dirs=[])
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["best"]["mode"], "fnos_gateway")
            self.assertEqual(rep["best"]["port"], port)

    def test_nothing_listening(self):
        rep = xd.scan(fnos_token="", ports=[_free_port()], dirs=[], fnos_port=_free_port())
        self.assertFalse(rep["ok"])
        self.assertIsNone(rep["best"])
        self.assertIn("没有响应", rep["notes"][0])

    def test_no_panel_mode_anymore(self):
        """2345 面板那条接法已经删掉：报告里不该再出现 panel 候选。"""
        with FakeXunlei("gateway", "ok") as port:
            rep = xd.scan(fnos_token=GOOD_TOKEN, ports=[port], dirs=[])
        self.assertEqual({c["mode"] for c in rep["candidates"]}, {"fnos_gateway"})


class TestAutoconnect(unittest.TestCase):
    """启动时自动连接：只有真打通才写配置；打不通不许动现有设置。"""

    def _cfg(self, tmp):
        from tmb.config import Config
        etc, var = os.path.join(tmp, "etc"), os.path.join(tmp, "var")
        os.makedirs(etc)
        os.makedirs(var)
        return Config(etc, var).load()

    def test_writes_port_and_tokens_when_ok(self):
        with FakeXunlei("gateway", "ok") as port, tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            res = xd.autoconnect(cfg, fnos_token=GOOD_TOKEN, fnos_port=port)
            self.assertTrue(res["ok"])
            self.assertEqual(cfg.get("download.xunlei.mode"), "fnos_gateway")
            self.assertEqual(cfg.get("download.xunlei.fnos_port"), port)
            self.assertEqual(cfg.get("download.xunlei.fnos_token"), GOOD_TOKEN)
            # 明文只在内存里写配置，回给界面的一律掩码
            self.assertNotIn(GOOD_TOKEN, json.dumps(res["applied"]))

    def test_does_not_touch_config_when_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            before = json.dumps(cfg.data["download"]["xunlei"], sort_keys=True)
            res = xd.autoconnect(cfg, fnos_token="", fnos_port=_free_port(), force=True)
            self.assertFalse(res["ok"])
            self.assertEqual(json.dumps(cfg.data["download"]["xunlei"], sort_keys=True),
                             before)


class TestReportText(unittest.TestCase):
    def test_format_report_contains_sections(self):
        with FakeXunlei("gateway", "ok") as port:
            rep = xd.scan(fnos_token=GOOD_TOKEN, ports=[], fnos_port=port, dirs=[])
            text = xd.format_report(rep)
            self.assertIn("迅雷侦测", text)
            self.assertIn("候选", text)
            self.assertIn("fnos_gateway", text)


if __name__ == "__main__":
    unittest.main()


class TestWebUIHook(unittest.TestCase):
    """面板「自动侦测」按钮背后的接口：复用登录态 → 写配置 → 复测。"""

    def test_detect_endpoint_applies_when_ok(self):
        import time
        from unittest import mock

        from tmb.config import Config
        from tmb.pipeline import Pipeline
        from tmb.store import Store
        from tmb.webui import AppContext, WebUI

        with FakeXunlei("gateway", "ok") as xport, \
                tempfile.TemporaryDirectory() as tmp:
            etc, var = os.path.join(tmp, "etc"), os.path.join(tmp, "var")
            os.makedirs(etc)
            os.makedirs(var)
            cfg = Config(etc, var).load()
            cfg.data["app"]["port"] = _free_port()
            cfg.data["app"]["bind"] = "127.0.0.1"
            store = Store(cfg.db_path)
            ctx = AppContext(cfg, store, Pipeline(cfg, store), None,
                             os.path.join(ROOT, "app", "ui"))
            ui = WebUI(ctx)
            ui.start()
            time.sleep(0.3)
            try:
                rep = xd.scan(fnos_token=GOOD_TOKEN, ports=[], fnos_port=xport, dirs=[])
                with mock.patch.object(xd, "scan", return_value=rep):
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/api/detect/xunlei" % cfg.data["app"]["port"],
                        data=json.dumps({"apply": True}).encode(),
                        method="POST",
                        headers={"Content-Type": "application/json",
                                 "Cookie": "fnos-token=" + GOOD_TOKEN})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        out = json.loads(r.read().decode())
            finally:
                ui.stop()
                store.close()

            self.assertTrue(out["ok"], out.get("notes"))
            self.assertEqual(out["applied"]["mode"], "fnos_gateway")
            self.assertEqual(out["applied"]["fnos_port"], xport)
            self.assertNotIn(GOOD_TOKEN, json.dumps(out, ensure_ascii=False))
            self.assertEqual(ctx.fnos_token, GOOD_TOKEN)
            # 直接访问面板端口时，Host 里的 8470 不是飞牛网关端口，不能被当成它
            self.assertEqual(ctx.fnos_port, 0)

            with open(cfg.config_path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["download"]["xunlei"]["mode"], "fnos_gateway")
            self.assertEqual(saved["download"]["xunlei"]["fnos_port"], xport)
            self.assertEqual(saved["download"]["xunlei"]["fnos_token"], GOOD_TOKEN)
