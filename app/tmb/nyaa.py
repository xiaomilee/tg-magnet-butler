"""sukebei.nyaa.si 检索与解析。

主通道用 RSS（`?page=rss`，结构稳定、字段齐全），失败或为空时回退到
HTML 结果页解析。两条通道最终都归一化成同一份 dict 结构，
并统一利用 infohash 补全 magnet（RSS 里没有 magnet，只有 infohash）。
"""
from __future__ import annotations

import html as htmllib
import logging
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

from . import util

log = logging.getLogger("tmb.nyaa")

_RATE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_MIN_INTERVAL = 1.1  # 对站点保持克制，避免被 Cloudflare 拦

_ITEM_FIELDS = {
    "title": "title",
    "link": "link",
    "guid": "guid",
    "pubdate": "pub_date",
    "seeders": "seeders",
    "leechers": "leechers",
    "downloads": "downloads",
    "infohash": "infohash",
    "category": "category",
    "categoryid": "category_id",
    "size": "size",
    "trusted": "trusted",
    "remake": "remake",
}


def _throttle():
    with _RATE_LOCK:
        delta = time.time() - _LAST_CALL[0]
        if delta < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - delta)
        _LAST_CALL[0] = time.time()


def build_search_url(cfg: dict, query: str, category="0_0", filt="0", sort="seeders",
                     page=1, rss=True):
    base = (cfg.get("base_url") or "https://sukebei.nyaa.si").rstrip("/")
    params = []
    if rss:
        params.append(("page", "rss"))
    params += [("q", query or ""), ("c", category or "0_0"),
               ("f", filt or "0"), ("s", sort or "seeders"), ("o", "desc")]
    if page and int(page) > 1:
        params.append(("p", str(int(page))))
    return base + "/?" + urllib.parse.urlencode(params)


def make_magnet(infohash: str, title: str, trackers=None) -> str:
    if not infohash:
        return ""
    tr = "".join("&tr=" + urllib.parse.quote(t, safe="") for t in (trackers or []))
    return ("magnet:?xt=urn:btih:%s&dn=%s%s"
            % (infohash, urllib.parse.quote(title or "", safe=""), tr))


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_rss(xml_text: str):
    """解析 nyaa/sukebei 的 RSS，返回结果列表。"""
    if not xml_text:
        return []
    xml_text = xml_text.lstrip("\ufeff").strip()
    if "<rss" not in xml_text[:2000] and "<?xml" not in xml_text[:2000]:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.debug("RSS 解析失败: %s", e)
        return []
    channel = root.find("channel")
    if channel is None:
        channel = root
    out = []
    for item in channel.findall("item"):
        rec = {}
        for child in list(item):
            key = _ITEM_FIELDS.get(_localname(child.tag))
            if key:
                rec[key] = (child.text or "").strip()
        title = rec.get("title") or ""
        infohash = (rec.get("infohash") or "").strip()
        view_url = rec.get("guid") or rec.get("link") or ""
        vid = ""
        m = re.search(r"/view/(\d+)", view_url)
        if m:
            vid = m.group(1)
        out.append({
            "id": vid,
            "title": title,
            "view_url": view_url,
            "torrent_url": rec.get("link") or "",
            "infohash": infohash.upper(),
            "magnet": "",
            "size_text": rec.get("size") or "",
            "size_bytes": util.parse_size(rec.get("size") or ""),
            "seeders": _int(rec.get("seeders")),
            "leechers": _int(rec.get("leechers")),
            "downloads": _int(rec.get("downloads")),
            "category": rec.get("category") or "",
            "category_id": rec.get("category_id") or "",
            "pub_ts": _parse_pubdate(rec.get("pub_date") or ""),
            "trusted": (rec.get("trusted") or "").lower() == "yes",
            "remake": (rec.get("remake") or "").lower() == "yes",
            "hidden": False,
            "source": "rss",
        })
    return out


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _parse_pubdate(text):
    text = (text or "").strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            st = time.strptime(text, fmt)
            return time.mktime(st)
        except ValueError:
            continue
    return 0.0


# ---------------- HTML 结果页 ----------------
# 站点模板（nyaadevs/nyaa 的 nyaa/templates/search_results.html）把行状态写在
# <tr class> 上，含义与配色一一对应：
#   deleted（灰，已删） / warning（黄，hidden） / danger（红，remake 重制）
#   success（绿，trusted 可信发布者） / default（白，普通）
# 也就是说绿底 = 上传者是站点认证的可信发布者，跟做种数、热度无关。
# 磁力就在 Link 列（第 4 列）的 <a href="magnet:?xt=urn:btih:...">，同一格里还有个
# <a href="/download/<id>.torrent">。注意 Jinja 自动转义会把 & 写成 &amp;，
# 取到后必须反转义，否则 dn / tr 参数全废（只剩 xt 还能用）。
_TR_RE = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.S | re.I)
_ROW_CLASS_RE = re.compile(r"""class\s*=\s*["']([^"']*)["']""", re.I)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']*)["']""", re.I)
_TD_RE = re.compile(r"<td[^>]*class=\"[^\"]*text-center[^\"]*\"[^>]*>(.*?)</td>", re.S | re.I)
_VIEW_A_RE = re.compile(
    r"""<a\b([^>]*?href\s*=\s*["'][^"']*/view/(\d+)[^>]*)>(.*?)</a>""", re.S | re.I)
_BTIH_RE = re.compile(r"btih:([0-9a-zA-Z]{32,40})", re.I)
_NUM_TD_RE = re.compile(r"^[\d,]+$")

# 站点状态 -> 我们的字段
_ROW_STATES = {
    "success": {"trusted": True},
    "danger": {"remake": True},
    "warning": {"hidden": True},
    "deleted": {"deleted": True},
}


def magnet_hash(magnet: str) -> str:
    """从磁力链接里取 infohash（兼容 40 位 hex 与 32 位 base32），统一大写。"""
    m = _BTIH_RE.search(magnet or "")
    return m.group(1).upper() if m else ""


def _row_state(attrs: str) -> dict:
    m = _ROW_CLASS_RE.search(attrs or "")
    tokens = (m.group(1) if m else "").lower().split()
    state = {"trusted": False, "remake": False, "hidden": False, "deleted": False}
    for tok in tokens:
        state.update(_ROW_STATES.get(tok) or {})
    return state


def _row_magnet(row: str) -> str:
    """Link 列里的磁力：优先 href，兜底扫原文（个别镜像写进 onclick/data-*）。"""
    for href in _HREF_RE.findall(row):
        href = htmllib.unescape(href).strip()
        if href.lower().startswith("magnet:") and _BTIH_RE.search(href):
            return href
    m = re.search(r"magnet:\?[^\s\"'<>]+", htmllib.unescape(row), re.I)
    return m.group(0).strip() if m and _BTIH_RE.search(m.group(0)) else ""


def _row_title(row: str):
    """取标题：优先 /view/<id> 那个 <a> 的 title 属性，其次它的正文。"""
    for m in _VIEW_A_RE.finditer(row):
        attrs, vid, inner = m.group(1), m.group(2), m.group(3)
        t = re.search(r"""title\s*=\s*["']([^"']*)["']""", attrs, re.I)
        title = htmllib.unescape(t.group(1)).strip() if t else ""
        if not title:
            title = htmllib.unescape(util.strip_html(inner)).strip()
        # 同一行还有「评论数」那个指向 /view/N#comments 的链接，正文是纯数字，跳过
        if title and not title.isdigit():
            return title, vid
    return "", ""


def parse_html(html: str):
    """HTML 结果页解析。绿行（success=可信发布）的标记也在这里读出来。"""
    if not html or "<tr" not in html:
        return []
    out = []
    for attrs, row in _TR_RE.findall(html):
        state = _row_state(attrs)
        if state["deleted"]:
            continue  # 已删记录站点仍会列出，但下不动
        magnet = _row_magnet(row)
        title, vid = _row_title(row)
        if not magnet and not vid:
            continue  # 表头、空行
        infohash = magnet_hash(magnet)
        if not title and magnet:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
            title = (q.get("dn") or [""])[0]

        texts = [util.strip_html(t).strip() for t in _TD_RE.findall(row)]
        nums = [t for t in texts if _NUM_TD_RE.match(t.replace(",", ""))]
        seeders = leechers = downloads = 0
        if len(nums) >= 3:
            seeders = _int(nums[-3].replace(",", ""))
            leechers = _int(nums[-2].replace(",", ""))
            downloads = _int(nums[-1].replace(",", ""))
        elif len(nums) == 2:
            seeders, leechers = _int(nums[0]), _int(nums[1])
        size_text = ""
        for t in texts:
            if re.search(r"\d\s*(?:[KMGT]i?B|B)\b", t, re.I):
                size_text = t
                break
        ts = re.search(r'data-timestamp="(\d+)"', row)
        cat = re.search(r'href="/\?c=([\d_]+)"', row)
        tm = re.search(r"""href\s*=\s*["']([^"']*/download/\d+\.torrent)["']""", row, re.I)

        out.append({
            "id": vid,
            "title": title,
            "view_url": ("/view/" + vid) if vid else "",
            "infohash": infohash,
            "magnet": magnet,
            "torrent_url": htmllib.unescape(tm.group(1)) if tm else "",
            "size_text": size_text,
            "size_bytes": util.parse_size(size_text),
            "seeders": seeders,
            "leechers": leechers,
            "downloads": downloads,
            "category": "",
            "category_id": (cat.group(1) if cat else ""),
            "pub_ts": float(ts.group(1)) if ts else 0.0,
            "trusted": state["trusted"],
            "remake": state["remake"],
            "hidden": state["hidden"],
            "source": "html",
        })
    return out


def finalize(items, trackers, base=""):
    """补全 magnet / 绝对链接 / 去重。"""
    base = (base or "https://sukebei.nyaa.si").rstrip("/")
    seen = set()
    out = []
    for it in items:
        ih = (it.get("infohash") or "").upper()
        if not ih:
            ih = magnet_hash(it.get("magnet") or "")
            if ih:
                it["infohash"] = ih
        key = ih or (it.get("id") or it.get("title"))
        if key in seen:
            continue
        seen.add(key)
        if not it.get("magnet") and ih:
            it["magnet"] = make_magnet(ih, it.get("title") or "", trackers)
        for field in ("view_url", "torrent_url"):
            if (it.get(field) or "").startswith("/"):
                it[field] = base + it[field]
        out.append(it)
    return out


def _mock_items(query, limit=20):
    """离线演示数据（TMB_MOCK=1 时启用），用于无外网环境验证链路与界面。"""
    cats = [("Art - Anime", "1_1"), ("Art - Doujinshi", "1_2"), ("Art - Manga", "1_4")]
    sizes = [("1.2 GiB", int(1.2 * 1024 ** 3)), ("850.0 MiB", int(850 * 1024 ** 2)),
             ("3.4 GiB", int(3.4 * 1024 ** 3))]
    seeders = [128, 42, 7, 301, 15, 63]
    now = time.time()
    out = []
    for i in range(min(int(limit), 6)):
        cat, cid = cats[i % len(cats)]
        title = "%s 示例资源 %s [DemoGroup]" % (
            query, ["1080p", "720p", "BDRip", "合集", "Vol.1", "中字"][i % 6])
        size_text, size_bytes = sizes[i % 3]
        out.append({
            "id": str(90000 + i),
            "title": title,
            "view_url": "https://sukebei.nyaa.si/view/%d" % (90000 + i),
            "torrent_url": "https://sukebei.nyaa.si/download/%d.torrent" % (90000 + i),
            "infohash": ("%040X" % (abs(hash(title)) % (16 ** 40))),
            "magnet": "",
            "size_text": size_text,
            "size_bytes": size_bytes,
            "seeders": seeders[i],
            "leechers": [4, 1, 0, 12, 2, 5][i],
            "downloads": [5200, 1800, 240, 9900, 310, 1200][i],
            "category": cat,
            "category_id": cid,
            "pub_ts": now - (i * 3 + 1) * 86400,
            "trusted": i % 3 == 0,
            "remake": False,
            "source": "mock",
        })
    return out


def search(cfg: dict, query: str, category="0_0", filt="0", sort="seeders",
           page=1, limit=20, proxy=None):
    """检索 sukebei。返回归一化结果列表（失败抛 HttpError）。

    cfg 既可以是完整配置，也可以只是 nyaa 段。proxy 传 None 时自己从配置里找；
    pipeline 传的是 nyya 段，看不到 telegram.proxy，所以由调用方显式传更稳。

    两条通道（RSS / HTML）串行尝试，并受总时限约束，避免在无外网的机器上
    卡住一两分钟。默认总预算 = timeout * 2。
    """
    full = cfg if (isinstance(cfg, dict) and isinstance(cfg.get("nyaa"), dict)) else None
    nyaa_cfg = full["nyaa"] if full else cfg
    timeout = int(nyaa_cfg.get("timeout") or 25)
    trackers = nyaa_cfg.get("trackers") or []

    if proxy is None:
        proxy = (nyaa_cfg.get("proxy")
                 or ((full or {}).get("telegram") or {}).get("proxy")
                 or "")
    proxy, proxy_src = util.effective_proxy(proxy)

    base = (nyaa_cfg.get("base_url") or "https://sukebei.nyaa.si").rstrip("/")

    if os.environ.get("TMB_MOCK") == "1":
        log.info("离线演示模式（TMB_MOCK=1）：返回示例数据")
        return finalize(_mock_items(query, limit), trackers, base)[:limit]

    # socks 代理由 urllib 直接支持不了，先说清楚，别让用户对着 errno 111 猜
    if proxy:
        parsed = util.parse_proxy(proxy)
        if parsed and parsed[0].startswith("socks"):
            raise util.HttpError(
                "代理 %s 是 socks 协议，程序用 urllib 直连，不支持 socks5://。"
                "请改填代理的 HTTP 端口（Clash/V2Ray 的 mixed 或 http 端口，通常是 7890）。"
                % proxy)
        log.info("检索使用代理 %s（来源 %s）", proxy, proxy_src)
    else:
        log.info("检索直连（未配置代理）")

    deadline = time.time() + max(8, timeout * 2)
    errors = []

    def _via():
        return "经代理 %s" % proxy if proxy else "直连"

    # 通道一：RSS
    url = build_search_url(nyaa_cfg, query, category, filt, sort, page, rss=True)
    items = []
    try:
        log.info("搜索(RSS): %s", url)
        _throttle()
        remaining = max(4, min(timeout, deadline - time.time()))
        status, text, _ = util.http_request(url, timeout=remaining, proxy=proxy, retries=0)
        items = parse_rss(text)
        if not items and "Just a moment" in text:
            errors.append("RSS 通道遇到 Cloudflare 人机验证")
    except util.HttpError as e:
        log.warning("RSS 通道失败：%s", e)
        errors.append("RSS|%s" % e)

    # 通道二：HTML 结果页
    if not items:
        url2 = build_search_url(nyaa_cfg, query, category, filt, sort, page, rss=False)
        try:
            log.info("搜索(HTML): %s", url2)
            _throttle()
            remaining = max(4, min(timeout, deadline - time.time()))
            status, text, _ = util.http_request(url2, timeout=remaining, proxy=proxy, retries=0)
            items = parse_html(text)
            if not items and ("Just a moment" in text or "cdn-cgi" in text):
                raise util.HttpError("站点触发了 Cloudflare 人机验证，请稍后重试或换代理出口")
        except util.HttpError as e:
            log.warning("HTML 通道失败：%s", e)
            errors.append("HTML|%s" % e)
            raise util.HttpError(
                "无法访问 %s：%s。%s" % (
                    nyaa_cfg.get("base_url") or "sukebei.nyaa.si",
                    _compact(errors, proxy),
                    _advice(errors, proxy)))

    items = finalize(items, trackers, base)
    # 先按做种降序；同做种时可信发布（绿行）优先，再比新旧
    items.sort(key=lambda x: (-x.get("seeders", 0), not x.get("trusted"),
                             -x.get("pub_ts", 0)))
    return items[:limit]


def _compact(errors, proxy):
    """两条通道往往同一个原因，去重压成一句，别在 Telegram 里刷屏。"""
    cleaned = []
    for e in errors:
        t = str(e).split("|", 1)[-1]      # 去掉 "RSS|" 之类的前缀
        if proxy:
            t = t.replace("经代理 %s " % proxy, "").replace("经代理 %s" % proxy, "")
        t = t.split("。")[0].strip()      # 只留第一句（含原因与耗时）
        t = t.strip(" :：")
        if t and t not in cleaned:
            cleaned.append(t)
    if not cleaned:
        return "未知原因"
    if len(cleaned) == 1:
        return "%s（RSS 与 HTML 两次均是此结果）" % cleaned[0]
    return "；".join(cleaned)


def _advice(errors, proxy):
    """根据失败文本给一句能直接照做的建议。"""
    blob = " ".join(errors)
    # 代理来自环境变量时说清楚，否则用户会以为程序没配代理却在硬走代理
    env_note = ""
    if proxy and proxy in set(util.env_proxies().values()):
        env_note = "（当前在用环境变量里的代理 %s，想换就在设置里显式填）" % proxy
    if "502" in blob or "Tunnel connection" in blob:
        return ("代理端口是通的，但它到站点的隧道失败了（502）：多半是代理节点挂了、"
                "或订阅规则把该域名拦了。换个节点，或把 sukebei.nyaa.si 加进代理规则。")
    if "407" in blob or "Proxy Authentication" in blob:
        return "代理要求认证：把地址写成 http://用户名:密码@主机:端口 再试。"
    if "403" in blob:
        return "站点返回 403：可能是 Cloudflare 拦了该出口 IP，换节点或稍后重试。"
    if "连接被拒绝" in blob:
        if proxy:
            return ("TCP 被立刻拒绝，基本可以断定代理端口没在监听：确认代理进程在跑、"
                    "地址和端口填对，且该端口是 HTTP 端口而非 socks%s。改完在面板点「网络诊断」复测。"
                    % env_note)
        return "TCP 被立刻拒绝。若你是被墙环境，需要配一个可用的 HTTP 代理。"
    if "连接超时" in blob or "被重置" in blob:
        return ("连不通且是超时/重置，属于典型被墙表现：在设置里填代理"
                "（形如 http://192.168.1.10:7890），并确认 NAS 能访问该代理%s。" % env_note)
    if "域名解析失败" in blob:
        return "DNS 解析不出来：检查 NAS 的 DNS，或把代理地址写成 IP。"
    return "在面板里点「网络诊断」逐层查看 DNS / TCP / 代理哪一层断的。"


def torrent_url_by_hash(cfg: dict, infohash: str, proxy=None):
    """按 infohash 找回这条种子的 `.torrent` 地址，找不到返回 ``""``。

    为什么要它：迅雷的解析接口对裸磁力有时只回一个目录节点，认不出文件清单，
    文件筛选就等于没开。种子里 `info.files` 才是权威清单，而站点上这条种子的
    `.torrent` 地址是可以按 infohash 精确定位的。

    两个坑：
    - RSS 通道**不支持**按 infohash 查（回一个空 feed），所以这里只走 HTML；
    - 搜索结果只有一条时站点会 302 到 `/view/N`，那一页里只有一个 `/download/N.torrent`，
      这时候直接用它；多条结果时按行匹配 infohash，**不能拿第一个链接凑数**
      （拿错种子 = 按错清单挑文件，比全下更糟）。
    """
    infohash = (infohash or "").strip().lower()
    if len(infohash) != 40:
        return ""
    full = cfg if (isinstance(cfg, dict) and isinstance(cfg.get("nyaa"), dict)) else None
    nyaa_cfg = full["nyaa"] if full else (cfg or {})
    base = (nyaa_cfg.get("base_url") or "https://sukebei.nyaa.si").rstrip("/")
    if proxy is None:
        proxy = (nyaa_cfg.get("proxy")
                 or ((full or {}).get("telegram") or {}).get("proxy") or "")
    proxy, _src = util.effective_proxy(proxy)
    if os.environ.get("TMB_MOCK") == "1":
        return ""
    timeout = int(nyaa_cfg.get("timeout") or 25)
    url = "%s/?q=%s&f=0&c=0_0" % (base, urllib.parse.quote(infohash))
    try:
        status, text, _raw = util.http_request(url, timeout=timeout, proxy=proxy, retries=1)
    except util.HttpError as e:
        log.info("按 infohash 找种子失败：%s", e)
        return ""
    if status >= 400 or not text:
        return ""
    if infohash not in text.lower():
        return ""
    links = re.findall(r"""href=["']([^"']*/download/\d+\.torrent)["']""", text)
    # 单条命中时站点会 302 到 /view/N。判断「这是不是种子详情页」不能只看链接数量：
    # 列表页也可能只有一个结果，这时页面里同样有 hash 字样，但那个链接未必属于它。
    # 详情页有文件列表块（torrent-file-list），这是它独有的标记。
    if len(links) == 1 and "torrent-file-list" in text:
        return urllib.parse.urljoin(base + "/", links[0])
    for it in parse_html(text):
        if (it.get("infohash") or "").lower() == infohash and it.get("torrent_url"):
            # 行里的 href 是相对路径（/download/N.torrent），得补成绝对地址才能直接下
            return urllib.parse.urljoin(base + "/", it["torrent_url"])
    return ""


def probe(cfg: dict, query="test"):
    """连通性自检，给 Web UI 的「测试连接」用。"""
    t0 = time.time()
    try:
        items = search(cfg, query, limit=5)
        return {
            "ok": True,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "count": len(items),
            "sample": items[0]["title"] if items else "",
            "source": items[0]["source"] if items else "",
        }
    except Exception as e:
        return {"ok": False, "elapsed_ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, e)}
