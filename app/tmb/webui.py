"""内置 Web 管理面板（标准库 http.server，无第三方依赖）。"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import posixpath
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (__version__, downloaders, intake, library, log as logmod,
               netdiag, nyaa, proxychan, xunlei_detect)
from .config import DEFAULT_CONFIG
from .pipeline import Pipeline

log = logging.getLogger("tmb.web")


class AppContext(object):
    """Web 层与业务层之间的上下文。"""

    def __init__(self, cfg, store, pipeline: Pipeline, bot=None, ui_dir=""):
        self.cfg = cfg
        self.store = store
        self.pipeline = pipeline
        self.bot = bot
        self.ui_dir = ui_dir
        self.started_at = time.time()
        # 从飞牛桌面打开本面板时，浏览器的 fnos-token 会随请求带进来。
        # 记住它，就能直接拿去调官方迅雷的网关接口，省掉手抄令牌。
        self.fnos_token = ""
        self.fnos_token_seen = False     # 这次有没有从浏览器里读到（区分「没读到」和「读到空」）
        self.fnos_port = 0
        # 最近一次自动连接/重连的结论：面板顶栏那一行「已连接 · 127.0.0.1:5666」用它
        self.xunlei_conn = {}
        # 浏览器刚送来新登录态时的自动重连：同一时间只跑一个，别被连点/多标签页打出并发
        self._reconnect_lock = threading.Lock()
        self.web_port = 0                # 本面板自己的监听端口，用来把 Host 里的它排除掉
        # 目录扫描是重活，同一时间只允许跑一个，避免连点把 IO 打满
        self.library_lock = threading.Lock()
        # 代理线路：把配置里的备用通道装进全局路由，util 的请求据此降级
        proxychan.install(cfg)

    def note_fnos_token(self, headers):
        tok = ""
        try:
            for raw in (headers.get("Cookie") or "").split(";"):
                if "=" not in raw:
                    continue
                key, val = raw.split("=", 1)
                if key.strip().lower() in ("fnos-token", "fnos_token", "trim-token",
                                          "connect-token", "fnos-session"):
                    if val.strip():
                        tok = val.strip()
            for name in ("X-Trim-Token", "X-Fnos-Token", "Fnos-Token"):
                val = headers.get(name)
                if val:
                    tok = val.strip()
            # 面板是通过哪个端口打开的，飞牛 Web 端口多半就是它。
            # 但要排除「直接访问本面板端口」这种情况——那 Host 里的就是 8470，
            # 不是飞牛网关端口，拿它去扫只会多一条「这个端口是别的网页服务」的噪音。
            host = (headers.get("Host") or "").strip()
            if ":" in host:
                try:
                    port = int(host.rsplit(":", 1)[1])
                except ValueError:
                    port = 0
                if port and port != self.web_port:
                    self.fnos_port = port
        except Exception:
            pass
        # 「这一次请求」有没有读到浏览器登录态。每次请求都重算：
        # 用户可能这次从飞牛桌面进（带 Cookie），下次直接输 IP 进（不带），
        # 面板上那句「没读到，请从飞牛桌面打开」要跟着变。
        self.fnos_token_seen = bool(tok)
        if tok and tok != self.fnos_token:
            self.fnos_token = tok
            self._remember_fnos_token(tok)
        return self.fnos_token

    def _remember_fnos_token(self, tok):
        """把浏览器带来的登录态写进配置。

        只留内存是不够的：Telegram、定时任务、失败重试走的都是配置里的副本，
        用户重登飞牛再打开面板，机器人却还拿着几天前的令牌，于是永远「鉴权失败」。
        只记在网关配置里——别的接法用不上它，别拿别的账号的 Cookie 覆盖手填值。
        """
        xcfg = ((self.cfg.data.get("download") or {}).get("xunlei") or {})
        if (xcfg.get("mode") or "fnos_gateway").lower() != "fnos_gateway":
            return False
        if str(xcfg.get("fnos_token") or "").strip() == tok:
            return False
        try:
            self.cfg.apply_patch({"download": {"xunlei": {
                "fnos_token": tok,
                "fnos_token_ts": int(time.time()),
                "fnos_token_source": "browser",
            }}})
            log.info("已用浏览器当前登录态更新 fnos-token（%d 字符）", len(tok))
            # 迅雷还没连上、而这串新登录态可能正好是缺的那把钥匙：后台补一次自动连接。
            # 不阻塞这次请求，用户刷新一下就能看到「已连接」。
            if not (self.xunlei_conn or {}).get("ok"):
                threading.Thread(target=self._auto_reconnect, daemon=True).start()
            return True
        except Exception:
            log.exception("写入 fnos-token 失败，本次仅在内存里使用")
            return False

    def _auto_reconnect(self):
        """后台重连一次（带锁，失败只写日志）。"""
        if not self._reconnect_lock.acquire(False):
            return
        try:
            res = xunlei_detect.autoconnect(self.cfg, fnos_token=self.fnos_token,
                                            fnos_port=self.fnos_port or None)
            port = res.get("port") or 0
            notes = (res.get("report") or {}).get("notes") or []
            self.xunlei_conn = {"ok": bool(res.get("ok")), "port": port,
                                "text": (notes[0] if notes else ""), "ts": time.time()}
            if res.get("ok"):
                log.info("拿到浏览器登录态后自动连接成功：飞牛网关 :%s", port)
            else:
                log.info("拿到浏览器登录态后仍没连上：%s", (notes or ["未知原因"])[0])
        except Exception as e:       # noqa: BLE001 - 后台线程不能把异常抛到别处
            log.warning("自动重连失败：%s: %s", type(e).__name__, e)
        finally:
            self._reconnect_lock.release()

    def auth_view(self):
        """给面板看：手上这串 fnos-token 从哪来、多久了、有没有读到。"""
        xcfg = ((self.cfg.data.get("download") or {}).get("xunlei") or {})
        try:
            ts = int(xcfg.get("fnos_token_ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        age = int(time.time() - ts) if ts > 0 else None
        return {
            "mode": xcfg.get("mode") or "",
            "has_token": bool(str(xcfg.get("fnos_token") or "").strip()),
            "has_pan_auth": bool(str(xcfg.get("pan_auth") or "").strip()),
            "source": xcfg.get("fnos_token_source") or "",
            "ts": ts,
            "age": age,
            "browser_cookie": bool(self.fnos_token),
            "browser_cookie_seen": bool(self.fnos_token_seen),
            "port": self.fnos_port or 0,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "tmb/" + __version__
    ctx = None  # type: AppContext

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _auth(self, query):
        token = (self.ctx.cfg.get("app.access_token") or "").strip()
        if not token:
            return True
        got = (self.headers.get("X-Token") or query.get("token", [""])[0] or "").strip()
        return got == token

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not path.startswith("/api/"):
            self.ctx.note_fnos_token(self.headers)
            return self._static(path)

        if not self._auth(query):
            return self._error("未授权：缺少或错误的访问口令", 401)

        try:
            if path == "/api/status":
                return self._json(self._status())
            if path == "/api/config":
                return self._json({"ok": True, "config": self.ctx.cfg.public_view(),
                                   "defaults": DEFAULT_CONFIG,
                                   "xunlei_auth": self.ctx.auth_view()})
            if path == "/api/rules":
                return self._json({"ok": True, "rules": self.ctx.cfg.rules})
            if path == "/api/logs":
                n = int(query.get("lines", ["200"])[0])
                return self._json({"ok": True, "lines": logmod.RING.snapshot(n)})
            if path == "/api/history":
                n = int(query.get("limit", ["30"])[0])
                return self._json({"ok": True, "items": self.ctx.store.recent_downloads(n),
                                   "stats": self.ctx.store.stats()})
            if path == "/api/events":
                n = int(query.get("limit", ["50"])[0])
                return self._json({"ok": True, "items": self.ctx.store.recent_events(n)})
            if path == "/api/proxy/channels":
                return self._json(dict(proxychan.status(),
                                       channels=proxychan.ROUTER.items()))   # 兼容老字段
            if path == "/api/library/items":
                n = int(query.get("limit", ["60"])[0])
                status = (query.get("status", [""])[0] or "").strip()
                return self._json({
                    "ok": True,
                    "items": self.ctx.store.library_recent(n, status),
                    "counts": self.ctx.store.library_counts(),
                    "scans": self.ctx.store.recent_library_scans(5),
                })
            return self._error("未知接口", 404)
        except BrokenPipeError:
            return
        except Exception as e:
            log.exception("GET %s 失败", path)
            return self._error("%s: %s" % (type(e).__name__, e), 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self.ctx.note_fnos_token(self.headers)
        if not self._auth(query):
            return self._error("未授权：缺少或错误的访问口令", 401)

        try:
            body = self._body()
            if path == "/api/config":
                patch = body.get("config") or {}
                self.ctx.cfg.apply_patch(patch)
                self._stamp_manual_token(patch)
                self.ctx.pipeline.reload_rules()
                # 通道可能在这次保存里被改过/删过：立刻重建，别等重启
                proxychan.install(self.ctx.cfg)
                log.info("配置已更新")
                return self._json({"ok": True, "config": self.ctx.cfg.public_view(),
                                   "xunlei_auth": self.ctx.auth_view()})
            if path == "/api/rules":
                rules = body.get("rules")
                if not isinstance(rules, dict) or "rules" not in rules:
                    return self._error("规则格式不合法，需要 {\"rules\": [...]}")
                self.ctx.cfg.save_rules(rules)
                self.ctx.pipeline.reload_rules()
                log.info("规则已更新（%d 条）", len(rules.get("rules") or []))
                return self._json({"ok": True, "rules": self.ctx.cfg.rules})
            if path == "/api/search":
                return self._json(self._search(body))
            if path == "/api/intake":
                return self._json(self._intake(body))
            if path == "/api/search/parsed":
                return self._json(self._search_parsed(body))
            if path == "/api/library/scan":
                return self._json(self._library_scan(body))
            if path == "/api/library/lookup":
                return self._json(self._library_lookup(body))
            if path == "/api/library/dups":
                return self._json(self._library_dups(body))
            if path == "/api/download":
                return self._json(self._download(body))
            if path == "/api/proxy/probe":
                return self._json(self._proxy_probe(body))
            if path == "/api/proxy/test":
                return self._json(self._proxy_test(body))
            if path == "/api/probe/nyaa":
                return self._json(nyaa.probe(self.ctx.cfg.data))
            if path == "/api/diag":
                # 分层诊断：DNS/TCP/代理/TLS 逐层测，结论直接给人看
                report = netdiag.run(self.ctx.cfg.data)
                report["text"] = netdiag.format_report(report)
                return self._json({"ok": True, "diag": report})
            if path == "/api/detect/xunlei":
                return self._json(self._detect_xunlei(body))
            if path == "/api/xunlei/reconnect":
                return self._json(self._detect_xunlei(body, force=True))
            if path == "/api/xunlei/folder":
                return self._json(self._xunlei_folder(body))
            if path == "/api/xunlei/parse":
                return self._json(self._xunlei_parse(body))
            if path == "/api/xunlei/refresh-token":
                changed, note = self.ctx.pipeline.refresh_xunlei_token(force=True)
                return self._json({"ok": True, "changed": bool(changed),
                                   "note": note or "本机没有更新的令牌",
                                   "token": self.ctx.pipeline.xunlei_token_state()})
            if path == "/api/probe/downloader":
                name = body.get("backend") or self.ctx.cfg.get("download.backend")
                inst = downloaders.make(name, self.ctx.cfg.data,
                                        self.ctx.cfg.get("telegram.proxy") or "")
                if not inst:
                    return self._error("未知下载器：%s" % name)
                return self._json(inst.probe())
            if path == "/api/bot/restart":
                if self.ctx.bot:
                    self.ctx.bot.stop()
                    time.sleep(0.2)
                    ok = self.ctx.bot.start()
                    return self._json({"ok": bool(ok),
                                       "error": None if ok else self.ctx.bot.last_error})
                return self._error("机器人未启用")
            if path == "/api/bot/pause":
                if self.ctx.bot:
                    self.ctx.bot.paused = bool(body.get("paused"))
                    return self._json({"ok": True, "paused": self.ctx.bot.paused})
                return self._error("机器人未启用")
            return self._error("未知接口", 404)
        except BrokenPipeError:
            return
        except Exception as e:
            log.exception("POST %s 失败", path)
            return self._error("%s: %s" % (type(e).__name__, e), 500)

    # ---------- 代理线路 ----------
    def _proxy_probe(self, body):
        """测线路：给了 url 就只测那条，否则整串线路全测一遍。

        只做代理握手的 CONNECT，不发业务请求：要回答的问题是「这条代理能不能
        把 NAS 带到 telegram / 检索站」，跟证书、站点本身的状态无关。
        """
        cfg = self.ctx.cfg
        one = str(body.get("url") or "").strip()
        timeout = float(body.get("timeout") or 6)
        targets = []
        if one:
            targets.append({"name": str(body.get("name") or "指定线路"), "url": one})
        else:
            primary = proxychan.config_primary(cfg)
            if primary:
                targets.append({"name": proxychan.short_name(primary), "url": primary})
            for ch in proxychan.ROUTER.items():
                targets.append({"name": proxychan.short_name(ch["url"]), "url": ch["url"]})
        if not targets:
            return {"ok": False, "items": [],
                    "error": "还没有可测的线路：先在代理框里填一行地址"}
        out = []
        for t in targets:
            r = proxychan.probe_line(t["url"], timeout=timeout)
            out.append({
                "name": t["name"],
                # 回写归一化后的地址：面板上填 192.168.1.10:7897，这里回 http://…
                "url": proxychan.normalize(t["url"]) or t["url"],
                "ok": r["ok"], "ms": r["ms"], "error": r["error"],
                "note": r.get("note") or "", "note_detail": r.get("note_detail") or "",
                "targets": [{"target": x["target"], "ok": x["ok"],
                             "ms": x["ms"], "error": x["error"]}
                            for x in r["targets"]],
            })
        good = [x for x in out if x["ok"]]
        parts = []
        for x in out:
            one = ("%d ms" % x["ms"]) if x["ok"] else (x["error"] or "不通")
            if x["ok"] and x.get("note"):
                one += "（%s）" % x["note"]
            parts.append("%s %s" % (x["name"], one))
        return {"ok": bool(good), "items": out, "text": "；".join(parts)}

    def _proxy_test(self, body):
        """把整串线路和直连全测一遍，挑出这次该走哪条。

        面板的「测速并切换」走这里。``lines`` 是输入框里的原始几行（一行一条，
        还没保存的那种），也可以照旧传 ``primary`` / ``channels``。
        选路只改内存顺序，不写配置。
        """
        cfg = self.ctx.cfg
        primary = body.get("primary")
        channels = body.get("channels")
        lines = body.get("lines")
        if isinstance(lines, list):
            # 输入框里的几行：第一行沿用 telegram.proxy 的位置，后面的进通道表
            urls = [str(u or "").strip() for u in lines]
            urls = [u for u in urls if u]
            primary = urls[0] if urls else ""
            channels = [{"url": u} for u in urls[1:]]
        if primary is None:
            primary = proxychan.config_primary(cfg)
        if channels is None:
            channels = proxychan.ROUTER.items()
        try:
            timeout = float(body.get("timeout") or 6)
        except (TypeError, ValueError):
            timeout = 6.0
        return proxychan.auto_test(str(primary or "").strip(), channels,
                                   timeout=max(1.0, min(timeout, 20.0)))

    # ---------- 业务 ----------
    def _status(self):
        cfg = self.ctx.cfg
        st = self.ctx.store.stats()
        bot = self.ctx.bot
        return {
            "ok": True,
            "version": __version__,
            "uptime": int(time.time() - self.ctx.started_at),
            "bot": {
                "running": bool(bot and bot._thread and bot._thread.is_alive()),
                "username": (bot.me or {}).get("username") if bot else "",
                "paused": bool(bot.paused) if bot else False,
                "last_error": bot.last_error if bot else "",
                "configured": bool(cfg.get("telegram.bot_token")),
                # 409 撞车时面板要能一眼看出「不是网络问题，是另一个实例在抢」
                "poll_state": getattr(bot, "poll_state", "idle") if bot else "idle",
                "poll_hint": bot.poll_hint() if bot and hasattr(bot, "poll_hint") else "",
            },
            "download": {
                "backend": cfg.get("download.backend"),
                "fallback": cfg.get("download.fallback"),
                "auto": cfg.get("download.auto_download"),
                "xunlei_mode": cfg.get("download.xunlei.mode"),
            },
            # pan_auth 还剩多久：面板据此在顶栏挂一个「快到期」的胶囊
            "xunlei_token": self.ctx.pipeline.xunlei_token_state(),
            # 自动连接的结果：面板据此显示「已连接 · 127.0.0.1:5666」
            "xunlei_conn": self.ctx.xunlei_conn or {},
            # 代理线路：一共几条、每条现在什么状态、最近走的哪条
            "proxy": dict(
                proxychan.status(proxychan.config_primary(cfg)),
                configured=proxychan.config_primary(cfg),
                nyaa_configured=cfg.get("nyaa.proxy") or "",
                auto=cfg.get("proxy_auto") is not False,
            ),
            "nyaa": {"base_url": cfg.get("nyaa.base_url")},
            "stats": st,
            "rules_count": len((cfg.rules or {}).get("rules") or []),
            "allowed_chats": len(cfg.get("telegram.allowed_chat_ids") or []),
        }

    # ---------- 历史字典 ----------
    def _library_scan(self, body):
        """手动触发一次目录分析。只对比、不删除记录；发现消失才写发现时间。"""
        if not self.ctx.library_lock.acquire(False):
            return {"ok": False, "error": "正在分析中，稍等这次跑完"}
        try:
            roots = body.get("roots")
            if isinstance(roots, str):
                roots = [r.strip() for r in roots.replace("，", ",").splitlines() if r.strip()]
            if roots:
                roots = [r for r in roots if str(r).strip()]
            # 面板上改了目录/深度，顺手记住，省得下次还要再填
            patch = {}
            if roots and roots != (self.ctx.cfg.get("library.roots") or []):
                patch["roots"] = roots
            for key, cast in (("max_depth", int), ("max_items", int)):
                if body.get(key) not in (None, ""):
                    try:
                        patch[key] = cast(body[key])
                    except (TypeError, ValueError):
                        pass
            if "include_files" in body:
                patch["include_files"] = bool(body["include_files"])
            if patch:
                self.ctx.cfg.apply_patch({"library": patch})
                roots = self.ctx.cfg.get("library.roots") or roots
            rep = library.scan(self.ctx.cfg, self.ctx.store, roots=roots)
            return rep
        finally:
            self.ctx.library_lock.release()

    def _library_lookup(self, body):
        text = (body.get("text") or body.get("q") or "").strip()
        terms = body.get("terms")
        if not terms:
            if not text:
                return {"ok": False, "error": "先输入要查的关键词"}
            plan = intake.parse(text, int(self.ctx.cfg.get("download.max_magnets") or 3))
            terms = [text]
            if plan.get("fc2"):
                terms.append(plan["fc2"]["code"])
        res = library.lookup(self.ctx.store, terms, self.ctx.cfg.data)
        return res

    def _library_dups(self, body):
        res = library.duplicates(self.ctx.store, self.ctx.cfg.data,
                                 min_size_mb=body.get("min_size_mb"))
        return res

    def _parse_payload(self, text):
        """判定 + 算出查询，不发网络请求。返回 (out, match)。"""
        got = self.ctx.pipeline.plan_parsed(text)
        plan, match = got["plan"], got["match"]
        magnets = []
        for mag in plan["magnets"]:
            magnets.append({
                "infohash": nyaa.magnet_hash(mag),
                "title": intake.title_from_magnet(mag) or intake.guess_title(text, [mag]),
                "magnet": mag,
            })
        out = {
            "ok": True,
            "mode": plan["mode"],
            "mode_label": intake.MODE_LABEL.get(plan["mode"], plan["mode"]),
            "reason": plan["reason"],
            "magnets": magnets,
            "fc2": plan["fc2"],
            "bad_magnets": plan["bad_magnets"],
            "text": intake.describe(plan),
        }
        if match is not None:
            out["query"] = match.query
            out["keywords"] = match.keywords
            out["rule"] = {
                "id": match.rule_id,
                "name": match.rule_name,
                "is_default": bool(match.is_default),
                "matched": match.matched_words,
                "category": match.rule.get("category") or "",
                "sort": match.rule.get("sort") or "",
                "filter": match.rule.get("filter") or "",
                "min_seeders": match.rule.get("min_seeders"),
            }
        return out, match

    def _intake(self, body):
        """解析预览：只判定这条消息会走哪条路，不发任何网络请求。"""
        text = (body.get("text") or body.get("q") or "").strip()
        if not text:
            return {"ok": False, "error": "先粘一段消息进来"}
        out, _match = self._parse_payload(text)
        return out

    def _search_parsed(self, body):
        """解析后搜：判定 → 亮出解析结果 → 拿判定出的查询真去站点搜一次。

        自带磁力的消息不搜（一步都不该往站点跑），只把磁力列出来给用户点投递。
        检索条件用**规则里的**分类/排序/过滤，跟机器人真跑时一致；
        面板「更多选项」里的手动条件在这里不生效，所以会在返回里写明用了哪条规则。
        """
        text = (body.get("text") or body.get("q") or "").strip()
        if not text:
            return {"ok": False, "error": "先粘一段消息进来"}
        out, match = self._parse_payload(text)
        out["searched"] = False
        if match is None:
            out["note"] = "自带磁力的消息不查站点，直接投递即可"
            return out
        rule = match.rule or {}
        try:
            result = self.ctx.pipeline.search(
                match.query,
                category=rule.get("category"),
                filt=rule.get("filter"),
                sort=rule.get("sort"),
                rule=rule,
                keywords=match.keywords,
                limit=int(body.get("limit") or 0) or None,
            )
        except Exception as e:
            log.warning("面板解析后搜失败: %s", e)
            # 解析结果本身是好的：照常返回，把失败单独说出来
            out["search_error"] = "%s: %s" % (type(e).__name__, e)
            return out
        out["searched"] = True
        out.update(self._result_payload(result))
        return out

    def _search(self, body):
        q = (body.get("q") or "").strip()
        if not q:
            return {"ok": False, "error": "请输入关键词"}
        rule = {
            "min_seeders": body.get("min_seeders", self.ctx.cfg.get("search.min_seeders")),
            "min_size_mb": body.get("min_size_mb", self.ctx.cfg.get("search.min_size_mb")),
            "max_size_mb": body.get("max_size_mb", self.ctx.cfg.get("search.max_size_mb")),
            "exclude_words": body.get("exclude_words") or [],
            "prefer_words": body.get("prefer_words") or [],
        }
        try:
            result = self.ctx.pipeline.search(
                q,
                category=body.get("category") or "0_0",
                filt=body.get("filter") or "0",
                sort=body.get("sort") or "seeders",
                rule=rule,
                keywords=body.get("keywords") or [t for t in q.split() if t],
                limit=int(body.get("limit") or 20),
            )
        except Exception as e:
            log.warning("面板搜索失败: %s", e)
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return self._result_payload(result)

    @staticmethod
    def _result_payload(result):
        """检索结果 → 面板能直接吃的结构。直接搜和解析后搜共用，免得两边字段长歪。"""
        items = []
        for it in result["items"]:
            items.append({
                "title": it.get("title"),
                "magnet": it.get("magnet"),
                "infohash": it.get("infohash"),
                "view_url": it.get("view_url"),
                "seeders": it.get("seeders"),
                "leechers": it.get("leechers"),
                "downloads": it.get("downloads"),
                "size": it.get("size_human"),
                "age": it.get("age_human"),
                "score": it.get("score"),
                "reasons": it.get("reasons"),
                "category_id": it.get("category_id"),
                "trusted": bool(it.get("trusted")),
                "remake": bool(it.get("remake")),
                "torrent_url": it.get("torrent_url") or "",
            })
        return {"ok": True, "items": items, "total": result["total"],
                "kept": result["kept"], "dropped": result["dropped"],
                "relaxed": result["relaxed"]}

    def _download(self, body):
        item = {
            "magnet": body.get("magnet") or "",
            "title": body.get("title") or "",
            "infohash": body.get("infohash") or "",
        }
        if not item["magnet"]:
            return {"ok": False, "error": "缺少 magnet"}
        res = self.ctx.pipeline.submit(item, backend=body.get("backend"))
        return {"ok": bool(res.get("ok")), "result": res}

    # ---------- 静态文件 ----------
    def _stamp_manual_token(self, patch):
        """手填/改过 fnos-token 就记下时间与来源。

        不记的话，界面上永远显示「几个月前」，用户没法判断这串东西还算不算数；
        判断过期与否全靠这个时间戳（``XunleiDownloader.token_age``）。
        """
        x = (((patch or {}).get("download") or {}).get("xunlei") or {})
        tok = x.get("fnos_token")
        if not isinstance(tok, str) or not tok.strip() or tok == "******":
            return
        self.ctx.cfg.apply_patch({"download": {"xunlei": {
            "fnos_token_ts": int(time.time()),
            "fnos_token_source": "manual",
        }}})

    def _detect_xunlei(self, body, force=False):
        """侦测本机迅雷（官方套件网关），打通就写配置并复测。

        1.0.31 起只有一个接法：飞牛网关。``force`` 是面板上的「立即重连」——
        即使端口看起来没变也重扫一遍，省得用户为了换一次令牌去重启服务。
        """
        res = xunlei_detect.autoconnect(self.ctx.cfg,
                                        fnos_token=self.ctx.fnos_token,
                                        fnos_port=self.ctx.fnos_port or None,
                                        force=force)
        rep = res["report"]
        best = rep.get("best") or {}
        probe = {}
        if res["ok"]:
            inst = downloaders.make("xunlei", self.ctx.cfg.data,
                                    self.ctx.cfg.get("telegram.proxy") or "")
            probe = inst.probe() if inst else {}
            res["ok"] = bool(probe.get("ok"))
            if res["ok"]:
                note = "已连上飞牛官方迅雷套件（端口 %s），配置已更新" % res["port"] \
                    if res.get("applied") else "已连上飞牛官方迅雷套件（端口 %s）" % res["port"]
                rep["notes"] = [note]
            else:
                rep["notes"] = ["端口通了但复测没通过：%s"
                                % (probe.get("error") or probe.get("hint") or "未知原因")]
        rep["applied"] = res.get("applied") or {}
        rep["probe"] = probe
        rep["ok"] = bool(res["ok"])
        rep["port"] = res.get("port") or 0
        rep["text"] = xunlei_detect.format_report(rep)
        rep.pop("_secrets", None)  # 明文令牌只用于写配置，不回给浏览器
        if best:
            self.ctx.xunlei_conn = {
                "ok": bool(res["ok"]),
                "port": rep["port"],
                "text": (rep["notes"] or [""])[0],
                "ts": time.time(),
            }
        return rep

    def _xunlei_folder(self, body):
        """列出「迅雷自己用过的下载目录」，body.apply=true 时把第一条写进配置。

        投递报 parent folder not found / 只回一个数字码时，用户最需要的就是一个
        真实可用的目录 id。任务列表里记着迅雷自己写过的 id，比自己抄靠谱。
        """
        inst = downloaders.make("xunlei", self.ctx.cfg.data,
                                self.ctx.cfg.get("telegram.proxy") or "")
        if not inst or not hasattr(inst, "folder_candidates"):
            return self._error("迅雷下载器不可用")
        try:
            cands = inst.folder_candidates(refresh=True)
        except Exception as e:
            log.exception("读取迅雷下载目录失败")
            return self._error("%s: %s" % (type(e).__name__, e))
        applied = ""
        if body.get("apply") and cands:
            best = cands[0]
            patch = {"parent_folder_id": best["parent_folder_id"]}
            if best.get("parent_folder_path"):
                patch["parent_folder_path"] = best["parent_folder_path"]
            self.ctx.cfg.apply_patch({"download": {"xunlei": patch}})
            applied = best.get("parent_folder_path") or best["parent_folder_id"]
        return {"ok": True, "folders": cands, "applied": applied}

    def _xunlei_parse(self, body):
        """解析测试：这条磁力里到底有几个文件、按当前规则会挑哪几个。

        只问不投。用户报「筛选没生效、全下了」时，第一件要弄清的事就是
        「程序眼里的文件清单长什么样」——是迅雷只回了一个目录节点，
        还是规则真的没生效。这个接口把两件事一次摆出来。
        """
        magnet = str(body.get("magnet") or "").strip()
        if not magnet:
            return self._error("先粘一条磁力链接")
        inst = downloaders.make("xunlei", self.ctx.cfg.data,
                                self.ctx.cfg.get("telegram.proxy") or "")
        if not inst or not hasattr(inst, "inspect"):
            return self._error("迅雷下载器不可用")
        probe = body.get("probe")
        if isinstance(probe, dict):
            # 内部诊断：把这段请求体原样打给迅雷的解析接口，回原始响应（截断）。
            # 迅雷各版本认的字段不一样，「往目录节点里钻」到底该用哪套参数，
            # 靠它现场试出来，不用为了一次试探就重新装一遍 fpk。
            try:
                got = inst._parse_body(probe)
            except Exception as e:  # noqa: BLE001 - 诊断接口自己不能崩
                return self._error("%s: %s" % (type(e).__name__, e))
            res = got.get("resources") or []
            return {"ok": True, "probe": probe, "count": len(res),
                    "resources": res[:20],
                    "raw": json.dumps(got.get("raw") or {}, ensure_ascii=False)[:4000]}
        torrent_url = str(body.get("torrent_url") or "").strip()
        if not torrent_url:
            infohash = nyaa.magnet_hash(magnet)
            torrent_url = self.ctx.pipeline.torrent_url_for(infohash) if infohash else ""
        try:
            rep = inst.inspect(magnet, torrent_url=torrent_url)
        except Exception as e:  # noqa: BLE001 - 诊断接口自己不能崩
            log.exception("解析测试失败")
            return self._error("%s: %s" % (type(e).__name__, e))
        rep.update({"ok": True, "magnet": magnet[:200], "torrent_url": torrent_url})
        return rep

    def _static(self, path):
        root = self.ctx.ui_dir
        if not root or not os.path.isdir(root):
            return self._error("UI 目录不存在", 500)
        if path in ("/", ""):
            path = "/index.html"
        safe = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        full = os.path.join(root, safe.replace("/", os.sep))
        if not os.path.abspath(full).startswith(os.path.abspath(root)):
            return self._error("非法路径", 403)
        if not os.path.isfile(full):
            full = os.path.join(root, "index.html")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._error("文件不存在", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 面板是局域网小站，每次升级都换 CSS/JS：只要 no-store 才能真正保证
        # 手机浏览器不会拿着旧 app.js 打开新页面（不然会看到「升级了但布局没变」）。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


class WebUI(object):
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.httpd = None
        self._thread = None

    def start(self):
        Handler.ctx = self.ctx
        bind = self.ctx.cfg.get("app.bind") or "0.0.0.0"
        port = int(self.ctx.cfg.get("app.port") or 8470)
        self.ctx.web_port = port
        self.httpd = ThreadingHTTPServer((bind, port), Handler)
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        name="webui", daemon=True)
        self._thread.start()
        log.info("管理面板已启动：http://%s:%d", bind, port)
        return True

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
