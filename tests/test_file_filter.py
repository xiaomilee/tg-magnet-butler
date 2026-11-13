"""多文件种子的「只下其中几个」。

迅雷建任务接口用 ``sub_file_index`` 表达选择：``-1`` 全选，``0,2,5`` 只下这几个。
这里锁的是「怎么挑」这件事本身，五个关键点：

1. 一个文件按扩展名只归一类，类别开关决定要不要；
2. 大小门槛对字幕和说明豁免（否则 100 MB 的门槛会把字幕全删）；
3. 拿不到大小的文件不能被误删（宁多留，不误杀）；
4. **一个都没留下时绝不发空选择**——空串在迅雷那边等于「一个都不要」，
   会建出一个没有文件的空壳任务；默认改回全选，也可以配成宁可不投；
5. 筛选结果要跟到回执里，否则「到底生效没有」只能去迅雷里数文件。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb.config import Config                                       # noqa: E402
from tmb.downloaders.base import DownloadResult                     # noqa: E402
from tmb.downloaders.xunlei import (                                # noqa: E402
    CATEGORY_LABEL, CATEGORY_ORDER, DEFAULT_FILE_FILTER, XunleiDownloader,
    classify_file, file_size_of, pick_note, size_human)
from tmb.pipeline import Pipeline                                   # noqa: E402
from tmb.store import Store                                         # noqa: E402

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
KB = 1024
MB = 1024 * KB
GB = 1024 * MB


def seed(index, name, size=None, **extra):
    node = {"file_index": index, "name": name}
    if size is not None:
        node["file_size"] = size
    node.update(extra)
    return node


# 一个很典型的合集包：正片 + 字幕 + 封面 + 说明 + 样片 + 附加包
PACK = [
    seed(0, "S01E01.mkv", 2 * GB),
    seed(1, "S01E01.srt", 40 * KB),
    seed(2, "cover.jpg", 90 * KB),
    seed(3, "info.nfo", 2 * KB),
    seed(4, "sample.mkv", 30 * MB),
    seed(5, "bonus.7z", 500 * MB),
]


class FilterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()

    def dl(self, select_all=False, file_filter=None, **over):
        sect = json.loads(json.dumps(self.cfg.data["download"]["xunlei"]))
        sect["select_all_files"] = select_all
        if file_filter is not None:
            sect["file_filter"] = file_filter
        sect.update(over)
        d = XunleiDownloader(sect)
        d.posted = []
        d._request = lambda path, body=None, **kw: (
            d.posted.append(body), {"task": {"id": "t1"}})[1]
        return d

    def send(self, d, files):
        res = d._post_task(MAGNET, "某合集", "device#x", files, {})
        body = d.posted[-1] if d.posted else None
        params = (body or {}).get("params") or {}
        return res, params.get("sub_file_index", "<未传>")

    @staticmethod
    def filter(**over):
        f = dict(DEFAULT_FILE_FILTER)
        f.update(over)
        return f


class TestClassify(unittest.TestCase):
    def test_common_extensions(self):
        cases = {"a.mkv": "video", "b.MP4": "video", "c.srt": "subtitle",
                 "d.ass": "subtitle", "e.jpg": "image", "f.png": "image",
                 "g.nfo": "doc", "h.txt": "doc", "i.7z": "archive",
                 "j.zip": "archive", "k.exe": "other", "README": "other"}
        for name, want in cases.items():
            self.assertEqual(classify_file(name), want, name)

    def test_dot_in_directory_does_not_confuse(self):
        # 只看最后一段扩展名，别被路径里的点带偏
        self.assertEqual(classify_file("S01.1080p/EP01.mkv"), "video")
        self.assertEqual(classify_file("S01.1080p/cover.jpg"), "image")

    def test_every_category_has_a_label(self):
        for cat in CATEGORY_ORDER:
            self.assertIn(cat, CATEGORY_LABEL)

    def test_size_parsing(self):
        self.assertEqual(file_size_of({"file_size": "1024"}), 1024)
        self.assertEqual(file_size_of({"file_size": 2048}), 2048)
        self.assertEqual(file_size_of({"size": "512"}), 512)
        self.assertEqual(file_size_of({}), 0)
        self.assertEqual(file_size_of({"file_size": "abc"}), 0)
        self.assertEqual(size_human(0), "0 B")
        self.assertEqual(size_human(1536), "1.5 KB")


class TestDefaults(FilterCase):
    def test_config_defaults_match_code_defaults(self):
        """config.py 和 xunlei.py 各有一份默认值，跑偏了就会「面板显示的和实际做的」不一致。"""
        self.assertEqual(self.cfg.data["download"]["xunlei"]["file_filter"],
                         DEFAULT_FILE_FILTER)

    def test_full_select_sends_minus_one(self):
        d = self.dl(select_all=True)
        res, idx = self.send(d, PACK)
        self.assertTrue(res.ok)
        self.assertEqual(idx, "-1")
        self.assertNotIn("_pick", res.raw, "全选时不该挂筛选说明")

    def test_single_file_sends_nothing(self):
        d = self.dl(select_all=False)
        _, idx = self.send(d, [seed(0, "one.mkv", 3 * GB)])
        self.assertEqual(idx, "<未传>")


class TestCategory(unittest.TestCase):
    def setUp(self):
        self.case = FilterCase()
        self.case.setUp()

    def pick(self, files, **over):
        return self.case.dl(select_all=False, file_filter=self.case.filter(**over)
                            ).select_files(files)

    def test_default_keeps_video_and_subtitle_only(self):
        idx, info = self.pick(PACK)
        self.assertEqual(idx, [0, 1])
        self.assertEqual(info["skipped"], {"image": 1, "doc": 1, "archive": 1})
        self.assertEqual(info["by_size"], 1)       # sample.mkv 30 MB
        self.assertEqual(info["kept"], 2)

    def test_all_categories_on_keeps_everything(self):
        idx, info = self.pick(PACK, video=True, subtitle=True, image=True,
                              doc=True, archive=True, other=True, min_size_mb=0)
        self.assertEqual(idx, [0, 1, 2, 3, 4, 5])
        self.assertEqual(info["kept"], 6)

    def test_image_alone(self):
        idx, _ = self.pick(PACK, video=False, subtitle=False, image=True,
                           min_size_mb=0)
        self.assertEqual(idx, [2])


class TestSize(FilterCase):
    def pick(self, files, **over):
        return self.dl(select_all=False, file_filter=self.filter(**over)
                       ).select_files(files)

    def test_small_video_dropped(self):
        idx, info = self.pick([seed(0, "big.mkv", 2 * GB),
                               seed(1, "small.mkv", 30 * MB)], video=True,
                              subtitle=False, min_size_mb=100)
        self.assertEqual(idx, [0])
        self.assertEqual(info["by_size"], 1)

    def test_subtitle_exempt_from_min_size(self):
        idx, info = self.pick([seed(0, "a.mkv", 2 * GB),
                               seed(1, "a.srt", 40 * KB)], min_size_mb=100)
        self.assertEqual(idx, [0, 1], "字幕不该被大小门槛删掉")
        self.assertEqual(info["by_size"], 0)

    def test_subtitle_can_be_squeezed_when_exempt_off(self):
        idx, info = self.pick([seed(0, "a.mkv", 2 * GB),
                               seed(1, "a.srt", 40 * KB)],
                              min_size_mb=100, subtitle_exempt_size=False)
        self.assertEqual(idx, [0])
        self.assertEqual(info["by_size"], 1)

    def test_max_size_drops_giant(self):
        idx, info = self.pick([seed(0, "huge.mkv", 80 * GB),
                               seed(1, "ok.mkv", 2 * GB)],
                              subtitle=False, min_size_mb=0, max_size_mb=10000)
        self.assertEqual(idx, [1])
        self.assertEqual(info["by_size"], 1)

    def test_unknown_size_is_not_dropped(self):
        # 拿不到大小就别猜它小，宁可多下
        idx, info = self.pick([seed(0, "a.mkv"), seed(1, "b.mkv", 2 * GB)],
                              min_size_mb=100)
        self.assertEqual(idx, [0, 1])
        self.assertEqual(info["by_size"], 0)


class TestName(FilterCase):
    def pick(self, files, **over):
        return self.dl(select_all=False, file_filter=self.filter(**over)
                       ).select_files(files)

    def test_exclude(self):
        idx, info = self.pick(PACK, name_exclude="sample,trailer", min_size_mb=0)
        self.assertNotIn(4, idx)
        self.assertEqual(info["by_name"], 1)

    def test_include_only(self):
        files = [seed(0, "show.1080p.mkv", 2 * GB),
                 seed(1, "show.2160p.mkv", 2 * GB)]
        idx, _ = self.pick(files, name_include="1080p", min_size_mb=0,
                           subtitle=False)
        self.assertEqual(idx, [0])

    def test_include_needs_all_words(self):
        files = [seed(0, "show.1080p.中字.mkv", 2 * GB),
                 seed(1, "show.1080p.mkv", 2 * GB)]
        idx, _ = self.pick(files, name_include="1080p,中字", min_size_mb=0,
                           subtitle=False)
        self.assertEqual(idx, [0])

    def test_exclude_wins_over_include(self):
        files = [seed(0, "show.1080p.sample.mkv", 2 * GB),
                 seed(1, "show.2160p.mkv", 2 * GB)]
        idx, info = self.pick(files, name_include="1080p",
                              name_exclude="sample", min_size_mb=0,
                              subtitle=False, fallback_all=False)
        self.assertEqual(idx, [])
        self.assertTrue(info["refused"])


class TestEmpty(unittest.TestCase):
    def setUp(self):
        self.case = FilterCase()
        self.case.setUp()

    def test_fallback_to_all_by_default(self):
        d = self.case.dl(select_all=False)      # 默认只留视频+字幕
        files = [seed(0, "setup.exe"), seed(1, "data.7z"), seed(2, "patch.rar")]
        idx, info = d.select_files(files)
        self.assertIsNone(idx)
        self.assertTrue(info["fallback"])
        self.assertEqual(info["kept"], 3)

    def test_fallback_sends_minus_one_not_empty(self):
        """空选择会建出没有文件的空壳任务，必须改成 -1。"""
        d = self.case.dl(select_all=False)
        res, idx = self.case.send(d, [seed(0, "setup.exe"), seed(1, "data.7z")])
        self.assertTrue(res.ok)
        self.assertEqual(idx, "-1")
        self.assertIn("fallback", res.raw.get("_pick") or {})

    def test_refuse_when_fallback_off(self):
        d = self.case.dl(select_all=False,
                         file_filter=self.case.filter(fallback_all=False))
        res = d._post_task(MAGNET, "t", "device#x",
                           [seed(0, "setup.exe"), seed(1, "data.7z")], {})
        self.assertFalse(res.ok)
        self.assertEqual(d.posted, [], "说好不投，就不该发请求")
        self.assertIn("按设置没有投递", res.message)

    def test_note_explains_fallback(self):
        note = pick_note({"total": 3, "kept": 3, "fallback": True})
        self.assertIn("全筛掉了", note)
        self.assertIn("全选", note)

    def test_note_explains_refuse(self):
        note = pick_note({"total": 3, "kept": 0, "refused": True})
        self.assertIn("没有投递", note)


class TestNote(unittest.TestCase):
    def test_partial_reads_like_a_sentence(self):
        note = pick_note({"total": 6, "kept": 2, "min_mb": 100,
                          "skipped": {"image": 1, "doc": 1, "archive": 1},
                          "by_size": 1})
        self.assertIn("6 个文件里挑了 2 个", note)
        self.assertIn("图片 1", note)
        self.assertIn("说明 1", note)
        self.assertIn("压缩包 1", note)
        self.assertIn("小于 100 MB 1", note)

    def test_nothing_to_say_when_all_kept(self):
        self.assertEqual(pick_note({"total": 6, "kept": 6}), "")

    def test_minimal_retry_is_admitted(self):
        note = pick_note({"total": 38, "kept": 38, "minimal": True})
        self.assertIn("筛选没生效", note)


class TestMinimalRetry(FilterCase):
    def test_filter_is_dropped_but_admitted(self):
        """精简请求会砍掉 sub_file_index，筛选等于没生效——回执必须说清楚。"""
        d = self.dl(select_all=False)
        res = d._post_task(MAGNET, "t", "device#x", PACK, {}, minimal=True)
        self.assertTrue(res.ok)
        self.assertNotIn("sub_file_index", (d.posted[-1] or {}).get("params") or {})
        self.assertTrue((res.raw.get("_pick") or {}).get("minimal"))
        self.assertIn("筛选没生效", pick_note(res.raw.get("_pick")))


class TestSubmitCarriesNote(unittest.TestCase):
    """筛选说明要一路带到 Telegram 回执，否则用户看不出筛选有没有生效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.data["download"]["xunlei"].update(
            {"mode": "panel", "pan_auth": "OK", "target": "device#x"})
        self.store = Store(os.path.join(self.tmp, "var", "t.db"))
        self.pipe = Pipeline(self.cfg, self.store)

    def test_pick_note_reaches_bundle(self):
        pick = {"total": 38, "kept": 12, "min_mb": 100,
                "skipped": {"image": 18, "doc": 6}, "by_size": 2}
        res = DownloadResult(True, "xunlei", "42", "已投递迅雷（12 / 38 个文件）",
                             {"_pick": pick})
        fake = mock.Mock()
        fake.name = "xunlei"
        fake.add = mock.Mock(return_value=res)
        with mock.patch("tmb.downloaders.chain", return_value=[fake]):
            out = self.pipe.submit({"title": "某合集", "magnet": MAGNET,
                                    "infohash": "b" * 40})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["pick"]["kept"], 12)
        self.assertIn("38 个文件里挑了 12 个", out["pick_note"])
        self.assertIn("图片 18", out["pick_note"])

    def test_reply_shows_the_note(self):
        bundle = {
            "rid": "r1", "ts": 0, "text": "", "mode": "magnet", "plan": {},
            "match": {"rule_name": "直接投递"}, "items": [], "submitted": [
                {"ok": True, "title": "某合集", "infohash": "b" * 40,
                 "backend": "xunlei", "task_id": "42",
                 "pick_note": "38 个文件里挑了 12 个（跳过：图片 18）"}],
            "error": None, "relaxed": False, "total": 1, "dropped": {},
        }
        text = self.pipe.format_direct(bundle)
        self.assertIn("挑了 12 个", text)
        self.assertIn("图片 18", text)

    def test_plain_result_has_no_extra_line(self):
        bundle = {
            "rid": "r1", "ts": 0, "text": "", "mode": "magnet", "plan": {},
            "match": {"rule_name": "直接投递"}, "items": [], "submitted": [
                {"ok": True, "title": "单文件", "infohash": "b" * 40,
                 "backend": "xunlei", "task_id": "42"}],
            "error": None, "relaxed": False, "total": 1, "dropped": {},
        }
        self.assertNotIn("挑了", self.pipe.format_direct(bundle))


if __name__ == "__main__":
    unittest.main()
