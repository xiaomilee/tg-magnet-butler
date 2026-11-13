"""极简 Telegram Bot API 客户端（只用标准库，不依赖 python-telegram-bot）。"""
from __future__ import annotations

import json
import logging
import time

from . import util

log = logging.getLogger("tmb.tg")


class TelegramError(Exception):
    def __init__(self, msg, code=None, retry_after=None):
        super().__init__(msg)
        self.code = code
        self.retry_after = retry_after

    @property
    def conflict(self):
        """409：这串 Token 还有别人在收消息（另一个实例轮询，或者设了 webhook）。"""
        return self.code == 409


def _http_message(method, err):
    """HTTP 非 2xx 的可读说明。

    Telegram 的错因写在响应体的 description 里（比如 409 是
    「Conflict: terminated by other getUpdates request」），只报状态码的话
    用户只能看到一个裸 409，不知道该停谁。
    """
    base = "%s 请求失败: %s" % (method, err)
    desc = ""
    try:
        raw = err.body or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        desc = str((json.loads(raw.decode("utf-8", "replace") or "{}")
                    or {}).get("description") or "").strip()
    except Exception:
        desc = ""
    if desc and desc not in base:
        return "%s（服务端原话：%s）" % (base, desc)
    return base


class Telegram(object):
    def __init__(self, token: str, proxy: str = "", api_base: str = "https://api.telegram.org"):
        if not token:
            raise TelegramError("bot_token 未配置")
        self.token = token
        # 没填代理时沿用环境变量里的代理，避免升级后行为变化（http_request 默认不走环境代理）
        self.proxy = util.effective_proxy(proxy)[0]
        self.api_base = (api_base or "https://api.telegram.org").rstrip("/")

    def _url(self, method):
        return "%s/bot%s/%s" % (self.api_base, self.token, method)

    def call(self, method, params=None, timeout=30, retries=2):
        params = params or {}
        url = self._url(method)
        # 每次调用重新问一遍线路顺序：面板上刚加的通道、刚被判死的线路，
        # 下一条消息就该按新顺序走，不用重启机器人。
        chain = util.proxy_chain(self.proxy)
        last = None
        for attempt in range(retries + 1):
            try:
                status, text, _ = util.http_request(
                    url, method="POST", data=params, timeout=timeout,
                    proxy=chain[min(attempt, len(chain) - 1)], retries=0)
                data = json.loads(text or "{}")
                if not data.get("ok"):
                    desc = data.get("description") or text[:200]
                    code = data.get("error_code")
                    retry_after = (data.get("parameters") or {}).get("retry_after")
                    if code == 429 and retry_after:
                        log.warning("Telegram 限流，等待 %ss", retry_after)
                        time.sleep(float(retry_after) + 0.5)
                        continue
                    raise TelegramError("%s: %s" % (method, desc), code, retry_after)
                return data.get("result")
            except util.HttpError as e:
                last = e
                if e.status == 429 or (e.status and e.status >= 500):
                    time.sleep(2 + attempt * 2)
                    continue
                raise TelegramError(_http_message(method, e), e.status)
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise TelegramError("%s 失败: %s: %s" % (method, type(e).__name__, e))
        raise TelegramError("%s 失败: %s" % (method, last))

    # ---------- 常用封装 ----------
    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=0, timeout=50, allowed_updates=None):
        params = {"offset": offset, "timeout": timeout}
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        return self.call("getUpdates", params, timeout=timeout + 20, retries=1)

    def get_webhook_info(self):
        """409 的第一个嫌疑人：以前给这个 bot 设过 webhook。设了就收不了 getUpdates。"""
        return self.call("getWebhookInfo") or {}

    def delete_webhook(self, drop_pending=False):
        """清掉 webhook，改回长轮询。默认不丢积压消息（drop_pending_updates=False）。"""
        return self.call("deleteWebhook",
                         {"drop_pending_updates": bool(drop_pending)})

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML",
                     disable_web_page_preview=True, reply_to=None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        if reply_to:
            params["reply_to_message_id"] = reply_to
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None,
                          parse_mode="HTML"):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": parse_mode, "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("editMessageText", params)

    def answer_callback(self, callback_id, text="", alert=False):
        try:
            self.call("answerCallbackQuery",
                      {"callback_query_id": callback_id, "text": text,
                       "show_alert": bool(alert)})
        except TelegramError as e:
            log.debug("answerCallbackQuery 失败: %s", e)

    def delete_message(self, chat_id, message_id):
        try:
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            pass
