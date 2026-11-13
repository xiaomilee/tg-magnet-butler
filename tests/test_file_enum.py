"""文件清单：为什么「筛选没生效、全下了」，以及现在怎么保证它生效。

起因：真机上一个 6 文件的种子（2 个正片 mp4 + 3 个广告 mp4 + 1 个说明 + 1 个 html），
规则是「只留视频、小于 100 MB 跳过」，结果迅雷把整包全下了。查下来的链条是：

    /drive/v1/resource/list 对这条磁力只回了一个节点
    → _flatten_files 只认出 1 个「文件」→ total_file_count=1
    → total<=1 时不发 sub_file_index（迅雷的规矩）
    → 迅雷按「没指定就全选」处理 → 全下

所以这里钉三件事：

1. 种子文件（.torrent）能解出权威清单，顺序就是 BT 的文件顺序，也就是 sub_file_index 的序号；
2. 有种子文件时根本不问迅雷，直接按它算；没有时逐套试 resource/list 的请求参数；
3. 什么都认不出来时，回执必须承认「没拿到清单，这次是全下」，不许假装「1 个文件」。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import torrentfile                                  # noqa: E402
from tmb.downloaders import xunlei as xl                     # noqa: E402
from tmb.downloaders.xunlei import XunleiDownloader           # noqa: E402

MB = 1024 * 1024
GB = 1024 * MB
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


# ---------- 最小的 bencode 编码器，用来造种子 ----------
def _b(v):
    v = v if isinstance(v, bytes) else str(v).encode()
    return b"%d:%s" % (len(v), v)


def _i(n):
    return b"i%de" % n


def _l(items):
    return b"l" + b"".join(items) + b"e"


def _d(items):
    out = b"d"
    for k in sorted(items):
        out += _b(k) + items[k]
    return out + b"e"


def _torrent(files, name="pack"):
    """files: [(相对路径, 字节数), …]；路径里可以有 '/'。"""
    entries = []
    for path, size in files:
        parts = _l([_b(p) for p in path.split("/")])
        entries.append(_d({b"length": _i(size), b"path": parts}))
    info = _d({b"name": _b(name), b"files": _l(entries)})
    return _d({b"info": info})


def _torrent_single(name, size):
    info = _d({b"name": _b(name), b"length": _i(size)})
    return _d({b"info": info})


# 真机上那条种子：2 个正片、3 个广告样片、1 个说明、1 个 html
REAL_PACK = [
    ("489155.com@FC2PPV-5003668-1.mp4", 3522799153),
    ("489155.com@FC2PPV-5003668-2.mp4", 760233533),
    ("台湾uu美少女直播 20年信誉保证服务全球.mp4", 13890047),
    ("最 新 位 址 獲 取：489155.com 收藏不迷路.txt", 136),
    ("社 區 最 新 情 報.mp4", 15089802),
    ("聚 合 全 網 H 直 播.html", 145),
]
REAL_EXPECTED = "0,1"


class TorrentFileCase(unittest.TestCase):
    def test_multi_file_order_and_size(self):
        files = torrentfile.files_of(_torrent(REAL_PACK))
        self.assertEqual(len(files), 6)
        self.assertEqual([f["name"] for f in files], [p for p, _ in REAL_PACK])
        self.assertEqual(files[0]["file_size"], 3522799153)
        # 子目录要拼成 a/b.mp4，别只剩文件名（否则同名不同目录分不开）
        nested = torrentfile.files_of(_torrent([("S01/E01.mkv", 10), ("S01/E02.mkv", 20)]))
        self.assertEqual([f["name"] for f in nested], ["S01/E01.mkv", "S01/E02.mkv"])

    def test_single_file_torrent(self):
        files = torrentfile.files_of(_torrent_single("只有一集.mp4", 1234))
        self.assertEqual(files, [{"name": "只有一集.mp4", "file_size": 1234}])

    def test_garbage_is_rejected(self):
        with self.assertRaises(torrentfile.BencodeError):
            torrentfile.files_of(b"<html>not a torrent</html>")
        with self.assertRaises(torrentfile.BencodeError):
            torrentfile.files_of(_d({b"info": _d({b"name": _b("x")})}))

    def test_shift_jis_name(self):
        name = "作品名".encode("cp932")
        blob = _torrent_single("x", 1).replace(_b("x"), _b(name))
        got = torrentfile.files_of(blob)
        self.assertEqual(got[0]["name"], "作品名")


class ListFilesCase(unittest.TestCase):
    """只把 «发请求» 换掉，其它全是真的。"""

    def setUp(self):
        xl._FILE_VARIANT["label"] = ""            # 模块级缓存不能在用例间串味
        xl._RETRY_PAUSE = 0                       # 「再问一轮」的等待在测试里没必要真等
        self.dl = XunleiDownloader({
            "mode": "panel", "pan_auth": "OK", "target": "device#x",
            "select_all_files": False, "timeout": 5,
            "file_filter": dict(xl.DEFAULT_FILE_FILTER),
        })
        self.calls = []

    def fake_request(self, path, body=None, **kw):
        self.calls.append((path, body))
        if path.endswith("/drive/v1/task"):
            return {"task": {"id": "T1"}}
        return {"resources": []}

    def run_with(self, fn):
        with mock.patch.object(XunleiDownloader, "_request",
                               side_effect=self.fake_request), \
                mock.patch.object(XunleiDownloader, "resolve_target",
                                  return_value="device#x"):
            return fn()

    def test_api_first_then_torrent(self):
        """先问迅雷（本地、快）；它认不出来才去抓种子。反过来会让每条投递都等外网。"""
        with mock.patch.object(torrentfile, "files_from_url",
                               return_value=[{"name": n, "file_size": s}
                                             for n, s in REAL_PACK]):
            files, meta = self.run_with(
                lambda: self.dl.list_files(MAGNET, torrent_url="http://x/1.torrent"))
        self.assertEqual(len(files), 6)
        self.assertEqual(meta["source"], "torrent")
        # 迅雷那几套参数都试过了（假接口一律回空），所以一定留下请求记录
        self.assertTrue(self.calls, "应该先问迅雷再退回种子文件")

    def test_array_variant_recognised(self):
        """第一套参数只回一个目录节点，第二套（数组 urls）才认出整棵树。"""
        tree = {"resources": [
            {"name": "a.mp4", "file_size": 3 * GB},
            {"name": "b.mp4", "file_size": 2 * GB},
            {"name": "c.txt", "file_size": 100},
        ]}

        def req(path, body=None, **kw):
            self.calls.append((path, body))
            if path.endswith("/resource/list"):
                return tree if isinstance((body or {}).get("urls"), list) else {
                    "resources": [{"name": "根目录", "kind": "drive#folder"}]}
            return {"task": {"id": "T1"}}

        with mock.patch.object(XunleiDownloader, "_request", side_effect=req):
            files, meta = self.dl.list_files(MAGNET)
        self.assertEqual(len(files), 3)
        self.assertTrue(meta["source"].startswith("api:"), meta["source"])
        self.assertEqual(xl._FILE_VARIANT["label"], "urls[]")
        # 认出好用那套之后，下次先试它（少打三次网络）
        self.calls = []
        with mock.patch.object(XunleiDownloader, "_request", side_effect=req):
            self.dl.list_files(MAGNET)
        self.assertEqual((self.calls[0][1] or {}).get("urls"), [MAGNET])

    def test_resolver_only_when_needed(self):
        """迅雷认不出（回空）时才按 infohash 现找种子地址。

        注意这里要真让「迅雷在线但没清单」：连不上迅雷时程序会直接收手
        （投递本身也马上会失败，再去下种子只是让用户白等），不走找地址这条路。
        """
        def empty(path, body=None, **kw):
            return {"resources": []} if path.endswith("/resource/list") else {}

        with mock.patch.object(XunleiDownloader, "_request", side_effect=empty), \
                mock.patch.object(torrentfile, "files_from_url",
                                  return_value=[{"name": n, "file_size": s}
                                                for n, s in REAL_PACK]) as fetch:
            files, meta = self.dl.list_files(MAGNET, resolver=lambda: "http://x/2.torrent")
        self.assertEqual(len(files), 6)
        self.assertEqual(meta["source"], "torrent")
        fetch.assert_called_once()

    def test_resolver_not_called_when_api_fine(self):
        tree = {"resources": [{"name": "a.mp4", "file_size": 3 * GB},
                              {"name": "b.mp4", "file_size": 2 * GB}]}

        def req(path, body=None, **kw):
            return tree if path.endswith("/resource/list") else {"task": {"id": "T1"}}

        hit = []
        with mock.patch.object(XunleiDownloader, "_request", side_effect=req):
            files, _meta = self.dl.list_files(MAGNET, resolver=lambda: hit.append(1))
        self.assertEqual(len(files), 2)
        self.assertEqual(hit, [])


class DeliveryCase(unittest.TestCase):
    """端到端：认出来的清单要真的变成 sub_file_index。"""

    def setUp(self):
        xl._FILE_VARIANT["label"] = ""
        xl._RETRY_PAUSE = 0
        self.dl = XunleiDownloader({
            "mode": "panel", "pan_auth": "OK", "target": "device#x",
            "select_all_files": False, "timeout": 5,
            "file_filter": dict(xl.DEFAULT_FILE_FILTER),
        })
        self.bodies = []

    def post_task(self, resources, options=None, torrent_files=None):
        def req(path, body=None, **kw):
            if path.endswith("/resource/list"):
                return {"resources": resources}
            self.bodies.append(body)
            return {"task": {"id": "T1"}}

        rows = [{"name": n, "file_size": s} for n, s in (torrent_files or [])]
        opts = dict(options or {})
        if torrent_files and not opts.get("torrent_url"):
            opts["torrent_url"] = "http://x/1.torrent"
        with mock.patch.object(XunleiDownloader, "_request", side_effect=req), \
                mock.patch.object(XunleiDownloader, "resolve_target",
                                  return_value="device#x"), \
                mock.patch.object(torrentfile, "files_from_url", return_value=rows):
            return self.dl.add(MAGNET, "某作品", opts)

    def test_six_files_filtered_to_two(self):
        res = self.post_task([{"name": "根目录", "kind": "drive#folder"}],
                             {"torrent_url": "http://x/1.torrent"},
                             torrent_files=REAL_PACK)
        self.assertTrue(res.ok, res.message)
        params = self.bodies[-1]["params"]
        self.assertEqual(params["sub_file_index"], REAL_EXPECTED)
        self.assertEqual(params["total_file_count"], "6")
        self.assertIn("2 / 6", res.message)

    def test_no_file_list_admits_it(self):
        """什么都认不出来时，宁可说「全下」，也不能报「1 个文件」蒙混过去。"""
        res = self.post_task([])
        self.assertTrue(res.ok, res.message)
        self.assertNotIn("sub_file_index", self.bodies[-1]["params"])
        self.assertIn("没拿到文件清单", res.message)

    def test_single_file_pack_still_filtered(self):
        """单文件种子也要过规则：它是图片、又关了兜底，就该拒投，而不是照下。"""
        f = dict(xl.DEFAULT_FILE_FILTER, fallback_all=False)
        self.dl.cfg["file_filter"] = f
        res = self.post_task([], torrent_files=[("封面.jpg", 2 * MB)])
        self.assertFalse(res.ok)
        self.assertIn("全筛掉", res.message)


class SubmitOptionsCase(unittest.TestCase):
    """pipeline 必须把种子地址递给下载器，否则下载器只能干瞪眼。"""

    def test_torrent_url_passed_through(self):
        import tempfile

        from tmb.config import Config
        from tmb.downloaders.base import DownloadResult
        from tmb.pipeline import Pipeline
        from tmb.store import Store

        tmp = tempfile.mkdtemp()
        cfg = Config(os.path.join(tmp, "etc"), os.path.join(tmp, "var")).load()
        cfg.data["download"]["backend"] = "xunlei"
        cfg.data["download"]["fallback"] = []
        cfg.data["download"]["xunlei"].update({"mode": "panel", "pan_auth": "OK",
                                               "target": "device#x",
                                               "auto_refresh_token": False})
        cfg.data["app"]["dedupe_days"] = 0
        store = Store(os.path.join(tmp, "var", "t.db"))
        pipe = Pipeline(cfg, store)
        seen = []

        class FakeDl:
            name = "xunlei"

            def add(self, magnet, title="", options=None):
                seen.append(options or {})
                return DownloadResult(True, "xunlei", "T1", "已投递迅雷（6 个文件）", {})

        try:
            with mock.patch.object(pipe, "refresh_xunlei_token",
                                   return_value=(False, "")), \
                    mock.patch("tmb.pipeline.downloaders.chain",
                               return_value=[FakeDl()]):
                pipe.submit({"title": "x", "magnet": MAGNET, "infohash": "a" * 40,
                             "torrent_url": "https://sukebei.nyaa.si/download/1.torrent"},
                            backend="xunlei")
        finally:
            store.close()
        self.assertEqual(seen[0]["torrent_url"],
                         "https://sukebei.nyaa.si/download/1.torrent")
        self.assertTrue(callable(seen[0]["torrent_resolver"]))


class InspectCase(unittest.TestCase):
    """面板「解析测试」：不投递，只把「认出几个 / 会挑哪几个」摆出来。"""

    def test_reports_keep_flags(self):
        xl._FILE_VARIANT["label"] = ""
        dl = XunleiDownloader({"mode": "panel", "pan_auth": "OK",
                               "target": "device#x", "select_all_files": False,
                               "timeout": 5,
                               "file_filter": dict(xl.DEFAULT_FILE_FILTER)})
        rows = [{"name": n, "file_size": s} for n, s in REAL_PACK]
        with mock.patch.object(XunleiDownloader, "_request",
                               return_value={"resources": []}), \
                mock.patch.object(torrentfile, "files_from_url", return_value=rows):
            rep = dl.inspect(MAGNET, torrent_url="http://x/1.torrent")
        self.assertEqual(rep["total"], 6)
        self.assertEqual(rep["sub_file_index"], REAL_EXPECTED)
        self.assertEqual([r["keep"] for r in rep["files"]],
                         [True, True, False, False, False, False])
        self.assertIn("挑了 2 个", rep["pick_note"])
        # 每种图都带着类别，面板才能显示「视频 / 说明 / 其他」
        self.assertEqual(rep["files"][3]["cat"], "doc")

    def test_no_download_happens(self):
        xl._FILE_VARIANT["label"] = ""
        dl = XunleiDownloader({"mode": "panel", "pan_auth": "OK",
                               "target": "device#x", "timeout": 5})
        seen = []

        def req(path, body=None, **kw):
            seen.append(path)
            if path.endswith("/resource/list"):
                return {"resources": []}
            raise AssertionError("解析测试不许建任务")

        with mock.patch.object(XunleiDownloader, "_request", side_effect=req), \
                mock.patch.object(torrentfile, "files_from_url", return_value=[]):
            dl.inspect(MAGNET)
        self.assertNotIn("/drive/v1/task", seen)


class DrillDownCase(unittest.TestCase):
    """真机现象（1.0.27，2026-11-12）：迅雷只回**一个目录节点**，一个文件都认不出来。

    ```
    resources = [{"is_dir": true, "id": "…", "name": "FC2PPV-5003668",
                  "file_count": "6", "file_size": …, "parent_id": …}]
    ```

    它不是「一个文件」（那样不发 sub_file_index → 全下），也不是「没有文件」（那样只能
    出网抓种子，代理一抖就全盘皆输），而是「清单在下一层」。所以必须往里钻一层。
    """

    def setUp(self):
        xl._FILE_VARIANT["label"] = ""
        xl._CHILD_VARIANT["label"] = ""
        xl._RETRY_PAUSE = 0
        self.dl = XunleiDownloader({
            "mode": "panel", "pan_auth": "OK", "target": "device#x",
            "select_all_files": False, "timeout": 5,
            "file_filter": dict(xl.DEFAULT_FILE_FILTER)})
        self.calls = []
        self.root = {"is_dir": True, "id": "DIR1", "name": "FC2PPV-5003668",
                     "file_count": str(len(REAL_PACK)), "parent_id": "0",
                     "file_size": 4000000000}
        self.kids = [{"name": n, "file_size": s, "file_index": i}
                     for i, (n, s) in enumerate(REAL_PACK)]

    def fake_request(self, path, body=None, **kw):
        self.calls.append((path, body))
        if path.endswith("/drive/v1/task"):
            return {"task": {"id": "T1"}}
        body = body or {}
        if body.get("parent_id") == "DIR1":
            return {"resources": self.kids, "list_id": "L1"}
        # 问磁力：只回一个没展开的根目录节点（就是真机上那种）
        return {"resources": [self.root], "list_id": "L1"}

    def run_with(self, fn):
        with mock.patch.object(XunleiDownloader, "_request",
                               side_effect=self.fake_request), \
                mock.patch.object(XunleiDownloader, "resolve_target",
                                  return_value="device#x"):
            return fn()

    def test_dir_node_is_drilled(self):
        files, meta = self.run_with(lambda: self.dl.list_files(MAGNET))
        self.assertEqual(len(files), 6)
        self.assertEqual([f["name"] for f in files], [p for p, _ in REAL_PACK])
        self.assertTrue(meta.get("expanded"))
        # 展开成功就不该再跑一趟外网
        self.assertEqual(meta.get("source", "")[:4], "api:")

    def test_drilled_list_gets_filtered(self):
        """展开来的清单要能真正参与筛选：6 个里挑出 2 个正片。"""
        files, _ = self.run_with(lambda: self.dl.list_files(MAGNET))
        indices, info = self.dl.select_files(files)
        self.assertEqual(info["total"], 6)
        self.assertEqual(indices, [0, 1])

    def test_no_torrent_download_after_drill(self):
        """钻到了清单就不许再去下种子——NAS 上代理不通时那要干等 30~40 秒。"""
        with mock.patch.object(torrentfile, "files_from_url") as fetch:
            self.run_with(lambda: self.dl.list_files(
                MAGNET, resolver=lambda: "https://x/1.torrent"))
        fetch.assert_not_called()

    def test_self_returning_variant_falls_through(self):
        """第一套参数被无视（回来还是目录本身）时，接着试下一套，别在这儿死循环。"""
        def req(path, body=None, **kw):
            self.calls.append((path, body))
            body = body or {}
            # 第一套（只给 parent_id）被无视：回来的还是目录节点本身
            if body.get("parent_id") == "DIR1" and "list_id" not in body:
                return {"resources": [self.root], "list_id": "L1"}
            if body.get("parent_id") == "DIR1":
                return {"resources": self.kids, "list_id": "L1"}
            return {"resources": [self.root], "list_id": "L1"}

        with mock.patch.object(XunleiDownloader, "_request", side_effect=req):
            files, meta = self.dl.list_files(MAGNET)
        self.assertEqual(len(files), 6)
        self.assertTrue(any("原样返回目录本身" in str(a.get("note") or "")
                            for a in meta["attempts"]))

    def test_single_file_dir_not_treated_as_dir(self):
        """file_count == 1 是真·单文件种子，不能当目录钻（钻了反而什么都拿不到）。"""
        self.assertFalse(xl._is_dir_node({"name": "a.mp4", "file_size": 10,
                                          "file_count": "1"}))
        self.assertTrue(xl._is_dir_node({"name": "pack", "file_count": "6"}))

    def test_inspect_shows_expanded(self):
        rep = self.run_with(lambda: self.dl.inspect(MAGNET))
        self.assertEqual(rep["total"], 6)
        self.assertTrue(rep["expanded"])
        self.assertEqual(rep["sub_file_index"], REAL_EXPECTED)
        # 展开后第一个文件长什么样，面板要能看到（排查时全靠它）
        self.assertEqual(rep["expand_sample"].get("file_index"), 0)


# 真机响应（飞牛网关 + 官方迅雷，2026-11-12）。注意三点：
#   1. 顶层是 {"list_id": …, "list": {"page_size": …, "resources": [...]}} —— 多包了一层；
#   2. 目录节点的内容在 "dir" 里，而 dir **不是路径字符串**，是 {"resources": [...]}；
#   3. 第一个文件没有 "file_index"（0 被省了），后面从 1 开始。
REAL_LIST_RESPONSE = {
    "list_id": "Ah5-uFirENP7DMJOSF1q0hjnWHGZjoy7",
    "list": {"page_size": 2000, "resources": [{
        "id": "-EpxVXItICJS06YwO5kp_cvhTH4.0",
        "name": "FC2PPV-5003668",
        "file_size": 4312012816,
        "file_count": 6,
        "is_dir": True,
        "parent_id": "-EpxVXItICJS06YwO5kp_cvhTH4",
        "meta": {"bt_infohash": "8D4849E2DBC8ABC33E9A1923AC3F6927CB41218A",
                 "bt_subfiles_num": "6"},
        "dir": {"page_size": 6, "resources": [
            {"id": "…0.0", "name": REAL_PACK[0][0], "file_size": REAL_PACK[0][1],
             "file_count": 1, "meta": {"mime_type": "video/mp4"}},
            {"id": "…0.1", "name": REAL_PACK[1][0], "file_size": REAL_PACK[1][1],
             "file_count": 1, "file_index": 1},
            {"id": "…0.2", "name": REAL_PACK[2][0], "file_size": REAL_PACK[2][1],
             "file_count": 1, "file_index": 2},
            {"id": "…0.3", "name": REAL_PACK[3][0], "file_size": REAL_PACK[3][1],
             "file_count": 1, "file_index": 3},
            {"id": "…0.4", "name": REAL_PACK[4][0], "file_size": REAL_PACK[4][1],
             "file_count": 1, "file_index": 4},
            {"id": "…0.5", "name": REAL_PACK[5][0], "file_size": REAL_PACK[5][1],
             "file_count": 1, "file_index": 5},
        ]},
    }]},
}


class RealResponseCase(unittest.TestCase):
    """用真机那份响应钉死：一次请求就要认出 6 个文件，绝不许多跑一趟外网。

    「还是一个文件都没认出来」的真身就在这份 JSON 里：整棵树被 `list` 和 `dir` 包了两层，
    老代码只认平铺的 `resources`，于是把它当成「一个空目录」丢掉了。
    """

    def setUp(self):
        xl._FILE_VARIANT["label"] = ""
        xl._CHILD_VARIANT["label"] = ""
        xl._RETRY_PAUSE = 0
        self.dl = XunleiDownloader({
            "mode": "panel", "pan_auth": "OK", "target": "device#x",
            "select_all_files": False, "timeout": 5,
            "file_filter": dict(xl.DEFAULT_FILE_FILTER)})
        self.paths = []

    def fake_request(self, path, body=None, **kw):
        self.paths.append((path, body))
        if path.endswith("/drive/v1/task"):
            return {"task": {"id": "T1"}}
        return REAL_LIST_RESPONSE

    def run_with(self, fn):
        with mock.patch.object(XunleiDownloader, "_request",
                               side_effect=self.fake_request):
            return fn()

    def test_six_files_from_one_request(self):
        files, meta = self.run_with(lambda: self.dl.list_files(MAGNET))
        self.assertEqual(len(files), 6)
        self.assertEqual([f["name"] for f in files], [p for p, _ in REAL_PACK])
        # 平铺出来了就够了，不用再钻也不用下种子
        self.assertFalse(meta.get("expanded"))
        self.assertEqual(len([c for c in self.paths
                              if c[0].endswith("/resource/list")]), 1)

    def test_missing_file_index_means_zero(self):
        files, _ = self.run_with(lambda: self.dl.list_files(MAGNET))
        indices, info = self.dl.select_files(files)
        self.assertEqual(info["total"], 6)
        self.assertEqual(indices, [0, 1])       # 第一个文件没 files_index 也算 0

    def test_no_torrent_download(self):
        with mock.patch.object(torrentfile, "files_from_url") as fetch:
            self.run_with(lambda: self.dl.list_files(
                MAGNET, resolver=lambda: "https://x/1.torrent"))
        fetch.assert_not_called()

    def test_delivery_sends_sub_file_index(self):
        """投递时真的带上 sub_file_index=0,1，而不是被当成「单文件」不发。"""
        seen = []

        def req(path, body=None, **kw):
            self.paths.append((path, body))
            if path.endswith("/drive/v1/task"):
                seen.append(body)
                return {"task": {"id": "T1"}}
            return REAL_LIST_RESPONSE

        with mock.patch.object(XunleiDownloader, "_request", side_effect=req), \
                mock.patch.object(XunleiDownloader, "resolve_target",
                                  return_value="device#x"):
            res = self.dl.add(MAGNET, "某作品")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(seen[0]["params"]["sub_file_index"], "0,1")
        self.assertEqual(seen[0]["params"]["total_file_count"], "6")

    def test_inspect_lists_them(self):
        rep = self.run_with(lambda: self.dl.inspect(MAGNET))
        self.assertEqual(rep["total"], 6)
        self.assertEqual(rep["sub_file_index"], REAL_EXPECTED)


class TorrentUrlCase(unittest.TestCase):
    """按 infohash 找 `.torrent` 地址：找错种子比找错文件更糟，这里专门钉这条。"""

    IH = "8d4849e2dbc8abc33e9a1923ac3f6927cb41218a"
    CFG = {"base_url": "https://sukebei.nyaa.si", "timeout": 5, "proxy": ""}

    def run_with(self, html, status=200):
        from tmb import nyaa
        with mock.patch.object(nyaa.util, "http_request",
                               return_value=(status, html, html.encode())):
            return nyaa.torrent_url_by_hash(self.CFG, self.IH)

    def test_single_result_view_page(self):
        """搜索结果只有一条时站点 302 到 /view/N，那一页就属于这条种子。"""
        html = ('<div class="torrent-file-list panel-body">list</div>'
                '<a href="magnet:?xt=urn:btih:%s">m</a>'
                '<a href="/download/4754736.torrent">dl</a>'
                '<td>Info hash: %s</td>' % (self.IH, self.IH))
        self.assertEqual(self.run_with(html),
                         "https://sukebei.nyaa.si/download/4754736.torrent")

    def test_list_page_matches_the_right_row(self):
        other = "a" * 40
        html = (
            '<tr><td><a href="/view/1">t1</a>'
            '<a href="magnet:?xt=urn:btih:%s">m</a>'
            '<a href="/download/1.torrent">d</a></td></tr>'
            '<tr><td><a href="/view/2">t2</a>'
            '<a href="magnet:?xt=urn:btih:%s">m</a>'
            '<a href="/download/2.torrent">d</a></td></tr>' % (other, self.IH))
        self.assertEqual(self.run_with(html),
                         "https://sukebei.nyaa.si/download/2.torrent")

    def test_no_matching_row_returns_nothing(self):
        """页面上有 hash 字样但没有对应的行时，绝不能拿第一个链接凑数。"""
        html = ('<tr><td><a href="/view/1">t1</a>'
                '<a href="magnet:?xt=urn:btih:%s">m</a>'
                '<a href="/download/1.torrent">d</a></td></tr>'
                '<p>搜索 %s 没有结果</p>' % ("b" * 40, self.IH))
        self.assertEqual(self.run_with(html), "")

    def test_empty_and_error(self):
        self.assertEqual(self.run_with("<rss></rss>"), "")
        self.assertEqual(self.run_with("", status=502), "")
        self.assertEqual(self.run_with("nothing"), "")   # 页面里根本没有这个 hash

    def test_bad_hash_rejected_without_request(self):
        from tmb import nyaa
        with mock.patch.object(nyaa.util, "http_request") as req:
            self.assertEqual(nyaa.torrent_url_by_hash(self.CFG, "abc"), "")
        self.assertFalse(req.called)


if __name__ == "__main__":
    unittest.main()

class PortFallbackCase(unittest.TestCase):
    """配置里的网关端口过期了，不该变成「未取到迅雷设备空间」。

    真机现场（2026-11-13）：配置指着一个没人监听的端口 → errno 111 → 一路失败到
    「未取到设备空间」，用户完全看不出是端口的事。网关 5666 明明是通的，
    程序应该自己换过去（1.0.31 起端口还由启动时的自动侦测写回配置），而不是让人去猜。
    """

    def setUp(self):
        xl._FILE_VARIANT["label"] = ""
        xl._CHILD_VARIANT["label"] = ""
        xl._RETRY_PAUSE = 0
        self.dl = XunleiDownloader({
            "mode": "fnos_gateway", "host": "127.0.0.1", "fnos_port": 5999,
            "pan_auth": "OK", "target": "device#x",
            "select_all_files": False, "timeout": 5,
            "file_filter": dict(xl.DEFAULT_FILE_FILTER)})
        self.urls = []

    def fake_http(self, url, **kw):
        self.urls.append(url)
        if ":5999" in url:
            raise xl.util.HttpError("连接被拒绝（errno 111）。（0.0s）")
        return 200, json.dumps(REAL_LIST_RESPONSE, ensure_ascii=False), b""

    def test_switches_to_gateway_when_port_refuses(self):
        with mock.patch.object(xl.util, "http_request", side_effect=self.fake_http):
            files, meta = self.dl.list_files(MAGNET)
        self.assertEqual(len(files), 6)
        self.assertIn(":5666/cgi/ThirdParty/xunlei/index.cgi", self.dl.api_base())
        self.assertTrue(any(":5999" in u for u in self.urls), "应该先打配置的地址")
        self.assertTrue(any(":5666" in u for u in self.urls), "然后自动改打 5666")

    def test_http_status_error_does_not_switch(self):
        """有 HTTP 状态码说明地址是对的（只是请求不对），换端口只会盖掉真错误。"""
        def boom(url, **kw):
            raise xl.util.HttpError("HTTP 400 https://x", 400, b"{}")

        with mock.patch.object(xl.util, "http_request", side_effect=boom):
            self.dl.list_files(MAGNET)
        self.assertEqual(self.dl.api_base(),
                         "http://127.0.0.1:5999/cgi/ThirdParty/xunlei/index.cgi")

    def test_preview_does_not_download_torrent(self):
        """预览已经拿到 6 个文件，就不该再跑一趟外网下种子（代理不通时会干等十几秒）。"""
        def req(path, body=None, **kw):
            if path.endswith("/drive/v1/tasks"):
                return {"tasks": []}
            return REAL_LIST_RESPONSE

        with mock.patch.object(XunleiDownloader, "_request", side_effect=req), \
                mock.patch.object(torrentfile, "files_from_url") as fetch:
            rep = self.dl.inspect(MAGNET, torrent_url="http://x/1.torrent")
        fetch.assert_not_called()
        self.assertEqual(rep["total"], 6)
        self.assertEqual(rep["sub_file_index"], REAL_EXPECTED)
