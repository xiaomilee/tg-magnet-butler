"""消息前置解析（磁力直达 / FC2 编号 / 整段检索）的回归测试。

用户给的真实样本：

    FC2PPV-4999948
    拥有25万粉丝的知名网红
    magnet:?xt=urn:btih:E8BE27D6280895F4838832CF84D90451B69B3AFC

这条消息同时具备「磁力」和「FC2」两个特征，必须按优先级走磁力直达。
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
from tmb.pipeline import Pipeline                         # noqa: E402
from tmb.store import Store                               # noqa: E402

HASH = "E8BE27D6280895F4838832CF84D90451B69B3AFC"
MAGNET = "magnet:?xt=urn:btih:" + HASH
USER_TEXT = ("FC2PPV-4999948\n拥有25万粉丝的知名网红\n" + MAGNET)


class TestIntakeParse(unittest.TestCase):
    def test_user_sample_takes_magnet_branch(self):
        plan = intake.parse(USER_TEXT)
        self.assertEqual(plan["mode"], intake.MODE_MAGNET)
        self.assertEqual(len(plan["magnets"]), 1)
        self.assertIn(HASH.lower(), plan["magnets"][0].lower())
        # FC2 被识别出来但明确标注「本次不用」
        self.assertEqual(plan["fc2"]["code"], "4999948")
        self.assertIn("FC2", plan["reason"])

    def test_fc2_without_magnet_searches_by_code(self):
        plan = intake.parse("FC2PPV-4999948 拥有25万粉丝的知名网红")
        self.assertEqual(plan["mode"], intake.MODE_FC2)
        self.assertEqual(plan["fc2"]["code"], "4999948")
        self.assertEqual(plan["magnets"], [])

    def test_fc2_code_variants(self):
        cases = {
            "FC2-PPV-1234567": "1234567",
            "fc2 ppv 1234567": "1234567",
            "FC2PPV_7654321 4K": "7654321",
            "【FC2PPV-4999948】": "4999948",
            "FC2 123456": "123456",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(intake.find_fc2(text)["code"], want)

    def test_fc2_prefers_seven_digit_code(self):
        hit = intake.find_fc2("FC2 123456 再看 FC2PPV-2468135")
        self.assertEqual(hit["code"], "2468135")

    def test_fc2_word_without_code_falls_back(self):
        plan = intake.parse("FC2 新作 有人收吗")
        self.assertEqual(plan["mode"], intake.MODE_SEARCH)
        self.assertTrue(plan["has_fc2"])
        self.assertIsNone(plan["fc2"])
        self.assertIn("没跟编号", plan["reason"])

    def test_plain_text_searches(self):
        plan = intake.parse("拥有25万粉丝的知名网红")
        self.assertEqual(plan["mode"], intake.MODE_SEARCH)
        self.assertFalse(plan["has_fc2"])

    def test_magnet_trailing_chinese_and_html_entity(self):
        text = ("看这个 magnet:?xt=urn:btih:%s&amp;dn=%%E6%%B5%%8B%%E8%%AF%%95"
                "&tr=udp://tracker.opentrackr.org:1337/announce，麻烦下一下" % HASH)
        plan = intake.parse(text)
        self.assertEqual(plan["mode"], intake.MODE_MAGNET)
        mag = plan["magnets"][0]
        self.assertNotIn("&amp;", mag)
        self.assertNotIn("，", mag)
        self.assertTrue(mag.endswith("/announce"))
        self.assertEqual(intake.title_from_magnet(mag), "测试")

    def test_uppercase_scheme_normalized(self):
        plan = intake.parse("MAGNET:?XT=urn:btih:" + HASH.lower())
        self.assertEqual(plan["mode"], intake.MODE_MAGNET)
        self.assertTrue(plan["magnets"][0].startswith("magnet:?xt=urn:btih:"))

    def test_duplicate_and_multiple_magnets(self):
        other = "1" * 40
        text = "\n".join([MAGNET, MAGNET.lower(), "magnet:?xt=urn:btih:" + other])
        plan = intake.parse(text)
        self.assertEqual(len(plan["magnets"]), 2)   # 同一个 infohash 只算一次
        capped = intake.parse(text, max_magnets=1)
        self.assertEqual(len(capped["magnets"]), 1)

    def test_truncated_magnet_is_reported_not_submitted(self):
        plan = intake.parse("截断的 magnet:?xt=urn:btih:E8BE27D6 应该是废的")
        self.assertEqual(plan["mode"], intake.MODE_SEARCH)
        self.assertEqual(plan["magnets"], [])
        self.assertEqual(len(plan["bad_magnets"]), 1)
        self.assertIn("忽略", intake.describe(plan))

    def test_guess_title_joins_lines(self):
        title = intake.guess_title(USER_TEXT, [MAGNET])
        self.assertIn("FC2PPV-4999948", title)
        self.assertIn("拥有25万粉丝的知名网红", title)


class TestIntakePipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.store = Store(os.path.join(self.tmp, "var", "t.sqlite3"))
        self.pipe = Pipeline(self.cfg, self.store)

    def tearDown(self):
        self.store.close()

    def _fake_submit(self, item, backend=None):
        return {"ok": True, "title": item["title"], "infohash": item["infohash"],
                "magnet": item["magnet"], "backend": "xunlei", "task_id": "t1",
                "message": "", "deduped": False}

    def test_magnet_branch_never_searches(self):
        """自带磁力就不该发检索请求——联网失败也不该影响投递。"""
        with mock.patch.object(self.pipe, "search",
                               side_effect=AssertionError("不该检索")), \
                mock.patch.object(self.pipe, "submit", side_effect=self._fake_submit):
            bundle = self.pipe.handle_text(USER_TEXT, chat_id=1)

        self.assertEqual(bundle["mode"], intake.MODE_MAGNET)
        self.assertTrue(bundle["ok"])
        self.assertEqual(len(bundle["submitted"]), 1)
        self.assertEqual(bundle["submitted"][0]["infohash"], HASH)
        self.assertEqual(bundle["items"], [])            # 不留「⬇️ N」按钮
        self.assertIsNone(self.pipe.keyboard(bundle))

        text = self.pipe.format_bundle(bundle)
        self.assertIn("直接投递", text)
        self.assertIn("已投递", text)
        self.assertIn(HASH, text)
        self.assertIn("成功 1 / 1", text)
        self.assertIn("FC2PPV-4999948", text)            # 说明为什么不用编号

        # 投递被 mock 掉了，落库由 submit 负责；这里只核对留痕
        events = [str(e) for e in self.store.recent_events(5)]
        self.assertTrue(any("磁力直达" in e for e in events), events)

    def test_magnet_branch_reports_failure(self):
        bad = {"ok": False, "title": "x", "infohash": HASH, "magnet": MAGNET,
               "backend": "", "task_id": "", "message": "未配置下载器", "deduped": False}
        with mock.patch.object(self.pipe, "submit", return_value=bad):
            bundle = self.pipe.handle_text(USER_TEXT)
        self.assertFalse(bundle["ok"])
        text = self.pipe.format_bundle(bundle)
        self.assertIn("未配置下载器", text)
        self.assertIn("成功 0 / 1", text)

    def test_fc2_branch_query_is_code_only(self):
        seen = {}

        def fake_search(query, **kw):
            seen["query"] = query
            seen["kw"] = kw
            return {"items": [], "relaxed": False, "total": 0, "dropped": {}}

        with mock.patch.object(self.pipe, "search", side_effect=fake_search):
            bundle = self.pipe.handle_text("FC2PPV-4999948 拥有25万粉丝的知名网红")

        self.assertEqual(seen["query"], "4999948")       # 只发编号
        self.assertEqual(bundle["mode"], intake.MODE_FC2)
        self.assertEqual(bundle["match"]["rule_id"], "fc2")
        self.assertIn("FC2", self.pipe.format_bundle(bundle))

    def test_fc2_rule_keeps_its_own_filters(self):
        rule = self.pipe.fc2_match(intake.parse("FC2PPV-1234567"))
        self.assertEqual(rule.rule_id, "fc2")
        self.assertEqual(rule.rule.get("limit"), 8)
        self.assertEqual(rule.query, "1234567")

    def test_fc2_without_rule_still_works(self):
        """用户把 fc2 规则删了也要能跑：退回兜底规则，query 仍是编号。"""
        self.cfg.rules["rules"] = [r for r in self.cfg.rules["rules"]
                                   if r.get("id") != "fc2"]
        self.pipe.reload_rules()
        match = self.pipe.fc2_match(intake.parse("FC2PPV-1234567"))
        self.assertTrue(match.is_default)
        self.assertEqual(match.query, "1234567")

    def test_plain_text_branch_unchanged(self):
        with mock.patch.object(self.pipe, "search") as s:
            s.return_value = {"items": [], "relaxed": False, "total": 0, "dropped": {}}
            bundle = self.pipe.handle_text("求个番剧 某作品 1080p")
        self.assertEqual(bundle["mode"], intake.MODE_SEARCH)
        self.assertIn("🎯", self.pipe.format_bundle(bundle))
        self.assertTrue(s.call_args[0][0])


class TestRuleMigration(unittest.TestCase):
    def test_old_rules_json_gets_fc2_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            etc, var = os.path.join(tmp, "etc"), os.path.join(tmp, "var")
            os.makedirs(etc)
            os.makedirs(var)
            from tmb import util
            util.save_json(os.path.join(etc, "rules.json"), {
                "version": 1,
                "default": {"name": "通用兜底"},
                "rules": [{"id": "mine", "name": "我的规则", "match": {"any": ["x"]}}],
            }, 0o644)
            cfg = Config(etc, var).load()
            ids = [r.get("id") for r in cfg.rules["rules"]]
            self.assertIn("mine", ids)                 # 用户自己的规则原样保留
            self.assertIn("fc2", ids)                  # 新内置规则补进来
            self.assertEqual(ids[0], "mine")           # 顺序不变
            self.assertEqual(cfg.rules["version"], 2)


class TestDownloaderChain(unittest.TestCase):
    def test_empty_fallback_means_no_fallback(self):
        """面板里把兜底列表清空要真的生效，不能又冒出 aria2/qB。"""
        from tmb import downloaders
        chain = downloaders.chain({"download": {"backend": "xunlei", "fallback": []}})
        self.assertEqual([d.name for d in chain], ["xunlei"])

    def test_missing_fallback_uses_default(self):
        from tmb import downloaders
        chain = downloaders.chain({"download": {"backend": "aria2"}})
        self.assertEqual([d.name for d in chain], ["aria2", "qbittorrent"])


if __name__ == "__main__":
    unittest.main()
