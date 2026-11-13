# -*- coding: utf-8 -*-
"""迅雷回了一段乱码：先解压/换编码，实在解不出就别把乱码当「服务端原话」发给用户。

用户实测报错里嵌着 `�       ���1K A ��˫�Ӡ�V� ZY�...` 这种东西——说明拿到的是
压缩流（或别的编码）被当文本读了。这类响应里往往就藏着真正的失败原因。
"""
import email.message
import gzip
import io
import os
import sys
import unittest
import urllib.error
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from tmb import util                                              # noqa: E402
from tmb.downloaders.xunlei import (                               # noqa: E402
    XunleiDownloader, _snippet, _clean_text)

MAGNET = "magnet:?xt=urn:btih:" + "f" * 40


def headers(**kw):
    m = email.message.Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


class FakeResp:
    def __init__(self, body, hdrs, status=200):
        self.body, self.headers, self.status = body, hdrs, status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, resp):
        self.resp = resp

    def open(self, req, timeout=None):
        return self.resp


def patch_http(resp):
    return mock.patch("tmb.util._build_opener", lambda *a, **k: FakeOpener(resp))


class TestDecompress(unittest.TestCase):
    def test_gzip_without_header(self):
        # 网关漏了 Content-Encoding 也要认出来，否则就是一屏乱码
        body = gzip.compress(b'{"error":"invalid token"}')
        self.assertEqual(util._decompress(body), b'{"error":"invalid token"}')

    def test_zlib_without_header(self):
        body = zlib.compress(b'{"ok":1}')
        self.assertEqual(util._decompress(body), b'{"ok":1}')

    def test_plain_bytes_untouched(self):
        self.assertEqual(util._decompress(b'{"a":1}'), b'{"a":1}')

    def test_broken_gzip_returns_raw(self):
        body = b"\x1f\x8b" + b"garbage"
        self.assertEqual(util._decompress(body), body)

    def test_http_request_decodes_gzipped_body(self):
        resp = FakeResp(gzip.compress(b'{"tasks": []}'), headers())
        with patch_http(resp):
            status, text, raw = util.http_request("http://192.168.1.2:5666/x")
        self.assertEqual(status, 200)
        self.assertIn('"tasks"', text)

    def test_http_error_body_is_decoded_too(self):
        body = gzip.compress('{"error":"下载目录不存在"}'.encode("utf-8"))
        err = urllib.error.HTTPError("http://h/x?pan_auth=SECRET123456", 400,
                                     "Bad Request", headers(), io.BytesIO(body))
        with mock.patch("tmb.util._build_opener", lambda *a, **k: _Raising(err)):
            with self.assertRaises(util.HttpError) as cm:
                util.http_request("http://h2010/x?pan_auth=SECRET123456", method="POST",
                                  data={"a": 1})
        self.assertIn("下载目录", cm.exception.body.decode("utf-8"))
        self.assertNotIn("SECRET123456", str(cm.exception))   # 报错里令牌要打码


class _Raising:
    def __init__(self, err):
        self.err = err

    def open(self, req, timeout=None):
        raise self.err


class TestGarbled(unittest.TestCase):
    def test_binary_detected(self):
        self.assertTrue(util.looks_garbled("\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd"))

    def test_normal_json_not_garbled(self):
        self.assertFalse(util.looks_garbled('{"error":"invalid token"}'))

    def test_clean_text_strips_control_chars(self):
        dirty = 'abc\x00\x01\x02 乱码\ufffd\ufffd\ufffd def'
        out = _clean_text(dirty)
        self.assertNotIn("\x00", out)
        self.assertNotIn("\ufffd\ufffd", out)
        self.assertIn("abc", out)

    def test_snippet_never_returns_runs_of_replacement_chars(self):
        got = _snippet({"_raw_text": "\ufffd" * 80})
        self.assertLessEqual(len(got), 3)


class TestBinaryResponse(unittest.TestCase):
    def _dl(self):
        cfg = {"mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5666,
               "pan_auth": "PAN", "fnos_token": "FNOS12345678", "target": "device#1"}
        return XunleiDownloader(cfg)

    def test_gzipped_response_is_parsed_normally(self):
        dl = self._dl()
        resp = FakeResp(gzip.compress(b'{"tasks": [{"target": "device#1"}]}'), headers())
        with patch_http(resp):
            data = dl._request("/drive/v1/tasks", method="GET")
        self.assertEqual(data["tasks"][0]["target"], "device#1")
        self.assertFalse(data.get("_binary"))

    def test_true_binary_is_flagged_and_not_guessed(self):
        dl = self._dl()
        blob = bytes(range(0, 32)) * 4          # 纯控制字节，任何编码都解不成文本
        resp = FakeResp(blob, headers())
        with patch_http(resp), self.assertLogs("tmb.xunlei", "WARNING"):
            data = dl._request("/drive/v1/task", {"url": MAGNET})
        self.assertTrue(data["_binary"])
        self.assertTrue(data["_raw_hex"].startswith("00"))
        self.assertNotIn("_auth_failed", data)   # 乱码里不许瞎认鉴权失败

    def test_auth_message_says_binary_instead_of_garbage(self):
        dl = self._dl()
        msg = dl._auth_message("token", {"_binary": True, "_raw_text": "\ufffd" * 40})
        self.assertIn("二进制", msg)
        self.assertNotIn("\ufffd\ufffd", msg)

    def test_gbk_response_falls_back(self):
        dl = self._dl()
        body = '{"error":"任务已存在"}'.encode("gb18030")
        resp = FakeResp(body, headers())
        with patch_http(resp):
            data = dl._request("/drive/v1/task", {"url": MAGNET})
        self.assertEqual(data.get("error"), "任务已存在")


class TestHttp400(unittest.TestCase):
    def test_message_names_fields_instead_of_generic(self):
        dl = XunleiDownloader({"mode": "panel", "pan_auth": "PAN"})
        err = util.HttpError("HTTP 400 http://x/y", 400, b"")
        msg = dl._http_fail_message(
            err, {"type": "user#download-url", "name": "n", "file_name": "f",
                  "space": "device#1", "params": {"url": "m", "target": "t"}})
        self.assertIn("HTTP 400", msg)
        self.assertIn("file_name", msg)
        self.assertNotIn("接口请求失败", msg)     # 不再是没信息量的那句话

    def test_other_status_keeps_generic(self):
        dl = XunleiDownloader({"mode": "panel", "pan_auth": "PAN"})
        err = util.HttpError("HTTP 500", 500, b"")
        self.assertIn("接口请求失败", dl._http_fail_message(err, {}))


if __name__ == "__main__":
    unittest.main()
