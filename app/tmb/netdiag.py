"""网络分层诊断。

NAS 上「连不上」的原因差别很大：DNS 不解析、TCP 被拒、代理端口没监听、TLS 握手失败、
站点 403…… 一句「无法访问」帮不了排查。这里把链路拆成几层，每层给出耗时与结论，
CLI（main.py --doctor）和 Web 面板共用同一套结论。

只依赖标准库。
"""
from __future__ import annotations

import concurrent.futures
import socket
import time
import urllib.parse

from . import util
from .util import effective_proxy, env_proxies, explain_net as explain, parse_proxy  # noqa: F401


def tcp_check(host, port, timeout=6.0):
    """只做 DNS + TCP 连接，返回 {ok, stage, ms, error, ip}。"""
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return {"ok": False, "stage": "dns", "ms": int((time.time() - t0) * 1000),
                "error": "DNS 解析失败：%s" % (e,), "ip": ""}

    last = ""
    for family, socktype, proto, _canon, addr in infos:
        s = None
        try:
            s = socket.socket(family, socktype, proto)
            s.settimeout(timeout)
            s.connect(addr)
            return {"ok": True, "stage": "tcp", "ms": int((time.time() - t0) * 1000),
                    "error": "", "ip": addr[0]}
        except OSError as e:
            last = explain(e)
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
    return {"ok": False, "stage": "tcp", "ms": int((time.time() - t0) * 1000),
            "error": last or "TCP 连接失败", "ip": infos[0][4][0] if infos else ""}


def http_check(url, proxy="", timeout=12.0):
    """发一个真实请求，返回 {ok, status, ms, error}。"""
    t0 = time.time()
    try:
        status, _text, _raw = util.http_request(url, timeout=timeout, proxy=proxy, retries=0)
        return {"ok": True, "status": status, "ms": int((time.time() - t0) * 1000), "error": ""}
    except util.HttpError as e:
        if e.status:
            return {"ok": False, "status": e.status, "ms": int((time.time() - t0) * 1000),
                    "error": "HTTP %s" % e.status}
        return {"ok": False, "status": 0, "ms": int((time.time() - t0) * 1000), "error": str(e)}
    except Exception as e:  # noqa: BLE001 - 诊断要兜住所有异常
        return {"ok": False, "status": 0, "ms": int((time.time() - t0) * 1000),
                "error": explain(e, proxy)}


def _proxy_layer(proxy, timeout=6.0):
    """代理本身通不通：先测端口，再决定是否值得发请求。"""
    parsed = parse_proxy(proxy)
    if not parsed:
        return None
    scheme, host, port = parsed
    item = {"proxy": proxy, "scheme": scheme, "host": host, "port": port}
    if scheme.startswith("socks"):
        item.update({"ok": False, "stage": "unsupported",
                     "error": "程序用 urllib 直连，不支持 socks5://。请填代理的 HTTP 端口"
                              "（Clash/V2Ray 一般有个 mixed 或 http 端口，如 7890），"
                              "或用 http:// 前缀指向支持 HTTP CONNECT 的端口。"})
        return item
    tcp = tcp_check(host, port, timeout)
    item.update({"ok": tcp["ok"], "stage": "tcp", "ms": tcp["ms"],
                 "error": "" if tcp["ok"] else "代理端口不通：" + tcp["error"]})
    return item


def run(cfg, timeout=8.0):
    """完整诊断，返回结构化结果（可 JSON 序列化）。

    两阶段并发：先并行测三条 TCP/代理端口，再并行测真正通的那些 HTTP。
    全串行的话一次要 40s+，面板上等不起。
    """
    nyaa_cfg = cfg.get("nyaa") or {}
    tg_cfg = cfg.get("telegram") or {}
    base = (nyaa_cfg.get("base_url") or "https://sukebei.nyaa.si").rstrip("/")

    site_proxy, site_src = effective_proxy(nyaa_cfg.get("proxy") or tg_cfg.get("proxy") or "")
    tg_proxy, tg_src = effective_proxy(tg_cfg.get("proxy") or "")

    report = {
        "ts": time.time(),
        "env_proxies": env_proxies(),
        "site": {"base_url": base, "proxy": site_proxy, "proxy_source": site_src},
        "telegram": {"proxy": tg_proxy, "proxy_source": tg_src,
                     "configured": bool(tg_cfg.get("bot_token"))},
        "checks": {},
        "verdict": [],
    }
    checks = report["checks"]

    host = urllib.parse.urlsplit(base).hostname or ""
    port = urllib.parse.urlsplit(base).port or (443 if base.startswith("https") else 80)
    tcp_timeout = min(timeout, 6)

    # 阶段一：TCP / 代理端口，互不依赖，并发跑
    jobs = {
        "site_direct_tcp": (tcp_check, (host, port, tcp_timeout)),
        "tg_direct_tcp": (tcp_check, ("api.telegram.org", 443, tcp_timeout)),
    }
    if site_proxy:
        jobs["site_proxy_layer"] = (_proxy_layer, (site_proxy, tcp_timeout))
    if tg_proxy and tg_proxy != site_proxy:
        jobs["tg_proxy_layer"] = (_proxy_layer, (tg_proxy, tcp_timeout))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {k: ex.submit(fn, *a) for k, (fn, a) in jobs.items()}
        for k, fut in futures.items():
            try:
                checks[k] = fut.result()
            except Exception as e:  # noqa: BLE001 - 诊断不能因为单点异常整体挂掉
                checks[k] = {"ok": False, "stage": "error", "error": explain(e), "ms": 0}

    # 阶段二：只测真正通的 HTTP 层
    if tg_proxy == site_proxy:
        checks.setdefault("tg_proxy_layer", checks.get("site_proxy_layer") or {})

    http_jobs = {}
    if (checks.get("site_direct_tcp") or {}).get("ok"):
        http_jobs["site_direct_http"] = (base + "/?page=rss&q=test", "")
    if site_proxy and (checks.get("site_proxy_layer") or {}).get("ok"):
        http_jobs["site_proxy_http"] = (base + "/?page=rss&q=test", site_proxy)
    if (tg_cfg.get("bot_token") and tg_proxy
            and (checks.get("tg_proxy_layer") or {}).get("ok")):
        http_jobs["tg_proxy_http"] = (
            "https://api.telegram.org/bot%s/getMe" % tg_cfg.get("bot_token"), tg_proxy)

    if http_jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = {k: ex.submit(http_check, url, px, timeout)
                       for k, (url, px) in http_jobs.items()}
            for k, fut in futures.items():
                try:
                    checks[k] = fut.result()
                except Exception as e:  # noqa: BLE001
                    checks[k] = {"ok": False, "stage": "error", "error": explain(e), "ms": 0}

    report["verdict"] = _verdict(report)
    return report


def _verdict(r):
    out = []
    c = r["checks"]
    sd_http = c.get("site_direct_http")
    sd_tcp = c.get("site_direct_tcp") or {}
    sp_http = c.get("site_proxy_http")
    sp_layer = c.get("site_proxy_layer") or {}

    if sd_http and sd_http.get("ok"):
        out.append(("ok", "站点直连可用（HTTP %s，%dms）。不需要配代理。"
                    % (sd_http.get("status"), sd_http.get("ms"))))
    elif sd_http:
        out.append(("warn", "站点直连失败：%s" % (sd_http.get("error") or "未知原因")))
    else:
        out.append(("warn", "站点 TCP 直连就不通（%s），HTTP 层不再尝试。"
                    % (sd_tcp.get("error") or "未知原因")))

    if r["site"]["proxy"]:
        if sp_layer and not sp_layer.get("ok"):
            out.append(("error", "代理不可用：%s" % sp_layer.get("error")))
        elif sp_http and sp_http.get("ok"):
            out.append(("ok", "经代理访问站点成功（HTTP %s，%dms）。"
                        % (sp_http.get("status"), sp_http.get("ms"))))
        elif sp_http:
            out.append(("error", "经代理访问站点失败：%s" % (sp_http.get("error") or "未知原因")))
    else:
        out.append(("error", "没有配置代理，而直连不通。请在设置里填 HTTP 代理"
                             "（形如 http://192.168.1.10:7890）。"))

    if r["env_proxies"] and not r["site"]["proxy_source"].startswith("config"):
        out.append(("warn", "程序当前在用环境变量代理 %s。若这不是你想要的出口，"
                            "要么清掉该变量，要么在设置里显式填代理。"
                    % ",".join(sorted(r["env_proxies"]))))

    if not r["telegram"]["configured"]:
        out.append(("warn", "还没有填 Telegram Bot Token。"))
    else:
        tg_http = c.get("tg_proxy_http") or {}
        tg_tcp = c.get("tg_direct_tcp") or {}
        if tg_http.get("ok"):
            out.append(("ok", "经代理访问 Telegram 成功。"))
        elif tg_tcp.get("ok"):
            out.append(("ok", "Telegram 可直连。"))
        elif not r["telegram"]["proxy"]:
            out.append(("error", "Telegram 不通且没配代理，机器人会连不上。"))
        elif c.get("tg_proxy_layer") and not c["tg_proxy_layer"].get("ok"):
            out.append(("error", "Telegram 代理不可用：%s" % c["tg_proxy_layer"].get("error")))
    return out


def format_report(report):
    """给 CLI / 面板用的纯文本报告。

    不用等宽对齐：中文是双宽字符，%-18s 在终端和浏览器里都对不齐，不如直排。
    """
    lines = ["== 网络诊断 ==", "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    env = report.get("env_proxies") or {}
    lines.append("环境变量代理：%s"
                 % (", ".join("%s=%s" % (k, v) for k, v in env.items()) or "无"))
    s = report["site"]
    lines.append("站点：%s" % s["base_url"])
    lines.append("实际出口：%s%s"
                 % (s["proxy"] or "直连（无代理）",
                    "（来自 %s）" % s["proxy_source"] if s["proxy_source"] else ""))

    labels = [
        ("site_direct_tcp", "站点 TCP 直连"),
        ("site_direct_http", "站点 HTTP 直连"),
        ("site_proxy_layer", "代理端口连通"),
        ("site_proxy_http", "站点 HTTP 经代理"),
        ("tg_direct_tcp", "Telegram TCP 直连"),
        ("tg_proxy_layer", "Telegram 代理端口"),
        ("tg_proxy_http", "Telegram 经代理"),
    ]
    lines.append("-- 分层结果 --")
    skip_dep = {"site_direct_http": "site_direct_tcp", "site_proxy_http": "site_proxy_layer",
                "tg_proxy_http": "tg_proxy_layer"}
    for key, label in labels:
        item = report["checks"].get(key)
        if not item:
            if key == "tg_proxy_http" and not report["telegram"].get("configured"):
                lines.append("  ○ %s：未填 Bot Token，跳过" % label)
            elif key == "tg_proxy_http" and not report["telegram"].get("proxy"):
                lines.append("  ○ %s：未配代理，跳过" % label)
            elif skip_dep.get(key) and report["checks"].get(skip_dep[key]) is not None:
                lines.append("  ○ %s：上一跳不通，已跳过" % label)
            continue
        if item.get("ok"):
            detail = []
            if item.get("ip"):
                detail.append("ip=%s" % item["ip"])
            if item.get("status"):
                detail.append("HTTP %s" % item["status"])
            detail.append("%dms" % item.get("ms", 0))
            lines.append("  √ %s：%s" % (label, "，".join(detail)))
        else:
            lines.append("  × %s：%s" % (label, item.get("error") or "失败"))

    lines.append("-- 结论 --")
    for level, text in report.get("verdict") or []:
        mark = {"ok": "√", "warn": "!", "error": "×"}.get(level, "-")
        lines.append("[%s] %s" % (mark, text))
    return "\n".join(lines)
