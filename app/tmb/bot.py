"""Telegram 机器人：长轮询 + 命令 + 内联按钮回调。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import util
from .analyzer import pick
from .pipeline import Pipeline
from .tg import Telegram, TelegramError

log = logging.getLogger("tmb.bot")

# 409 撞车时的重试节奏。真撞车时催得紧没用，15 秒一次只会把日志刷满；
# 而「自己点重连」造成的撞车是几秒内自愈的，所以先用短退避试一次。
CONFLICT_RETRY = 3
CONFLICT_BACKOFF = 300

HELP_TEXT = """<b>磁力管家</b>

把任意一段文字、或者从别处<u>转发</u>过来的消息丢给我，
我会按关键词自动分拣，去 sukebei 搜索、挑出最靠谱的结果，并投递到飞牛里的迅雷。

<b>用法</b>
· 直接发消息：<code>求 某某 1080p 中字</code>
· 转发消息：把别人发来的文字转发给我即可
· 指定关键词搜索：<code>/search 关键词</code>
· 忽略历史字典强制重下：<code>/force 关键词</code>（库里已有、或曾有但已删的，都要它才继续）
· 查看规则：<code>/rules</code>　状态：<code>/status</code>　统计：<code>/stats</code>
· 暂停/恢复接收消息：<code>/pause</code> <code>/resume</code>（暂停期间只回命令，不再检索、不再投递）

<b>说明</b>
搜索结果按做种数、热度、新鲜度、标题命中率综合排序；
自动投递的是第 1 名，点按钮可以换别的条目。
面板里关掉「自动投递」后不会自动下：文字消息只回结果列表、自带磁力的消息只回按钮，
点 <b>⬇️ N</b> 才真正投进迅雷。"""


class BotService(object):
    def __init__(self, cfg, store, pipeline: Pipeline):
        self.cfg = cfg
        self.store = store
        self.pipeline = pipeline
        self.tg = None
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0
        self._offset_path = os.path.join(cfg.var_dir, "offset.json")
        self.paused = False
        self.last_error = ""
        self.started_at = 0.0
        self.me = {}
        # 轮询健康度：ok / conflict / webhook_cleared，面板拿它挂提示
        self.poll_state = "idle"
        self._gen = 0                 # 线程代际：重连时把还在长轮询里那条判老，让它自己退
        self._conflict_streak = 0     # 连着撞了几次 409
        self._conflict_logged = False
        self._webhook_checked = False
        self._load_offset()

    # ---------- 生命周期 ----------
    def _load_offset(self):
        data = util.load_json(self._offset_path, {}) or {}
        self._offset = int(data.get("offset") or 0)

    def _save_offset(self):
        try:
            util.save_json(self._offset_path, {"offset": self._offset}, 0o600)
        except OSError:
            pass

    def start(self):
        token = self.cfg.get("telegram.bot_token")
        if not token:
            log.warning("未配置 telegram.bot_token，机器人不会启动")
            self.last_error = "未配置 bot token"
            return False
        try:
            self.tg = Telegram(token, proxy=self.cfg.get("telegram.proxy") or "")
            self.me = self.tg.get_me() or {}
            log.info("机器人已连接：@%s", self.me.get("username"))
        except Exception as e:
            self.last_error = "连接 Telegram 失败: %s" % e
            log.error(self.last_error)
            return False
        self._stop.clear()
        self._gen += 1
        gen = self._gen
        self._thread = threading.Thread(target=self._loop, args=(gen,),
                                        name="tg-poll", daemon=True)
        self._thread.start()
        self.started_at = time.time()
        return True

    def stop(self):
        """停轮询。代际 +1：还在 getUpdates 里挂着的那条线程回来后不会再抢一轮。"""
        self._stop.set()
        self._gen += 1

    # ---------- 轮询 ----------
    def _stale(self, gen):
        return self._stop.is_set() or (gen is not None and gen != self._gen)

    def _loop(self, gen=None):
        backoff = 2
        while not self._stale(gen):
            try:
                updates = self.tg.get_updates(
                    offset=self._offset,
                    timeout=int(self.cfg.get("telegram.poll_timeout") or 50),
                    allowed_updates=["message", "callback_query"])
            except TelegramError as e:
                # 重连时被新连接顶掉的那条旧请求也会回 409，它已经该退了，不必报警
                if self._stale(gen):
                    return
                if getattr(e, "conflict", False):
                    self._stop.wait(self._on_conflict(e))
                    continue
                self.last_error = str(e)
                log.warning("轮询异常: %s", e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            except Exception as e:
                if self._stale(gen):
                    return
                self.last_error = str(e)
                log.exception("轮询未知异常")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

            backoff = 2
            self._poll_ok()
            for up in updates or []:
                if self._stale(gen):
                    return
                self._offset = max(self._offset, int(up.get("update_id", 0)) + 1)
                self._save_offset()
                try:
                    self._dispatch(up)
                except Exception:
                    log.exception("处理 update 失败")

    # ---------- 409 轮询冲突 ----------
    def _poll_ok(self):
        """这一轮长轮询正常返回了：把冲突状态清掉（只在状态变化时说一句）。"""
        if self.poll_state in ("conflict", "webhook_cleared"):
            log.info("轮询已恢复正常")
            self.last_error = ""
        self.poll_state = "ok"
        self._conflict_streak = 0
        self._conflict_logged = False

    def _on_conflict(self, err):
        """HTTP 409：同一串 Bot Token 被别处抢着收消息。返回这次等多久。"""
        self.poll_state = "conflict"
        self._conflict_streak += 1
        if self._conflict_streak < 2:
            # 面板上点「重连」、改完配置保存，都会走到这：旧的长轮询还挂在 Telegram 那边，
            # 新的请求把它顶掉，旧的那条回 409。等两秒，旧的退了自然接上。
            self.last_error = "轮询冲突：正在切换连接"
            log.info("轮询冲突（%s），可能是刚重连、旧的长轮询还没退，先等几秒再看", err)
            return CONFLICT_RETRY
        # 连着撞两次就不是自己人打架了：先看是不是被 webhook 占着
        if not self._webhook_checked:
            self._webhook_checked = True
            if self._try_clear_webhook():
                return CONFLICT_RETRY
        self.last_error = ("轮询冲突：同一个 Bot Token 还有别处在收消息"
                           "（另一台设备的磁力管家 / 没退干净的旧进程 / 设过 webhook）")
        if not self._conflict_logged:
            self._conflict_logged = True
            log.warning(
                "轮询冲突（HTTP 409，服务端原话见上）：这串 Bot Token 还有另一个地方在收消息，"
                "本次先等 %d 分钟再试。常见原因："
                "① 另一台设备或容器上跑着同一个机器人（同一个 token 只能有一处 getUpdates）；"
                "② 旧进程没退干净——先在应用中心「停止」再「启动」，别直接重装；"
                "③ 以前给这个 bot 设过 webhook，长轮询就收不到消息了。"
                "处理完不用重启，最长 %d 分钟后会自动接上。",
                CONFLICT_BACKOFF // 60, CONFLICT_BACKOFF // 60)
        return CONFLICT_BACKOFF

    def _try_clear_webhook(self):
        """409 的头号原因：这个 bot 被设过 webhook。本程序只用长轮询，清掉即可。"""
        if not self.cfg.get("telegram.auto_clear_webhook", True):
            log.warning("检测到轮询冲突，但 telegram.auto_clear_webhook 已关闭，不自动清 webhook")
            return False
        try:
            info = self.tg.get_webhook_info() or {}
        except TelegramError as e:
            log.debug("查 webhook 失败，跳过：%s", e)
            return False
        url = str(info.get("url") or "").strip()
        if not url:
            log.debug("没有设置 webhook，409 来自另一个轮询实例")
            return False
        try:
            self.tg.delete_webhook()
        except TelegramError as e:
            log.warning("清除 webhook 失败：%s", e)
            return False
        log.info("这个机器人原本设了 webhook（%s），长轮询抢不过它；已清除，改回 getUpdates", url)
        self.last_error = "已清除之前设置的 webhook，改回长轮询"
        self.poll_state = "webhook_cleared"
        return True

    def poll_hint(self):
        """面板侧边栏那一行字：短、能直接告诉人该干什么。"""
        if self.poll_state == "conflict":
            return "同一 Bot Token 另有实例在收消息"
        if self.poll_state == "webhook_cleared":
            return "已清除 webhook，改回长轮询"
        return ""

    # ---------- 分发 ----------
    def _dispatch(self, up):
        if "callback_query" in up:
            self._on_callback(up["callback_query"])
        elif "message" in up:
            self._on_message(up["message"])

    def _allowed(self, chat_id):
        ids = self.cfg.get("telegram.allowed_chat_ids") or []
        return (not ids) or (chat_id in ids)

    def _is_admin(self, user_id):
        ids = self.cfg.get("telegram.admin_user_ids") or []
        return (not ids) or (user_id in ids)

    def send(self, chat_id, text, reply_markup=None, reply_to=None):
        try:
            return self.tg.send_message(chat_id, text, reply_markup=reply_markup,
                                        reply_to=reply_to)
        except TelegramError as e:
            log.warning("发送失败: %s", e)
            # 兜底：去掉 HTML 重发一次，避免标签错误导致完全静默
            try:
                plain = util.strip_html(text.replace("<code>", "").replace("</code>", ""))
                return self.tg.send_message(chat_id, plain[:4000], reply_markup=reply_markup)
            except TelegramError:
                return None

    def _on_message(self, msg):
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        user = msg.get("from") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        msg_id = msg.get("message_id")

        if not self._allowed(chat_id):
            log.info("忽略未授权会话 %s", chat_id)
            return
        if not text:
            return

        if text.startswith("/"):
            self._on_command(chat_id, user, text, msg_id)
            return

        if self.paused:
            self.send(chat_id, "⏸ 已暂停接收。发来的内容不会处理，发送 /resume 恢复。",
                      reply_to=msg_id)
            return

        is_forward = bool(msg.get("forward_origin") or msg.get("forward_from")
                          or msg.get("forward_from_chat") or msg.get("forward_date"))
        log.info("收到%s：%s", "转发" if is_forward else "消息", text[:80])
        if not is_forward:
            # 普通文本也接受，但提示一下更顺手的用法只提示一次（避免噪音），此处不提示
            pass
        self._run_pipeline(chat_id, text, msg_id)

    def _run_pipeline(self, chat_id, text, msg_id=None, edit_message_id=None, force=False):
        wait = None
        if not msg_id and not edit_message_id:
            wait = self.send(chat_id, "🔎 正在处理消息…")
        t0 = time.time()
        bundle = self.pipeline.handle_text(text, chat_id, force=force)
        elapsed = time.time() - t0
        body = self.pipeline.format_bundle(bundle)
        body += "\n\n<i>耗时 %.1fs</i>" % elapsed
        kb = self.pipeline.keyboard(bundle)

        if edit_message_id:
            try:
                self.tg.edit_message_text(chat_id, edit_message_id, body, reply_markup=kb)
                return bundle
            except TelegramError:
                pass
        if wait and wait.get("message_id"):
            try:
                self.tg.edit_message_text(chat_id, wait["message_id"], body, reply_markup=kb)
                return bundle
            except TelegramError:
                pass
        self.send(chat_id, body, reply_markup=kb, reply_to=msg_id)
        return bundle

    # ---------- 命令 ----------
    def _on_command(self, chat_id, user, text, msg_id):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        if cmd in ("/start", "/help"):
            self.send(chat_id, HELP_TEXT, reply_to=msg_id)
            return

        if cmd == "/id":
            self.send(chat_id, "chat_id：<code>%s</code>\nuser_id：<code>%s</code>"
                      % (chat_id, user.get("id")), reply_to=msg_id)
            return

        if cmd == "/status":
            self.send(chat_id, self._status_text(), reply_to=msg_id)
            return

        if cmd == "/rules":
            self.send(chat_id, self._rules_text(), reply_to=msg_id)
            return

        if cmd == "/stats":
            self.send(chat_id, self._stats_text(), reply_to=msg_id)
            return

        if cmd in ("/pause", "/resume"):
            if not self._is_admin(user.get("id")):
                self.send(chat_id, "无权限。", reply_to=msg_id)
                return
            self.paused = cmd == "/pause"
            self.send(chat_id, "⏸ 已暂停：内容不再处理（命令仍可用）" if self.paused
                      else "▶️ 已恢复", reply_to=msg_id)
            return

        if cmd == "/search":
            q = " ".join(args).strip()
            if not q:
                self.send(chat_id, "用法：<code>/search 关键词</code>", reply_to=msg_id)
                return
            self._run_pipeline(chat_id, q, msg_id)
            return

        if cmd == "/force":
            # 历史字典说「已存在」但你就是要重下时用
            q = " ".join(args).strip()
            if not q:
                self.send(chat_id, "用法：<code>/force 关键词</code>（忽略历史字典，强制检索投递）",
                          reply_to=msg_id)
                return
            self._run_pipeline(chat_id, q, msg_id, force=True)
            return

        self.send(chat_id, "未知命令。发送 /help 查看用法。", reply_to=msg_id)

    def _status_text(self):
        d = self.cfg.data
        st = self.store.stats()
        lines = ["<b>运行状态</b>"]
        lines.append("机器人：@%s" % (self.me.get("username") or "未连接"))
        lines.append("运行时长：%s" % _dur(time.time() - self.started_at if self.started_at else 0))
        lines.append("暂停：%s" % ("是" if self.paused else "否"))
        lines.append("自动投递：%s" % (
            "开（命中第 %s 名直接下）" % (d.get("download", {}).get("auto_pick") or 1)
            if d.get("download", {}).get("auto_download", True)
            else "关（只回结果，等点按钮）"))
        lines.append("下载后端：<code>%s</code>（兜底 %s）" % (
            util.esc(d.get("download", {}).get("backend")),
            util.esc(",".join(d.get("download", {}).get("fallback") or []))))
        lines.append("迅雷模式：<code>%s</code> %s:%s" % (
            util.esc(d.get("download", {}).get("xunlei", {}).get("mode")),
            util.esc(d.get("download", {}).get("xunlei", {}).get("host")),
            util.esc(d.get("download", {}).get("xunlei", {}).get("port"))))
        lines.append("站点：<code>%s</code>" % util.esc(d.get("nyaa", {}).get("base_url")))
        lines.append("累计投递：%d（今日 %d）" % (st["success"], st["today"]))
        if self.last_error:
            lines.append("最近错误：%s" % util.esc(self.last_error[:200]))
        return "\n".join(lines)

    def _rules_text(self):
        rules = (self.cfg.rules or {}).get("rules") or []
        if not rules:
            return "还没有配置规则。"
        lines = ["<b>关键词规则</b>（按顺序匹配，命中分最高者生效）"]
        for r in rules:
            flag = "✅" if r.get("enabled", True) else "⛔"
            words = (r.get("match", {}) or {}).get("any") or []
            lines.append("%s <b>%s</b>　命中词：%s" % (
                flag, util.esc(r.get("name") or r.get("id")),
                util.esc("、".join(words[:8]) or "（正则）")))
            lines.append("　分类 %s　≥%s 做种　自动投递 %s" % (
                util.esc(r.get("category") or "0_0"), r.get("min_seeders", 0),
                "开" if r.get("auto_download", True) else "关"))
        dflt = (self.cfg.rules or {}).get("default") or {}
        lines.append("")
        lines.append("兜底规则：查询模板 <code>%s</code>" % util.esc(dflt.get("query_template")))
        return "\n".join(lines)

    def _stats_text(self):
        st = self.store.stats()
        rows = self.store.recent_downloads(8)
        lines = ["<b>统计</b>"]
        lines.append("累计任务 %d，成功 %d，今日 %d" % (st["total"], st["success"], st["today"]))
        if rows:
            lines.append("")
            lines.append("<b>最近投递</b>")
            for r in rows:
                icon = {"ok": "✅", "queued": "⏳", "skipped_dup": "⏭"}.get(r.get("status"), "⚠️")
                lines.append("%s %s　<i>%s</i>" % (
                    icon, util.esc(util.truncate(r.get("title"), 60)),
                    util.esc(time.strftime("%m-%d %H:%M", time.localtime(r.get("ts") or 0)))))
        return "\n".join(lines)

    # ---------- 按钮 ----------
    def _on_callback(self, cq):
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        user = cq.get("from") or {}
        if not self._allowed(chat_id):
            self.tg.answer_callback(cq.get("id"), "未授权")
            return

        parts = data.split(":")
        action = parts[0] if parts else ""

        if action == "dl" and len(parts) == 3:
            rid, idx = parts[1], int(parts[2] or 1)
            rec = self.store.get_search(rid)
            if not rec:
                self.tg.answer_callback(cq.get("id"), "会话已过期，请重新搜索")
                return
            item = pick((rec["payload"] or {}).get("items") or [], idx)
            if not item:
                self.tg.answer_callback(cq.get("id"), "该条目已不存在")
                return
            self.tg.answer_callback(cq.get("id"), "正在投递…")
            res = self.pipeline.submit(item)
            if res.get("ok"):
                self.send(chat_id, "✅ 已投递 <b>%s</b>\n后端：%s%s" % (
                    util.esc(util.truncate(res.get("title"), 80)),
                    util.esc(res.get("backend")),
                    ("\n任务号：<code>%s</code>" % util.esc(res.get("task_id")))
                    if res.get("task_id") else ""))
            elif res.get("deduped"):
                self.send(chat_id, "⏭ %s" % util.esc(res.get("message")))
            else:
                self.send(chat_id, "⚠️ 投递失败：%s\n\n磁力：\n<code>%s</code>" % (
                    util.esc(res.get("message")), util.esc(item.get("magnet"))))
            return

        if action == "rs" and len(parts) == 2:
            rec = self.store.get_search(parts[1])
            self.tg.answer_callback(cq.get("id"), "重新搜索…")
            if not rec:
                self.send(chat_id, "会话已过期，请重新发送关键词。")
                return
            q = (rec["payload"] or {}).get("query") or rec.get("query") or ""
            self._run_pipeline(chat_id, q, edit_message_id=message_id)
            return

        if action == "mg" and len(parts) == 2:
            rec = self.store.get_search(parts[1])
            self.tg.answer_callback(cq.get("id"), "已发送")
            if not rec:
                return
            bundle = {"items": (rec["payload"] or {}).get("items") or []}
            text = self.pipeline.magnet_list_text(bundle, max_items=4)
            for chunk in _split(text, 3800):
                self.send(chat_id, chunk)
            return

        self.tg.answer_callback(cq.get("id"), "未知操作")


def _split(text, size):
    out = []
    while text:
        out.append(text[:size])
        text = text[size:]
    return out or [""]


def _dur(sec):
    sec = int(sec or 0)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%d天%d小时" % (d, h)
    if h:
        return "%d小时%d分" % (h, m)
    return "%d分%d秒" % (m, s)
