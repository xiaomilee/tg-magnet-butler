"""`util.http_download`：断点续传。

为什么值得单独测：这条路径的唯一职责是**把 .torrent 完整拿下来**，而它面对的是一个
会在半路掐断响应的代理（实测固定在 16087 字节处断，`IncompleteRead: 5674 more expected`）。
「断了就重下」在这里是死循环——整下重试十次还是断在同一处；只有带 `Range` 从断点续
才拿得到完整文件。把这条逻辑锁进测试，免得以后被人「简化」回 retry。
"""

import http.client
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

from tmb import util  # noqa: E402

URL = "https://example.invalid/download/1.torrent"
BODY = bytes(range(256)) * 4          # 1024 字节，好算断点


class _Resp:
    """假的 urllib 响应：`cut` 有值时 read() 抛 IncompleteRead（带已读部分）。"""

    def __init__(self, status, headers, body, cut=0):
        self.status = status
        self.headers = headers
        self._body = body
        self._cut = cut

    def read(self):
        if self._cut:
            raise http.client.IncompleteRead(self._body[:self._cut],
                                             len(self._body) - self._cut)
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, resps):
        self.resps = list(resps)
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        return self.resps.pop(0)

    def ranges(self):
        return [r.get_header("Range") for r in self.reqs]


class TestDownload(unittest.TestCase):
    def run_with(self, resps, **kw):
        op = _Opener(resps)
        with mock.patch.object(util, "_build_opener", return_value=op):
            got = util.http_download(URL, proxy="http://127.0.0.1:7890", **kw)
        return got, op

    def test_incomplete_read_resumes_with_range(self):
        """断在 400 字节 → 下一次必须带 `Range: bytes=400-`，拼出来才是完整文件。"""
        got, op = self.run_with([
            _Resp(200, {"Content-Length": str(len(BODY))}, BODY, cut=400),
            _Resp(206, {"Content-Range": "bytes 400-1023/1024"}, BODY[400:]),
        ])
        self.assertEqual(got, BODY)
        self.assertEqual(len(op.reqs), 2)
        self.assertIsNone(op.ranges()[0], "第一次不该带 Range：还不知道断点在哪")
        self.assertEqual(op.ranges()[1], "bytes=400-")

    def test_server_ignores_range_restarts_cleanly(self):
        """服务端不认 Range（照样回 200 全量）：以这次的全量重新算，别把两段拼出垃圾。"""
        got, _ = self.run_with([
            _Resp(200, {"Content-Length": str(len(BODY))}, BODY, cut=400),
            _Resp(200, {"Content-Length": str(len(BODY))}, BODY),
        ])
        self.assertEqual(got, BODY)

    def test_gives_up_with_bytes_report(self):
        """一直断：抛错，并且说清已经拿到多少——排查时要靠这个数判断是不是同一个断点。"""
        resp = lambda: _Resp(200, {"Content-Length": str(len(BODY))}, BODY, cut=400)
        with mock.patch.object(util, "_build_opener",
                               return_value=_Opener([resp(), resp(), resp(), resp()])):
            with self.assertRaises(util.HttpError) as ctx:
                util.http_download(URL, proxy="http://127.0.0.1:7890",
                                   attempts=4, budget=30)
        self.assertIn("已拿到", str(ctx.exception))

    def test_never_returns_half_file(self):
        """预算用尽就抛错，绝不把半截数据当完整的交出去（bencode 会解出鬼东西）。"""
        resp = lambda: _Resp(200, {"Content-Length": str(len(BODY))}, BODY, cut=400)
        with mock.patch.object(util, "_build_opener",
                               return_value=_Opener([resp() for _ in range(4)])):
            with self.assertRaises(util.HttpError):
                util.http_download(URL, proxy="http://127.0.0.1:7890",
                                   attempts=4, budget=0.1)


if __name__ == "__main__":
    unittest.main()
