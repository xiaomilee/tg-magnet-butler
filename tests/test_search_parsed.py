"""面板「解析后搜」：先按机器人那套判定解析，再拿判定出的查询去站点搜。

两个不能错的地方：
1. 自带磁力的消息**一次检索都不该发**（跟机器人 handle_text 的优先级一致）；
2. 检索条件要用**规则里的**分类/排序，而不是面板「更多选项」里那几个框——
   否则面板上试出来的结果跟机器人真跑出来的对不上，这个按钮就白做了。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import intake, webui  # noqa: E402
from tmb.config import Config  # noqa: E402
from tmb.pipeline import Pipeline  # noqa: E402
from tmb.store import Store  # noqa: E402

HASH = "E8BE27D6280895F4838832CF84D90451B69B3AFC"
MAGNET = "magnet:?xt=urn:btih:" + HASH
MAGNET_TEXT = "FC2PPV-4999948\n拥有25万粉丝的知名网红\n" + MAGNET
FC2_TEXT = "FC2PPV-4999948 某作品 中字"
PLAIN_TEXT = "某作品名 1080p 中字"


class TestParsedSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.save_rules({"version": 1, "rules": [{
            "id": "video",
            "name": "有码视频",
            "enabled": True,
            "match": {"any": ["某作品名"]},
            "strip_words": ["中字"],
            "query_template": "{keywords}",
            "category": "2_2",
            "filter": "0",
            "sort": "date",
            "limit": 5,
            "min_seeders": 3,
            "min_size_mb": 0,
            "max_size_mb": 0,
            "exclude_words": [],
            "prefer_words": [],
            "auto_download": True,
            "pick": 1,
        }]})
        self.store = Store(os.path.join(self.tmp, "var", "t.sqlite3"))
        self.pipe = Pipeline(self.cfg, self.store)
        # 只用到 self.ctx.pipeline，绕开 BaseHTTPRequestHandler 的构造函数
        self.handler = webui.Handler.__new__(webui.Handler)
        self.handler.ctx = SimpleNamespace(pipeline=self.pipe, cfg=self.cfg, store=self.store)

    def tearDown(self):
        self.store.close()

    # ---------- 判定层 ----------
    def test_magnet_message_has_no_match(self):
        got = self.pipe.plan_parsed(MAGNET_TEXT)
        self.assertEqual(got["plan"]["mode"], intake.MODE_MAGNET)
        self.assertIsNone(got["match"], "自带磁力不该再去算检索查询")

    def test_fc2_query_is_code_only(self):
        got = self.pipe.plan_parsed(FC2_TEXT)
        self.assertEqual(got["plan"]["mode"], intake.MODE_FC2)
        self.assertEqual(got["match"].query, "4999948")

    def test_plain_text_uses_rule(self):
        got = self.pipe.plan_parsed(PLAIN_TEXT)
        self.assertEqual(got["plan"]["mode"], intake.MODE_SEARCH)
        self.assertEqual(got["match"].rule_id, "video")
        self.assertIn("某作品名", got["match"].query)

    # ---------- 接口层 ----------
    def test_magnet_message_never_searches(self):
        with mock.patch.object(self.pipe, "search",
                               side_effect=AssertionError("自带磁力不该检索")):
            out = self.handler._search_parsed({"text": MAGNET_TEXT})
        self.assertTrue(out["ok"])
        self.assertFalse(out["searched"])
        self.assertEqual(out["mode"], intake.MODE_MAGNET)
        self.assertEqual(len(out["magnets"]), 1)
        self.assertEqual(out["magnets"][0]["infohash"], HASH)
        self.assertIn("直接投递", out["note"])

    def test_plain_text_searches_with_rule_conditions(self):
        seen = {}

        def fake_search(query, **kw):
            seen["query"] = query
            seen.update(kw)
            return {"items": [], "total": 0, "kept": 0, "dropped": {}, "relaxed": False}

        with mock.patch.object(self.pipe, "search", side_effect=fake_search):
            out = self.handler._search_parsed({"text": PLAIN_TEXT, "limit": 5})

        self.assertTrue(out["searched"])
        self.assertIn("某作品名", seen["query"])
        # 关键：分类/排序来自规则，不是面板那四个框
        self.assertEqual(seen["category"], "2_2")
        self.assertEqual(seen["sort"], "date")
        self.assertEqual(seen["rule"].get("min_seeders"), 3)
        self.assertEqual(seen["limit"], 5)
        self.assertEqual(out["rule"]["id"], "video")
        self.assertEqual(out["rule"]["category"], "2_2")
        self.assertEqual(out["query"], seen["query"])

    def test_search_failure_keeps_parse_result(self):
        """站点抽风时，解析结果照样要给——否则用户分不清是「没解析出来」还是「没连上」。"""
        with mock.patch.object(self.pipe, "search", side_effect=RuntimeError("连接超时")):
            out = self.handler._search_parsed({"text": FC2_TEXT})
        self.assertTrue(out["ok"])
        self.assertFalse(out["searched"])
        self.assertIn("连接超时", out["search_error"])
        self.assertEqual(out["fc2"]["code"], "4999948")
        self.assertEqual(out["query"], "4999948")

    def test_empty_text_rejected(self):
        out = self.handler._search_parsed({"text": "   "})
        self.assertFalse(out["ok"])
        self.assertIn("先粘一段消息", out["error"])

    def test_intake_endpoint_shares_the_plan(self):
        """老接口 /api/intake 仍然只判定不发请求，但查询串也要跟着给全。"""
        with mock.patch.object(self.pipe, "search",
                               side_effect=AssertionError("不该检索")):
            out = self.handler._intake({"text": PLAIN_TEXT})
        self.assertTrue(out["ok"])
        self.assertIn("某作品名", out["query"])
        self.assertEqual(out["rule"]["id"], "video")


if __name__ == "__main__":
    unittest.main()
