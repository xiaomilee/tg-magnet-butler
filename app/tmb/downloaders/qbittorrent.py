"""qBittorrent Web API 适配器（兜底）。

以前这里所有失败都吞成一句「登录失败」，等于没报错。qB 的失败原因其实
分得很清楚，只是藏在 HTTP 状态码和返回体里：

    200 + "Ok."    -> 登录成功
    200 + "Fails." -> 账号密码不对（qB 5.x 首次启动会生成随机临时密码）
    401 + 空       -> Host header 校验 / CSRF 拦截（用域名或内网 IP 访问最常见）
    403 + 空       -> 连续登录失败被临时封 IP，或反代没转发登录接口
    404            -> 地址/端口不是 qB WebUI，或子路径没带前缀
    登录返回 Ok 但后续 403 -> Set-Cookie 被反代改写/丢弃

下面把这些一一翻成「原因 + 怎么修」，并且：
  * 内网地址强制直连，不让 telegram/nyaa 的代理劫持 NAS 本机请求
  * 登录后用 /api/v2/app/version 复核会话，不只信登录接口的返回体
  * 投递时若会话过期自动重登一次
"""
from __future__ import annotations

import errno
import http.cookiejar
import logging
import urllib.error
import urllib.parse
import urllib.request

from .. import util
from .base import BaseDownloader, DownloadResult

log = logging.getLogger("tmb.qb")

_HINT_HOSTCHECK = (
    "qB 设置 → Web 界面：关闭「启用 Host header 校验」，"
    "或把「服务器域名」加上 NAS 的 IP/域名（也可填 *）；"
    "程序与 qB 同机时，地址直接写 http://127.0.0.1:8080。"
)
_HINT_BANNED = (
    "连续登录失败会被 qB 临时封 IP。等 15 分钟或重启 qB 再试；"
    "长期方案是到「设置 → Web 界面」把「最大认证失败次数」调大。"
)
_HINT_CREDS = (
    "qB 默认 admin/adminadmin；v5 起首次启动会在容器日志里打印随机临时密码，"
    "登录后到「设置 → Web 界面」改掉。"
)


def _user_or(value):
    v = str(value or "").strip()
    return v if v else "空"


class QbittorrentDownloader(BaseDownloader):
    name = "qbittorrent"

    def __init__(self, cfg: dict, proxy: str = ""):
        self.cfg = cfg or {}
        self.url = (self.cfg.get("url") or "http://127.0.0.1:8080").strip().rstrip("/")
        if "://" not in self.url:
            self.url = "http://" + self.url
        # 内网/本机地址一律直连：把 NAS 上的 127.0.0.1 / 192.168.x 交给机场代理只会超时
        if util.is_local_url(self.url):
            self.proxy = ""
        else:
            self.proxy = str(self.cfg.get("proxy") or proxy or "").strip()
        self._opener = None
        self._logged_in = False
        self.last_error = ""
        self.last_hint = ""
        self.version = ""

    # ---------------- 底层 ----------------
    def _build(self):
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy}))
        else:
            # 显式清空，否则 urllib 会套用 http_proxy 环境变量
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def _open(self, path, data=None, method="POST", timeout=20):
        """返回 (status, body)。4xx/5xx 不抛异常，交给上层分类，避免原因丢失。"""
        if self._opener is None:
            self._build()
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(self.url + path, data=body, method=method)
        req.add_header("Referer", self.url + "/")
        req.add_header("Accept", "text/plain, application/json;q=0.9, */*;q=0.8")
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            return e.code, raw.decode("utf-8", "replace")

    def _explain(self, e):
        """直连时不要把「代理没启动」这类话带进报错里，避免误导。"""
        return util.explain_net_short(e, self.proxy)

    def _version(self):
        """未登录也能探：200 说明免密可用，403 说明要登录，401/404 是硬故障。"""
        try:
            status, text = self._open("/api/v2/app/version", method="GET", timeout=12)
        except Exception as e:
            return "", self._explain(e)
        if status == 200:
            return (text or "").strip() or "?", ""
        return "", "HTTP %s" % status

    # ---------------- 登录 ----------------
    def login(self):
        self._logged_in = False
        user = str(self.cfg.get("username") or "").strip()
        pwd = str(self.cfg.get("password") or "")
        try:
            status, text = self._open("/api/v2/auth/login",
                                      {"username": user, "password": pwd})
        except Exception as e:
            self.last_error = "连不上 %s：%s" % (self.url, self._explain(e))
            self.last_hint = self._conn_hint(e)
            log.warning("qB 连接失败 %s: %s", self.url, e)
            return False

        text = (text or "").strip()
        if status == 200 and text.lower().startswith("ok"):
            ver, why = self._version()
            if ver:
                self.version = ver
                self._logged_in = True
                self.last_error = ""
                self.last_hint = ""
                log.info("qB 登录成功：%s v%s", self.url, ver)
                return True
            self.last_error = "登录返回 Ok，但会话没生效（复核 %s）" % (why or "无响应")
            self.last_hint = ("Cookie 没带回来：多半是反代改写/丢弃了 Set-Cookie，"
                              "或 qB 前面还有一层网关鉴权。建议直连 WebUI 端口。")
            return False

        self.last_error, self.last_hint = self._classify(status, text)
        log.warning("qB 登录失败 %s: %s", self.url, self.last_error)
        return False

    def _classify(self, status, text):
        body = util.strip_html(text or "")
        body = " ".join(body.split())[:140]
        low = body.lower()

        if status == 401:
            return ("被 qB 的 Host header 校验 / CSRF 拦截（HTTP 401）", _HINT_HOSTCHECK)
        if status == 403:
            if "ban" in low:
                return ("IP 已被 qB 封禁（连续登录失败）", _HINT_BANNED)
            if not body:
                return ("被拒绝（HTTP 403）：可能是密码错太多次被临时封禁，或反代没转发登录接口",
                        "按顺序排查：① 之前密码试错太多被临时封 IP（等 15 分钟或重启 qB）；"
                        "② 反代/网关没把 /api/v2/auth/login 转发到 qB；"
                        "③ qB 开了「仅 HTTPS」，地址要写 https://。")
            return ("被拒绝（HTTP 403）：%s" % body, "检查反代鉴权或 qB 的「仅 HTTPS」开关。")
        if status == 404:
            return ("接口不存在（HTTP 404）",
                    "地址或端口不是 qB WebUI：确认端口（默认 8080），"
                    "若挂在子路径要带前缀，例如 http://nas:8080/qbt。")
        if status == 405:
            return ("请求方法不被允许（HTTP 405）", "反代只转发了 GET，检查反代配置。")
        if low.startswith("fails"):
            who = "用户名「%s」，密码%s" % (_user_or(self.cfg.get("username")),
                                          "已填" if self.cfg.get("password") else "为空")
            return ("账号或密码不对（qB 返回 Fails.；%s）" % who, _HINT_CREDS)
        if not body:
            return ("登录接口没有返回内容（HTTP %s）" % status,
                    "qB 前面有反代/网关把响应截断了，建议直连端口试试。")
        return ("登录失败（HTTP %s）：%s" % (status, body), "把这段原文贴出来可以继续定位。")

    def _conn_hint(self, e):
        reason = getattr(e, "reason", e)
        code = getattr(reason, "errno", None) or getattr(e, "errno", None)
        if code in (errno.ECONNREFUSED, 10061):
            return ("端口上没有服务：确认 qBittorrent 已启动、WebUI 端口（默认 8080）已开启；"
                    "若 qB 只监听 127.0.0.1，跨机器访问不了，需要改 bind 地址或用反代。")
        if code in (errno.ETIMEDOUT, 10060):
            return "数据包没回应：检查 NAS 防火墙、qB 端口映射，以及地址是否写成了外网域名。"
        if code in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return "目标不可达：确认地址与 NAS 在同一网段，或有可用的路由。"
        text = str(e).lower()
        if "certificate" in text or "ssl" in text:
            return ("证书校验失败：自签证书可临时设环境变量 TMB_INSECURE_TLS=1，"
                    "或把地址从 https:// 改成 http://。")
        return "检查 qB 是否运行、地址端口是否正确、以及本机到 qB 的网络是否通。"

    # ---------------- 对外 ----------------
    def add(self, magnet, title="", options=None):
        data = {"urls": magnet}
        if self.cfg.get("save_path"):
            data["savepath"] = self.cfg["save_path"]
        if self.cfg.get("category"):
            data["category"] = self.cfg["category"]
        if self.cfg.get("paused"):
            data["paused"] = "true"
        if self.cfg.get("tags"):
            data["tags"] = self.cfg["tags"]

        for attempt in (0, 1):
            if not self._logged_in and not self.login():
                return DownloadResult(False, self.name, "",
                                      self.last_error or "qB 登录失败")
            try:
                status, text = self._open("/api/v2/torrents/add", data)
            except Exception as e:
                return DownloadResult(False, self.name, "",
                                      "qB 请求失败：%s" % self._explain(e))

            if status == 403 and attempt == 0:
                # 会话过期（qB 重启/超时）：重登一次再试
                self._logged_in = False
                continue
            if status == 200 and "fail" not in (text or "").lower():
                return DownloadResult(True, self.name, "", "已投递 qBittorrent")
            if status == 415:
                return DownloadResult(False, self.name, "",
                                      "qB 认为磁力/种子无效（HTTP 415）")
            msg = " ".join(util.strip_html(text or "").split())[:120]
            return DownloadResult(False, self.name, "",
                                  "qB 拒绝（HTTP %s）%s" % (status, msg))
        return DownloadResult(False, self.name, "", self.last_error or "qB 会话反复失效")

    def probe(self) -> dict:
        info = {"backend": self.name, "url": self.url,
                "proxy": self.proxy or "直连（内网跳过代理）"}
        # 先不登录探一下：开了「对本地主机免除身份验证」时本来就不需要账号
        ver, why = self._version()
        if ver:
            self.version = ver
            self._logged_in = True
            info.update(ok=True, version=ver, auth="免密（qB 对来源地址免验证）")
            return info
        if why == "HTTP 401":
            info.update(ok=False, error="被 qB 的 Host header 校验 / CSRF 拦截（HTTP 401）",
                        hint=_HINT_HOSTCHECK)
            return info
        if why == "HTTP 404":
            info.update(ok=False, error="接口不存在（HTTP 404）：这个地址不是 qB WebUI",
                        hint="确认端口（默认 8080）与子路径前缀是否正确。")
            return info
        if why and not why.startswith("HTTP"):
            # 连接层面的错误，登录一定也失败，直接报，省掉一次可能触发封禁的尝试
            info.update(ok=False, error="连不上 %s：%s" % (self.url, why),
                        hint="检查 qB 是否运行、端口是否 8080、绑定的地址是否只允许本机。")
            return info

        if not self.login():
            info.update(ok=False, error=self.last_error or "登录失败",
                        hint=self.last_hint)
            return info
        info.update(ok=True, version=self.version or "?", auth="账号密码登录")
        return info
