"""代理通道：地址清洗、降级顺序、探测、以及「失败了真的会换下一条」。

这套东西的价值全在最后一条：主代理挂了（Clash 关机、端口换了、节点被墙），
请求要自己换到备用线路上继续跑。所以集成用例里架了一个真的 socket 代理，
让 util.http_request 先撞一个死端口、再落到活代理上，确认它拿回了 200。
"""

import os
import socket
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

from tmb import proxychan, util  # noqa: E402


# ---------------------------------------------------------------------------
# 假的 HTTP 代理：GET 直接回一段固定内容，CONNECT 只回状态行（探测只需要这一行）
# ---------------------------------------------------------------------------
class FakeProxy(object):
    def __init__(self, code=200, body=b"ok", deny=()):
        self.code = code
        self.body = body
        # 这些主机名回 503：模拟「Clash 里检索站被分到另一个策略组、那组节点挂了」
        # —— telegram 照样通，只有检索站被拒。真机上就是这么误判的。
        self.deny = tuple(deny)
        self.hits = 0
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(2048)
                if not chunk:
                    return
                buf += chunk
            self.hits += 1
            line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if line.upper().startswith("CONNECT"):
                parts = line.split()
                host = parts[1].rsplit(":", 1)[0] if len(parts) > 1 else ""
                if self.code == 200 and host not in self.deny:
                    conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    conn.sendall(b"HTTP/1.1 %d Nope\r\n\r\n"
                                 % (self.code if self.code != 200 else 503))
                return
            body = self.body
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def dead_url():
    """一个能连上又被立刻拒掉的地址：先占端口再放掉，端口就空着。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


class CleanTests(unittest.TestCase):
    def test_normalize_adds_scheme(self):
        self.assertEqual(proxychan.normalize("192.168.1.10:7897"),
                         "http://192.168.1.10:7897")
        self.assertEqual(proxychan.normalize("  192.168.1.10:7897  "),
                         "http://192.168.1.10:7897")
        # 带路径/尾斜杠的只留下 scheme://host:port，别把垃圾一起存进配置
        self.assertEqual(proxychan.normalize("http://192.168.1.10:7897/"),
                         "http://192.168.1.10:7897")

    def test_normalize_keeps_socks_and_auth(self):
        self.assertEqual(proxychan.normalize("socks5://127.0.0.1:7891"),
                         "socks5://127.0.0.1:7891")
        self.assertEqual(proxychan.normalize("http://u:p@10.0.0.2:8080"),
                         "http://u:p@10.0.0.2:8080")

    def test_normalize_rejects_incomplete(self):
        """没写端口的不猜：Clash 7890、Verge 7897、v2rayN 10809，猜错比报错更糟。"""
        for bad in ("", "   ", "192.168.1.10", "http://", "ftp://1.2.3.4:21",
                    "http://1.2.3.4:notaport"):
            self.assertEqual(proxychan.normalize(bad), "", bad)

    def test_clean_items(self):
        items = proxychan.clean_items([
            {"name": "书房PC", "url": "192.168.1.10:7897"},
            "http://10.0.0.5:7890",                       # 纯字符串也认
            {"url": "192.168.1.10:7897"},                 # 和第一条重复，丢掉
            {"name": "关掉的", "url": "http://10.0.0.6:7890", "enabled": False},
            {"url": ""},                                  # 空行不算一条
            {"name": 1, "url": "http://10.0.0.7:7890"},   # 名字不是字符串也能用
        ])
        self.assertEqual([c["url"] for c in items], [
            "http://192.168.1.10:7897", "http://10.0.0.5:7890",
            "http://10.0.0.6:7890", "http://10.0.0.7:7890"])
        self.assertEqual(items[0]["name"], "书房PC")
        self.assertTrue(items[1]["name"])                 # 没名字给个默认的
        self.assertFalse(items[2]["enabled"])
        self.assertTrue(items[0]["enabled"])

    def test_clean_items_limits(self):
        raw = [{"url": "http://10.0.0.%d:7890" % i} for i in range(1, 20)]
        self.assertEqual(len(proxychan.clean_items(raw)), proxychan.MAX_CHANNELS)

    def test_clean_items_tolerates_garbage(self):
        self.assertEqual(proxychan.clean_items(None), [])
        self.assertEqual(proxychan.clean_items("http://1.2.3.4:80"), [])
        self.assertEqual(proxychan.clean_items([None, 1, {}]), [])


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.r = proxychan.Router([
            {"name": "A", "url": "http://10.0.0.1:7890"},
            {"name": "B", "url": "http://10.0.0.2:7890"},
            {"name": "C", "url": "http://10.0.0.3:7890", "enabled": False},
        ])

    def test_primary_first(self):
        self.assertEqual(self.r.candidates("http://127.0.0.1:7890"),
                         ["http://127.0.0.1:7890", "http://10.0.0.1:7890",
                          "http://10.0.0.2:7890"])

    def test_no_primary_means_direct_last(self):
        """没填主代理：先试通道（用户加通道就是为了能用上），直连垫底。"""
        self.assertEqual(self.r.candidates(""),
                         ["http://10.0.0.1:7890", "http://10.0.0.2:7890", ""])

    def test_failed_primary_moves_to_back(self):
        self.r.note("http://127.0.0.1:7890", False, "连接被拒绝")
        self.assertEqual(self.r.candidates("http://127.0.0.1:7890"),
                         ["http://10.0.0.1:7890", "http://10.0.0.2:7890",
                          "http://127.0.0.1:7890"])

    def test_recovery_clears_penalty(self):
        self.r.note("http://127.0.0.1:7890", False, "超时")
        self.r.note("http://127.0.0.1:7890", True)
        self.assertEqual(self.r.candidates("http://127.0.0.1:7890")[0],
                         "http://127.0.0.1:7890")

    def test_dedup_primary_also_a_channel(self):
        r = proxychan.Router([{"url": "http://10.0.0.1:7890"}])
        self.assertEqual(r.candidates("http://10.0.0.1:7890"), ["http://10.0.0.1:7890"])

    def test_disabled_channel_never_tried(self):
        self.assertNotIn("http://10.0.0.3:7890", self.r.candidates(""))

    def test_note_keeps_last_used(self):
        self.r.note("http://10.0.0.1:7890", True)
        self.assertEqual(self.r.last_used, "http://10.0.0.1:7890")
        self.r.note("http://10.0.0.1:7890", False, "断了")
        self.assertEqual(self.r.last_used, "")       # 刚断的那条不能还算「正在用」

    def test_status_shape(self):
        self.r.note("http://10.0.0.1:7890", False, "ERR_CONNECTION_REFUSED")
        st = self.r.status()
        self.assertEqual(st["count"], 3)
        first = st["items"][0]
        self.assertEqual(first["url"], "http://10.0.0.1:7890")
        self.assertFalse(first["ok"])
        self.assertTrue(first["cooling"])
        self.assertIn("REFUSED", first["error"])
        self.assertGreaterEqual(first["age"], 0)

    def test_health_key_normalized(self):
        """面板上手写 192.168.1.10:7897，请求里用 http://…：状态表得认得是同一条。"""
        r = proxychan.Router([{"url": "192.168.1.10:7897"}])
        r.note("192.168.1.10:7897", False, "断了")
        item = r.status()["items"][0]
        self.assertFalse(item["ok"])
        self.assertTrue(item["cooling"])
        r.note("http://192.168.1.10:7897", True, ms=42)
        item = r.status()["items"][0]
        self.assertTrue(item["ok"])
        self.assertEqual(item["ms"], 42)
        self.assertFalse(item["cooling"])

    def test_reset(self):
        self.r.note("http://10.0.0.1:7890", True)
        self.r.reset()
        self.assertEqual(self.r.items(), [])
        self.assertEqual(self.r.status()["count"], 0)


class ProbeTests(unittest.TestCase):
    def test_probe_ok(self):
        p = FakeProxy(200)
        try:
            r = proxychan.probe(p.url(), targets=(("example.com", 443),), timeout=3)
            self.assertTrue(r["ok"], r)
            self.assertIsNotNone(r["ms"])
            self.assertEqual(r["targets"][0]["target"], "example.com:443")
            self.assertEqual(p.hits, 1)
        finally:
            p.close()

    def test_probe_rejected(self):
        p = FakeProxy(403)
        try:
            r = proxychan.probe(p.url(), targets=(("example.com", 443),), timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("403", r["error"])
        finally:
            p.close()

    def test_probe_requires_auth(self):
        p = FakeProxy(407)
        try:
            r = proxychan.probe(p.url(), targets=(("example.com", 443),), timeout=3)
            self.assertFalse(r["ok"])
            self.assertIn("账号密码", r["error"])
        finally:
            p.close()

    def test_probe_dead_port(self):
        r = proxychan.probe(dead_url(), targets=(("example.com", 443),), timeout=2)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_probe_socks_message(self):
        """urllib 不支持 socks：别让人以为「填了 socks5 就配好了」。"""
        r = proxychan.probe("socks5://127.0.0.1:7891")
        self.assertFalse(r["ok"])
        self.assertIn("http://", r["error"])

    def test_probe_bad_url(self):
        r = proxychan.probe("192.168.1.10")
        self.assertFalse(r["ok"])
        self.assertIn("7897", r["error"])       # 例子直接给对，省一轮问答

    def test_probe_line_records_health(self):
        proxychan.ROUTER.reset()
        p = FakeProxy(200)
        try:
            proxychan.probe_line(p.url(), targets=(("example.com", 443),), timeout=3)
            self.assertTrue(proxychan.ROUTER.health(p.url())["ok"])
        finally:
            p.close()
            proxychan.ROUTER.reset()

    def test_partial_line_is_still_usable(self):
        """检索站被代理拒（503）不能把整条线路判死。

        真机现场：Clash 把检索站分到另一个策略组，那组节点挂了 → telegram 通、
        检索站 503。旧逻辑要求「两个目标都通」，于是主代理和备用全红、程序落到
        直连，连本来能用的投递也一起废了。
        """
        p = FakeProxy(200, deny=("sukebei.nyaa.si",))
        try:
            r = proxychan.probe(p.url(), timeout=3)
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["partial"], r)
            self.assertEqual(r["note"], "检索站不通")
            self.assertIn("503", r["note_detail"])
            self.assertEqual(r["error"], "")          # 能用，就不该带「死因」
            self.assertIsNotNone(r["ms"])             # 时间取的是通的那条
        finally:
            p.close()

    def test_all_targets_denied_is_dead(self):
        """两个目标都被拒才是真的不通——别把「部分可用」放宽成「全都能用」。"""
        p = FakeProxy(200, deny=("sukebei.nyaa.si", "api.telegram.org"))
        try:
            r = proxychan.probe(p.url(), timeout=3)
            self.assertFalse(r["ok"], r)
            self.assertFalse(r["partial"], r)
            self.assertIn("503", r["error"])
            self.assertEqual(r["note"], "")
        finally:
            p.close()

    def test_partial_line_is_not_sticky_dead(self):
        """部分可用的线路要能进健康表并当「活的」：不冷却、状态写清楚了。"""
        proxychan.ROUTER.reset()
        p = FakeProxy(200, deny=("sukebei.nyaa.si",))
        try:
            proxychan.probe_line(p.url(), timeout=3)
            h = proxychan.ROUTER.health(p.url())
            self.assertTrue(h["ok"], h)
            self.assertEqual(h["note"], "检索站不通")
            proxychan.ROUTER.set_items([{"name": "ATV", "url": p.url()}])
            st = proxychan.ROUTER.status()
            self.assertTrue(st["items"][0]["ok"])
            self.assertEqual(st["items"][0]["note"], "检索站不通")
            self.assertFalse(st["items"][0]["cooling"])
        finally:
            p.close()
            proxychan.ROUTER.reset()


class FailoverTests(unittest.TestCase):
    """真跑一遍 util.http_request：第一条死、第二条活。"""

    def setUp(self):
        self.good = FakeProxy(200, b"through-backup")
        self.bad = dead_url()
        self.router = proxychan.Router([{"name": "备用", "url": self.good.url()}])
        util.set_proxy_chain(self.router.candidates)
        util.set_proxy_notice(self.router.note)

    def tearDown(self):
        util.set_proxy_chain(None)
        util.set_proxy_notice(None)
        proxychan.ROUTER.reset()
        self.good.close()

    def test_retry_switches_to_next_line(self):
        status, text, _ = util.http_request(
            "http://example.com/anything", proxy=self.bad, retries=1,
            timeout=3, backoff=0)
        self.assertEqual(status, 200)
        self.assertEqual(text, "through-backup")
        self.assertEqual(self.good.hits, 1)
        # 死的那条被记了一笔，而且 5 分钟内排到后面去
        self.assertFalse(self.router.health(self.bad)["ok"])
        self.assertEqual(self.router.candidates(self.bad)[0], self.good.url())

    def test_last_line_is_reused(self):
        """重试次数比线路多时，末条一直用下去，不会「轮空」。"""
        chain = self.router.candidates("")
        self.assertEqual(len(chain), 2)      # 通道 + 直连
        self.assertEqual(chain[0], self.good.url())

    def test_local_url_never_uses_channel(self):
        """内网地址必须直连：把 NAS 自己的服务交给代理只会多一次失败。"""
        srv = _LocalServer()
        try:
            status, text, _ = util.http_request(srv.url(), proxy=self.good.url(),
                                               retries=0, timeout=3)
            self.assertEqual(status, 200)
            self.assertEqual(text, "local")
            self.assertEqual(self.good.hits, 0)   # 代理一次都没被打到
        finally:
            srv.close()

    def test_install_hooks_router(self):
        proxychan.install({"proxy_channels": [
            {"name": "PC", "url": "192.168.1.10:7897", "enabled": True}]})
        try:
            self.assertEqual(proxychan.ROUTER.items()[0]["url"],
                             "http://192.168.1.10:7897")
            self.assertEqual(util.proxy_chain(""), ["http://192.168.1.10:7897", ""])
            self.assertEqual(util.proxy_chain("http://127.0.0.1:7890"),
                             ["http://127.0.0.1:7890", "http://192.168.1.10:7897"])
        finally:
            proxychan.install({})

    def test_install_tolerates_broken_config(self):
        class Broken(object):
            def get(self, key, default=None):
                raise RuntimeError("boom")

        proxychan.install(Broken())
        self.assertEqual(proxychan.ROUTER.items(), [])
        util.set_proxy_chain(None)
        util.set_proxy_notice(None)


class _Cfg(dict):
    """够用的配置桩：支持 ``cfg.get("a.b")`` 这种点号路径。"""

    def get(self, key, default=None):
        cur = self
        for part in str(key).split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


class AutoTestTests(unittest.TestCase):
    """自动测速与切换：线路互为备胎——还通就继续用，断了才按顺序换下一条。"""

    def tearDown(self):
        proxychan.ROUTER.reset()

    def test_partial_line_beats_direct(self):
        """只通一个目标的线路也要被选中，别落到直连。

        真机现场：主代理端口没服务（死），备用 ATV 只有检索站被拒，
        telegram 10 ms 就通。旧逻辑把 ATV 也判死 → 无可选 → 「已切到直连」，
        结果投递也一起挂了。
        """
        part = FakeProxy(200, deny=("sukebei.nyaa.si",))
        try:
            rep = proxychan.auto_test(dead_url(), [{"name": "ATV", "url": part.url()}],
                                      timeout=3, include_direct=True)
            self.assertEqual(rep["picked_kind"], "channel", rep)
            self.assertEqual(rep["picked"], part.url())
            self.assertEqual(rep["picked_note"], "检索站不通")
            self.assertIn(proxychan.short_name(part.url()), rep["text"])
            self.assertIn("检索站不通", rep["text"])
            self.assertNotIn("ATV", rep["failed"])     # ATV 不算死线（第一条和直连才死）
        finally:
            part.close()

    def test_first_run_follows_order(self):
        """还没选定走向时按顺序挑：主代理排在备用前面，备用再快也顶不掉它。"""
        prim = FakeProxy(200)
        bak = FakeProxy(200)
        try:
            rep = proxychan.auto_test(prim.url(), [{"name": "B", "url": bak.url()}],
                                      targets=(("example.com", 443),), timeout=3,
                                      include_direct=False)
            self.assertEqual(rep["picked_kind"], "primary", rep)
            self.assertEqual(rep["picked"], prim.url())
            self.assertIn("现在走「%s」" % proxychan.short_name(prim.url()), rep["text"])
            self.assertEqual(proxychan.ROUTER.candidates(prim.url())[0], prim.url())
        finally:
            prim.close()
            bak.close()

    def test_switches_to_live_channel(self):
        good = FakeProxy(200)
        bad = dead_url()
        proxychan.ROUTER.set_items([{"name": "书房PC", "url": good.url()}])
        try:
            rep = proxychan.auto_test(bad, [{"name": "书房PC", "url": good.url()}],
                                      targets=(("example.com", 443),), timeout=2,
                                      include_direct=False)
            self.assertEqual(rep["picked_kind"], "channel", rep)
            self.assertEqual(rep["picked"], good.url())
            self.assertFalse(rep["kept"])
            self.assertIn("现在走", rep["text"])
            self.assertIn(proxychan.short_name(good.url()), rep["text"])
            # 选中的那条真的顶到第一位（下一次请求就走它）
            self.assertEqual(proxychan.ROUTER.candidates(bad)[0], good.url())
            # 但主代理还排在它后面：备用一挂，下一次重试就轮它上
            self.assertEqual(proxychan.ROUTER.candidates(bad)[1], proxychan.normalize(bad))
            # 死掉的主代理被记账，但配置里的地址一个字都没动
            self.assertFalse(proxychan.ROUTER.health(bad)["ok"])
        finally:
            good.close()

    def test_keeps_the_line_in_use(self):
        """正在用的备用通道还通，就不因为主代理恢复而切回去——互为备胎，不来回跳。"""
        prim = FakeProxy(200)
        bak = FakeProxy(200)
        try:
            proxychan.ROUTER.set_items([{"name": "B", "url": bak.url()}])
            proxychan.ROUTER.set_preferred(prim.url(), bak.url(), "channel", "B")
            rep = proxychan.auto_test(prim.url(), proxychan.ROUTER.items(),
                                      targets=(("example.com", 443),), timeout=3,
                                      include_direct=False)
            self.assertEqual(rep["picked"], bak.url())
            self.assertTrue(rep["kept"])
            self.assertIn("正在用", rep["text"])
            self.assertEqual(proxychan.ROUTER.candidates(prim.url())[0], bak.url())
            # 主代理没被扔掉，只是退居第二：这就是「互为备胎」
            self.assertEqual(proxychan.ROUTER.candidates(prim.url())[1], prim.url())
        finally:
            prim.close()
            bak.close()

    def test_switches_back_when_the_channel_dies(self):
        """备用通道挂了：主代理立刻顶上来（「互为备胎」的另一半）。"""
        prim = FakeProxy(200)
        dead = dead_url()
        try:
            proxychan.ROUTER.set_items([{"name": "B", "url": dead}])
            proxychan.ROUTER.set_preferred(prim.url(), dead, "channel", "B")
            rep = proxychan.auto_test(prim.url(), proxychan.ROUTER.items(),
                                      targets=(("example.com", 443),), timeout=2,
                                      include_direct=False)
            self.assertEqual(rep["picked"], prim.url())
            self.assertFalse(rep["kept"])
            self.assertIn("已切到「%s」" % proxychan.short_name(prim.url()), rep["text"])
            self.assertEqual(proxychan.ROUTER.candidates(prim.url())[0], prim.url())
        finally:
            prim.close()

    def test_direct_is_not_sticky(self):
        """代理全灭时落到直连，但直连不算备胎：代理一恢复就回到代理上。"""
        prim = FakeProxy(200)
        try:
            proxychan.ROUTER.set_preferred(prim.url(), "", "direct")
            rep = proxychan.auto_test(prim.url(), [],
                                      targets=(("example.com", 443),), timeout=3,
                                      include_direct=False)
            self.assertEqual(rep["picked"], prim.url())
            self.assertFalse(rep["kept"])
            self.assertIn("已切到「%s」" % proxychan.short_name(prim.url()), rep["text"])
        finally:
            prim.close()

    def test_all_proxies_down_falls_to_direct(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        bad = dead_url()
        try:
            rep = proxychan.auto_test(bad, [{"name": "B", "url": dead_url()}],
                                      targets=(("127.0.0.1", port),), timeout=2)
            self.assertEqual(rep["picked_kind"], "direct", rep)
            self.assertEqual(rep["picked"], "")
            self.assertIn("直连", rep["text"])
            # 直连顶到第一位，任何请求都别再先撞一遍死代理
            self.assertEqual(proxychan.ROUTER.candidates(bad)[0], "")
        finally:
            srv.close()

    def test_nothing_configured(self):
        rep = proxychan.auto_test("", [], include_direct=False)
        self.assertFalse(rep["ok"])
        self.assertIsNone(rep["picked"])
        self.assertIn("空着", rep["text"])

    def test_preference_is_per_primary(self):
        """检索站可能单独填了代理：机器人那边的切换不该把它也顶掉。"""
        r = proxychan.Router([{"name": "B", "url": "http://10.0.0.2:7890"}])
        r.set_preferred("http://127.0.0.1:7890", "http://10.0.0.2:7890", "channel")
        self.assertEqual(r.candidates("http://127.0.0.1:7890")[0], "http://10.0.0.2:7890")
        self.assertEqual(r.candidates("http://127.0.0.2:7890")[0], "http://127.0.0.2:7890")

    def test_preferred_line_that_died_is_ignored(self):
        r = proxychan.Router([{"name": "B", "url": "http://10.0.0.2:7890"}])
        p = "http://127.0.0.1:7890"
        r.set_preferred(p, "http://10.0.0.2:7890", "channel")
        r.note("http://10.0.0.2:7890", False, "断了")
        self.assertEqual(r.candidates(p)[0], p)     # 回到主代理，别赖在死线上

    def test_stale_preference_is_dropped(self):
        """选中过的通道被删了：不能还拿它当第一条，否则每次请求都先撞死地址。"""
        r = proxychan.Router([{"name": "B", "url": "http://10.0.0.2:7890"}])
        p = "http://127.0.0.1:7890"
        r.set_preferred(p, "http://10.0.0.2:7890", "channel")
        r.set_items([])                     # 用户把通道删了
        self.assertEqual(r.candidates(p)[0], p)

    def test_install_drops_choice_when_channels_change(self):
        proxychan.install({"proxy_channels": [{"url": "http://10.0.0.2:7890"}]})
        proxychan.ROUTER.set_preferred("", "http://10.0.0.2:7890", "channel")
        proxychan.install({"proxy_channels": [{"url": "http://10.0.0.3:7890"}]})
        try:
            self.assertIsNone(proxychan.ROUTER.status()["preferred"])
        finally:
            proxychan.install({})

    def test_status_reports_choice(self):
        r = proxychan.Router()
        r.set_preferred("http://127.0.0.1:7890", "http://10.0.0.2:7890", "channel", "书房PC")
        st = r.status()
        self.assertEqual(st["preferred"], "http://10.0.0.2:7890")
        self.assertEqual(st["preferred_kind"], "channel")
        self.assertEqual(st["preferred_name"], "书房PC")
        # 没给名字就退回身份标签，面板上不至于显示空
        r.set_preferred("http://127.0.0.1:7890", "http://127.0.0.1:7890", "primary")
        self.assertEqual(r.status()["preferred_name"], "线路 1")
        self.assertIsNone(r.preferred("http://127.0.0.9:1"))

    def test_has_lines(self):
        self.assertFalse(proxychan.has_lines(_Cfg(proxy_channels=[], telegram={"proxy": ""})))
        self.assertTrue(proxychan.has_lines(_Cfg(proxy_channels=[], telegram={"proxy": "http://a:1"})))
        self.assertTrue(proxychan.has_lines(
            _Cfg(proxy_channels=[{"url": "http://b:2"}], telegram={"proxy": ""})))

    def test_install_reads_auto_flag(self):
        proxychan.install({"proxy_channels": [], "proxy_auto": False})
        try:
            self.assertFalse(proxychan.ROUTER.status()["auto"])
        finally:
            proxychan.install({})


class LineListTests(unittest.TestCase):
    """1.0.40：面板上只剩一个框，一行一个地址。

    后端还认 ``telegram.proxy`` + ``proxy_channels`` 这两个键（各处代码都在读），
    但语义变了：第一行就是第一行，后面几行平级排队，不分主备。
    """

    def tearDown(self):
        proxychan.ROUTER.reset()

    def test_short_name(self):
        """面板一行里要同时放地址和结果，整个 URL 放不下。"""
        self.assertEqual(proxychan.short_name("http://192.168.1.11:7890"),
                         "192.168.1.11:7890")
        self.assertEqual(proxychan.short_name("192.168.1.11:7890/"), "192.168.1.11:7890")
        self.assertEqual(proxychan.short_name("http://u:p@10.0.0.2:8080"),
                         "10.0.0.2:8080")
        self.assertEqual(proxychan.short_name(""), "直连")

    def test_status_lists_every_line_in_order(self):
        """整串线路都要出现在状态里：第一行那条以前是「隐形的」。"""
        r = proxychan.Router([{"url": "http://10.0.0.2:7890"}])
        st = r.status("192.168.1.11:7890")
        self.assertEqual([x["url"] for x in st["lines"]],
                         ["http://192.168.1.11:7890", "http://10.0.0.2:7890"])
        self.assertEqual(st["lines"][0]["label"], "192.168.1.11:7890")
        self.assertIsNone(st["lines"][0]["ok"])     # 没测过，别写成「通」

    def test_module_status_takes_primary(self):
        """面板调的是模块级 status(primary)：少这一个参数 /api/status 会整个 500。"""
        proxychan.install({})
        try:
            self.assertIn("lines", proxychan.status("http://192.168.1.11:7890"))
        finally:
            proxychan.install({})

    def test_status_reports_partial_note(self):
        r = proxychan.Router([{"url": "http://10.0.0.2:7890"}])
        r.note("10.0.0.2:7890", True, ms=12, note="检索站不通", note_detail="503")
        line = r.status("")["lines"][0]
        self.assertTrue(line["ok"])
        self.assertEqual(line["note"], "检索站不通")
        self.assertEqual(line["note_detail"], "503")

    def test_config_primary_falls_back_to_nyaa_proxy(self):
        """老配置里只填了检索代理：那一行就是第一行，别当成没配。"""
        self.assertEqual(proxychan.config_primary(
            _Cfg(telegram={"proxy": ""}, nyaa={"proxy": "http://b:2"})), "http://b:2")
        self.assertEqual(proxychan.config_primary(
            _Cfg(telegram={"proxy": "http://a:1"}, nyaa={"proxy": "http://b:2"})),
            "http://a:1")
        self.assertEqual(proxychan.config_primary(_Cfg(telegram={"proxy": ""})), "")

    def test_lines_may_arrive_as_plain_strings(self):
        """输入框交给后端的是一串字符串（第一行之后的行），别被当成脏数据丢掉。"""
        good = FakeProxy(200)
        proxychan.install({"proxy_channels": [{"url": good.url()}]})
        try:
            rep = proxychan.auto_test("", [good.url()],
                                      targets=(("example.com", 443),), timeout=3,
                                      include_direct=False)
            self.assertEqual(rep["picked"], good.url())
            self.assertEqual(proxychan.ROUTER.candidates("")[0], good.url())
        finally:
            proxychan.install({})
            good.close()


class _LocalServer(object):
    """假装是 NAS 本机上的服务（127.0.0.1），用来验证「内网直连」。"""

    def __init__(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"local"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    unittest.main()
