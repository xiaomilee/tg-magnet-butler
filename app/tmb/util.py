"""通用工具：HTTP 请求、体积/时间解析、JSON 原子读写。

只依赖标准库，确保飞牛 fnOS 自带 python3 即可运行。
"""
from __future__ import annotations

import errno
import gzip
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class HttpError(Exception):
    def __init__(self, msg, status=None, body=b""):
        super().__init__(msg)
        self.status = status
        self.body = body


# 迅雷/飞牛这类接口把令牌放在 query 里（``?pan_auth=...``）。报错会把整个 URL 带出来，
# 用户复制给我们看排查信息时令牌就跟着外传了——打码，但保留前几位够辨认。
_SECRET_KEYS = ("pan_auth", "fnos-token", "fnos_token", "token", "auth",
                "password", "passwd", "pwd", "key", "secret")


def mask_url(url):
    """给 URL 里的敏感参数打码（排障要看地址，不需要看令牌）。"""
    text = str(url)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return text
    if not parts.query:
        return text
    out, changed = [], False
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        low = k.lower()
        if len(v) >= 6 and any(s in low for s in _SECRET_KEYS):
            # 掩码用纯 ASCII：URL 会再 quote 一遍，… 会被编码成 %E2%80%A6，反而难读
            v = v[:4] + "***" + v[-2:]
            changed = True
        out.append((k, v))
    if not changed:
        return text
    query = "&".join("%s=%s" % (urllib.parse.quote(k, safe=""),
                                urllib.parse.quote(v, safe="*"))
                     for k, v in out)
    return urllib.parse.urlunsplit(parts._replace(query=query))


_HINT_REFUSED = ("目标端口没有服务在监听。配了代理的话，多半是代理没启动、端口写错，"
                 "或代理只监听 127.0.0.1 而程序不在同一网络里。")
_HINT_TIMEOUT = "数据包没回应，常见于被墙、地址不可达或代理链路中断。"
_HINT_RESET = "握手中断，常见于中间设备干扰（被墙的典型表现）。"

# 网络错误码 -> (短描述, 该怎么修)。NAS 上排障全靠这段。
# 前半是 Linux/POSIX errno，后半是 Windows 的 WSA 码（开发机用得上）。
_ERRNO_HINTS = {
    errno.ECONNREFUSED: ("连接被拒绝", _HINT_REFUSED),
    errno.ETIMEDOUT: ("连接超时", _HINT_TIMEOUT),
    errno.ECONNRESET: ("连接被重置", _HINT_RESET),
    errno.ENETUNREACH: ("网络不可达", "路由不通，检查 NAS 网络与网关设置。"),
    errno.EHOSTUNREACH: ("主机不可达", "目标地址不可达，检查路由或代理。"),
    errno.EADDRNOTAVAIL: ("本地地址不可用", "本机网络配置异常。"),
    10061: ("连接被拒绝", _HINT_REFUSED),
    10060: ("连接超时", _HINT_TIMEOUT),
    10054: ("连接被重置", _HINT_RESET),
}


def explain_net(exc, proxy=""):
    """把底层网络异常翻成「人话 + 建议」，并标明是否走了代理。"""
    via = "经代理 %s " % proxy if proxy else ""
    reason = getattr(exc, "reason", exc)
    code = getattr(reason, "errno", None)
    if code is None:
        code = getattr(exc, "errno", None)

    if isinstance(reason, socket.gaierror):
        return "%s域名解析失败（%s）。检查 DNS，或把代理地址写成 IP。" % (via, reason)
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        # 经代理时「握手中断」几乎不是 CA 的问题：是代理那头把连接关了
        # （节点出去连不上、或规则没放行）。这时劝人设 TMB_INSECURE_TLS 是南辕北辙，
        # 只会把证书校验关掉、问题照旧。
        low = str(reason).lower()
        cut = any(k in low for k in ("eof", "closed", "connection reset",
                                     "broken pipe", "handshake failure"))
        if proxy and cut:
            return ("%sTLS 握手中断（%s）：代理在握手阶段就把连接关掉了，"
                    "多半是这个节点出去连不上，或代理规则没放行。换条线路再试"
                    "（备用通道 / 代理里换个节点）；同一台 NAS 上换个节点就好，那和系统 CA 无关。"
                    % (via, reason))
        return ("%sTLS 握手失败（%s）。NAS 系统 CA 过期时可设 TMB_INSECURE_TLS=1 临时跳过。"
                % (via, reason))
    if code in _ERRNO_HINTS:
        short, hint = _ERRNO_HINTS[code]
        return "%s%s（errno %s）。%s" % (via, short, code, hint)
    text = str(reason)
    if "timed out" in text.lower():
        return "%s连接超时。%s" % (via, _ERRNO_HINTS[errno.ETIMEDOUT][1])
    return "%s%s" % (via, text)


def explain_net_short(exc, proxy=""):
    """直连场景的短文案：去掉「配了代理的话…」那半句，避免误导。

    下载器都在 NAS 本机/内网，报错里扯代理只会让人白折腾一次。
    """
    text = explain_net(exc, proxy)
    if not proxy:
        cut = text.find("。配了代理的话")
        if cut != -1:
            text = text[:cut] + "。"
    return text


def parse_proxy(proxy):
    """拆出 (scheme, host, port)；识别 socks，好给出明确提示而不是 errno 111。"""
    if not proxy:
        return None
    p = proxy.strip()
    if "://" not in p:
        p = "http://" + p
    u = urllib.parse.urlsplit(p)
    scheme = (u.scheme or "http").lower()
    default_port = 443 if scheme == "https" else (1080 if scheme.startswith("socks") else 8080)
    return scheme, u.hostname, (u.port or default_port)


def env_proxies():
    """环境变量里设了哪些代理。urllib 会自动使用，用户常常忘了这茬。"""
    out = {}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(k)
        if v:
            out[k] = v
    return out


def effective_proxy(proxy=""):
    """实际生效的代理：显式配置优先，否则回落环境变量。返回 (proxy, 来源)。"""
    if proxy and str(proxy).strip():
        return str(proxy).strip(), "config"
    env = env_proxies()
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        if env.get(k):
            return env[k].strip(), "env:" + k
    return "", ""


# ---------------------------------------------------------------- 代理线路
# 降级顺序由 proxychan 决定，util 只问一句「这次按什么顺序试」。
# 没装钩子时行为跟以前一模一样：就试配置里那一条。
_PROXY_CHAIN = None
_PROXY_NOTICE = None


def set_proxy_chain(fn):
    """fn(configured) -> 代理地址列表，第一个最该用；空串表示直连。"""
    global _PROXY_CHAIN
    _PROXY_CHAIN = fn


def set_proxy_notice(fn):
    """fn(url, ok, error="") —— 真实请求跑完后回报这条线路的健康度。"""
    global _PROXY_NOTICE
    _PROXY_NOTICE = fn


def _dedup_proxy(items):
    out, seen = [], set()
    for u in items:
        u = str(u or "").strip()
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def proxy_chain(configured=""):
    """这次请求要依次尝试的代理列表（去重、主代理一定在里面）。"""
    conf = str(configured or "").strip()
    if _PROXY_CHAIN is None:
        return [conf]
    try:
        chain = _dedup_proxy(list(_PROXY_CHAIN(conf) or []))
    except Exception:       # 路由自己坏了，退回「只用配置里那条」
        return [conf]
    if not chain:
        return [conf]
    if conf and conf not in chain:
        chain.insert(0, conf)
    return chain


def _note_proxy(url, ok, error=""):
    if not _PROXY_NOTICE:
        return
    try:
        _PROXY_NOTICE(url, bool(ok), error)
    except Exception:
        pass


def _build_opener(proxy: str = "", use_env: bool = True):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    elif not use_env:
        # 显式清空：否则 urllib 会偷偷套用 http_proxy/https_proxy 环境变量。
        # 内网地址（迅雷 127.0.0.1:2345 之类）走代理必然失败，必须先关掉。
        handlers.append(urllib.request.ProxyHandler({}))
    else:
        # 显式清空，避免 urllib 偷偷套用环境变量代理导致行为不可预期
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    # 部分 NAS 系统 CA 过期，这里不退化成不校验，只允许环境变量关闭
    if os.environ.get("TMB_INSECURE_TLS") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


# WireGuard / Tailscale 常用 100.64.0.0/10（RFC 6598 CGNAT）。Python 的 ipaddress
# 不把它算进 is_private，不特判就会被当成公网丢给代理——而代理到不了家里的 NAS。
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _decompress(raw, content_encoding=""):
    """按 Content-Encoding 解压；头缺失但内容确是压缩流时也认。

    迅雷网关有版本会漏掉 ``Content-Encoding: gzip``，直接 ``read()`` 拿到的是压缩流，
    再按文本解码就是满屏乱码（用户看到的就是这种）。所以解压失败不报错，原样返回。
    """
    if not raw:
        return raw
    enc = (content_encoding or "").lower()

    def _try_gzip(buf):
        try:
            return gzip.decompress(buf)
        except (OSError, EOFError):
            return None

    def _try_zlib(buf):
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(buf, wbits)
            except zlib.error:
                continue
        return None

    if "gzip" in enc:
        return _try_gzip(raw) or raw
    if "deflate" in enc:
        return _try_zlib(raw) or raw
    # 头里什么都没写，但确实是压缩流（1f 8b = gzip，78 = zlib）
    if raw[:2] == b"\x1f\x8b":
        return _try_gzip(raw) or raw
    if raw[:1] == b"\x78":
        return _try_zlib(raw) or raw
    return raw


def is_local_url(url):
    """目标是否本机/内网地址。这类请求不该走代理（相当于 NO_PROXY）。"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return ip.version == 4 and ip in _CGNAT


def route_of(url, proxy=""):
    """这条请求会走直连还是代理。排障时一眼看清「是不是代理把它带歪了」。"""
    local = is_local_url(url)
    conf = str(proxy or "").strip()
    return {
        "local": local,
        "proxy": "" if local else conf,
        "direct": local or not conf,
        "why": ("目标是内网/保留地址，强制直连、忽略代理" if local else
                "目标是公网地址，按配置经代理 %s" % conf if conf else
                "没配代理，直连"),
    }


def http_request(url, method="GET", headers=None, data=None, timeout=25,
                 proxy="", retries=2, backoff=1.5):
    """带重试的 HTTP 请求，返回 (status, text, raw_bytes)。

    data 为 dict 时按 JSON 提交，为 bytes/str 时原样提交。
    目标是内网地址时自动绕过代理：本地服务（迅雷/aria2/qb）走代理只会失败。
    """
    local = is_local_url(url)
    if local:
        proxy = ""
    # 一条线路一次机会：第 N 次重试自动换第 N 条代理（末条会一直用下去）
    chain = [""] if local else proxy_chain(proxy)
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        # 不主动要压缩：迅雷网关有的版本会压缩却不回 Content-Encoding，客户端读到的就是
        # 一屏乱码（实测踩过）。真被压了也还有 _decompress 兜底。
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v is not None})

    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data

    last = None
    t0 = time.time()
    for attempt in range(retries + 1):
        use = chain[min(attempt, len(chain) - 1)]
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            opener = _build_opener(use, use_env=not local)
            with opener.open(req, timeout=timeout) as resp:
                raw = _decompress(resp.read(), resp.headers.get("Content-Encoding") or "")
                charset = resp.headers.get_content_charset() or _guess_charset(raw)
                _note_proxy(use, True)
                return resp.status, raw.decode(charset, "replace"), raw
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            raw = _decompress(raw, (e.headers.get("Content-Encoding") or "")
                              if e.headers else "")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                # 502/503/504 多半是代理自己挂了，算这条线路一次失败
                _note_proxy(use, False, "HTTP %s" % e.code)
                last = HttpError("HTTP %s" % e.code, e.code, raw)
                time.sleep(backoff * (attempt + 1))
                continue
            # 拿到了响应（哪怕是 404/401），说明这条线路是通的
            _note_proxy(use, True)
            raise HttpError("HTTP %s %s" % (e.code, mask_url(url)), e.code, raw)
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError,
                http.client.HTTPException) as e:
            # IncompleteRead 也归这里：走代理下 .torrent 时常被中途掐断，
            # 至少重试一次比直接失败靠谱（截断的种子解不出文件清单）。
            # 带上耗时：连接被拒是毫秒级，超时是秒级，一眼能分开
            msg = "%s（%.1fs）" % (explain_net(e, use), time.time() - t0)
            _note_proxy(use, False, msg)
            last = HttpError(msg)
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise last
    raise last or HttpError("request failed")


def _read_partial(resp):
    """读满响应体；被中途掐断时把**已经读到的部分**带回来。

    走代理下小文件经常这样断：``Content-Length`` 说 21761，实际只给到 16087
    （``IncompleteRead(16087 bytes read, 5674 more expected)``）。那 16 KB 是好数据，
    扔了太可惜——断点续传就靠它接着把剩下的要走完。
    """
    try:
        return resp.read(), False
    except http.client.IncompleteRead as e:
        return (e.partial or b""), True


def http_download(url, headers=None, timeout=20, proxy="", attempts=4,
                  max_bytes=8 * 1024 * 1024, budget=30):
    """下载一个小文件（实际上就是 ``.torrent``），**断了能接着下**。

    为什么不用 ``http_request``：

    * 走代理下 nyaa 的种子文件常在半截断流，整下重试十次也还是断在同一个字节数上，
      白等十几秒；可截断前那一段是完好的，用 ``Range: bytes=<已有字节>-`` 把剩下的
      续上，一次就能拼出完整文件。
    * 代理忽快忽慢（实测同一条 URL 5.8s / 18s / 22s 都有），所以不按「试几次」而是按
      **总预算 ``budget`` 秒**来控：在预算内一直试，超了就认输。按次数试的最坏情况
      会把用户干等一分钟。

    拿不到就抛 ``HttpError``，由调用方按「没有清单」降级——绝不半路返回半截数据。
    """
    local = is_local_url(url)
    if local:
        proxy = ""
    # 断了换线路续传：已经拿到的字节留在 buf 里，下一轮用 Range 接着要
    chain = [""] if local else proxy_chain(proxy)
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/x-bittorrent,*/*;q=0.8",
        "Accept-Encoding": "identity",   # 要的是二进制本身，别让中间层压一道
        "Connection": "close",
    }
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v is not None})

    buf = b""
    total = 0
    last = ""
    deadline = time.time() + max(1, budget)
    for attempt in range(max(1, attempts)):
        left = deadline - time.time()
        if left < 2:
            break
        use = chain[min(attempt, len(chain) - 1)]
        h = dict(hdrs)
        if buf:
            h["Range"] = "bytes=%d-" % len(buf)
        cut = False
        try:
            req = urllib.request.Request(url, headers=h, method="GET")
            opener = _build_opener(use, use_env=not local)
            # 单次超时不超过剩余预算：否则最后一次尝试会把总时限撑破
            with opener.open(req, timeout=min(timeout, left)) as resp:
                if resp.status == 206:
                    m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
                    if m:
                        total = int(m.group(1))
                else:
                    # 服务端不认 Range：这份就是全量，从头算
                    buf = b""
                    cl = (resp.headers.get("Content-Length") or "").strip()
                    total = int(cl) if cl.isdigit() else 0
                chunk, cut = _read_partial(resp)
        except urllib.error.HTTPError as e:
            if e.code == 416 and buf:
                return buf          # 已经下满了，服务端嫌 Range 越界
            raise HttpError("HTTP %s %s" % (e.code, mask_url(url)), e.code, b"")
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError,
                http.client.HTTPException) as e:
            last = explain_net(e, use)
            _note_proxy(use, False, last)
            continue

        if len(buf) + len(chunk) > max_bytes:
            raise HttpError("文件大得不像话（超过 %d MB）" % (max_bytes // 1048576))
        if not chunk:
            # 这一轮 0 字节：留着已经拿到的部分，下一轮从断点续（别丢掉重来）
            last = last or "服务端没有回数据"
            continue
        buf += chunk
        _note_proxy(use, True)

        if total and len(buf) >= total:
            return buf                      # 拼满了
        if not cut and not total:
            return buf                      # 服务端没给长度、读到 EOF：只能认定它完整

    raise HttpError("下载失败%s（已拿到 %d 字节）"
                    % ("：" + last if last else "", len(buf)))


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def looks_garbled(text) -> bool:
    """解码后是不是一屏乱码（压缩流/二进制被当文本读了）。

    判据宽松：替换字符或控制字符占到一成、且至少 4 个。宁可把个别生僻正文判成乱码
    （那只是少显示几个字），也别把整段二进制当「服务端原话」发给用户。
    """
    t = str(text or "")
    if not t:
        return False
    bad = t.count("\ufffd")
    bad += len(_CTRL_RE.findall(t))
    return bad >= 4 and bad * 10 >= len(t)


def _guess_charset(raw: bytes) -> str:
    head = raw[:4096].decode("ascii", "ignore").lower()
    m = re.search(r'charset=["\']?([a-z0-9_\-]+)', head)
    if m:
        return m.group(1)
    return "utf-8"


_SIZE_UNITS = {
    "b": 1, "byte": 1, "bytes": 1,
    "kb": 1000, "kib": 1024,
    "mb": 1000 ** 2, "mib": 1024 ** 2,
    "gb": 1000 ** 3, "gib": 1024 ** 3,
    "tb": 1000 ** 4, "tib": 1024 ** 4,
}


def parse_size(text: str) -> int:
    """'1.2 GiB' -> 字节数；无法解析返回 0。"""
    if not text:
        return 0
    m = re.search(r"([\d.,]+)\s*([a-zA-Z]+)", str(text))
    if not m:
        return 0
    num = m.group(1).replace(",", "")
    try:
        val = float(num)
    except ValueError:
        return 0
    unit = m.group(2).lower()
    return int(val * _SIZE_UNITS.get(unit, 1))


def human_size(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "?"
    if num <= 0:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return ("%.1f %s" % (num, unit)).replace(".0 ", " ")
        num /= 1024.0
    return "?"


def human_age(ts: float) -> str:
    if not ts:
        return "?"
    delta = max(0, time.time() - ts)
    if delta < 3600:
        return "%d 分钟前" % max(1, delta / 60)
    if delta < 86400:
        return "%d 小时前" % (delta / 3600)
    if delta < 86400 * 30:
        return "%d 天前" % (delta / 86400)
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def now_ts() -> float:
    return time.time()


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        return default


def save_json(path, data, mode=0o600):
    """原子写入，避免断电/崩溃写坏配置文件。"""
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def deep_merge(base: dict, patch: dict) -> dict:
    """把 patch 合并进 base 的**深拷贝**（dict 递归，其余覆盖）。

    必须 deepcopy：浅拷贝会让 cfg.data 与 DEFAULT_CONFIG 共用嵌套 dict，
    改一处配置就把模块级默认值一起改掉（表现为「重置默认」也回不去）。
    """
    import copy

    out = copy.deepcopy(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_path(data: dict, dotted: str, default=None):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(data: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return data


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


# ---------------- pan_auth 清理 ----------------
# pan_auth 是 JWT（三段、点分隔）。从 F12 里「复制令牌」时，粘到的往往是整条
# ``…/device/info/watch?pan_auth=eyJ…&device_space=``——直接存进配置的话，网关会
# 把这一整串当 token 发给迅雷云，回一句
# ``checkAuth failed: token contains an invalid number of segments``。
# 看着像「令牌过期」，其实只是形状不对：里面那段 JWT 好得很。
_PAN_AUTH_IN_TEXT = re.compile(
    r"""pan[-_]?auth[\"'\s]*(?:=|%3D|:)[\"'\s]*([A-Za-z0-9_.\-]{20,})""", re.I)


def pan_auth_looks_valid(token) -> bool:
    """像不像一个 JWT（三段、够长、不是一条地址）。只判形状，不解内容。"""
    text = str(token or "").strip()
    if not text or text.lower().startswith(("http://", "https://")):
        return False
    return text.count(".") == 2 and len(text) >= 60


def clean_pan_auth(value) -> str:
    """把「整条地址 / 带引号的片段 / Bearer 前缀」收拾成纯 JWT。

    干净的 JWT 原样返回；认不出形状的也原样返回（不猜、不截断），
    由调用方用 :func:`pan_auth_looks_valid` 决定要不要提示用户。
    """
    text = str(value or "").strip().strip("\"'").strip()
    if not text:
        return ""
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    for candidate in (text, text.replace("%3D", "=").replace("%3d", "=")):
        probe = candidate
        if "%" in probe:
            try:
                probe = urllib.parse.unquote(probe)
            except Exception:
                probe = candidate
        found = _PAN_AUTH_IN_TEXT.search(probe)
        if found:
            return found.group(1).strip().strip("\"'")
    return text.split("&")[0].strip()


# ---------------- 飞牛登录 Cookie ----------------
# 1.0.42：新版 fnOS 网关（迅雷套件 3.23.5 起）改用 ``fnwg_token`` + ``ost``
# 这一对做鉴权；更要命的是——**只要 Cookie 里出现 ``fnos-token`` 这个名字，
# 网关就整条拒**，值和位置都不看，连静态页都不给，回 13 字节 ``invalid token``。
# 2026-11-14 拿真 Cookie 逐项剥离实测：
#   fnwg_token + ost            → 200，任务列表正常
#   fnwg_token + ost + fnos-token → invalid token（放最前、放最后都一样）
# 所以「从 F12 复制整条 Cookie」这条路必须先洗一遍，否则越贴越糟。
_COOKIE_NEEDS_FNWG = "fnwg_token"
_COOKIE_DROP_WITH_FNWG = ("fnos-token", "fnos_token")


def clean_fnos_cookie(value) -> str:
    """把「整条 Cookie 行」收拾成可以直接发给飞牛网关的一串。

    能处理：带 ``Cookie:`` 前缀、带引号、多行粘贴、多余空白。
    规则：串里已经有 ``fnwg_token``（新版飞牛）时，把 ``fnos-token`` 整项丢掉——
    留着它会触发网关的严格校验，回 ``invalid token``，比不带还糟。
    """
    text = str(value or "").strip().strip("\"'").strip()
    if not text:
        return ""
    low = text.lower()
    for head in ("cookie:", "cookie："):
        if low.startswith(head):
            text = text[len(head):]
            break
    parts = []
    for raw in re.split(r"[;\n\r]+", text):
        item = raw.strip().strip("\"'").strip()
        if not item or "=" not in item:
            continue
        name = item.split("=", 1)[0].strip()
        if name:
            parts.append((name, item))
    if not parts:
        return ""
    names = {n.lower() for n, _ in parts}
    drop = set(_COOKIE_DROP_WITH_FNWG) if _COOKIE_NEEDS_FNWG in names else set()
    out, seen = [], set()
    for name, item in parts:
        key = name.lower()
        if key in drop or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return "; ".join(out)


def fnos_cookie_has_fnwg(value) -> bool:
    """这串 Cookie 里有没有新版飞牛要的 ``fnwg_token``。"""
    return re.search(r"(?:^|;\s*)fnwg_token\s*=", str(value or ""), re.I) is not None


# 套件首页会把当前 pan_auth 内联在 ``uiauth(value){ return "eyJ…" }`` 里。
# 有它能自动换新令牌，用户就不必每次去 F12 里抄。
_JWT_IN_PAGE = re.compile(
    r"""["'](eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})["']""")


def find_jwt_in_page(text) -> str:
    """从套件首页 HTML 里抠出内联的 pan_auth（JWT）。找不到返回空串。"""
    found = _JWT_IN_PAGE.search(str(text or ""))
    return found.group(1) if found else ""


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "")


def esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
