"""自动侦测本机迅雷，并尽量把它接上。

能自动做的部分
--------------
* 找应用目录（``/var/apps/*``、``/vol*/@appstore/*`` 等）、找进程、找监听端口
* 按「飞牛网关」的接口路径逐个端口打指纹，判断到底缺哪个令牌（1.0.31 起不再认 2345 面板）
* 在迅雷自己的目录里**尽力**捞 `pan_auth` / `fnos-token`（限目录、限大小、只读）
* 命中后把配置写好并立刻复测一次

做不到的部分
------------
`pan_auth` 本质是迅雷账号的登录态，服务端不会无缘无故把它交出来。捞不到时只有两条
路：浏览器 F12 手抄一次（见 docs/迅雷接入.md），或者网关模式下复用你当前飞牛的登录态
（面板会把访问自己的那个 `fnos-token` Cookie 顺手交给这里）。
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.parse

from . import util
from .downloaders.xunlei import _find_target

APP_KEYWORDS = ("xunlei", "迅雷")
GATEWAY_PATH = "/cgi/ThirdParty/xunlei/index.cgi"
GATEWAY_PORT = 5666
# 飞牛 Web 端口被改过时，网关端口就跟着变。这些是「值得先试一下」的常见值；
# 真正兜底的是 listening_ports() 里 56xx 段那些实际在监听的口。
GATEWAY_PORTS = (5666, 5667, 5668, 8000, 8080)

_TOKEN_PATTERNS = {
    "pan_auth": re.compile(r"""pan[-_]?auth["'\s:=&]{0,8}([A-Za-z0-9_\-\.]{12,128})""", re.I),
    "fnos_token": re.compile(r"""fnos[-_]?token["'\s:=]{0,8}([A-Za-z0-9_\-\.]{12,128})""", re.I),
}
_TEXT_EXT = (".json", ".conf", ".cfg", ".ini", ".yml", ".yaml", ".txt", ".env", ".log", ".sh")


# ---------------- 系统侦察 ----------------
def parse_proc_net(text):
    """从 /proc/net/tcp 里挑出 LISTEN(0A) 的本地端口。纯函数，方便测试。"""
    ports = set()
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        local = parts[1]
        if ":" not in local:
            continue
        try:
            ports.add(int(local.rsplit(":", 1)[1], 16))
        except ValueError:
            continue
    return sorted(ports)


def listening_ports():
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                ports |= set(parse_proc_net(f.read()))
        except OSError:
            continue
    return sorted(p for p in ports if 0 < p < 65536)


def processes():
    """找命令行里带 xunlei 的进程；顺带从参数里抠可能的端口。"""
    out = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids[:4000]:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                raw = f.read(4096)
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if not cmd:
            continue
        low = cmd.lower()
        if "xunlei" not in low and "迅雷" not in cmd:
            continue
        ports = sorted({int(n) for n in re.findall(r"\b(2[0-9]{3}|[1-6][0-9]{4})\b", cmd)
                        if 1024 <= int(n) <= 65535})
        out.append({"pid": int(pid), "cmd": cmd[:240], "ports": ports})
        if len(out) >= 20:
            break
    return out


def app_dirs(extra_roots=None):
    """列出一层目录名里带 xunlei / 迅雷 的应用目录。"""
    roots = ["/var/apps", "/usr/trim/apps"]
    for vol in glob.glob("/vol*"):
        roots += [os.path.join(vol, "@appstore"), os.path.join(vol, "@appdata"),
                  os.path.join(vol, "@appcenter")]
    if extra_roots:
        roots = list(extra_roots) + roots
    found = []
    for root in roots:
        for path in glob.glob(os.path.join(root, "*")):
            name = os.path.basename(path).lower()
            if not os.path.isdir(path):
                continue
            if any(k in name or k in os.path.basename(path) for k in APP_KEYWORDS):
                found.append({"path": path, "name": os.path.basename(path)})
    return found[:12]


# ---------------- 令牌打捞 ----------------
def harvest(dirs, max_files=300, max_bytes=512 * 1024):
    """在迅雷自己的目录里找 pan_auth / fnos-token。

    只读、限深、限大小，命中多个时全部返回（后面会用探测结果验证哪个真能用）。
    """
    hits = {"pan_auth": [], "fnos_token": []}
    seen = set()
    budget = max_files
    for item in dirs or []:
        root = item["path"] if isinstance(item, dict) else str(item)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth > 3:
                dirnames[:] = []
                continue
            for fn in filenames:
                if budget <= 0:
                    return hits
                if not fn.lower().endswith(_TEXT_EXT):
                    continue
                full = os.path.join(dirpath, fn)
                if full in seen:
                    continue
                seen.add(full)
                budget -= 1
                try:
                    if os.path.getsize(full) > max_bytes:
                        continue
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                for key, pat in _TOKEN_PATTERNS.items():
                    for m in pat.finditer(text):
                        val = m.group(1).strip(".")
                        if len(val) < 12 or val in [h["value"] for h in hits[key]]:
                            continue
                        hits[key].append({"value": val, "source": full})
    return hits


def mask(token):
    token = str(token or "")
    if len(token) <= 8:
        return "***"
    return token[:4] + "…" + token[-4:]


# ---------------- HTTP 指纹 ----------------
def _tasks_query(pan_auth):
    return urllib.parse.urlencode({
        "pan_auth": pan_auth or "", "device_space": "",
        "type": "user#runner", "limit": "1",
    })


def gateway_url(host, fnos_port, pan_auth=""):
    return "http://%s:%d%s/drive/v1/tasks?%s" % (host, fnos_port, GATEWAY_PATH,
                                                 _tasks_query(pan_auth))


def _get(url, cookie="", timeout=3):
    headers = {"Accept": "application/json, text/html;q=0.8"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        status, text, _raw = util.http_request(
            url, headers=headers, timeout=timeout, proxy="", retries=0)
        return status, text
    except util.HttpError as e:
        body = e.body.decode("utf-8", "replace") if isinstance(e.body, bytes) else str(e.body)
        return (e.status or 0), body
    except Exception:
        return 0, ""


def classify(status, text, mode=""):
    """把响应翻成 (状态, 人话)。状态：ok / need_token / no_space / absent / error

    只按网关的判据：这个端口上返回 HTML，往往只是「该端口还跑了别的网页服务」，
    不能算迅雷在（以前 2345 面板未登录时回的就是自己的网页，判据不一样）。

    ``mode`` 留着只为兼容老调用方，1.0.31 起不再有第二种判据。
    """
    body = (text or "").strip()
    if status == 200:
        if body[:1] in "{[":
            try:
                data = json.loads(body)
            except ValueError:
                return "ok", "接口有响应（JSON 解析失败但已连通）"
            if _find_target(data):
                return "ok", "已拿到设备空间"
            return "no_space", "能连通，但没有设备空间（令牌无效或迅雷未登录账号）"
        low = body[:800].lower()
        if "<html" in low or "<!doctype" in low:
            return "absent", "这个端口是别的网页服务，不像迅雷网关"
        # 网关把请求转给了迅雷，迅雷回一句纯文本 invalid token：
        # 说明接口是通的、被拒的是令牌。以前这里判成「不是迅雷接口」，
        # 报告看上去像「迅雷没装/端口错了」，把人带偏。
        low_body = body[:200].lower()
        if "token" in low_body and ("invalid" in low_body or "expired" in low_body
                                    or "无效" in body or "过期" in body):
            return "need_token", "接口在，但令牌被拒（%s）" % body[:60]
        return "absent", "这个端口返回的不是迅雷接口"
    if status in (401, 403):
        return "need_token", "接口要求登录态（HTTP %s）" % status
    if status == 404:
        return "absent", "这个地址没有迅雷接口（404）"
    if status == 0:
        return "absent", "端口没有响应"
    return "error", "HTTP %s" % status


def fingerprint(host, port, pan_auth="", fnos_token="", timeout=3):
    """打一次网关指纹：这个端口上有没有迅雷、缺不缺令牌。"""
    url = gateway_url(host, port, pan_auth)
    cookie = ""
    if fnos_token:
        # 传进来的可能是老版的裸 token，也可能是新版要的整条 Cookie
        # （fnwg_token + ost）。带分号/带 fnwg_token 的按整条处理，别再套前缀——
        # 套了就成 ``fnos-token=fnwg_token=…``，网关一样拒。
        if ";" in fnos_token or util.fnos_cookie_has_fnwg(fnos_token):
            cookie = util.clean_fnos_cookie(fnos_token)
        else:
            cookie = "fnos-token=%s" % fnos_token
    status, text = _get(url, cookie=cookie, timeout=timeout)
    state, detail = classify(status, text)
    if state == "need_token" and not fnos_token:
        detail = "需要飞牛登录 Cookie（新版飞牛要 fnwg_token + ost）"
    shown = url.replace(pan_auth, mask(pan_auth)) if pan_auth else url
    return {"mode": "fnos_gateway", "host": host, "port": port, "url": shown,
            "status": status, "state": state, "detail": detail,
            "sample": (text or "")[:200]}


# ---------------- 主流程 ----------------
def _gateway_ports(observed=None, cfg_port=None, extra=None, defaults=True):
    """候选网关端口，按「最可能」排序：面板实际被访问的端口 → 配置 → 常见值 → 本机在监听的 56xx。

    ``defaults=False`` 只用来测试：给定端口就只打给定端口，别去探测本机真实端口
    （否则几十秒都花在等超时上，结果还随机器环境变）。
    """
    out = []
    for cand in [observed, cfg_port] + (list(GATEWAY_PORTS) if defaults else []) + list(extra or []):
        try:
            cand = int(cand or 0)
        except (TypeError, ValueError):
            continue
        if cand and cand not in out:
            out.append(cand)
    if defaults:
        for p in listening_ports():
            if 5600 <= p <= 5700 and p not in out:
                out.append(p)
    return out


def scan(fnos_token="", cfg=None, ports=None, fnos_port=None, dirs=None,
         hosts=None, timeout=3):
    """扫一遍本机，返回报告。ports/dirs 可注入，方便测试。"""
    xcfg = ((cfg or {}).get("download") or {}).get("xunlei") or {}
    procs = processes()
    dirs = app_dirs() if dirs is None else dirs

    gw_ports = _gateway_ports(fnos_port, xcfg.get("fnos_port"), ports,
                              defaults=ports is None)
    fnos_port = gw_ports[0] if gw_ports else GATEWAY_PORT

    hits = harvest(dirs)
    # 手上可能同时有好几串候选：配置里的、浏览器刚送进来的、磁盘上捞到的。
    # 谁也说不清哪个还有效，那就都试一遍，第一个打通的算数。
    pan_cands = _dedup([util.clean_pan_auth(xcfg.get("pan_auth"))]
                       + [util.clean_pan_auth(h["value"]) for h in hits["pan_auth"]])
    tok_cands = _dedup([str(fnos_token or "").strip(),
                        str(xcfg.get("fnos_token") or "").strip()]
                       + [h["value"] for h in hits["fnos_token"]])
    pan_auth = pan_cands[0] if pan_cands else ""
    tok = tok_cands[0] if tok_cands else ""
    tok_from = _origin(hits["fnos_token"], tok,
                       "浏览器当前登录态" if fnos_token and tok == fnos_token else "config")

    measured = []
    for host in (hosts or ["127.0.0.1"]):
        for gw_port in gw_ports:
            measured.append(fingerprint(host, gw_port, pan_auth, tok, timeout))

    # 第一轮全没打通时，换别的候选令牌再试一遍（只换令牌，不再扫端口）。
    # 这一步救的正是「配置里存着几天前的 fnos-token，浏览器刚送进来的是新的」，
    # 以及「pan_auth 在磁盘上还有一份更新的」这两种情况。
    alternates = False
    if not any(m["state"] == "ok" for m in measured):
        hosts = hosts or ["127.0.0.1"]
        combos = [(t, p) for t in tok_cands for p in pan_cands
                  if (t, p) != (tok, pan_auth)]
        for cand_tok, cand_pan in combos[:6]:
            for host in hosts:
                for gw_port in gw_ports:
                    m = fingerprint(host, gw_port, cand_pan, cand_tok, timeout)
                    m["retry"] = True
                    measured.append(m)
                    if m["state"] == "ok":
                        pan_auth = cand_pan
                        if cand_tok != tok:
                            tok, tok_from = cand_tok, "候选备用令牌"
                        alternates = True
                        break
                if alternates:
                    break
            if alternates:
                break

    live = [m for m in measured if m["state"] != "absent"]
    best = _pick(live)

    report = {
        "ok": bool(best and best["state"] == "ok"),
        "apps": [d["path"] for d in dirs],
        "processes": procs,
        "ports": gw_ports,
        "fnos_port": fnos_port,
        "token_source": tok_from,
        "alternates": alternates,
        # 明文令牌只留在 _secrets 里供写入配置用，对外一律只给掩码
        "tokens": {
            "pan_auth": {"masked": mask(pan_auth) if pan_auth else "",
                         "from": _origin(hits["pan_auth"], pan_auth, "config")},
            "fnos_token": {"masked": mask(tok) if tok else "", "from": tok_from},
        },
        "_secrets": {"pan_auth": pan_auth, "fnos_token": tok},
        "candidates": measured,
        "best": best,
        "notes": [],
    }
    report["notes"] = _notes(report)
    return report


def _dedup(values):
    """去空、去重，保持原顺序（越靠前越可信）。"""
    out = []
    for v in values or []:
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def mode_label(mode=""):
    return "飞牛官方迅雷套件（网关）"


def _first(items):
    return items[0]["value"] if items else ""


def _origin(items, value, fallback):
    if not value:
        return ""
    for it in items:
        if it["value"] == value:
            return it["source"]
    return fallback


def _pick(live):
    if not live:
        return None
    ok = [m for m in live if m["state"] == "ok"]
    if ok:
        return ok[0]
    live.sort(key=lambda m: 0 if m["state"] in ("need_token", "no_space") else 1)
    return live[0]


def _notes(rep):
    """结论放最前面，环境信息垫后：用户先要「连上了吗／缺什么」。"""
    notes = []
    best = rep["best"]
    if best is None:
        notes.append("所有候选地址都没有响应：可能迅雷没在运行，或端口被改过。")
    elif best["state"] == "ok":
        notes.append("已连通%s（端口 %s）。" % (mode_label(best["mode"]), best["port"]))
    elif best["state"] == "need_token":
        if "令牌被拒" in (best.get("detail") or ""):
            # 网关已经把请求转给迅雷了，是迅雷不认 pan_auth——跟飞牛登录态无关
            notes.append("网关是通的，但迅雷不认 pan_auth（%s）："
                         "飞牛登录态跟这个没关系，先去迅雷套件确认账号还在线，"
                         "再重抓一次 pan_auth。" % best["detail"])
        else:
            # 手上有登录态却还是 403：那不是「缺」，是这串过期了（飞牛重启、
            # 会话到期都会），别让用户以为配置里根本没填过。
            shown = ((rep.get("tokens") or {}).get("fnos_token") or {}).get("masked") or ""
            if shown:
                notes.append("网关在，但它没认这串飞牛登录态（%s）：多半已经过期。"
                             "从飞牛桌面重新打开一次本面板，浏览器会把新的登录态带进来，"
                             "程序会自动换用；也可手动粘贴 Cookie 里的 fnos-token。" % shown)
            else:
                notes.append("网关在，但缺 fnos-token：从飞牛桌面打开本面板会自动带上登录态，"
                             "再点一次即可；也可手动粘贴 Cookie 里的 fnos-token。")
    elif best["state"] == "no_space":
        notes.append("接口通了但取不到设备空间：令牌多半过期了，或者迅雷客户端没登录账号。")
    else:
        notes.append("接口报 %s：%s" % (best["status"] or "?", best["detail"]))

    if rep.get("alternates") and best and best["state"] == "ok":
        notes.append("换了一串备用令牌才打通（用的是「%s」那串），已写进配置。"
                     % (rep.get("token_source") or "备用"))

    if not rep["apps"] and not rep["processes"]:
        notes.append("本机没看到迅雷的应用目录或进程：确认应用中心里的「迅雷」已经装上并启动；"
                     "Docker 版不走网关，只能在配置里手填 api_base。")
    return notes


def patch_for(rep):
    """把最佳候选翻成 config 里 download.xunlei 的局部配置。"""
    best = rep.get("best")
    if not best:
        return {}
    patch = {
        "mode": "fnos_gateway",
        "host": best["host"],
        "fnos_port": best["port"],
        "api_base": "",
        "target": "",
    }
    secrets = rep.get("_secrets") or {}
    if secrets.get("pan_auth"):
        # 只写 JWT 本身：整条 watch 地址存进去，网关就当 token 发给迅雷云，
        # 回的是一句「segments 不对」，没人能从那句话里猜出是形状问题
        patch["pan_auth"] = util.clean_pan_auth(secrets["pan_auth"])
    if secrets.get("fnos_token"):
        patch["fnos_token"] = secrets["fnos_token"]
        patch["fnos_token_ts"] = int(time.time())
        patch["fnos_token_source"] = rep.get("token_source") or "detect"
    return patch


def autoconnect(cfg, fnos_token="", fnos_port=None, force=False):
    """自动侦测并把可用配置写回去（启动时、以及面板「立即重连」都走这里）。

    只有真打通（``best.state == "ok"``）才写配置——打不通时把原本能用的设置覆盖掉，
    是最难查的一类故障。返回值给日志/接口用：``ok`` / ``applied`` / ``report`` / ``port``。

    ``force`` 用于「立即重连」：即使端口和令牌看起来没变，也重扫一遍并刷一次配置。
    """
    xcfg = ((cfg.data or {}).get("download") or {}).get("xunlei") or {}
    rep = scan(fnos_token=fnos_token, fnos_port=fnos_port, cfg=cfg.data)
    best = rep.get("best") or {}
    out = {"ok": bool(best and best.get("state") == "ok"),
           "port": int(best.get("port") or 0) if best else 0,
           "applied": {}, "report": rep}
    if not out["ok"]:
        return out
    cur_port = 0
    try:
        cur_port = int(xcfg.get("fnos_port") or 0)
    except (TypeError, ValueError):
        cur_port = 0
    need = (force or cur_port != out["port"]
            or not str(xcfg.get("pan_auth") or "").strip()
            or not str(xcfg.get("fnos_token") or "").strip())
    if need:
        patch = patch_for(rep)
        if patch:
            cfg.apply_patch({"download": {"xunlei": patch}})
            out["applied"] = {k: (mask(v) if k in ("pan_auth", "fnos_token") else v)
                              for k, v in patch.items()}
    return out


def format_report(rep):
    """给人看的命令行报告。"""
    lines = ["迅雷侦测"]
    lines.append("  应用目录：%s" % ("、".join(rep["apps"]) or "未找到"))
    procs = rep.get("processes") or []
    lines.append("  进程：%s" % ("、".join("pid %s" % p["pid"] for p in procs) or "未找到"))
    lines.append("  候选端口：%s" % ("、".join(str(p) for p in rep["ports"]) or "无"))
    tk = rep["tokens"]
    lines.append("  pan_auth：%s" % (tk["pan_auth"]["masked"] or "未取到"))
    lines.append("  fnos-token：%s" % (tk["fnos_token"]["masked"] or "未取到"))
    lines.append("")
    lines.append("  候选：")
    for c in rep["candidates"]:
        lines.append("    [%s] %s:%s  %s  %s" % (
            c["mode"], c["host"], c["port"],
            "✔" if c["state"] == "ok" else "·", c["detail"]))
    lines.append("")
    for n in rep["notes"]:
        lines.append("  · %s" % n)
    return "\n".join(lines)
