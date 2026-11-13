"""qBittorrent 适配器回归测试：用假 qB 服务复现真实的各种失败姿势。

真机上报「登录失败」时看不出原因，就是因为这些分支以前都收敛成一句话。
这里把 qB 的常见响应逐个造出来，钉住「原因 + 建议」的输出。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb.downloaders.qbittorrent import QbittorrentDownloader   # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=t"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeQB(object):
    """最小 qB WebUI：mode 决定它怎么使坏。"""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.state = {"add_hits": 0}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass

            def _send(self, code, body="", cookie=None):
                data = (body or "").encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return "SID=" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                mode = outer.mode
                if mode == "hostcheck":
                    return self._send(401)
                if mode == "wrongpath":
                    return self._send(404)
                if self.path.startswith("/api/v2/app/version"):
                    if self._authed() or mode == "bypass":
                        return self._send(200, "v4.6.2")
                    return self._send(403)
                return self._send(404)

            def do_POST(self):
                mode = outer.mode
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                if mode == "hostcheck":
                    return self._send(401)
                if mode == "wrongpath":
                    return self._send(404)
                if self.path.startswith("/api/v2/auth/login"):
                    if mode == "ban":
                        return self._send(403)
                    if mode == "fails" or form.get("password") != "secret":
                        return self._send(200, "Fails.")
                    if mode == "cookiebroken":
                        return self._send(200, "Ok.")
                    return self._send(200, "Ok.", cookie="SID=abc123; path=/")
                if self.path.startswith("/api/v2/torrents/add"):
                    if not self._authed():
                        return self._send(403)
                    if mode == "expire" and outer.state["add_hits"] == 0:
                        outer.state["add_hits"] += 1
                        return self._send(403)
                    return self._send(200, "Ok.")
                return self._send(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return "http://127.0.0.1:%d" % self.port

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


def _probe(mode, password="secret", **cfg):
    with FakeQB(mode) as url:
        d = QbittorrentDownloader({"url": url, "username": "admin",
                                   "password": password, **cfg})
        return d.probe()


class TestQBLoginDiagnosis(unittest.TestCase):
    def test_login_ok(self):
        d = _probe("ok")
        self.assertTrue(d["ok"])
        self.assertEqual(d["version"], "v4.6.2")
        self.assertIn("登录", d["auth"])

    def test_bypass_auth_needs_no_account(self):
        d = _probe("bypass")
        self.assertTrue(d["ok"])
        self.assertIn("免密", d["auth"])

    def test_wrong_password_is_named(self):
        d = _probe("ok", password="nope")
        self.assertFalse(d["ok"])
        self.assertIn("Fails", d["error"])
        self.assertIn("密码", d["error"])
        self.assertIn("adminadmin", d["hint"])

    def test_blank_password_flagged(self):
        d = _probe("ok", password="")
        self.assertFalse(d["ok"])
        self.assertIn("为空", d["error"])

    def test_banned_ip(self):
        d = _probe("ban")
        self.assertFalse(d["ok"])
        self.assertIn("403", d["error"])
        self.assertIn("封", d["error"] + d["hint"])

    def test_host_header_validation(self):
        d = _probe("hostcheck")
        self.assertFalse(d["ok"])
        self.assertIn("Host", d["error"])
        self.assertIn("Host header", d["hint"])

    def test_wrong_path(self):
        d = _probe("wrongpath")
        self.assertFalse(d["ok"])
        self.assertIn("404", d["error"])

    def test_login_ok_but_cookie_missing(self):
        d = _probe("cookiebroken")
        self.assertFalse(d["ok"])
        self.assertIn("会话没生效", d["error"])
        self.assertIn("Cookie", d["hint"])

    def test_connection_refused_is_not_reported_as_login_failure(self):
        d = QbittorrentDownloader({"url": "http://127.0.0.1:%d" % _free_port(),
                                   "username": "admin", "password": "x"}).probe()
        self.assertFalse(d["ok"])
        self.assertIn("连不上", d["error"])
        self.assertNotIn("登录失败", d["error"])
        self.assertIn("端口", d["hint"])


class TestQBAdd(unittest.TestCase):
    def test_add_ok(self):
        with FakeQB("ok") as url:
            d = QbittorrentDownloader({"url": url, "username": "admin",
                                       "password": "secret",
                                       "save_path": "/vol1/dl", "category": "tmb"})
            self.assertTrue(d.probe()["ok"])
            res = d.add(MAGNET, "标题")
            self.assertTrue(res.ok, res.message)

    def test_add_relogin_after_session_expired(self):
        with FakeQB("expire") as url:
            d = QbittorrentDownloader({"url": url, "username": "admin",
                                       "password": "secret"})
            self.assertTrue(d.login())
            res = d.add(MAGNET)
            self.assertTrue(res.ok, res.message)


class TestQBProxyWiring(unittest.TestCase):
    def test_lan_address_ignores_proxy(self):
        d = QbittorrentDownloader({"url": "http://192.168.1.5:8080"},
                                  proxy="http://127.0.0.1:7890")
        self.assertEqual(d.proxy, "")

    def test_public_address_keeps_proxy(self):
        d = QbittorrentDownloader({"url": "http://qb.example.com:8080"},
                                  proxy="http://127.0.0.1:7890")
        self.assertEqual(d.proxy, "http://127.0.0.1:7890")

    def test_explicit_proxy_wins_for_public(self):
        d = QbittorrentDownloader({"url": "http://qb.example.com:8080",
                                   "proxy": "http://10.0.0.2:3128"},
                                  proxy="http://127.0.0.1:7890")
        self.assertEqual(d.proxy, "http://10.0.0.2:3128")

    def test_url_without_scheme_gets_http(self):
        d = QbittorrentDownloader({"url": "127.0.0.1:8080/"})
        self.assertEqual(d.url, "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
