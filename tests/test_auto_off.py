"""关掉「自动投递」之后，到底哪些环节还会发生。

用户问过：「关闭自动投递，信息流转到哪里？迅雷上有操作什么吗？」
以前的答案是「检索照做、迅雷不动」——但**消息自带磁力时会直接投递**，
开关对它完全无效。那等于关了个假开关。这里把四个组合都钉住：

| 全局开关 | 消息形态 | 检索 | 投迅雷 | 回复 |
| --- | --- | --- | --- | --- |
| 关 | 普通文字 | 做 | 不做 | 结果列表 + 「自动投递已关闭」 |
| 关 | 自带磁力 | 不做 | 不做 | 链接 + ⬇️ 按钮，说明还没进迅雷 |
| 开 | 普通文字、规则关 | 做 | 不做 | 结果列表 + 指明是哪条规则 |
| 开 | 自带磁力 | 不做 | 做 | 投递结果 |
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
from tmb.analyzer import pick                             # noqa: E402
from tmb.config import Config                             # noqa: E402
from tmb.pipeline import Pipeline                         # noqa: E402
from tmb.store import Store                               # noqa: E402

HASH = "B" * 40
MAGNET = "magnet:?xt=urn:btih:" + HASH
ITEM = {"title": "XYZ-456 测试 1080p 中文字幕",
        "magnet": "magnet:?xt=urn:btih:" + "A" * 40,
        "infohash": "a" * 40, "seeders": 50, "downloads": 10,
        "size_human": "1.2 GB", "age_human": "1 天", "score": 9.0,
        "reasons": [], "trusted": False}


class _Base(unittest.TestCase):
    auto = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.cfg.apply_patch({"download": {"auto_download": self.auto}})
        self.store = Store(os.path.join(self.tmp, "var", "t.sqlite3"))
        self.pipe = Pipeline(self.cfg, self.store)
        self.submitted = []
        self.searched = []

    def tearDown(self):
        self.store.close()

    # ---- 假检索 / 假投递 ----
    def _search(self, query, **kw):
        self.searched.append(query)
        return {"items": [dict(ITEM)], "relaxed": False, "total": 1, "dropped": {}}

    def _submit(self, item, backend=None):
        self.submitted.append(item.get("title"))
        return {"ok": True, "title": item.get("title"), "infohash": item.get("infohash"),
                "magnet": item.get("magnet"), "backend": "xunlei", "task_id": "t-1",
                "message": "", "deduped": False}

    def _text(self, text, **kw):
        with mock.patch.object(self.pipe, "search", side_effect=self._search), \
                mock.patch.object(self.pipe, "submit", side_effect=self._submit):
            return self.pipe.handle_text(text, chat_id=1, **kw)

    def _magnet(self, text=MAGNET, **kw):
        with mock.patch.object(self.pipe, "search", side_effect=self._search), \
                mock.patch.object(self.pipe, "submit", side_effect=self._submit):
            return self.pipe.handle_text(text, chat_id=1, **kw)


class TestAutoOffPlainText(_Base):
    """开关关掉、发一段普通文字：只检索，不碰迅雷。"""

    auto = False

    def test_still_searches_so_you_can_see_results(self):
        b = self._text("求 XYZ-456 1080p 中字")
        self.assertEqual(self.searched, ["XYZ-456 1080p 中字"])
        self.assertEqual(len(b["items"]), 1)

    def test_never_touches_xunlei(self):
        b = self._text("求 XYZ-456 1080p 中字")
        self.assertEqual(self.submitted, [])
        self.assertEqual(b["submitted"], [])
        self.assertEqual(self.store.recent_downloads(10), [])   # 「最近投递」保持空
        self.assertEqual(self.store.stats()["total"], 0)

    def test_reply_says_why_nothing_was_downloaded(self):
        text = self.pipe.format_bundle(self._text("求 XYZ-456 1080p 中字"))
        self.assertIn("自动投递已关闭", text)
        self.assertIn("⬇️", text)                # 明确告诉人怎么补投
        self.assertNotIn("已投递", text)

    def test_buttons_still_there(self):
        b = self._text("求 XYZ-456 1080p 中字")
        kb = self.pipe.keyboard(b)
        self.assertIsNotNone(kb)
        datas = [x["callback_data"] for row in kb["inline_keyboard"] for x in row]
        self.assertIn("dl:%s:1" % b["rid"], datas)

    def test_search_is_recorded_for_the_buttons(self):
        b = self._text("求 XYZ-456 1080p 中字")
        rec = self.store.get_search(b["rid"])
        self.assertIsNotNone(rec)
        self.assertEqual(len(rec["payload"]["items"]), 1)
        self.assertEqual(pick(rec["payload"]["items"], 1)["infohash"], "a" * 40)


class TestAutoOffMagnet(_Base):
    """开关关掉、消息里自带磁力：这条以前会绕过开关直接下，现在必须停下。"""

    auto = False

    def test_does_not_submit(self):
        b = self._magnet()
        self.assertEqual(self.submitted, [])
        self.assertEqual(b["submitted"], [])
        self.assertFalse(b["ok"])
        self.assertTrue(b["auto_off"])

    def test_no_download_row(self):
        self._magnet()
        self.assertEqual(self.store.recent_downloads(10), [])

    def test_magnet_is_kept_with_a_button_so_it_is_not_lost(self):
        b = self._magnet()
        self.assertEqual(len(b["items"]), 1)
        self.assertEqual(b["items"][0]["infohash"].lower(), HASH.lower())
        kb = self.pipe.keyboard(b)
        datas = [x["callback_data"] for row in kb["inline_keyboard"] for x in row]
        self.assertEqual(datas, ["dl:%s:1" % b["rid"]])   # 只有补投，没有「重搜」

        # 按钮点下去要能真的取到这条
        rec = self.store.get_search(b["rid"])
        self.assertEqual(pick(rec["payload"]["items"], 1)["magnet"], MAGNET)

    def test_reply_states_it_is_not_in_xunlei_yet(self):
        text = self.pipe.format_direct(self._magnet())
        self.assertIn("自动投递已关闭", text)
        self.assertIn("还没进迅雷", text)
        self.assertIn("⬇️ 1", text)
        self.assertNotIn("成功 0", text)          # 不是「失败」，是「等你点」

    def test_event_log_is_honest(self):
        self._magnet()
        kinds = [e["text"] for e in self.store.recent_events(5)]
        self.assertTrue(any("等确认" in k for k in kinds), kinds)


class TestRuleLevelOff(_Base):
    """全局开着、某条规则自己关了：这一条只列结果，并说清是哪条规则。"""

    auto = True

    def test_rule_switch_wins(self):
        rules = self.cfg.rules
        rules["rules"][0]["auto_download"] = False
        self.cfg.save_rules(rules)
        self.pipe.reload_rules()

        # 让这条消息命中第 0 条规则
        word = (rules["rules"][0].get("match") or {}).get("any")[0]
        b = self._text("求 %s 1080p" % word)
        self.assertEqual(b["match"]["rule_id"], rules["rules"][0]["id"])
        self.assertEqual(self.submitted, [])
        text = self.pipe.format_bundle(b)
        self.assertIn("关掉了自动投递", text)
        self.assertIn(rules["rules"][0].get("name"), text)


class TestAutoOn(_Base):
    """开关开着：行为保持原样，别把正常路径改坏。"""

    auto = True

    def test_magnet_submits(self):
        b = self._magnet()
        self.assertEqual(len(self.submitted), 1)
        self.assertTrue(b["ok"])
        self.assertEqual(b["items"], [])          # 投过了就不再挂按钮
        self.assertIsNone(self.pipe.keyboard(b))

    def test_plain_text_submits_pick_one(self):
        b = self._text("求 XYZ-456 1080p 中字")
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(b["submitted"][0]["index"], 1)
        self.assertNotIn("自动投递已关闭", self.pipe.format_bundle(b))


class TestExplicitOverride(_Base):
    """auto 参数是给「这一次就要投」用的（面板手动、以后的 /now 之类）。"""

    auto = False

    def test_auto_true_overrides_config(self):
        b = self._text("求 XYZ-456 1080p 中字", auto=True)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(len(b["submitted"]), 1)

    def test_auto_false_overrides_config(self):
        self.cfg.apply_patch({"download": {"auto_download": True}})
        b = self._text("求 XYZ-456 1080p 中字", auto=False)
        self.assertEqual(self.submitted, [])
        self.assertTrue(b["auto_off"])


class TestMagnetModeDetection(unittest.TestCase):
    """磁力消息走的是 handle_magnets，别再顺手去检索一遍。"""

    def test_mode_is_magnet(self):
        self.assertEqual(intake.parse(MAGNET)["mode"], intake.MODE_MAGNET)


if __name__ == "__main__":
    unittest.main()
