"""代理线路：一串平级的地址，从上往下试，不通自动换下一条。

为什么需要它
------------
1.0.31 之后迅雷已经不用人管了，剩下唯一还得手填的就是代理：国区 NAS 访问
Telegram 和检索站都得经一条本机/局域网的 Clash。那条 Clash 一旦关机、换端口、
或者节点抽风，机器人就整段瘫掉，面板上只留一句「连接被拒绝」。

这个模块维护的是一串**平级**的线路，没有主次之分（1.0.40 起面板上就一个框，
一行一条）：

* 第 0 条取 ``telegram.proxy``（兼容旧配置，就是「填在第一行的那个」），可以为空；
* 之后按顺序接 ``proxy_channels``（第二行往下）；
* 请求失败、重试时自动换下一条（见 ``util.http_request`` 的 attempt 循环）；
* 某条刚失败过的，5 分钟内排到队尾（``BAD_TTL``），省得每次请求都先撞一次墙；
* ``auto_test()`` 定期把整串线路测一遍，把「这次该走谁」记在内存里（``set_preferred``）：
  线路**互为备胎**——正在用的那条只要还通就一直用下去，它断了才换另一条；
  换过去之后也不因为「原来那条恢复了」就硬切回来，免得线路来回跳
  ——**不改用户填的配置**。

内网/本机地址永远直连，不进这个队列——把 NAS 自己的迅雷网关交给机场代理，
只会多一次超时。
"""
from __future__ import annotations

import re
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import util

MAX_CHANNELS = 6
# 一条线路刚失败过，多久内排到后面去。太短会让轮询一直撞墙，太长会错过它恢复。
BAD_TTL = 300.0
# 定时复测的间隔。比 BAD_TTL 短，线路恢复后能早点切回来。
AUTO_INTERVAL = 180.0
# 探测用的目标：一个是机器人要连的，一个是检索要连的。
# **只要有一个通，这条线路就算能用**——它们经常不是一起坏的（Clash 里给检索站单独
# 分了组、组里节点挂了，就是 telegram 通、检索站 503）。要求「全通」会把一条好好的
# 线路判成死线，然后整串线路全红、落到直连，那才是真的全废。
DEFAULT_TARGETS = (("api.telegram.org", 443), ("sukebei.nyaa.si", 443))
# 探测结果里的人话名字：面板上「检索站不通」比「sukebei.nyaa.si:443 不通」好读
TARGET_LABEL = {"api.telegram.org": "Telegram", "sukebei.nyaa.si": "检索站",
                "nyaa.si": "检索站"}
_PROBE_TIMEOUT = 6.0
# 测「直连」用更短的超时：它只是兜底，不通就别拖着整个测速一起等
_DIRECT_TIMEOUT = 3.0

# 线路的身份：只用来区分「填在第一行的」「后面几行的」「不用代理」，不是主次。
# 名字直接落在面板上，所以这里写成中性的「线路 1 / 线路 / 直连」。
KIND_LABEL = {"primary": "线路 1", "channel": "线路", "direct": "直连"}


def short_name(url):
    """地址的短写法：``http://192.168.1.11:7890`` → ``192.168.1.11:7890``。

    面板上要在一行里说完「正在用谁 + 多少毫秒 + 哪个站不通」，整个 URL 太长，
    手机上必被省略号截掉。空地址就是直连。
    """
    u = str(url or "").strip()
    if not u:
        return "直连"
    try:
        p = urllib.parse.urlsplit(u if "://" in u else "http://" + u)
        host = p.netloc or u
    except ValueError:
        host = u
    return host.split("@")[-1] or u



def _target_label(target):
    """``sukebei.nyaa.si:443`` → ``检索站``；不认识的域名就还它本身。"""
    host = str(target or "").split(":")[0]
    return TARGET_LABEL.get(host, host)


def _verdict(results):
    """把「每个目标通不通」汇总成这条线路的结论。

    判定标准是 **至少一个目标通**，不是「全通」：

    * 全通      → ``ok``，没有备注；
    * 部分通    → ``ok`` + ``note``（「检索站不通」）+ ``note_detail``（具体原因）；
    * 全不通    → ``ok=False``，``error`` 是第一条失败原因。

    为什么要这样：机器人和检索站走的是同一条代理，但 Clash 里它们常常被分到
    不同规则、不同策略组。检索站的组挂了，机器人照样能用——这时候把整条线路
    判死，只会让程序落到直连、把本来能跑的投递也一起弄坏。
    """
    good = [r["ms"] for r in results if r.get("ok") and r.get("ms") is not None]
    bad = [r for r in results if not r.get("ok")]
    ok = bool([r for r in results if r.get("ok")])
    note = note_detail = ""
    if ok and bad:
        note = "%s不通" % _target_label(bad[0].get("target"))
        note_detail = bad[0].get("error") or ""
    return {
        "ok": ok,
        "partial": ok and bool(bad),
        "ms": min(good) if good else None,
        "error": "" if ok else ((bad[0].get("error") or "") if bad else ""),
        "note": note,
        "note_detail": note_detail,
    }

_OK_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def normalize(url):
    """把用户随手写的地址收拾成 ``scheme://host:port``；认不出就返回 ""。

    没写 ``http://`` 的自动补上（面板上没人愿意敲协议名），但**没写端口的不补**：
    Clash 是 7890、Verge 是 7897、v2rayN 是 10809…… 猜一个只会让人以为配好了，
    实际连的是别的服务。
    """
    p = str(url or "").strip()
    if not p:
        return ""
    if "://" not in p:
        p = "http://" + p
    try:
        u = urllib.parse.urlsplit(p)
        port = u.port
    except ValueError:
        return ""
    scheme = (u.scheme or "http").lower()
    if scheme not in _OK_SCHEMES or not u.hostname or not port:
        return ""
    # netloc 里可能带 user:pass@，原样留着
    return "%s://%s" % (scheme, u.netloc)


def clean_items(raw, limit=MAX_CHANNELS):
    """洗一遍线路列表：去空、去重、限条数。脏数据不该让整份配置报废。"""
    out, seen = [], set()
    for item in list(raw or [])[:limit * 4]:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = normalize(item.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        # 面板上不显示名字了（一行一条地址，靠顺序说话），名字只是配置里留个记号。
        # 从 2 起算：第 1 行是 telegram.proxy，通道从第 2 行开始。
        name = str(item.get("name") or "").strip() or ("线路 %d" % (len(out) + 2))
        out.append({
            "name": name[:24],
            "url": url,
            "enabled": item.get("enabled", True) is not False,
        })
        if len(out) >= limit:
            break
    return out


def _dedup(order):
    out, seen = [], set()
    for u in order:
        u = str(u or "").strip()
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


class Router(object):
    """线路表 + 健康记录。所有方法都可并发调用（面板轮询与后台投递同时来）。"""

    def __init__(self, items=None):
        self._lock = threading.Lock()
        self._items = clean_items(items)
        self._health = {}
        self.last_used = ""      # 最近一次真的成功的线路；"" = 直连
        # 自动测速选出来的「这次该走谁」，按「哪条主代理」分开记：
        # 机器人（telegram.proxy）和检索站（nyaa.proxy）可能不是同一条，不能互相顶掉。
        # None = 没测过，按正常顺序；"" = 直接走直连；"http://…" = 就走这条。
        self._preferred = {}
        self._pref_shown = {}
        self.auto = True         # 面板上的「自动切换」开关，install() 时按配置填

    # ---------- 线路表 ----------
    def set_items(self, items):
        with self._lock:
            self._items = clean_items(items)

    def items(self):
        with self._lock:
            return [dict(c) for c in self._items]

    def _is_bad(self, url):
        h = self._health.get(url) or {}
        return float(h.get("bad_until") or 0) > time.time()

    def candidates(self, primary=""):
        """这次请求按什么顺序试代理。第一个就是最该用的那条。

        顺序本来是「填的第一条 → 后面几条（按填的顺序）→ 直连」，两种情况会变：

        * 自动测速选定了某条（``set_preferred``）——它顶到第一位，其余照旧往后排。
          选中的通常是后面某条（第一条先断的），这时第一条自动成为它的备胎：
          那条一挂，下一次重试就轮到第一条；
        * 某条刚失败过（5 分钟内），排到队尾，但它随时可能恢复，不扔掉。

        第一条留空时不留空位：直接「线路先、直连垫底」，省掉一次白等的超时。
        """
        p = str(primary or "").strip()
        key = normalize(p) or ""
        with self._lock:
            chans = [c["url"] for c in self._items if c.get("enabled")]
            pref = (self._preferred.get(key) or {}).get("url")
            bad_pref = pref is not None and pref != "" and self._is_bad(pref)
            bad = self._is_bad(p) if p else False
        if pref is not None and (pref == "" or pref == p or pref in chans):
            if not bad_pref:
                rest = ([p] if p and p != pref else []) + [c for c in chans if c != pref]
                if not p:
                    rest.append("")      # 没主代理时，直连也要留在兜底位
                return _dedup([pref] + rest)
        # 记着的那条已经被删掉/改掉了（pref 不在表里），当没选过处理
        if p and not bad:
            order = [p] + chans
        elif p:
            order = chans + [p]
        else:
            order = chans + [""]
        return _dedup(order)

    # ---------- 自动切换选出来的那条 ----------
    def preferred(self, primary=""):
        """上次自动测速选定的走向：``None`` = 还没选过，``""`` = 直连，地址 = 就走那条。"""
        key = normalize(primary) or ""
        with self._lock:
            return (self._preferred.get(key) or {}).get("url")

    def set_preferred(self, primary, url, kind="", name=""):
        """记住「这次该走哪条」。``url=""`` 表示直连，``None`` 表示没有可用线路。

        只写内存，不碰配置：用户填的主代理永远原样留着。主代理和备用通道互为备胎，
        所以选定之后就一直指着它，直到下一轮测速发现它不通。
        """
        key = normalize(primary) or ""
        with self._lock:
            if url is None:
                self._preferred.pop(key, None)
                self._pref_shown = {"url": None, "kind": "", "primary": key, "name": ""}
            else:
                label = name or (KIND_LABEL.get(kind) or "") or url
                self._preferred[key] = {"url": url, "kind": kind, "name": label,
                                        "at": time.time()}
                self._pref_shown = {"url": url, "kind": kind, "primary": key, "name": label}
                self.last_used = url

    def clear_preferred(self):
        """线路表变了：选中的走向作废，「正在用」也别再指着一条可能已经不存在的线。"""
        with self._lock:
            self._preferred = {}
            self._pref_shown = {}
            self.last_used = ""

    # ---------- 健康记录 ----------
    def note(self, url, ok, error="", ms=None, note="", note_detail=""):
        """记一次真实请求的结果。请求本身已经在 util 里跑完了，这里只记账。

        键一律用 ``normalize`` 之后的形式：面板上写 ``192.168.1.10:7897``、
        配置里存的是它、而请求实际用的是 ``http://192.168.1.10:7897``——
        不归一化的话，刚测过的线路在状态里还是「没测过」。

        ``note`` 是「能用但不完整」的说明（例：检索站不通），它**不影响** ok：
        线路算活的，只是有个目标到不了。
        """
        key = normalize(url) or str(url or "").strip()
        now = time.time()
        with self._lock:
            h = self._health.setdefault(key, {})
            h["ok"] = bool(ok)
            h["at"] = now
            if ms is not None:
                h["ms"] = int(ms)
            if ok:
                h.pop("error", None)
                h["bad_until"] = 0
                if note:
                    h["note"] = str(note)[:80]
                    h["note_detail"] = str(note_detail or "")[:160]
                else:
                    h.pop("note", None)
                    h.pop("note_detail", None)
                self.last_used = key
            else:
                h.pop("note", None)
                h.pop("note_detail", None)
                h["error"] = str(error or "")[:160]
                h["bad_until"] = now + BAD_TTL
                if self.last_used == key:
                    self.last_used = ""

    def health(self, url):
        key = normalize(url) or str(url or "").strip()
        with self._lock:
            return dict(self._health.get(key) or {})

    def reset(self):
        with self._lock:
            self._items = []
            self._health = {}
            self._preferred = {}
            self._pref_shown = {}
            self.last_used = ""

    # ---------- 给面板看 ----------
    def status(self, primary=""):
        """整串线路的状态。``primary`` 是填在第一行的那个地址（可空）。

        ``lines`` 是**按尝试顺序**排好的全部线路（第一条 + 后面的 + 直连不收），
        每条带上最近一次实测结果。面板拿它直接画「哪条活着、哪条在冷却」，
        不用为了看一眼状态先点一次测速。
        """
        now = time.time()
        p = normalize(primary) or ""
        with self._lock:
            items = []
            for c in self._items:
                h = dict(self._health.get(c["url"]) or {})   # items 里已是归一化地址
                at = float(h.get("at") or 0)
                items.append({
                    "name": c["name"],
                    "url": c["url"],
                    "enabled": c["enabled"],
                    "ok": h.get("ok"),
                    "error": h.get("error") or "",
                    "note": h.get("note") or "",
                    "note_detail": h.get("note_detail") or "",
                    "ms": h.get("ms"),
                    "age": int(now - at) if at else None,
                    "cooling": bool(h.get("bad_until") and h["bad_until"] > now),
                })
            # 第一行也进 lines：不然「上面那条死没死」只能靠猜
            lines = []
            for url in _dedup(([p] if p else []) + [c["url"] for c in self._items]):
                h = dict(self._health.get(url) or {})
                at = float(h.get("at") or 0)
                lines.append({
                    "url": url,
                    "label": short_name(url),
                    "ok": h.get("ok"),
                    "error": h.get("error") or "",
                    "note": h.get("note") or "",
                    "note_detail": h.get("note_detail") or "",
                    "ms": h.get("ms"),
                    "age": int(now - at) if at else None,
                    "cooling": bool(h.get("bad_until") and h["bad_until"] > now),
                })
            used = self.last_used
            shown = dict(self._pref_shown)
            auto = self.auto
        return {"ok": True, "count": len(items), "items": items, "lines": lines,
                "configured": p, "last_used": used,
                "preferred": shown.get("url"), "preferred_kind": shown.get("kind") or "",
                "preferred_name": shown.get("name") or "", "auto": bool(auto)}


# ---------------------------------------------------------------------------
# 探测：面板上那个「测」按钮
# ---------------------------------------------------------------------------
def _connect_through(phost, pport, thost, tport, timeout):
    """用 CONNECT 试一下「这条代理能不能把我们带到 thost:tport」。

    只做代理握手、不做 TLS：需要验证的正是「代理放不放行这个目标」，
    至于证书那是下一步的事，混在一起报错反而看不出是代理挂了还是站点挂了。
    """
    t0 = time.time()
    try:
        sock = socket.create_connection((phost, pport), timeout=timeout)
    except OSError as e:
        return {"ok": False, "ms": None,
                "error": util.explain_net_short(e, "http://%s:%d" % (phost, pport))}
    try:
        sock.settimeout(timeout)
        sock.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n"
                      "Proxy-Connection: keep-alive\r\n\r\n"
                      % (thost, tport, thost, tport)).encode("latin-1"))
        buf = b""
        while b"\r\n" not in buf and len(buf) < 400:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
        ms = int((time.time() - t0) * 1000)
        line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
        m = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})", line)
        code = int(m.group(1)) if m else 0
        if 200 <= code < 300:
            return {"ok": True, "ms": ms, "error": ""}
        if code == 407:
            return {"ok": False, "ms": ms, "error": "代理要账号密码（407）"}
        if code == 0:
            return {"ok": False, "ms": ms, "error": "这个端口不是 HTTP 代理（没回 HTTP 响应）"}
        return {"ok": False, "ms": ms, "error": "代理不肯连 %s（HTTP %d）" % (thost, code)}
    except socket.timeout:
        return {"ok": False, "ms": None,
                "error": "超时 %ss：代理能连上，但它没把我们带出去" % int(timeout)}
    except OSError as e:
        return {"ok": False, "ms": None, "error": util.explain_net_short(e, "")}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def probe(url, targets=DEFAULT_TARGETS, timeout=_PROBE_TIMEOUT):
    """测一条线路：逐个目标做 CONNECT，至少一个通就算这条线可用（见 ``_verdict``）。

    返回 ``{"ok", "ms", "error", "note", "note_detail", "targets": [{target, ok, ms, error}]}``。
    """
    # 先按 normalize 的规矩过一遍：没写端口的地址在这里就被拦下，
    # 别让它带着 parse_proxy 的默认 8080 去连一个「看起来像代理」的端口
    norm = normalize(url)
    if not norm:
        return {"ok": False, "ms": None, "targets": [],
                "error": "地址没写对，形如 http://192.168.1.10:7897（端口别漏）"}
    parsed = util.parse_proxy(norm)
    scheme, phost, pport = parsed
    if str(scheme).startswith("socks"):
        # urllib 不支持 socks；Clash 的 mixed 端口同时收 http，换个前缀就行
        return {"ok": False, "ms": None, "targets": [],
                "error": "只支持 http 端口：Clash 的 mixed 端口把 socks5:// 换成 http:// 即可"}
    results = []
    for thost, tport in targets:
        r = _connect_through(phost, pport, thost, tport, timeout)
        r["target"] = "%s:%d" % (thost, tport)
        results.append(r)
    out = _verdict(results)
    out["targets"] = results
    return out


def probe_line(url, targets=DEFAULT_TARGETS, timeout=_PROBE_TIMEOUT):
    """探测一条线路，并把结果记进健康表（面板点一下，状态就跟着变）。"""
    r = probe(url, targets=targets, timeout=timeout)
    ROUTER.note(url, r["ok"], r.get("error") or "", r.get("ms"),
                note=r.get("note") or "", note_detail=r.get("note_detail") or "")
    return r


def probe_direct(targets=DEFAULT_TARGETS, timeout=_DIRECT_TIMEOUT):
    """测「不用代理能不能出去」。只做 TCP 建连，够判断有没有这条路。

    它排在最后，所以只要代理能用就不该走到这儿；但代理全挂的时候，
    直连是不是通，决定了面板该写「已切到直连」还是「全都连不上」。
    """
    def one(target):
        thost, tport = target
        t0 = time.time()
        try:
            sock = socket.create_connection((thost, tport), timeout=timeout)
        except OSError as e:
            return {"target": "%s:%d" % (thost, tport), "ok": False, "ms": None,
                    "error": util.explain_net_short(e, "")}
        ms = int((time.time() - t0) * 1000)
        try:
            sock.close()
        except OSError:
            pass
        return {"target": "%s:%d" % (thost, tport), "ok": True, "ms": ms, "error": ""}

    # 目标并行走：一个域名可能要试好几个 IP，串起来能把「测一次」拖到十几秒
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as ex:
        results = list(ex.map(one, targets))
    out = _verdict(results)
    out["targets"] = results
    return out


def auto_test(primary="", items=None, targets=DEFAULT_TARGETS, timeout=_PROBE_TIMEOUT,
              include_direct=True):
    """把整串线路测一遍，挑出「接下来该走哪条」。线路之间互为备胎。

    ``primary`` 是填在第一行的地址（旧配置里的主代理），``items`` 是后面几行。

    * **并行测**：一条线最多等一个超时窗口，十条线也一样，不会越加越慢；
    * **当前这条还通就不动它**：正在走的线路（上次选定的那条）只要这次还能用，
      就继续用它——第一条修好了也不硬切回来，免得线路来回跳、连接池反复重建；
    * **它不通才换**：换的时候按 填的顺序 → 直连 挑第一条能用的，所以第一条是
      后面几条的备胎，后面几条也是第一条的备胎；
    * 直连不算备胎：代理里只要有一条能用，就不赖在直连上；
    * 结果写进健康表，并把选中的那条记进 ``Router`` 的「这次走谁」（只写内存）。

    返回 ``{"ok", "lines", "picked", "picked_kind", "text", "failed"}``。
    """
    prim = normalize(primary) or ""
    chans = clean_items(items) if items is not None else ROUTER.items()
    lines = []
    if prim:
        lines.append({"kind": "primary", "name": short_name(prim), "url": prim})
    for c in chans:
        if not c.get("enabled") or c["url"] == prim:
            continue
        lines.append({"kind": "channel", "name": c["name"], "url": c["url"]})
    if include_direct:
        lines.append({"kind": "direct", "name": KIND_LABEL["direct"], "url": ""})
    if not lines:
        return {"ok": False, "lines": [], "picked": None, "picked_kind": "",
                "failed": [], "text": "还没有线路：一个地址都没填（空着就是直连）"}

    def run(line):
        url = line["url"]
        r = (probe_direct(targets, timeout=min(timeout, _DIRECT_TIMEOUT)) if not url
             else probe(url, targets=targets, timeout=timeout))
        out = dict(line)
        out.update({"ok": r["ok"], "ms": r["ms"], "error": r["error"],
                    "note": r.get("note") or "",
                    "note_detail": r.get("note_detail") or ""})
        return out

    with ThreadPoolExecutor(max_workers=min(8, len(lines))) as ex:
        results = list(ex.map(run, lines))

    # 上次选定的那条（None = 还没选过）。它在这次结果里还通，就继续用，不因为
    # 主代理恢复而切回去——这就是「互为备胎」：谁在扛着就一直扛，断了才换人。
    prev = ROUTER.preferred(prim)
    picked = None
    if prev:                       # "" 是直连：那不是备胎，代理能用了就该切走
        picked = next((r for r in results if r["ok"] and r["url"] == prev), None)
    kept = picked is not None
    if picked is None:
        picked = next((r for r in results if r["ok"]), None)
    for r in results:
        if r["url"]:                 # 直连不进健康表：它不是「一条通道」
            ROUTER.note(r["url"], r["ok"], r.get("error") or "", r.get("ms"),
                        note=r.get("note") or "", note_detail=r.get("note_detail") or "")

    # 只有「路由自己认识的线路」才值得记住走向：面板上填了还没保存的新地址，
    # 选它也没用——真正发请求时它不在表里，硬要先用只会多撞一次墙。
    known = [prim] + [c["url"] for c in ROUTER.items()]
    if picked is None:
        ROUTER.set_preferred(prim, None, "")
    elif picked["url"] == "" or picked["url"] in known:
        ROUTER.set_preferred(prim, picked["url"], picked["kind"], picked["name"])

    # 这句话会直接显示在卡片标题右边，短一点：好在手机上不被省略号截掉
    tail = ("· %s" % picked["note"]) if (picked and picked.get("note")) else ""
    if picked is None:
        text = "全都不通：每条线路和直连都失败"
    elif picked["kind"] == "direct":
        text = "线路都不通，已切到直连" + tail
    else:
        # 只报地址，不报「主/备」：面板上已经没有主次这个概念了
        label = "「%s」" % short_name(picked["url"])
        ms = ("（%d ms）" % picked["ms"]) if picked["ms"] else ""
        if kept:
            text = "正在用%s%s" % (label, ms)
        elif prev is None:
            text = "现在走%s%s" % (label, ms)
        else:
            text = "已切到%s%s" % (label, ms)
        if tail:
            text += tail
    return {"ok": picked is not None, "lines": results,
            "picked": picked["url"] if picked else None,
            "picked_kind": picked["kind"] if picked else "",
            "picked_note": (picked or {}).get("note") or "",
            "picked_note_detail": (picked or {}).get("note_detail") or "",
            "kept": bool(kept), "switched_from": prev,
            "failed": [r["name"] for r in results if not r["ok"]],
            "text": text}



def has_lines(cfg=None):
    """配置里有没有可测的东西（填了地址，或还有启用的通道）。定时复测靠它跳过空配置。"""
    if cfg is None:
        return bool(ROUTER.items())
    try:
        if str(cfg.get("telegram.proxy") or "").strip():
            return True
        if str(cfg.get("nyaa.proxy") or "").strip():   # 老配置：只填了检索代理
            return True
        return bool(clean_items(cfg.get("proxy_channels") or []))
    except Exception:                # 配置坏了就当没有，别让后台线程崩
        return False


def config_primary(cfg):
    """配置里「第一行」那个地址：telegram.proxy，没填就退回老的 nyaa.proxy。"""
    try:
        for key in ("telegram.proxy", "nyaa.proxy"):
            val = str(cfg.get(key) or "").strip()
            if val:
                return val
    except Exception:
        pass
    return ""


def autotest_config(cfg, timeout=_PROBE_TIMEOUT):
    """按当前配置跑一次自动选路（后台定时任务用）。"""
    return auto_test(config_primary(cfg), ROUTER.items(), timeout=timeout)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
ROUTER = Router()


def install(cfg=None):
    """把配置里的通道装进全局路由，并挂到 util 的两个钩子上。

    ``cfg`` 可以是 Config 对象（有 ``.get``），也可以是普通 dict。
    """
    raw = []
    auto = True
    if cfg is not None:
        try:
            raw = cfg.get("proxy_channels") or []
            auto = cfg.get("proxy_auto") is not False
        except Exception:      # 配置坏了也不能让程序起不来
            raw = []
    before = [c["url"] for c in ROUTER.items()]
    ROUTER.set_items(raw)
    # 线路表变了（加了/删了/改了通道），之前自动选中的那条可能已经不存在——
    # 留着它会让所有请求先去撞一个死地址
    if before != [c["url"] for c in ROUTER.items()]:
        ROUTER.clear_preferred()
    ROUTER.auto = auto
    util.set_proxy_chain(ROUTER.candidates)
    util.set_proxy_notice(ROUTER.note)
    return ROUTER


def status(primary=""):
    return ROUTER.status(primary)
