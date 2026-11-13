"""核心逻辑单元测试：python -m unittest discover -s tests"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tmb import analyzer, nyaa, util                       # noqa: E402
from tmb.classifier import (Router, extract_keywords, normalize,   # noqa: E402
                            title_match_ratio)
from tmb.config import Config, default_rules               # noqa: E402
from tmb.downloaders.xunlei import _find_target, _flatten_files  # noqa: E402
from tmb.pipeline import Pipeline                          # noqa: E402
from tmb.store import Store                                # noqa: E402

RSS_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:nyaa="https://sukebei.nyaa.si/xmlns/nyaa">
<channel>
<title>sukebei</title>
<item>
  <title>Some Release 1080p [Group]</title>
  <link>https://sukebei.nyaa.si/download/1001.torrent</link>
  <guid isPermaLink="true">https://sukebei.nyaa.si/view/1001</guid>
  <pubDate>Sun, 01 Nov 2026 10:00:00 -0000</pubDate>
  <nyaa:seeders>42</nyaa:seeders>
  <nyaa:leechers>3</nyaa:leechers>
  <nyaa:downloads>1234</nyaa:downloads>
  <nyaa:infoHash>ABCDEF0123456789ABCDEF0123456789ABCDEF01</nyaa:infoHash>
  <nyaa:categoryId>1_1</nyaa:categoryId>
  <nyaa:category>Art - Anime</nyaa:category>
  <nyaa:size>1.2 GiB</nyaa:size>
  <nyaa:trusted>No</nyaa:trusted>
  <nyaa:remake>No</nyaa:remake>
</item>
<item>
  <title>Some Release 1080p duplicate</title>
  <guid>https://sukebei.nyaa.si/view/1002</guid>
  <nyaa:seeders>5</nyaa:seeders>
  <nyaa:downloads>10</nyaa:downloads>
  <nyaa:infoHash>abcdef0123456789abcdef0123456789abcdef01</nyaa:infoHash>
  <nyaa:size>1.2 GiB</nyaa:size>
</item>
</channel>
</rss>
"""

HTML_SAMPLE = """
<table class="table torrent-list"><tbody>
<tr class="default">
  <td class="text-center"><a href="/?c=1_1"><img src="/static/img/icons/nyaa/1_1.png"></a></td>
  <td colspan="2"><a href="/view/2001" title="Another &amp; Release 720p">Another &amp; Release 720p</a>
      <a class="comments" href="/view/2001#comments">3</a></td>
  <td class="text-center"><a href="/download/2001.torrent"><i class="fa fa-download"></i></a></td>
  <td class="text-center" data-timestamp="1761984000">2026-11-01</td>
  <td class="text-center">800.0 MiB</td>
  <td class="text-center">17</td>
  <td class="text-center">2</td>
  <td class="text-center">120</td>
  <td class="text-center"><a href="magnet:?xt=urn:btih:1111222233334444555566667777888899990000&dn=x">Magnet</a></td>
</tr>
</tbody></table>
"""


# 取自 nyaa/sukebei 真实模板（nyaa/templates/search_results.html）渲染出来的结构：
#   绿底 = tr class="success" = trusted 上传者；红 = danger(remake)；灰 = deleted。
# Link 列里 magnet 在前、.torrent 在后，且 & 被 Jinja 转义成 &amp;。
HTML_REAL = """
<table class="table table-bordered table-hover table-striped torrent-list"><tbody>
<tr class="default">
  <td><a href="/?c=1_1" title="Art - Anime"><img src="/static/img/icons/sukebei/1_1.png"></a></td>
  <td colspan="2"><a href="/view/3001" title="Shop Title A 1080p">Shop Title A 1080p</a></td>
  <td class="text-center">
    <a href="/download/3001.torrent"><i class="fa fa-fw fa-download"></i></a>
    <a href="magnet:?xt=urn:btih:aaaabbbbccccddddeeeeffff0000111122223333&amp;dn=Shop%20Title%20A&amp;tr=udp%3A%2F%2Ft1.example%3A80"><i class="fa fa-fw fa-magnet"></i></a>
  </td>
  <td class="text-center">1.2 GiB</td>
  <td class="text-center" data-timestamp="1762000000">2026-11-01 10:00</td>
  <td class="text-center">4</td>
  <td class="text-center">0</td>
  <td class="text-center">11</td>
</tr>
<tr class="success">
  <td><a href="/?c=1_1" title="Art - Anime"><img src="/static/img/icons/sukebei/1_1.png"></a></td>
  <td colspan="2"><a href="/view/3002" title="+++ Shop Title A 1080p trusted">+++ Shop Title A 1080p trusted</a></td>
  <td class="text-center">
    <a href="/download/3002.torrent"><i class="fa fa-fw fa-download"></i></a>
    <a href="magnet:?xt=urn:btih:1111222233334444555566667777888899990000&amp;dn=%2B%2B%2B%20Shop&amp;tr=udp%3A%2F%2Ft2.example%3A80"><i class="fa fa-fw fa-magnet"></i></a>
  </td>
  <td class="text-center">2.4 GiB</td>
  <td class="text-center" data-timestamp="1762100000">2026-11-02 10:00</td>
  <td class="text-center">9</td>
  <td class="text-center">1</td>
  <td class="text-center">2</td>
</tr>
<tr class="danger">
  <td><a href="/?c=1_2" title="Art - Doujinshi"><img src="/static/img/icons/sukebei/1_2.png"></a></td>
  <td colspan="2"><a href="/view/3003" title="Batch Remake">Batch Remake</a></td>
  <td class="text-center"><a href="magnet:?xt=urn:btih:9999888877776666555544443333222211110000&amp;dn=Batch"><i class="fa fa-magnet"></i></a></td>
  <td class="text-center">4.0 GiB</td>
  <td class="text-center" data-timestamp="1762200000">2026-11-03 10:00</td>
  <td class="text-center">30</td>
  <td class="text-center">5</td>
  <td class="text-center">88</td>
</tr>
<tr class="deleted">
  <td><a href="/?c=1_2"><img src="/static/img/icons/sukebei/1_2.png"></a></td>
  <td colspan="2"><a href="/view/3004" title="Deleted One">Deleted One</a></td>
  <td class="text-center"><a href="magnet:?xt=urn:btih:1234123412341234123412341234123412341234&amp;dn=Gone"><i class="fa fa-magnet"></i></a></td>
  <td class="text-center">1.0 GiB</td>
  <td class="text-center" data-timestamp="1762300000">2026-11-04 10:00</td>
  <td class="text-center">2</td>
  <td class="text-center">0</td>
  <td class="text-center">1</td>
</tr>
</tbody></table>
"""

class TestUtil(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(util.parse_size("1.2 GiB"), int(1.2 * 1024 ** 3))
        self.assertEqual(util.parse_size("800.0 MiB"), int(800 * 1024 ** 2))
        self.assertEqual(util.parse_size(""), 0)
        self.assertEqual(util.parse_size("???"), 0)

    def test_human_size(self):
        self.assertEqual(util.human_size(1024 ** 3), "1 GiB")

    def test_deep_merge_and_path(self):
        base = {"a": {"b": 1, "c": 2}}
        merged = util.deep_merge(base, {"a": {"c": 3}})
        self.assertEqual(merged["a"], {"b": 1, "c": 3})
        self.assertEqual(base["a"]["c"], 2)

    def test_esc(self):
        self.assertEqual(util.esc("<a>&"), "&lt;a&gt;&amp;")


class TestClassifier(unittest.TestCase):
    def test_normalize_strips_url_and_emoji(self):
        out = normalize("帮我下这个 https://x.com/a 😀 【某作品】 1080p")
        self.assertNotIn("http", out)
        self.assertNotIn("😀", out)
        self.assertIn("某作品", out)

    def test_extract_keywords(self):
        kw = extract_keywords("求 帮我下 某作品 1080p 中字 谢谢")
        self.assertIn("某作品", kw)
        self.assertIn("1080p", kw)
        self.assertNotIn("谢谢", kw)

    def test_intent_words_fully_stripped(self):
        """「能不能帮我找一下」这类整串口语不该留下「我找」之类的碎词。"""
        kw = extract_keywords("能不能帮我找一下 书名 Vol.2")
        self.assertEqual(kw, ["书名", "Vol.2"])

    def test_title_with_leading_verb_kept(self):
        """单字前缀不剥离，避免把「下流梗」「求生之路」削掉首字。"""
        self.assertIn("下流梗不存在的灰暗世界",
                      extract_keywords("下流梗不存在的灰暗世界 1080p"))
        self.assertIn("求生之路", extract_keywords("求生之路 全集"))

    def test_router_match(self):
        router = Router(default_rules())
        m = router.route("求个番剧 某作品 1080p")
        self.assertEqual(m[0].rule_id, "show")
        self.assertTrue(m[0].query)
        self.assertIn("某作品", m[0].query)

    def test_router_default(self):
        router = Router({"rules": [], "default": {"query_template": "{raw}"}})
        m = router.route("完全没有命中词的字符串")
        self.assertTrue(m[0].is_default)
        self.assertEqual(m[0].query, "完全没有命中词的字符串")

    def test_title_ratio(self):
        self.assertEqual(title_match_ratio(["abc"], "xx ABC yy"), 1.0)
        self.assertEqual(title_match_ratio(["abc", "zzz"], "abc"), 0.5)
        self.assertEqual(title_match_ratio([], "abc"), 1.0)


class TestNyaa(unittest.TestCase):
    def test_parse_rss(self):
        items = nyaa.parse_rss(RSS_SAMPLE)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["seeders"], 42)
        self.assertEqual(first["size_bytes"], int(1.2 * 1024 ** 3))
        self.assertEqual(first["id"], "1001")
        self.assertTrue(first["infohash"].isupper())
        self.assertEqual(first["category_id"], "1_1")
        self.assertGreater(first["pub_ts"], 1700000000)

    def test_parse_html(self):
        items = nyaa.parse_html(HTML_SAMPLE)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["id"], "2001")
        self.assertEqual(it["seeders"], 17)
        self.assertEqual(it["leechers"], 2)
        self.assertEqual(it["downloads"], 120)
        self.assertIn("Another & Release", it["title"])
        self.assertEqual(it["pub_ts"], 1761984000)
        self.assertTrue(it["magnet"].startswith("magnet:?"))

    def test_parse_html_green_row_and_magnet(self):
        items = nyaa.parse_html(HTML_REAL)
        self.assertEqual(len(items), 3)          # deleted 行被跳过
        by_id = {it["id"]: it for it in items}

        green = by_id["3002"]
        self.assertTrue(green["trusted"])        # 绿底 = trusted 上传者
        self.assertEqual(green["infohash"],
                         "1111222233334444555566667777888899990000")
        # 关键：magnet 必须反转义，否则 dn / tr 参数全废
        self.assertNotIn("&amp;", green["magnet"])
        self.assertIn("&dn=", green["magnet"])
        self.assertTrue(green["magnet"].startswith("magnet:?xt=urn:btih:"))

        self.assertFalse(by_id["3001"]["trusted"])
        self.assertTrue(by_id["3003"]["remake"])
        self.assertEqual(by_id["3003"]["seeders"], 30)
        self.assertIn("/download/3002.torrent", green["torrent_url"])

    def test_finalize_absolutizes_torrent_url(self):
        items = nyaa.finalize(nyaa.parse_html(HTML_REAL), [], "https://sukebei.nyaa.si")
        green = [it for it in items if it["id"] == "3002"][0]
        self.assertTrue(green["torrent_url"].startswith("https://sukebei.nyaa.si/download/"))
        self.assertTrue(green["view_url"].startswith("https://sukebei.nyaa.si/view/"))

    def test_magnet_hash_forms(self):
        hex40 = "aaaabbbbccccddddeeeeffff0000111122223333"
        self.assertEqual(nyaa.magnet_hash("magnet:?xt=urn:btih:" + hex40.upper()),
                         hex40.upper())
        # 太短的不是 infohash，不要当命中
        self.assertEqual(nyaa.magnet_hash("magnet:?xt=urn:btih:ABC123"), "")
        base32 = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U"
        self.assertEqual(nyaa.magnet_hash("magnet:?xt=urn:btih:" + base32.lower()),
                         base32.upper())
        self.assertEqual(nyaa.magnet_hash(""), "")

    def test_finalize_dedup_and_magnet(self):
        items = nyaa.finalize(nyaa.parse_rss(RSS_SAMPLE), ["udp://t.example:80/announce"])
        self.assertEqual(len(items), 1)  # 同一 infohash 去重
        self.assertIn("btih:ABCDEF", items[0]["magnet"])
        self.assertIn("&tr=", items[0]["magnet"])

    def test_build_url(self):
        cfg = {"base_url": "https://sukebei.nyaa.si"}
        url = nyaa.build_search_url(cfg, "测试 关键词", "1_2", "0", "seeders", rss=True)
        self.assertIn("page=rss", url)
        self.assertIn("c=1_2", url)
        self.assertIn("q=", url)


class TestProxyWiring(unittest.TestCase):
    """曾经的真 bug：pipeline 只传 nyya 段，而代码写的是 cfg.get("nyaa.proxy")，
    点号路径在普通 dict 上永远取不到，导致配了代理也一直直连。"""

    def setUp(self):
        self._orig = util.http_request
        self.seen = []

    def tearDown(self):
        util.http_request = self._orig
        os.environ.pop("TMP_NO_PROXY_TEST", None)

    def _patch(self):
        def fake(url, **kw):
            self.seen.append(kw.get("proxy"))
            return 200, RSS_SAMPLE, b""
        util.http_request = fake

    def test_proxy_from_nyaa_section(self):
        self._patch()
        cfg = {"base_url": "https://sukebei.nyaa.si", "proxy": "http://10.0.0.2:7890",
               "timeout": 5}
        nyaa.search(cfg, "q", limit=1)
        self.assertTrue(self.seen)
        self.assertEqual(self.seen[0], "http://10.0.0.2:7890")

    def test_proxy_falls_back_to_telegram(self):
        self._patch()
        cfg = {"nyaa": {"base_url": "https://sukebei.nyaa.si", "timeout": 5},
               "telegram": {"proxy": "http://10.0.0.3:7890"}}
        nyaa.search(cfg, "q", limit=1)
        self.assertEqual(self.seen[0], "http://10.0.0.3:7890")

    def test_explicit_proxy_wins(self):
        self._patch()
        cfg = {"base_url": "https://sukebei.nyaa.si", "timeout": 5}
        nyaa.search(cfg, "q", limit=1, proxy="http://10.0.0.9:1080")
        self.assertEqual(self.seen[0], "http://10.0.0.9:1080")

    def test_socks_rejected_early(self):
        self._patch()
        cfg = {"base_url": "https://sukebei.nyaa.si", "timeout": 5}
        with self.assertRaises(util.HttpError) as ctx:
            nyaa.search(cfg, "q", limit=1, proxy="socks5://127.0.0.1:1080")
        self.assertIn("socks", str(ctx.exception))
        self.assertEqual(self.seen, [])  # 根本没发请求


class TestNetHelpers(unittest.TestCase):
    def test_parse_proxy(self):
        self.assertEqual(util.parse_proxy("http://1.2.3.4:7890"), ("http", "1.2.3.4", 7890))
        self.assertEqual(util.parse_proxy("1.2.3.4:7890"), ("http", "1.2.3.4", 7890))
        self.assertEqual(util.parse_proxy("socks5://1.2.3.4:1080")[0], "socks5")
        self.assertIsNone(util.parse_proxy(""))

    def test_local_url(self):
        self.assertTrue(util.is_local_url("http://127.0.0.1:2345/drive/v1/task"))
        self.assertTrue(util.is_local_url("http://localhost:6800/jsonrpc"))
        self.assertTrue(util.is_local_url("http://192.168.1.10:8080"))
        self.assertFalse(util.is_local_url("https://sukebei.nyaa.si/"))

    def test_explain_refused(self):
        import errno
        exc = OSError(errno.ECONNREFUSED, "Connection refused")
        text = util.explain_net(exc, "http://127.0.0.1:7890")
        self.assertIn("连接被拒绝", text)
        self.assertIn("7890", text)

    def test_explain_short_drops_proxy_clause(self):
        # 直连本机下载器时不该提代理，否则用户会去查代理白费功夫
        import errno
        exc = OSError(errno.ECONNREFUSED, "Connection refused")
        text = util.explain_net_short(exc, "")
        self.assertIn("连接被拒绝", text)
        self.assertNotIn("代理", text)
        self.assertTrue(text.endswith("。"))

    def test_explain_tls_eof_via_proxy_points_at_the_line(self):
        """经代理握手被关：别劝人去设 TMB_INSECURE_TLS，那是 CA 的解法，白折腾。"""
        import ssl as _ssl
        exc = _ssl.SSLEOFError("EOF occurred in violation of protocol (_ssl.c:992)")
        text = util.explain_net(exc, "http://192.168.1.11:7890")
        self.assertIn("握手中断", text)
        self.assertIn("192.168.1.11:7890", text)
        self.assertNotIn("TMB_INSECURE_TLS", text)
        self.assertIn("换条线路", text)

    def test_explain_tls_direct_keeps_ca_hint(self):
        """直连时 CA 过期确实是最常见原因，提示要留着。"""
        import ssl as _ssl
        exc = _ssl.SSLError("certificate verify failed")
        text = util.explain_net(exc, "")
        self.assertIn("TMB_INSECURE_TLS", text)


def _item(**kw):
    base = {
        "id": "1", "title": "Some Release 1080p", "infohash": "A" * 40,
        "magnet": "magnet:?xt=urn:btih:" + "A" * 40, "size_text": "1 GiB",
        "size_bytes": 1024 ** 3, "seeders": 30, "leechers": 1, "downloads": 100,
        "pub_ts": __import__("time").time() - 3600, "category_id": "1_1",
        "view_url": "https://sukebei.nyaa.si/view/1", "trusted": False, "remake": False,
    }
    base.update(kw)
    return base


class TestAnalyzer(unittest.TestCase):
    def test_trusted_bonus_moves_green_row_up(self):
        items = nyaa.finalize(nyaa.parse_html(HTML_REAL), [])
        plain = analyzer.analyze(items, rule={}, search_cfg={"trusted_bonus": 0},
                                 keywords=["shop"])
        self.assertFalse(plain["items"][0]["trusted"])   # 不加权时绿行排不到第一

        boosted = analyzer.analyze(items, rule={}, search_cfg={"trusted_bonus": 60},
                                   keywords=["shop"])
        self.assertTrue(boosted["items"][0]["trusted"])   # 加权后绿行被抬上来
        self.assertIn("可信发布", boosted["items"][0]["reasons"])

    def test_filter_seeders(self):
        res = analyzer.analyze([_item(seeders=0), _item(id="2", seeders=50)],
                               rule={"min_seeders": 5}, keywords=["some"])
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["dropped"]["seeders"], 1)

    def test_exclude_words(self):
        res = analyzer.analyze([_item(title="Some Release SAMPLE 1080p")],
                               rule={"exclude_words": ["sample"]}, keywords=[])
        self.assertEqual(res["kept"], 0)
        self.assertEqual(res["dropped"]["exclude"], 1)
        self.assertFalse(res["relaxed"])  # 排除词是明确意图，不自动放宽

    def test_prefer_boosts_score(self):
        a = analyzer.analyze([_item()], rule={"prefer_words": ["1080p"]}, keywords=[])["items"][0]
        b = analyzer.analyze([_item()], rule={}, keywords=[])["items"][0]
        self.assertGreater(a["score"], b["score"])

    def test_relax_when_all_filtered(self):
        res = analyzer.analyze([_item(seeders=0)], rule={"min_seeders": 999}, keywords=[])
        self.assertTrue(res["relaxed"])
        self.assertEqual(res["kept"], 1)

    def test_pick(self):
        items = [{"a": 1}, {"a": 2}]
        self.assertEqual(analyzer.pick(items, 2)["a"], 2)
        self.assertIsNone(analyzer.pick(items, 5))


class TestXunleiHelpers(unittest.TestCase):
    def test_flatten_files(self):
        tree = [{"kind": "drive#folder", "resources": [
            {"name": "a.mkv", "file_size": 100, "file_index": 0},
            {"name": "b.srt", "file_size": 10, "file_index": 1}]},
            {"name": "c.mp4", "file_size": 200}]
        files = _flatten_files(tree)
        self.assertEqual([f["name"] for f in files], ["a.mkv", "b.srt", "c.mp4"])

    def test_find_target(self):
        resp = {"tasks": [{"type": "user#runner", "params": {"target": "dev123#abc"}}]}
        self.assertEqual(_find_target(resp), "dev123#abc")
        self.assertEqual(_find_target({"a": 1}), "")


class TestConfigAndStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.etc = os.path.join(self.tmp, "etc")
        self.var = os.path.join(self.tmp, "var")
        self.cfg = Config(self.etc, self.var).load()

    def test_defaults_written(self):
        self.assertTrue(os.path.exists(os.path.join(self.etc, "rules.json")))
        self.assertEqual(self.cfg.get("download.backend"), "xunlei")

    def test_mask_and_keep(self):
        self.cfg.set("telegram.bot_token", "123:abc")
        self.cfg.save()
        view = self.cfg.public_view()
        self.assertEqual(view["telegram"]["bot_token"], "******")
        # 用脱敏值回写不应覆盖真实值
        self.cfg.apply_patch({"telegram": {"bot_token": "******"}})
        self.assertEqual(self.cfg.get("telegram.bot_token"), "123:abc")

    def test_old_panel_config_migrates_to_gateway(self):
        """1.0.31：旧的 ``mode=panel`` 必须被迁走，不能让程序继续打 2345。

        真机现场：默认值就是 panel，用户从没选过，却一直在打一个没人监听的端口。
        """
        import json
        path = os.path.join(self.etc, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"download": {"xunlei": {"mode": "panel", "port": 2345,
                                               "pan_auth": "OLD"}}}, f)
        cfg = Config(self.etc, self.var).load()
        self.assertEqual(cfg.get("download.xunlei.mode"), "fnos_gateway")
        self.assertIsNone((cfg.data["download"]["xunlei"]).get("port"))
        self.assertEqual(cfg.get("download.xunlei.fnos_port"), 5666)

    def test_store_dedupe(self):
        store = Store(os.path.join(self.var, "t.sqlite3"))
        store.add_download("H1", "t", "m", "xunlei", "42", "ok")
        self.assertTrue(store.seen_recently("H1", 30))
        self.assertFalse(store.seen_recently("H2", 30))
        self.assertEqual(store.stats()["success"], 1)
        store.close()


class TestPipelineFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(os.path.join(self.tmp, "etc"),
                          os.path.join(self.tmp, "var")).load()
        self.store = Store(os.path.join(self.tmp, "var", "x.sqlite3"))
        self.pipeline = Pipeline(self.cfg, self.store)

    def test_format_bundle(self):
        bundle = {
            "rid": "abc12345",
            "match": {"rule_name": "影视", "matched": ["番"]},
            "query": "某作品 1080p",
            "keywords": ["某作品", "1080p"],
            "items": [_item(), _item(id="2", title="Second <b>")],
            "submitted": [{"index": 1, "ok": True, "backend": "xunlei", "task_id": "9"}],
            "total": 2,
            "dropped": {},
        }
        text = self.pipeline.format_bundle(bundle)
        self.assertIn("影视", text)
        self.assertIn("已投递", text)
        self.assertIn("&lt;b&gt;", text)  # 标题里的 HTML 必须被转义
        kb = self.pipeline.keyboard(bundle)
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)

    def test_magnet_list(self):
        bundle = {"items": [_item()]}
        text = self.pipeline.magnet_list_text(bundle)
        self.assertIn("magnet:?xt=urn:btih:", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
