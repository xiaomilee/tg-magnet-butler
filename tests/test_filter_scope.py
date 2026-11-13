"""文件筛选的作用范围：凡是能把磁力交给迅雷的路，都得过同一份规则。

用户问过：「文件筛选是全局生效的吗？无论是搜索后投递，还是关闭自动投递后
直接用磁力链接，都需要文件筛选。」

答案是「是」——但这句话得能被验证，所以这里把四条入口各跑一遍，
断言它们发出去的 ``sub_file_index`` 完全一样：

| 入口 | 谁触发 | 代码路径 |
| --- | --- | --- |
| 搜索后自动投递 | 收到文字，检索命中第一条 | ``handle_text`` → ``submit`` |
| 磁力直达（自动投递开） | 消息里自带磁力 | ``handle_magnets`` → ``submit`` |
| 关掉自动投递后点按钮补投 | Telegram 回执上的「⬇️ 现在下」 | ``submit`` |
| 面板上点「投递」 | 搜索结果卡片 / 磁力列表 | ``webui._download`` → ``submit`` |

四条最后都落到 ``XunleiDownloader.add`` → ``_post_task`` → ``select_files``，
所以只要这里钉住「同一个 PACK 得到的序号一致」，就不会出现某条路偷偷绕过。
另外还钉三件事：全选是唯一的总闸、单文件种子也过规则、换后端时必须承认管不着。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import intake                                    # noqa: E402
from tmb.config import Config                             # noqa: E402
from tmb.downloaders.base import DownloadResult            # noqa: E402
from tmb.downloaders.xunlei import XunleiDownloader        # noqa: E402
from tmb.pipeline import Pipeline                          # noqa: E402
from tmb.store import Store                                # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
MB = 1024 * 1024
GB = 1024 * MB

# 和 test_file_filter 里那个合集包同一副模样：正片 + 字幕 + 封面 + 说明 + 样片
PACK = [
    {"file_index": 0, "name": "S01E01.mkv", "file_size": 2 * GB},
    {"file_index": 1, "name": "S01E01.srt", "file_size": 40 * 1024},
    {"file_index": 2, "name": "cover.jpg", "file_size": 90 * 1024},
    {"file_index": 3, "name": "info.nfo", "file_size": 2 * 1024},
    {"file_index": 4, "name": "sample.mkv", "file_size": 30 * MB},
]
EXPECTED = "0,1"          # 默认规则：只留视频 + 字幕，30 MB 的样片不够 100 MB

ITEM = {"title": "某作品 1080p 中字",
        "magnet": "magnet:?xt=urn:btih:" + "c" * 40,
        "infohash": "c" * 40, "seeders": 50, "downloads": 10,
        "size_human": "1.2 GB", "age_human": "1 天", "score": 9.0,
        "reasons": [], "trusted": False}


class ScopeCase(unittest.TestCase):
    """真实的 XunleiDownloader：只把「解析链接」和「发请求」换成假的。"""

    files = PACK

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        dl = self.cfg.data["download"]
        dl["backend"] = "xunlei"
        dl["fallback"] = []
        dl["auto_download"] = True
        dl["xunlei"].update({"mode": "panel", "pan_auth": "OK",
                             "target": "device#x", "auto_refresh_token": False})
        self.cfg.data["app"]["dedupe_days"] = 0
        self.store = Store(os.path.join(self.tmp, "var", "t.db"))
        self.pipe = Pipeline(self.cfg, self.store)
        self.posted = []

    def tearDown(self):
        self.store.close()

    # ---- 假的迅雷 ----
    def _request(self, path, body=None, **kw):
        self.posted.append((path, body))
        if path.endswith("/resource/list"):
            return {"resources": self.files}
        return {"task": {"id": "t1"}}

    def run_with_fake(self, fn):
        """把「问迅雷」「找设备」「找种子地址」三件出网/读盘的事都按住。

        第三件必须一起按住：``submit`` 会为裸磁力按 infohash 去站点找 ``.torrent``
        （为了让文件清单准），不按住的话每个用例都要真连一次 sukebei，跑到超时。
        这些用例只关心「挑哪几个文件」，清单由 ``_request`` 的桩直接给。
        """
        with mock.patch.object(XunleiDownloader, "_request", side_effect=self._request), \
                mock.patch.object(XunleiDownloader, "resolve_target",
                                  return_value="device#x"), \
                mock.patch.object(self.pipe, "torrent_url_for", return_value=""):
            return fn()

    # ---- 抓包 ----
    def index_sent(self):
        """最后一次建任务请求里的 sub_file_index（没传就是「未传」）。"""
        for path, body in reversed(self.posted):
            if path.endswith("/drive/v1/task"):
                return ((body or {}).get("params") or {}).get("sub_file_index", "未传")
        return "没发建任务请求"

    # ---- 四条入口 ----
    def via_search(self):
        """搜索后自动投递。"""
        with mock.patch.object(self.pipe, "search", return_value={
                "items": [dict(ITEM)], "relaxed": False, "total": 1, "dropped": {}}):
            return self.pipe.handle_text("某作品 1080p 中字", chat_id=1)

    def via_magnet(self, want=True):
        """消息自带磁力。"""
        plan = intake.parse("某作品\n" + MAGNET, 3)
        return self.pipe.handle_magnets(plan, chat_id=1, want=want)

    def via_button(self):
        """关掉自动投递后，点回执上的「⬇️ 现在下」。bot 做的就是这一步。"""
        return self.pipe.submit({"index": 1, "title": "某合集",
                                 "magnet": MAGNET, "infohash": "a" * 40})

    def via_panel(self):
        """面板「投递」按钮。"""
        return self.pipe.submit({"title": "某合集", "magnet": MAGNET,
                                 "infohash": "a" * 40}, backend="xunlei")


class TestEveryPathFilters(ScopeCase):
    def test_search_auto_delivery(self):
        bundle = self.run_with_fake(self.via_search)
        self.assertTrue(bundle["submitted"][0]["ok"], bundle["submitted"])
        self.assertEqual(self.index_sent(), EXPECTED)
        self.assertIn("挑了 2 个", bundle["submitted"][0]["pick_note"])

    def test_magnet_direct(self):
        bundle = self.run_with_fake(self.via_magnet)
        self.assertTrue(bundle["ok"], bundle)
        self.assertEqual(self.index_sent(), EXPECTED)

    def test_button_after_auto_off(self):
        # 先确认关掉开关时真的没投（否则下面那句「点按钮才下」就是假的）
        bundle = self.run_with_fake(lambda: self.via_magnet(want=False))
        self.assertFalse(bundle["submitted"])
        self.assertEqual(self.index_sent(), "没发建任务请求")
        # 再走按钮那条路
        out = self.run_with_fake(self.via_button)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.index_sent(), EXPECTED)

    def test_panel_deliver(self):
        out = self.run_with_fake(self.via_panel)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.index_sent(), EXPECTED)

    def test_all_four_agree(self):
        """四条路发出去的序号必须一模一样，差一个都说明有地方在绕规则。"""
        got = []
        for fn in (self.via_search, self.via_magnet, self.via_button, self.via_panel):
            self.posted.clear()
            self.run_with_fake(fn)
            got.append(self.index_sent())
        self.assertEqual(got, [EXPECTED] * 4, got)


class TestFullSelectIsTheOnlyGate(ScopeCase):
    def test_full_select_is_off_by_default(self):
        self.assertFalse(self.cfg.data["download"]["xunlei"]["select_all_files"],
                         "默认就该按规则挑，否则「全局生效」是句空话")

    def test_switching_it_off_turns_filter_off_everywhere(self):
        self.cfg.data["download"]["xunlei"]["select_all_files"] = True
        for fn in (self.via_search, self.via_magnet, self.via_button, self.via_panel):
            self.posted.clear()
            self.run_with_fake(fn)
            self.assertEqual(self.index_sent(), "-1", fn.__name__)


class TestSingleFileTorrent(ScopeCase):
    """只有一个文件的磁力（很常见的「直链」形态）也得过规则。"""

    files = [{"file_index": 0, "name": "cover.jpg", "file_size": 90 * 1024}]

    def test_single_image_is_refused_when_it_is_the_only_file(self):
        self.cfg.data["download"]["xunlei"]["file_filter"]["fallback_all"] = False
        out = self.run_with_fake(self.via_magnet)
        self.assertFalse(out["ok"])
        self.assertIn("全筛掉了", out["submitted"][0]["message"])

    def test_single_image_falls_back_by_default(self):
        out = self.run_with_fake(self.via_magnet)
        self.assertTrue(out["ok"], out)
        # 只有一个文件时用不着给迅雷指序号
        self.assertEqual(self.index_sent(), "未传")


class TestOtherBackendAdmitsIt(ScopeCase):
    def test_aria2_says_filter_does_not_apply(self):
        fake = mock.Mock()
        fake.name = "aria2"
        fake.add = mock.Mock(return_value=DownloadResult(
            True, "aria2", "gid-1", "已交给 aria2", {}))
        with mock.patch("tmb.downloaders.chain", return_value=[fake]):
            out = self.pipe.submit({"title": "某合集", "magnet": MAGNET,
                                    "infohash": "a" * 40})
        self.assertTrue(out["ok"], out)
        self.assertIn("只对迅雷生效", out["message"])

    def test_no_noise_when_filter_is_off(self):
        self.cfg.data["download"]["xunlei"]["select_all_files"] = True
        fake = mock.Mock()
        fake.name = "aria2"
        fake.add = mock.Mock(return_value=DownloadResult(
            True, "aria2", "gid-1", "已交给 aria2", {}))
        with mock.patch("tmb.downloaders.chain", return_value=[fake]):
            out = self.pipe.submit({"title": "某合集", "magnet": MAGNET,
                                    "infohash": "a" * 40})
        self.assertNotIn("只对迅雷生效", out["message"])


if __name__ == "__main__":
    unittest.main()
