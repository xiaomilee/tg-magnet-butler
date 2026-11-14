"""迅雷适配器。

接入方式
--------
只走飞牛网关 ``/cgi/ThirdParty/xunlei/index.cgi``，也就是飞牛应用中心里的官方「迅雷」App：
需要一个 ``pan_auth`` 令牌，外加一个 fnOS 登录 ``fnos-token``（浏览器打开面板时会自动带上）。

1.0.31 起删掉了「2345 面板」这条接法——那台机器上从来没有独立面板，``mode`` 默认却是
``panel``，于是谁都没选过却一直在打一个没人监听的端口（errno 111）。端口现在由启动时
的自动侦测决定（见 ``xunlei_detect``），``fnos_port`` 只是它的落脚点。

接口：
    GET  /drive/v1/tasks?type=user%23runner   -> 解析设备空间 target
    POST /drive/v1/resource/list              -> 解析磁力/种子里的文件树
    POST /drive/v1/task                       -> 创建下载任务
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse

from .. import torrentfile, util
from .base import BaseDownloader, DownloadResult

log = logging.getLogger("tmb.xunlei")

# 「问迅雷这条磁力里有哪些文件」的几套请求参数。各版本认的不一样，第一套是历史用法
# （字符串 urls），后面几套是照着迅雷自己面板的抓包写的（数组 urls / space / recursive）。
# 谁能认出多于一个文件就记住它，后面的投递直接用，不再一个个试。
FILE_LIST_BODIES = (
    ("urls", lambda m, space: {"urls": m, "page_size": 2000}),
    ("urls[]", lambda m, space: {"urls": [m], "page_size": 2000}),
    ("urls[]+space", lambda m, space: {"urls": [m], "space": space, "page_size": 2000}),
    ("url+space", lambda m, space: {"url": m, "space": space, "page_size": 2000}),
    ("urls[]+recursive", lambda m, space: {"urls": [m], "recursive": True,
                                           "page_size": 2000}),
)
_FILE_VARIANT = {"label": ""}          # 进程内记住哪套参数好使（下载器是每次新建的）
# 「再问一轮」前的等待：迅雷云端解析磁力有时晚一步。测试里会把它置 0。
_RETRY_PAUSE = 1.2

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".rmvb", ".rm", ".wmv", ".flv", ".mov", ".ts",
             ".m4v", ".webm", ".vob", ".mpg", ".mpeg", ".3gp", ".f4v", ".ogv", ".iso")
SUB_EXT = (".srt", ".ass", ".ssa", ".sub", ".idx", ".sup")

# 文件类别 → 扩展名。classify_file 按这个顺序取第一个命中的，所以别把宽泛的放前面。
CATEGORY_EXT = (
    ("video", VIDEO_EXT),
    ("subtitle", SUB_EXT),
    ("image", (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")),
    ("doc", (".nfo", ".txt", ".md", ".diz", ".sfv", ".url")),
    ("archive", (".zip", ".rar", ".7z", ".tar", ".gz", ".001")),
)
CATEGORY_LABEL = {"video": "视频", "subtitle": "字幕", "image": "图片",
                  "doc": "说明", "archive": "压缩包", "other": "其他"}
CATEGORY_ORDER = ("video", "subtitle", "image", "doc", "archive", "other")

# 筛选规则的默认值。config.py 里有同样一份（新装时写进配置文件），
# 这里这份是「老配置里没有 file_filter 时」的兜底，两边由测试比对防止跑偏。
DEFAULT_FILE_FILTER = {
    "video": True,
    "subtitle": True,
    "image": False,
    "doc": False,
    "archive": False,
    "other": False,
    "min_size_mb": 100,
    "max_size_mb": 0,
    "name_include": "",
    "name_exclude": "",
    "subtitle_exempt_size": True,
    "fallback_all": True,
}


def classify_file(name: str) -> str:
    """按扩展名归类：video / subtitle / image / doc / archive / other。"""
    low = (name or "").strip().lower()
    dot = low.rfind(".")
    if dot < 0:
        return "other"
    ext = low[dot:]
    for cat, exts in CATEGORY_EXT:
        if ext in exts:
            return cat
    return "other"


def file_size_of(node: dict) -> int:
    """文件字节数。迅雷这个字段有时是字符串，有时干脆没有——拿不到就返回 0。"""
    for key in ("file_size", "size", "length"):
        v = (node or {}).get(key)
        if v is None or v == "":
            continue
        try:
            return int(float(str(v)))
        except (TypeError, ValueError):
            continue
    return 0


def size_human(n: int) -> str:
    step = 1024.0
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if v < step or unit == "GB":
            if unit == "B":
                return "%d B" % int(v)
            return ("%.1f %s" % (v, unit)) if v < 100 else ("%d %s" % (round(v), unit))
        v /= step
    return "%d GB" % round(v)


def pick_note(info: dict) -> str:
    """把筛选结果说成一句话，贴进 Telegram 回执。"""
    info = info or {}
    total = int(info.get("total") or 0)
    if info.get("minimal"):
        return "本次改用了精简请求，文件筛选没生效（全下了 %d 个）" % total
    if info.get("fallback"):
        return "规则把 %d 个文件全筛掉了，已改回全选（宁可多下，也不建空任务）" % total
    if info.get("refused"):
        return "规则把 %d 个文件全筛掉了，按设置没有投递" % total
    kept = int(info.get("kept") or 0)
    if not total or kept >= total:
        return ""
    parts = []
    for cat in CATEGORY_ORDER:
        n = (info.get("skipped") or {}).get(cat)
        if n:
            parts.append("%s %d" % (CATEGORY_LABEL[cat], n))
    if info.get("by_name"):
        parts.append("名称不符 %d" % info["by_name"])
    if info.get("by_size"):
        parts.append("小于 %d MB %d" % (info.get("min_mb") or 0, info["by_size"])
                     if info.get("min_mb") else "过大 %d" % info["by_size"])
    tail = ("（跳过：%s）" % " · ".join(parts)) if parts else ""
    return "%d 个文件里挑了 %d 个%s" % (total, kept, tail)

# 鉴权失败的特征串。迅雷这类接口失败时常常不回 JSON，body 就是一行纯文本
# （最典型的就是 "invalid token"），所以只能按文本判。
_AUTH_ACCOUNT_HINTS = (
    "refresh token not found", "device_space_not_active", "space_not_active",
    "device space not active",
)
_AUTH_TOKEN_HINTS = (
    "invalid token", "token invalid", "token expired", "token is invalid",
    "invalid_token", "token_expired", "unauthorized", "not login",
    "not logged", "need login", "login required", "please login",
    "invalid session", "session expired", "auth fail", "authentication failed",
)
# 迅雷对「建任务」有自己的额度/频率限制：连续投递几次之后，它会回 HTTP 403 加一句
# ``task_create_count_limit``（error_code 11，「任务创建次数达到上限」）。
# 这既不是令牌过期也不是目录不对——更不该拿去重试：每试一次都在扣同一份额度。
# 归错类还会连带触发「重扫令牌」和「换目录/减字段重投」，白白多打三次请求。
_QUOTA_HINTS = (
    "task_create_count_limit", "task_create_limit", "create_count_limit",
    "task_create_too_fast", "create_task_too_fast", "任务创建次数达到上限",
    "创建任务次数达到上限", "创建任务太频繁",
    "too many requests", "rate limit", "ratelimit", "too frequent",
)
# 目录类错误：parent_folder_id / parent_folder_path 指向的位置不存在。
# 换设备、重装迅雷、目录被删、把路径当 id 填，都会回这一句。
_FOLDER_HINTS = (
    "parent folder not found", "parent_folder not found", "parent folder does not exist",
    "invalid parent folder", "parent_folder_id", "parent folder invalid",
    "folder not found", "folder_not_found", "folder does not exist",
    "folder not exist", "no such folder", "invalid folder", "invalid_folder",
    "target folder not found", "destination folder not found",
)


def classify_auth(text) -> str:
    """响应文本是不是鉴权问题。返回 '' / 'token'（令牌无效）/ 'account'（账号没登录）。"""
    low = (text or "").lower()
    for h in _AUTH_ACCOUNT_HINTS:
        if h in low:
            return "account"
    for h in _AUTH_TOKEN_HINTS:
        if h in low:
            return "token"
    return ""


def classify_quota(text) -> bool:
    """是不是迅雷的「建任务额度/频率」限制（HTTP 403 + task_create_count_limit）。

    必须和鉴权分开判：429/403 一律当令牌失效的话，用户会去重登迅雷、重抓 pan_auth、
    清 fnos-token，折腾一圈回来还是同一个 403——因为额度就是没了。
    """
    low = str(text or "").lower()
    return any(h.lower() in low for h in _QUOTA_HINTS)


def explain_quota(cfg=None) -> str:
    """限流时说清「这是迅雷那边的额度，不是你的配置坏了」，并给出能做的事。"""
    return ("迅雷不让再建任务了：任务创建次数达到上限（task_create_count_limit，"
            "HTTP 403 / error_code 11）。这是迅雷账号侧的额度限制——非会员本来就有每日任务数上限，"
            "短时间内连续投递也会撞上。不是令牌失效、也不是下载目录不对。"
            "等额度恢复再试（通常几十分钟，跨天重置）；急着下就在手机迅雷 App 里手动加这条磁力，"
            "或配 aria2 / qBittorrent 兜底。这次没有重试、也没去重扫令牌："
            "再试只会继续扣同一份额度")


def _age_text(seconds) -> str:
    """「3 天前」这种人话。没有时间戳返回空串。"""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    if seconds < 120:
        return "刚更新不到两分钟"
    if seconds < 3600:
        return "%d 分钟前" % int(seconds // 60)
    if seconds < 86400:
        return "%d 小时前" % int(seconds // 3600)
    return "%d 天前" % int(seconds // 86400)


def _span_text(seconds) -> str:
    """「2 小时前 / 3 天后」——用在过期时间上，正数表示还剩多久。"""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    future = seconds >= 0
    seconds = abs(seconds)
    if seconds < 120:
        body = "不到两分钟"
    elif seconds < 3600:
        body = "%d 分钟" % int(seconds // 60)
    elif seconds < 86400:
        body = "%d 小时" % int(seconds // 3600)
    else:
        body = "%d 天" % int(seconds // 86400)
    return body + ("后" if future else "前")


def pan_auth_life(token) -> dict:
    """pan_auth 是 JWT：payload 是明文 base64，不用密钥就能读签发/过期时间。

    迅雷那句 ``invalid token`` 十有八九就是它过期了。把这个时间直接摆出来，
    比让人去猜「到底是 fnos-token 还是 pan_auth」有用得多。
    """
    token = str(token or "").strip()
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    def _ts(key):
        val = payload.get(key)
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        # 迅雷有的版本用毫秒
        return num / 1000.0 if num > 1e11 else num

    iat, exp = _ts("iat"), _ts("exp")
    out = {"iat": iat, "exp": exp, "expired": None, "text": ""}
    if exp is None:
        return out
    left = exp - time.time()
    out["expired"] = left < 0
    out["left"] = left
    out["text"] = "已于 %s过期" % _span_text(left) if left < 0 else "还有 %s到期" % _span_text(left)
    return out


def _looks_like_pan_error(text) -> bool:
    """这句话是迅雷侧回的（认 pan_auth），还是飞牛网关侧回的（认 fnos-token）。

    迅雷云端的固定说法：``invalid token`` / ``token invalid`` / ``token expired``；
    飞牛网关拦人时是 401、HTML 登录页、``need login``、``未登录`` 这类。
    """
    low = (text or "").lower()
    if not low:
        return False
    if "<html" in low or "need login" in low or "未登录" in low or "请登录" in low:
        return False
    return ("invalid token" in low or "token invalid" in low or "token expired" in low
            or "token无效" in low or "令牌" in low)


def _pan_fix_text(cfg, head) -> str:
    """pan_auth 的修法，顺带把 JWT 里写的过期时间摆出来。"""
    life = pan_auth_life((cfg or {}).get("pan_auth"))
    msg = head
    if life.get("text"):
        msg += "：这串 pan_auth " + life["text"]
    msg += ("。修法：飞牛里点开迅雷套件，确认账号还在线（掉登录就重新扫码），"
            "再回本面板点「自动侦测并连接」重抓；也可以照 docs/迅雷接入.md 从迅雷面板的请求里"
            "复制新的 pan_auth 粘到设置里保存——飞牛不用重登")
    return msg


def explain_auth(kind, mode, cfg, culprit="", age=None, cloud_ok=False) -> str:
    """把鉴权失败翻成「是哪个令牌、该去哪儿换」。

    ``culprit`` 是实测出来的责任方（见 ``XunleiDownloader.auth_culprit``）：
    ``fnos_token`` / ``pan_auth`` / ``drop_token``。空表示还没测出来，
    此时别硬说是谁——网关模式两个令牌一起发，光看返回分不清。
    """
    cfg = cfg or {}
    mode = (mode or "panel").lower()
    if kind == "account":
        return ("迅雷账号没登录，或设备空间还没激活：打开迅雷面板扫码登录，"
                "等面板里能看到空间了再试")
    # 形状不对的时候，后面所有「令牌过期／账号掉了」的推断都是白费功夫：
    # 网关拿到一条地址只会回 "token contains an invalid number of segments"。
    # 只拦「明显是把地址粘进来了」这一种（含 pan_auth= 或 ://）；其它非 JWT
    # 的怪值交给下面按过期时间的老逻辑，别把所有提示都改写成形状问题。
    raw_pan = str(cfg.get("pan_auth") or "").strip()
    if (raw_pan and not util.pan_auth_looks_valid(raw_pan)
            and ("pan_auth=" in raw_pan or "://" in raw_pan)):
        return ("配置里的 pan_auth 不是 JWT（迅雷令牌是三段、用 . 分隔）。"
                "多半是复制时把整条 device/info/watch 地址一起粘进来了——"
                "只要其中 pan_auth= 后面那一段。保存时会自动掐出来，"
                "也可以点「自动侦测并连接」重抓一次")
    if mode == "fnos_gateway":
        if culprit == "token_ok":
            if cloud_ok:
                # 磁力能解析出来 = pan_auth 对云端有效（解析必须问云端），那就别再劝人重抓令牌
                return ("令牌是好的，磁力也解析出来了——说明 pan_auth 对迅雷云端有效，"
                        "被拒的是「建任务」这一步的参数：设备空间 id 过期、文件清单和资源对不上，"
                        "或某个可选字段这个版本不认。已自动用最简参数重试过一次；"
                        "仍失败就把日志里的「原始响应」发出来，那是一手证据")
            # 读得通、写不通：读列表在多版本网关里问的是本机任务库，建任务才真去问云端，
            # 所以别再把方向指成「资源/空间」那样笼统——先查 pan_auth，再排除资源本身。
            return ("令牌没问题：读任务列表能通，是同一次投递里的「建任务」被拒。"
                    "读列表多半问的是本机任务库，建任务才真去问迅雷云端——先点「自动侦测并"
                    "连接」重抓 pan_auth；再换个磁力试一次，排除这条资源本身被迅雷拒。"
                    "原始响应已写进日志，把日志级别改成 DEBUG 可看到完整报文")
        if culprit == "drop_token":
            return ("这个网关其实不校验 fnos-token：去掉它就能连通，带着反而被拒。"
                    "把设置里的 fnos-token 清空保存即可")
        if culprit == "pan_auth":
            return _pan_fix_text(cfg, "飞牛登录 Cookie 是好的，问题在迅雷自己的 pan_auth")
        life = pan_auth_life(cfg.get("pan_auth"))
        if life.get("expired"):
            # 连对比都没必要了：JWT 自己写着过期时间
            return _pan_fix_text(cfg, "先别管登录态——迅雷的 pan_auth 自己就写着过期了")
        if not (cfg.get("fnos_cookie") or "").strip() and not (cfg.get("fnos_token") or "").strip():
            return ("网关模式缺飞牛登录 Cookie（新版飞牛要的是 Cookie 里 "
                    "fnwg_token + ost 这一对，老的 fnos-token 已经不认了）："
                    "从飞牛桌面点开本面板，它会自动带上；或在浏览器 F12 → Network → "
                    "点一条 drive/v1 请求 → 复制 Cookie 整行，粘到设置里保存")
        when = _age_text(age)
        head = ("飞牛登录 Cookie 失效了（配置里这个是 %s 存的，网关没认它）" % when if when
                else "飞牛登录 Cookie 失效了（网关没认它）")
        # 刚换来的还被拒，让人去重登飞牛就是白折腾：换三条更可能的排查方向
        if age is not None and age < 600:
            tail = ("这个 Cookie 是刚抓的还被拒：先确认「飞牛网关端口」是飞牛 Web 端口（默认 5666），"
                    "再确认这台飞牛上装的确实是官方迅雷、且属于当前登录的账号")
        else:
            tail = ("重登飞牛不会自动更新这里存的值，必须重抓一次：浏览器 F12 → Network → "
                    "点一条 drive/v1 请求 → 复制 Cookie 整行，粘到设置的「飞牛登录 Cookie」保存。"
                    "粘整条就行，程序会自动剔掉让网关拒收的 fnos-token")
        return head + "：" + tail
    if not (cfg.get("pan_auth") or "").strip():
        return ("面板模式没填 pan_auth：浏览器登录迅雷面板后复制令牌，"
                "或点「自动侦测并连接」自动获取")
    return ("pan_auth 失效了（迅雷重新登录、升级或换设备后令牌会变）："
            "点「自动侦测并连接」重抓，或在迅雷面板重新复制一次")


def classify_task_error(data) -> str:
    """任务被拒的原因分类：''（看不出来）/ 'quota' / 'token' / 'account' / 'folder'。

    先判限流、再判鉴权、最后看目录：限流和鉴权都会回 403，认错了就会指错方向
    （让人去重登迅雷、换令牌，实际只是额度用完了）；而令牌问题必须比目录先报，
    否则用户会去删目录、白折腾。
    """
    text = _snippet(data, 4000) if isinstance(data, dict) else str(data or "")
    if classify_quota(text):
        return "quota"
    kind = classify_auth(text)
    if kind:
        return kind
    low = text.lower()
    for h in _FOLDER_HINTS:
        if h in low:
            return "folder"
    return ""


def explain_folder(cfg=None) -> str:
    """说清「目录 id 找不到」的来龙去脉与两种修法。"""
    return ("下载目录 id 在当前迅雷里对不上（换设备、重装迅雷、目录被删，"
            "或把路径当成 id 填进去都会这样）。两种改法：把「下载目录 id」留空"
            "＝投到迅雷默认目录；要指定目录，在迅雷面板文件列表里选中目录，"
            "从地址栏的 parent_folder_id= 后面复制那串 id")


def looks_like_path(value) -> bool:
    """填的是路径而不是 id —— 这种必然被迅雷拒，本地就能拦下来。"""
    s = str(value or "").strip()
    return bool(s) and ("/" in s or "\\" in s)


# 迅雷公开帮助页 / 社区里能查到的错误码（旧版远程下载沿用到 pan 引擎）。
# 4 位数的新码（比如 9126）官方没公开对照表，这里不猜，只如实说「它没说原因」。
_ERROR_CODE_HINTS = {
    "2": ("远程调用失败",
          "迅雷设备空间的反向代理超时，多半是迅雷刚启动或网络抖了一下，过一会儿重试"),
    "107": ("创建文件或写数据失败",
            "下载目录不存在或没有写权限：确认迅雷里能看到该目录，NAS 上跑迅雷的用户也能写"),
    "108": ("磁盘空间不足", "清理下载盘，或把下载目录换到别的盘"),
    "112": ("磁盘空间不足", "清理下载盘，或把下载目录换到别的盘"),
    "131": ("服务器连接异常", "迅雷服务端临时故障，等几分钟再试"),
    "202": ("任务已存在", "这条资源迅雷里已经有了，不用重复投递"),
}


def extract_code(data) -> str:
    """从响应里挖出数字错误码；纯数字响应体也算。"""
    if isinstance(data, dict):
        for key in ("error_code", "err_code", "errorCode", "code"):
            val = data.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) or (isinstance(val, str) and val.strip().isdigit()):
                code = str(val).strip()
                if code and code != "0":
                    return code
        text = _snippet(data, 200).strip()
    else:
        text = str(data or "").strip()
    return text if text.isdigit() and 1 <= len(text) <= 6 else ""


def explain_code(code) -> tuple:
    """错误码 → (短标题, 该怎么办)。查不到返回空元组，别编。"""
    return _ERROR_CODE_HINTS.get(str(code or "").strip(), ())



_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_text(text) -> str:
    """收掉控制字符与解码残留：这种东西发到 Telegram 就是一屏方块。"""
    t = str(text or "")
    if not t:
        return ""
    t = _CTRL_CHARS.sub(" ", t)
    # 连着的替换字符压成一个问号，别一行几十个 �
    t = re.sub(r"\ufffd{2,}", "?", t)
    return re.sub(r"[ ]{3,}", "  ", t).strip()


def _snippet(data, limit=200) -> str:
    """响应里最像人话的那一段，用来判断错误类型、也给用户看。"""
    if isinstance(data, dict):
        for key in ("_raw_text", "error_description", "error", "message"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return _clean_text(val)[:limit]
        # body 就是空的：别把 {"_raw_text": ""} 这种壳子当成「有话说」，
        # 鉴权对比要靠「空 vs 不空」来判断，这里得如实给空。
        if "_raw_text" in data:
            return ""
        try:
            return json.dumps(data, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return str(data)[:limit]
    return str(data or "")[:limit]


class XunleiDownloader(BaseDownloader):
    name = "xunlei"

    def __init__(self, cfg: dict, proxy: str = ""):
        self.cfg = cfg or {}
        self.proxy = proxy
        # 手里这串 pan_auth 可能被粘成了整条 watch 地址（见 util.clean_pan_auth）：
        # 请求前先掐出里面的 JWT。只是就地纠正这份引用，落盘由 Config 负责。
        raw_pan = self.cfg.get("pan_auth")
        if raw_pan:
            cleaned = util.clean_pan_auth(raw_pan)
            if cleaned != raw_pan:
                self.cfg["pan_auth"] = cleaned
        self._target = (self.cfg.get("target") or "").strip()
        # 上一次请求撞到的鉴权问题（'' / 'token' / 'account'），给上层做重试判断
        self._auth_failed = ""
        self._auth_text = ""
        # 最近一次鉴权失败的「责任方」实测结果：'' / fnos_token / pan_auth / drop_token
        self._auth_culprit = ""
        self._auth_evidence = {}
        # 最近一次 /drive/v1/tasks 的原始响应，用来捞「迅雷自己用过的下载目录」
        self._tasks_data = None
        # 磁力解析成功过 → pan_auth 对云端有效（解析必须问云端），报错时据此分叉
        self._cloud_ok = False
        # 撞上建任务额度/频率限制（task_create_count_limit）：这不是配置问题，
        # 上层据此跳过「换目录 / 减字段 / 重扫令牌」那些重试
        self._quota_hit = False
        # 配置那个地址连不上时自动换到的另一个（2345 ↔ 5666 网关），只在本进程内生效
        self._base_override = ""

    # ---------- 基础 ----------
    def api_base(self) -> str:
        """迅雷接口地址。现在只有一种接法：飞牛官方套件的网关。

        1.0.31 去掉了「2345 面板」——那台机器上根本没有独立面板，``mode`` 的默认值
        却一直是 ``panel``，于是谁都没选过、却一直在打一个没人监听的端口。
        手填 ``api_base`` 仍被尊重（留给 Docker 版）。
        """
        if self._base_override:
            return self._base_override
        if self.cfg.get("api_base"):
            return self.cfg["api_base"].rstrip("/")
        host = (self.cfg.get("host") or "127.0.0.1").strip()
        port = int(self.cfg.get("fnos_port") or 5666)
        return "http://%s:%d/cgi/ThirdParty/xunlei/index.cgi" % (host, port)

    @staticmethod
    def _local_web_ports():
        """本机正在监听的、像飞牛 Web 端口的那些（5600-5700）。

        飞牛允许改 Web 端口，改过之后 5666 就空了。读 ``/proc/net/tcp`` 只花几毫秒，
        比逐个端口盲试便宜；非 Linux / 读不到时返回空。
        """
        out = []
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 4 or parts[3] != "0A":
                    continue
                try:
                    port = int(parts[1].rsplit(":", 1)[1], 16)
                except (ValueError, IndexError):
                    continue
                if 5600 <= port <= 5700 and port not in out:
                    out.append(port)
        return out

    def _candidate_bases(self):
        """连不上时依次试的网关地址：配置的端口 → 5666 → 5667 → 本机在监听的 56xx。

        只用来兜住「端口被改过 / 配置里存着旧端口」，不猜别的机器。
        手填过 ``api_base`` 就完全尊重它，不换。
        """
        if self.cfg.get("api_base"):
            return []
        host = (self.cfg.get("host") or "127.0.0.1").strip()
        ports = []
        for p in [int(self.cfg.get("fnos_port") or 5666), 5666, 5667] + self._local_web_ports():
            if p not in ports:
                ports.append(p)
        cur = int(self.cfg.get("fnos_port") or 5666)
        return [("http://%s:%d/cgi/ThirdParty/xunlei/index.cgi" % (host, p),
                 "飞牛网关 %s:%d" % (host, p))
                for p in ports if p != cur]

    def _open(self, path, extra, method, body, with_token=True):
        """发一次请求；**连不上**时按候选端口逐个再试。

        只对「没有 HTTP 状态」的失败（连不上、超时）换端口：那是配置里的端口不对；
        HTTP 4xx/5xx 说明地址是对的，换了反而把真正的错误盖掉。
        """
        timeout = int(self.cfg.get("timeout") or 30)
        try:
            return util.http_request(
                self._url(path, extra), method=method,
                headers=self._headers(with_token=with_token), data=body,
                timeout=timeout, proxy=self.proxy, retries=1)
        except util.HttpError as e:
            if getattr(e, "status", None) or self._base_override:
                raise
            for base, label in self._candidate_bases():
                log.warning("迅雷地址 %s 连不上（%s）；自动改试%s", self.api_base(), e, label)
                self._base_override = base
                try:
                    return util.http_request(
                        self._url(path, extra), method=method,
                        headers=self._headers(with_token=with_token), data=body,
                        timeout=timeout, proxy=self.proxy, retries=1)
                except util.HttpError as e2:
                    if getattr(e2, "status", None):
                        raise
                    e = e2
            raise

    def _cookie(self) -> str:
        """发给飞牛网关的 Cookie 串。

        1.0.42：新版飞牛（迅雷套件 3.23.5 起）认 ``fnwg_token`` + ``ost``，
        老的 ``fnos-token`` 反而一出现就被整条拒（13 字节 ``invalid token``，
        连静态页都不给）。所以这里优先用 ``fnos_cookie``，而且绝不把
        ``fnos-token`` 拼进去——拼了就是亲手把自己锁在门外。
        """
        jar = util.clean_fnos_cookie(self.cfg.get("fnos_cookie"))
        if jar:
            return jar
        tok = self._token()
        if not tok:
            return ""
        # 老字段里也可能粘的是整条 Cookie（1.0.41 教过用户粘 URL，难免有人粘到这里）
        if ";" in tok or util.fnos_cookie_has_fnwg(tok):
            return util.clean_fnos_cookie(tok)
        return "fnos-token=" + tok

    def _headers(self, with_token=True):
        h = {"Content-Type": "application/json"}
        if not with_token:
            return h
        jar = self._cookie()
        if not jar:
            return h
        xla = str(self.cfg.get("xla_ci") or "").strip()
        # xla_ci 只该是纯值；1.0.41 的救急写法（往里塞整条 Cookie）已由 config
        # 迁移拆开，这里再兜一次，别把脏值当 XLA_CI 发出去。
        if xla and ";" not in xla and "=" not in xla and "xla_ci=" not in jar.lower():
            jar = jar + "; XLA_CI=" + xla
        h["Cookie"] = jar
        # 老版飞牛的反代有的只认自定义头；新版认 Cookie，多发的头里带着
        # fnos-token 反倒会被判无效，所以只在确实走老路时才补。
        if "fnos-token=" in jar:
            tok = self._token()
            if tok:
                h["X-Trim-Token"] = tok
                h["X-Fnos-Token"] = tok
        return h

    def _fetch_page(self) -> tuple:
        """GET 套件首页——只验登录 Cookie，压根不过问 pan_auth。

        返回 (HTML, 证据)。能拿到 HTML = Cookie 有效；同样回 ``invalid token``
        = Cookie 不行。这是分辨「谁是罪魁」唯一靠谱的办法（见 :meth:`auth_culprit`）。
        """
        url = self.api_base().rstrip("/") + "/"
        ev = {"url": url}
        try:
            status, text, _raw = util.http_request(
                url, headers=self._headers(), timeout=8,
                proxy=self.proxy, retries=0)
        except util.HttpError as e:
            status = getattr(e, "status", 0) or 0
            body = e.body
            text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        except Exception as e:                        # 网络/代理问题，别当成鉴权结论
            ev["error"] = str(e)
            return "", ev
        ev["status"] = status
        ev["body"] = (text or "").strip()[:120]
        if status == 200 and "<html" in (text or "").lower():
            return text, ev
        return "", ev

    def fetch_pan_auth(self) -> tuple:
        """从套件首页内联的 ``uiauth()`` 里读出当前 pan_auth，返回 (token, 说明)。

        新版套件不把 pan_auth 落盘，但首页 HTML 里就写着
        ``uiauth(value){ return "eyJ…" }``——只要登录 Cookie 是好的，就能自己换新。
        这是 1.0.42 的重点：令牌轮换不用再麻烦用户去 F12 里抄。
        """
        page, ev = self._fetch_page()
        if not page:
            return "", "套件首页打不开（%s）" % (ev.get("body") or ev.get("error")
                                              or ev.get("status") or "无响应")
        token = util.find_jwt_in_page(page)
        if not token:
            return "", "首页里没找到内联令牌"
        if not util.pan_auth_looks_valid(token):
            return "", "首页里的令牌形状不对"
        return token, "已从套件首页读到令牌"

    def _token(self) -> str:
        return str(self.cfg.get("fnos_token") or "").strip()

    def token_age(self):
        """配置里的 fnos-token 存了多久（秒）。没记录时间戳返回 None。"""
        try:
            ts = float(self.cfg.get("fnos_token_ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= 0:
            return None
        return max(0.0, time.time() - ts)

    def auth_culprit(self, with_text=""):
        """网关模式下判「这句 ``invalid token`` 是谁的锅」。

        1.0.41 之前拿同一个接口连问两次（带 / 不带 fnos-token）比对回答。这个
        办法在新版飞牛上必然判反：网关只要看到 ``fnos-token`` 就整条拒，带着和
        不带当然一模一样，于是被读成「网关不看 Cookie → 那就是 pan_auth 过期」，
        把人引去重登迅雷、重抓令牌，折腾一圈还是原样（2026-11-14 就绕过这么一次）。

        1.0.42 改成问一个**不需要 pan_auth** 的地址：``<api_base>/`` 是套件首页，
        登录 Cookie 有效才吐 HTML。
          * 拿到 HTML            → Cookie 是好的 → 问题在 pan_auth
          * 同样回 invalid token → Cookie 不行   → 问题在飞牛登录 Cookie
        """
        mode = (self.cfg.get("mode") or "panel").lower()
        if mode != "fnos_gateway":
            return "", {}
        page, ev = self._fetch_page()
        ev["probe"] = "GET %s/" % self.api_base()
        if page:
            ev["reason"] = ("套件首页能打开（%d 字节），登录 Cookie 有效 → 剩下只可能是 pan_auth"
                            % len(page))
            return "pan_auth", ev
        body = (ev.get("body") or "").strip()
        if ev.get("error"):
            ev["reason"] = "探针请求本身没发出去（%s），先别怪令牌" % ev["error"]
            return "", ev
        if _looks_like_pan_error(body):
            if util.fnos_cookie_has_fnwg(self._cookie()):
                ev["reason"] = ("带着 fnwg_token 仍被整条拒 → 这串飞牛登录 Cookie 已经失效了")
            elif not self._cookie():
                ev["reason"] = "一个飞牛登录 Cookie 都没配 → 网关一律拒，跟迅雷没关系"
            else:
                ev["reason"] = ("Cookie 里只有老的 fnos-token；新版飞牛只认 "
                                "fnwg_token + ost → 网关一律拒")
            return "fnos_token", ev
        ev["reason"] = "探针回了别的东西（%s），先查网络/端口" % (body[:60] or "空响应")
        return "", ev

    def _url(self, path: str, extra: dict = None):
        params = {"pan_auth": self.cfg.get("pan_auth") or "", "device_space": ""}
        if extra:
            params.update(extra)
        sep = "&" if "?" in path else "?"
        return "%s%s%s%s" % (self.api_base(), path, sep,
                             urllib.parse.urlencode(params, doseq=True))

    def _http_fail_message(self, e, body, minimal=False) -> str:
        """HTTP 层就失败时说清是「请求体不被接受」还是别的，别一句「接口请求失败」了事。"""
        keys = ",".join(sorted(body.keys())) if isinstance(body, dict) else "-"
        params = ""
        if isinstance(body, dict) and isinstance(body.get("params"), dict):
            params = ",".join(sorted(body["params"].keys()))
        if e.status == 400:
            return ("网关不接受这个请求体（HTTP 400）：多半是缺必填字段或字段类型不对，"
                    "换令牌、换目录都没用。%s发了 %s；params 里有 %s。"
                    "把这条原始响应发出来，程序按真实字段要求适配"
                    % ("精简后" if minimal else "本次", keys, params or "（空）"))
        if e.status in (413, 415, 422):
            return ("网关拒绝了这个请求（HTTP %s）：请求体太大或格式不被接受，"
                    "把这条原始响应发出来" % e.status)
        return "接口请求失败: %s" % e

    def _route_note(self, info) -> str:
        """失败时补一句「这条请求走了代理」——WireGuard/Clash 环境下最容易怀疑错对象。"""
        r = info.get("route") or {}
        if not r or r.get("local") or not r.get("proxy"):
            return ""
        return ("另外：这个地址没被当作内网，请求是经代理 %s 发的。如果它其实是家里的 NAS"
                "（WireGuard / 域名访问），先确认代理这边放行且能通" % r["proxy"])

    def _redact(self, payload, limit=1200) -> str:
        """给日志用的请求/响应文本：令牌打码、磁力哈希打掉中段、过长截断。"""
        if payload is None or payload == "":
            return ""
        if isinstance(payload, (dict, list)):
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(payload)
        else:
            text = str(payload)
        for secret in (self._token(), str(self.cfg.get("pan_auth") or "").strip()):
            if secret and len(secret) >= 8:
                text = text.replace(secret, secret[:4] + "***" + secret[-2:])
        text = re.sub(r"(btih:)([0-9a-zA-Z]{6})[0-9a-zA-Z]+([0-9a-zA-Z]{4})",
                      r"\1\2***\3", text)
        return text[:limit]

    def _request(self, path, body=None, method="POST", extra=None, with_token=True):
        url = self._url(path, extra)
        timeout = int(self.cfg.get("timeout") or 30)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("迅雷请求 → %s %s 令牌:%s%s", method, self._redact(url, 400),
                      "带" if with_token else "不带",
                      (" 体:" + self._redact(body if body is not None else extra))
                      if (body is not None or extra) else "")
        payload = body if body is not None else (b"" if method == "POST" else None)
        try:
            status, text, raw = self._open(path, extra, method, payload,
                                           with_token=with_token)
        except util.HttpError as e:
            # 401/403 走异常分支，同样归到「令牌问题」，别让它变成一句「接口请求失败」。
            # 但 403 里混着「建任务次数上限」这种限流：那不是令牌的错，先把它挑出来，
            # 否则上层会去重扫令牌、换目录、减字段各试一遍，每试一次都在扣额度。
            if e.status in (401, 403):
                text = ""
                try:
                    text = (e.body or b"").decode("utf-8", "replace")
                except Exception:
                    pass
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("迅雷响应 ← %s %s HTTP %s 体:%s", method,
                              self._redact(url, 400), e.status,
                              self._redact(text, 1500))
                if classify_quota(text):
                    data = {"_raw_text": text or ("HTTP %s" % e.status),
                            "_quota": True, "_http_status": e.status}
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        parsed.update({"_quota": True, "_http_status": e.status})
                        data = parsed
                    return data
                return {"_raw_text": text or ("HTTP %s" % e.status),
                        "_auth_failed": classify_auth(text) or "token",
                        "_http_status": e.status}
            raise
        if log.isEnabledFor(logging.DEBUG):
            log.debug("迅雷响应 ← %s %s HTTP %s 体:%s", method,
                      self._redact(url, 400), status, self._redact(text, 1500))
        if util.looks_garbled(text):
            # 可能是编码猜错了（迅雷有的版本回 GBK 中文，按 utf-8 解就是一堆 �）
            try:
                alt = (raw or b"").decode("gb18030")
            except (UnicodeDecodeError, AttributeError):
                alt = ""
            if alt and not util.looks_garbled(alt):
                text = alt
        try:
            data = json.loads(text)
        except ValueError:
            data = {"_raw_text": text[:2000]}
        if not isinstance(data, dict):
            data = {"_raw_text": text[:2000]}
        if "_raw_text" in data and util.looks_garbled(data["_raw_text"]):
            # 解压兜底也没救回来：说明它既不是文本也不是 gzip/zlib。
            # 留着原始字节，别把满屏乱码当「服务端原话」发给用户。
            data["_binary"] = True
            try:
                data["_raw_hex"] = (raw or b"")[:120].hex()
            except (TypeError, AttributeError):
                pass
            log.warning("迅雷回了一段非文本响应（%d 字节，开头 hex=%s），已存进 raw 供排查",
                        len(raw or b""), (raw or b"")[:48].hex())
        kind = "" if data.get("_binary") else classify_auth(_snippet(data, 2000))
        if not kind and "_raw_text" in data and not data.get("_binary"):
            # 回的是网页而不是接口：网关/面板没认登录态时就是这么答的
            # （直接跳登录页、或把面板首页丢回来），别当成「连通了」。
            head = str(data.get("_raw_text") or "")[:600].lower()
            if "<html" in head or "<!doctype" in head:
                kind = "token"
        if kind:
            data["_auth_failed"] = kind
        return data

    # ---------- 设备空间 ----------
    def resolve_target(self, force=False) -> str:
        if self._target and not force:
            return self._target
        try:
            data = self._request("/drive/v1/tasks", method="GET",
                                 extra={"type": "user#runner", "limit": "20"})
        except util.HttpError as e:
            log.warning("解析迅雷设备空间失败: %s", e)
            return ""
        if data.get("_auth_failed"):
            # 记下来：add() 拿到空 target 时要能说清是「令牌坏了」而不是「没解析到」
            self._auth_failed = data["_auth_failed"]
            self._auth_text = _snippet(data, 60)
            log.warning("迅雷鉴权失败(%s)：%s", self._auth_failed, _snippet(data))
            return ""
        if data.get("_quota"):
            # 限流也会挡在取设备空间这一步（同一个账号的额度）：别让 add() 报成
            # 「没取到设备空间，检查 pan_auth」——那会把人往令牌上带
            self._quota_hit = True
            log.warning("取设备空间被限流：%s", _snippet(data, 120))
            return ""
        target = _find_target(data)
        if target:
            self._target = target
            log.info("迅雷设备空间 target=%s", target)
        else:
            log.warning("未能从响应中解析出 target: %s",
                        json.dumps(data, ensure_ascii=False)[:400])
        self._tasks_data = data
        return self._target

    def folder_candidates(self, refresh=False) -> list:
        """迅雷自己用过的下载目录，按出现次数排序。

        先看设备空间那次请求的响应，里面常带着任务列表；没有就单独再查一次任务列表。
        查不到不报错——不是每条链路都必须有目录。
        """
        if refresh or self._tasks_data is None:
            try:
                self._tasks_data = self._request(
                    "/drive/v1/tasks", method="GET",
                    extra={"type": "user#runner", "limit": "50"})
            except util.HttpError as e:
                log.warning("捞取迅雷下载目录失败: %s", e)
                return []
        out = harvest_folders(self._tasks_data)
        if out:
            return out
        try:
            data = self._request("/drive/v1/tasks", method="GET", extra={"limit": "50"})
        except util.HttpError as e:
            log.warning("再查一次任务列表失败: %s", e)
            return []
        return harvest_folders(data)

    def _auth_message(self, kind, data=None):
        """一句能照做的鉴权失败说明。

        网关模式先做一次对比（见 ``auth_culprit``），别再把可能的 pan_auth 问题
        说成 fnos-token 问题——用户会一直去重登飞牛，越试越困惑。
        """
        mode = (self.cfg.get("mode") or "panel").lower()
        culprit = ""
        if kind == "token" and mode == "fnos_gateway" and self._token():
            culprit, evidence = self.auth_culprit(_snippet(data, 200) if data else "")
            self._auth_culprit = culprit
            self._auth_evidence = evidence or {}
            if evidence.get("reason"):
                log.info("鉴权对比：%s（带 token=%r / 不带=%r）", evidence["reason"],
                         evidence.get("with_token", ""), evidence.get("without_token", ""))
        msg = "迅雷鉴权失败：%s" % explain_auth(kind, mode, self.cfg, culprit,
                                                self.token_age(),
                                                cloud_ok=self._cloud_ok)
        if isinstance(data, dict) and data.get("_binary"):
            msg += "（迅雷这次回的是二进制内容、不是文字，原始字节已记进日志）"
            return msg
        orig = _snippet(data, 60) if data else ""
        if orig and len(orig) <= 60:
            msg += "（服务端原话：%s）" % orig
        return msg

    # ---------- 解析磁力 ----------
    def _parse_body(self, body):
        """打一次 ``resource/list``，返回 ``{"raw": …, "resources": […]}``。"""
        data = self._request("/drive/v1/resource/list", body)
        resources = _resources_of(data)
        if resources:
            self._cloud_ok = True
        return {"raw": data, "resources": resources}

    def _list_at(self, path, body, extra=None):
        """打一次「列目录」接口（路径/查询参数都不一定，见 ``_child_bodies``），
        返回同 ``_parse_body`` 的结构。"""
        data = self._request(path, body, extra=extra)
        resources = _resources_of(data)
        if resources:
            self._cloud_ok = True
        return {"raw": data, "resources": resources}

    def parse_url(self, magnet: str):
        """老接口：用默认那套参数问一次迅雷（外部只用 add / list_files）。"""
        try:
            return self._parse_body({"urls": magnet, "page_size": 2000})
        except util.HttpError as e:
            log.warning("迅雷解析链接失败: %s", e)
            return None

    # ---------- 往目录节点里钻 ----------
    def _list_id_of(self, parsed) -> str:
        raw = parsed.get("raw") if isinstance(parsed, dict) else None
        if isinstance(raw, dict):
            for k in ("list_id", "listId", "gcid"):
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    def _deep_files(self, parsed, magnet, space, meta, tag, depth=0):
        """把一次 ``resource/list`` 的响应变成真正的文件列表。

        关键在**目录节点**：迅雷对裸磁力常常只回一个 ``is_dir`` 的根目录（带 ``file_count``
        但没有子节点），它不是「一个文件」也不是「没有文件」，而是「你要的清单在下一层」。
        直接拍平会得到 0 个（于是去外网下种子，代理一抖就全盘皆输），
        当成 1 个文件更糟（于是不发 ``sub_file_index``，整个种子全下）——两种都踩过。
        """
        res = _resources_of(parsed)
        leaves = _flatten_files(res)
        dirs = [n for n in res if _is_dir_node(n) and not _children_of(n)]
        if dirs:
            leaves = leaves + self._drill_many(dirs, parsed, magnet, space, meta,
                                               tag, depth)
        return leaves

    def _drill_many(self, nodes, parsed, magnet, space, meta, tag, depth):
        out = []
        for node in nodes:
            out.extend(self._drill(node, parsed, magnet, space, meta, tag, depth))
        return out

    def _drill(self, node, parsed, magnet, space, meta, tag, depth):
        """展开一个目录节点：试几套请求参数，哪套给出子节点就用哪套。"""
        if depth >= _MAX_DIR_DEPTH:
            return []
        want = node.get("file_count")
        key = _node_id(node) or str(node.get("name") or "")
        cache = self.__dict__.setdefault("_drill_cache", {})
        if key and key in cache:
            return cache[key]

        list_id = self._list_id_of(parsed)
        cands = _child_bodies(node, magnet, list_id, space)
        known = _CHILD_VARIANT.get("label")
        if known:
            cands.sort(key=lambda v: 0 if v[0] == known else 1)

        for label, path, body, extra in cands:
            try:
                child = self._list_at(path, body, extra)
            except util.HttpError as e:
                meta["attempts"].append({"variant": "%s/钻:%s" % (tag, label),
                                         "count": -1, "error": str(e)})
                if not getattr(e, "status", None):
                    # 端口没开/超时：换参数是打同一扇门，别在这里耗掉几十秒
                    break
                continue
            cres = _resources_of(child)
            if not cres:
                meta["attempts"].append({"variant": "%s/钻:%s" % (tag, label),
                                         "count": 0})
                continue
            if len(cres) == 1 and _same_node(cres[0], node):
                # 参数被无视了，回来还是这个目录本身——换下一套，别在这儿递归下去
                meta["attempts"].append({"variant": "%s/钻:%s" % (tag, label),
                                         "count": 0, "note": "原样返回目录本身"})
                continue
            sub = _flatten_files(cres)
            if not sub:
                sub = self._drill_many(cres, child, magnet, space, meta, tag,
                                       depth + 1)
            meta["attempts"].append({"variant": "%s/钻:%s" % (tag, label),
                                     "count": len(sub), "shape": _shape(cres)})
            if not sub:
                continue
            _CHILD_VARIANT["label"] = label
            meta["expanded"] = True      # 清单是从本地面板实打实展开来的，不用再出网
            if not meta.get("expand_sample"):
                meta["expand_sample"] = _brief(sub[0])
            if want and str(want).isdigit() and int(want) != len(sub):
                log.info("目录 %s 说有 %s 个文件，展开拿到 %d 个（可能还有分页）",
                         node.get("name") or key, want, len(sub))
            if key:
                cache[key] = sub
            return sub

        meta["attempts"].append({"variant": "%s/钻:%s" % (tag, "全部"), "count": 0,
                                 "note": "没能钻进目录 %s（%s 个文件）"
                                         % (node.get("name") or key, want or "?")})
        # 钻不动时把目录节点本身留下来：它的 id / dir 就是下次现场试参数要用的东西
        meta.setdefault("dir_nodes", []).append(_brief(node))
        if key:
            cache[key] = []
        return []

    def list_files(self, magnet, torrent_url="", space="", force_all=False,
                   resolver=None):
        """认出这条磁力到底有哪些文件，返回 ``(files, meta)``。

        为什么不是一次请求就完事：迅雷有的版本对**磁力**只回一个「根目录」节点，子文件
        拿不到。程序只看到 1 个文件就发不出 ``sub_file_index``，迅雷于是把整个种子全下——
        现象就是「文件筛选完全没生效」。所以分两条路：

        1. **先问迅雷**（本地、几十毫秒）：逐套试 ``resource/list`` 的请求参数（上次好用
           那套排最前）。它认出多于一个文件就完事——那些节点的 ``file_index`` 正是
           ``sub_file_index`` 要的序号，比任何二手清单都准；
        2. **它认不出来时**才去取 ``.torrent``（``torrent_url``，或按 infohash 现找）：
           ``info.files`` 是权威清单，顺序就是 BT 的文件顺序。

        顺序不能反：走代理下种子可能十几秒甚至超时，本地接口却没这个毛病；而只要迅雷
        给了完整树，就没有理由再跑一趟外网。

        ``meta`` 里有 ``source`` / ``attempts`` / ``raw``，面板的「解析测试」直接展示。
        """
        meta = {"source": "", "attempts": [], "raw": None, "magnet": magnet[:120]}
        files = []
        order = list(FILE_LIST_BODIES)
        known = _FILE_VARIANT.get("label")
        if known:
            order.sort(key=lambda v: 0 if v[0] == known else 1)

        best = None
        best_label = ""
        # 两轮：迅雷云端解析有时晚一步，第一轮一个都没认出来（或只给个空目录节点），
        # 隔一秒再问通常就有了。本地请求几十毫秒，比跑去外网下种子便宜得多。
        for round_no in range(2):
            for label, build in order:
                tag = label if round_no == 0 else label + "+再问"
                try:
                    parsed = self._parse_body(build(magnet, space))
                except util.HttpError as e:
                    meta["attempts"].append({"variant": tag, "count": -1,
                                             "error": str(e)})
                    if not getattr(e, "status", None):
                        # 连不上（端口没开、超时）：换请求参数也是打同一个门，别白等四五轮。
                        # 这种时候投递本身也马上会失败，更没必要为了一份清单再跑去外网下种子
                        # ——那会让用户干等一分钟，最后拿到的还是「投递失败」。
                        return files, meta
                    continue
                got = self._deep_files(parsed, magnet, space, meta, tag)
                meta["attempts"].append({"variant": tag, "count": len(got),
                                         "shape": _shape(parsed.get("resources"))})
                if meta["raw"] is None:
                    meta["raw"] = parsed.get("raw")
                if best is None or len(got) > len(best):
                    best, best_label = got, tag
                if len(got) > 1:
                    if not _FILE_VARIANT.get("label"):
                        _FILE_VARIANT["label"] = label
                    if not force_all:
                        meta["source"] = "api:%s" % tag
                        return got, meta

            if force_all or len(best or []) > 1 or round_no:
                break
            time.sleep(_RETRY_PAUSE)

        return self._finish(files, best, best_label, meta, magnet,
                            torrent_url, resolver, space)

    def _finish(self, files, best, best_label, meta, magnet, torrent_url,
                resolver, space):
        """迅雷这边认不全时，去拿 ``.torrent`` 里的权威清单（拿不到就用手上最好的）。"""
        # 迅雷这边最多只认出 1 个（往往就是个没展开的目录节点）：去拿种子文件里的权威清单。
        # 裸磁力没有地址时，才按 infohash 现找一次（这一步要出网，所以放在最后）。
        url = torrent_url
        if best and len(best) > 1:
            # 本地已经认出多于一个文件：按上面写的规矩「认出多于一个就完事」。
            # 以前只认 expanded（钻目录）这一种，于是面板预览（force_all）明明已经拿到
            # 6 个文件，还要再跑一趟外网下种子——代理不通时就是干等十几秒甚至超时，
            # 预览结果却一个字都不会变。
            url = ""
        if not url and resolver and len(best or []) <= 1 and not meta.get("expanded"):
            try:
                url = resolver() or ""
            except Exception as e:  # noqa: BLE001 - 回调失败也要能投递
                log.info("找种子文件的回调失败：%s", e)
                url = ""
            # 记一笔：面板上「找不到种子地址」和「地址有了但下不下来」是两种毛病，
            # 不写清就只能靠猜（前者站点里没有这条 infohash，后者是代理/网络的事）
            meta["attempts"].append({
                "variant": "找种子地址", "count": 1 if url else 0,
                "note": "找到了" if url else "没找到（站点里没有这条 infohash）"})
        if url:
            # 时限交给 torrentfile 的默认值（15 秒 + 断点续传）：走代理连 nyaa 建连
            # 就要 10 秒上下，收紧超时只会让这条路必然失败（实测过 4 秒全是超时）。
            tf = torrentfile.files_from_url(url, proxy=self.proxy)
            meta["attempts"].append({"variant": "torrent", "count": len(tf),
                                     "note": "种子文件（权威清单）" if tf
                                     else "种子文件读不出来"})
            if tf and len(tf) >= len(best or []):
                meta["source"] = "torrent"
                meta["torrent_url"] = url
                return tf, meta
            files = tf

        if best:
            meta["source"] = meta["source"] or ("api:%s" % best_label)
            return best, meta
        return files, meta

    def inspect(self, magnet, torrent_url=""):
        """面板「解析测试」：这条磁力认出了什么、按当前规则会挑哪几个。"""
        space = ""
        try:
            space = self.resolve_target() or ""
        except Exception as e:  # noqa: BLE001 - 只是为了让诊断多一份信息
            log.info("解析测试时取设备空间失败：%s", e)
        files, meta = self.list_files(magnet, torrent_url=torrent_url, space=space,
                                      force_all=True)
        rows = []
        for i, node in enumerate(files):
            name = node.get("name") or node.get("file_name") or ""
            rows.append({"index": node.get("file_index", i), "pos": i, "name": name,
                         "size": file_size_of(node), "cat": classify_file(name)})
        indices, info = self.select_files(files)
        kept = set(indices or [])
        full = indices is None
        for r in rows:
            r["keep"] = full or r["index"] in kept
        # 认不出清单时，「迅雷到底回了什么」是全项目最贵的信息：把顶层键一起带上，
        # 下次再遇到「0 个文件」，一眼就能看出是 resources 为空、还是换了别的字段名。
        raw = meta.get("raw") or {}
        resources = raw.get("resources") if isinstance(raw, dict) else None
        return {"files": rows, "total": len(files), "pick": info,
                "pick_note": pick_note(info),
                "sub_file_index": ("（不传，单文件或没认出来）" if not rows else
                                   "-1" if full else
                                   ",".join(str(i) for i in (indices or []))),
                "source": meta.get("source") or "",
                "expanded": bool(meta.get("expanded")),
                "expand_sample": meta.get("expand_sample") or {},
                "dir_nodes": meta.get("dir_nodes") or [],
                "attempts": meta.get("attempts") or [],
                "raw_keys": sorted(raw.keys()) if isinstance(raw, dict) else [],
                "raw_shape": _shape(resources if resources else raw)}

    def filter_cfg(self) -> dict:
        """当前生效的文件筛选规则（老配置缺字段时按默认值补齐）。"""
        f = dict(DEFAULT_FILE_FILTER)
        got = self.cfg.get("file_filter")
        if isinstance(got, dict):
            f.update({k: v for k, v in got.items() if v is not None})
        return f

    def select_files(self, files, select_all=None):
        """决定这次下哪几个文件。

        返回 ``(indices, info)``：

        * ``indices is None`` → 全选（调用方发 ``sub_file_index = "-1"``）
        * ``indices == []``   → 一个都没留下且不允许兜底，调用方应拒绝投递
        * 其余 → 要下的序号列表

        ``info`` 里带 total / kept / skipped / by_size / by_name / fallback，
        用来拼回执，让人知道「筛选到底生效了没有」。
        """
        total = len(files)
        info = {"total": total, "kept": total, "skipped": {}, "by_size": 0,
                "by_name": 0, "fallback": False, "refused": False, "min_mb": 0}
        if not total:
            return None, info

        # 「全选」= 关掉筛选。缺这个键时按「要筛」处理：筛选是全局规则，
        # 不该因为某个老配置里没写这个字段就悄悄失效。
        if select_all is None:
            select_all = bool(self.cfg.get("select_all_files", False))
        if select_all:
            return None, info

        f = self.filter_cfg()
        try:
            min_mb = max(0, int(f.get("min_size_mb") or 0))
        except (TypeError, ValueError):
            min_mb = 0
        try:
            max_mb = max(0, int(f.get("max_size_mb") or 0))
        except (TypeError, ValueError):
            max_mb = 0
        inc = [x.strip().lower() for x in str(f.get("name_include") or "").split(",")
               if x.strip()]
        exc = [x.strip().lower() for x in str(f.get("name_exclude") or "").split(",")
               if x.strip()]
        # 字幕天生几十 KB，拿 100 MB 去筛等于把字幕全删了，所以默认豁免
        exempt = f.get("subtitle_exempt_size") is not False
        info["min_mb"] = min_mb

        kept = []
        for i, node in enumerate(files):
            name = node.get("name") or node.get("file_name") or ""
            low = name.lower()
            cat = classify_file(name)
            # 缺这个键时按「视频 + 字幕」处理，和默认值一致
            if not f.get(cat, cat in ("video", "subtitle")):
                info["skipped"][cat] = info["skipped"].get(cat, 0) + 1
                continue
            if exc and any(k in low for k in exc):
                info["by_name"] += 1
                continue
            if inc and not all(k in low for k in inc):
                info["by_name"] += 1
                continue
            if min_mb or max_mb:
                if not (exempt and cat in ("subtitle", "doc")):
                    size = file_size_of(node)
                    # size 为 0 表示拿不到大小，宁可留着也不误删
                    if min_mb and size and size < min_mb * 1024 * 1024:
                        info["by_size"] += 1
                        continue
                    if max_mb and size > max_mb * 1024 * 1024:
                        info["by_size"] += 1
                        continue
            kept.append(node.get("file_index", i))

        info["kept"] = len(kept)
        if not kept:
            # 绝不发空选择：那会建出一个没有文件的空壳任务，还得人去删
            if f.get("fallback_all") is not False:
                info["fallback"] = True
                info["kept"] = total
                return None, info
            info["refused"] = True
            return [], info
        # 只有一个文件时用不着给迅雷指序号（指了反而可能被当成「第 N 个以外的都不要」），
        # 但规则已经过完了：这条磁力若被筛掉，上面就 fallback / refused 了
        if total <= 1:
            return None, info
        return kept, info

    def _folder_params(self):
        """配置里填的目录参数，返回 (params, 提示)。

        填成路径（带 /）的必然是错的——迅雷要的是 folder id，不是路径。
        这种情况本地就拦下来，省一次注定失败的网络请求。
        """
        fid = str(self.cfg.get("parent_folder_id") or "").strip()
        if not fid:
            return {}, ""
        if looks_like_path(fid):
            return {}, "「下载目录 id」填的是路径而不是 id"
        out = {"parent_folder_id": fid}
        path = str(self.cfg.get("parent_folder_path") or "").strip()
        if path:
            out["parent_folder_path"] = path
        return out, ""

    def _folder_message(self, data=None, folder=None):
        """目录无效的说明：为什么、怎么改。

        folder 为空说明这次压根没带目录参数（落默认目录）还被拒——那就不是
        「id 填错了」，而是迅雷里找不到任何可写目录，说法要换一种，别误导用户去改 id。
        """
        if folder:
            msg = "迅雷下载目录无效：%s" % explain_folder(self.cfg)
        else:
            msg = ("迅雷没找到可用的下载目录：本次用的是默认目录（没指定 id）也被拒，"
                   "通常是迅雷里没有任何可写目录，或换过设备空间还没准备好。"
                   "打开迅雷面板确认能看到文件列表；也可以在面板填一个真实存在的目录 id")
        orig = _snippet(data, 60) if data else ""
        if orig and len(orig) <= 60:
            msg += "（服务端原话：%s）" % orig
        return msg

    def _code_message(self, data, code=""):
        """只给了数字错误码时，尽量说人话；查不到就如实说「它没给原因」。"""
        code = code or extract_code(data)
        hint = explain_code(code) if code else ()
        head, advice = hint if len(hint) == 2 else ("", "")
        if head:
            msg = "迅雷拒绝了任务（错误码 %s：%s）—— %s" % (code, head, advice)
        else:
            msg = ("迅雷拒绝了任务（错误码 %s），它没给文字说明，只回了码。"
                   "常见三类原因：① 下载目录不存在或不可写；② 设备空间刚换过、还没准备好；"
                   "③ 这条资源被迅雷侧策略拒绝。先打开迅雷面板手动新建一条任务试试，"
                   "能成就是本程序参数的问题，不能成是迅雷那边的问题" % (code or "未知"))
        raw = _snippet(data, 160).strip()
        # 响应体里只有那个码的时候不必重复一遍
        if raw and raw != code and len(raw) > len(code) + 6:
            msg += "（服务端原话：%s）" % raw
        return msg

    def add(self, magnet: str, title: str = "", options=None) -> DownloadResult:
        options = options or {}
        self._auth_failed = ""
        self._auth_text = ""
        self._quota_hit = False
        target = options.get("target") or self.resolve_target()
        if not target:
            if self._quota_hit:
                return DownloadResult(False, self.name, "", explain_quota(self.cfg),
                                      {"_stage": "tasks", "_quota": True})
            if self._auth_failed:
                return DownloadResult(False, self.name, "",
                                      self._auth_message(self._auth_failed, self._auth_text),
                                      {"_auth_failed": self._auth_failed,
                                       "_stage": "tasks"})
            return DownloadResult(False, self.name, "",
                                  "未取到迅雷设备空间(target)。请在设置里检查 pan_auth / 登录状态",
                                  {"_stage": "tasks"})

        parsed_files, enum = self.list_files(
            magnet, torrent_url=options.get("torrent_url") or "", space=target,
            resolver=options.get("torrent_resolver"))
        files = parsed_files or []
        if files:
            log.info("文件清单：认出 %d 个文件（来源 %s）", len(files),
                     enum.get("source") or "未知")
        else:
            log.warning("文件清单：这条磁力一个文件都没认出来（试过 %s），"
                        "只能按全选投递——文件筛选这次不起作用",
                        ", ".join("%s=%s" % (a.get("variant"), a.get("count"))
                                  for a in (enum.get("attempts") or [])) or "-")
        folder, note = self._folder_params()
        if options.get("_no_folder"):
            folder, note = {}, ""

        res = self._post_task(magnet, title, target, files, folder)
        if res.ok:
            if note:
                res.message = _join_note(res.message, note + "，本次已投递到迅雷默认目录")
                res.raw = _mark_folder_fallback(res.raw, "", note)
            return res

        tried = [("初投", res)]
        if (res.raw or {}).get("_quota"):
            # 迅雷在限流：换目录、减字段、重扫令牌都不会让它松口，只会再扣一次额度
            log.warning("迅雷建任务被限流（task_create_count_limit），本次不再重试")
            return res
        kind = (res.raw or {}).get("_error_kind") or ""
        code = (res.raw or {}).get("_error_code") or ""
        # 查不到含义的码（比如 9126）也按「可能是目录问题」处理：既然不知道它嫌什么，
        # 那就把三种投法都试一遍；已知与目录无关的码（空间不足、任务已存在）不折腾。
        unknown_code = bool(code) and not explain_code(code)
        folder_ish = kind == "folder" or unknown_code

        # 1) 自带的目录 id 被迅雷判为无效 → 先落默认目录，别因为一个过期 id 卡死整条链路
        if folder and folder_ish and self.cfg.get("auto_fallback_folder", True):
            log.warning("迅雷拒了带目录的投递（kind=%s code=%s），改用默认目录重试一次",
                        kind or "-", code or "-")
            again = self._post_task(magnet, title, target, files, {})
            if again.ok:
                again.message = _join_note(again.message,
                                           "原下载目录 id 无效，本次已投递到迅雷默认目录")
                again.raw = _mark_folder_fallback(
                    again.raw, folder.get("parent_folder_id") or "", "")
                return again
            tried.append(("改用默认目录重试", again))
            res = again
            kind = (again.raw or {}).get("_error_kind") or ""
            code = (again.raw or {}).get("_error_code") or ""

        # 2) 目录为空还被拒（迅雷常常要求必须带目录）→ 用「迅雷自己用过的目录」再试一次。
        #    任务列表里记着这些 id，那是迅雷亲口承认能用的，比自己瞎填靠谱。
        if folder_ish and not options.get("_no_folder"):
            skip = (folder or {}).get("parent_folder_id") or ""
            cands = [c for c in self.folder_candidates()
                     if c.get("parent_folder_id") and c["parent_folder_id"] != skip]
            if cands:
                best = cands[0]
                params = {"parent_folder_id": best["parent_folder_id"]}
                if best.get("parent_folder_path"):
                    params["parent_folder_path"] = best["parent_folder_path"]
                log.info("用已有任务里的下载目录重试：%s",
                         best.get("parent_folder_path") or best["parent_folder_id"])
                last = self._post_task(magnet, title, target, files, params)
                if last.ok:
                    last.message = _join_note(
                        last.message, "本次用的是已有任务里的下载目录：%s" % (
                            best.get("parent_folder_path") or best["parent_folder_id"]))
                    last.raw = _mark_folder_fallback(last.raw, "", "")
                    last.raw["_folder_recovered"] = best
                    return last
                tried.append(("改用已有任务的目录重试", last))
                res = last

        # 3) 令牌对云端明明有效（磁力都解析出来了）却还被拒 → 多半是请求体里某个可选字段
        #    这个版本不认（mime_type / sub_file_index / file_size 都可能是雷），减到最少再试。
        if (self._cloud_ok and (res.raw or {}).get("_auth_failed")
                and self.cfg.get("auto_minimal_body", True)):
            log.warning("云端令牌有效（磁力可解析）却仍被拒，改用最简请求体重试一次")
            lean = self._post_task(magnet, title, target, files, {}, minimal=True)
            if lean.ok:
                lean.message = _join_note(lean.message, "已改用最简请求参数投递成功")
                lean.raw = dict(lean.raw or {})
                lean.raw["_minimal_body"] = True
                return lean
            tried.append(("改用最简参数重试", lean))
            res = lean

        if len(tried) > 1:
            # 三种投法常常撞同一句话，别把同一段报错复制三遍（Telegram 里会很难看）
            groups = []
            for label, r in tried:
                for g in groups:
                    if g[1] == r.message:
                        g[0].append(label)
                        break
                else:
                    groups.append(([label], r.message))
            res.message = "；".join("%s：%s" % (" / ".join(g[0]), g[1]) for g in groups)
            res.raw = dict(res.raw or {})
            res.raw.setdefault("_error_kind", kind)
        return res

    def _post_task(self, magnet, title, target, files, folder, minimal=False):
        """真正发 POST /drive/v1/task。

        folder 为空 dict 表示落到默认目录；``minimal=True`` 只留迅雷认得出的必填字段
        （有的版本会因为多带了 mime_type / sub_file_index / file_size 而兜底回
        ``invalid token``，那种时候减字段比换令牌有用）。
        """
        total = len(files) or 1
        first = files[0] if files else {}
        file_name = _clean_name(first.get("name") or first.get("file_name") or "") or title
        file_size = str(first.get("file_size") or 0)
        mime = first.get("mime_type") or ""

        params = {"url": magnet, "target": target, "total_file_count": str(total)}
        pick = {}
        # minimal 只砍「可选、各版本认法还不一」的两个：mime_type / sub_file_index。
        # 别砍 file_name —— 删掉网关直接 400（实测），那样连真正的错误都问不到了。
        if not minimal and mime:
            params["mime_type"] = mime
        if not minimal and files:
            # 单文件种子也过规则：否则「图片一律不下」在只含一张图的磁力上就漏了
            indices, pick = self.select_files(files)
            if indices == []:
                log.warning("文件筛选一个都没留下，按配置不投递：%s", title[:60])
                return DownloadResult(False, self.name, "",
                                      "文件筛选把 %d 个文件全筛掉了，按设置没有投递"
                                      "（想改成自动兜底全选，把 fallback_all 打开）" % total,
                                      {"_stage": "pick", "_pick": pick})
            if total > 1:
                if indices is None:
                    params["sub_file_index"] = "-1"
                else:
                    params["sub_file_index"] = ",".join(str(i) for i in indices)
                    log.info("多文件种子按规则挑了 %d/%d：%s", len(indices), total,
                             params["sub_file_index"])
        elif minimal and total > 1 and not bool(self.cfg.get("select_all_files", False)):
            # 精简请求把 sub_file_index 砍了，筛选这次等于没生效——回执必须说明
            pick = {"total": total, "kept": total, "minimal": True}
        params.update(folder or {})

        body = {
            "type": "user#download-url",
            "name": _clean_name(title) or file_name or "tmb-task",
            "file_name": file_name or "tmb-task",
            "file_size": file_size,
            "space": target,
            "params": params,
        }
        try:
            resp = self._request("/drive/v1/task", body)
        except util.HttpError as e:
            return DownloadResult(False, self.name, "",
                                  self._http_fail_message(e, body, minimal),
                                  {"error": str(e), "_http_status": e.status})

        task = resp.get("task") or {}
        task_id = str(task.get("id") or resp.get("id") or resp.get("task_id") or "")
        ok = bool(task_id) or resp.get("HttpStatus") == 0
        if ok:
            raw = dict(resp or {})
            notable = bool(pick) and (pick.get("kept", total) != total
                                      or pick.get("minimal") or pick.get("fallback")
                                      or pick.get("refused"))
            if notable:
                raw["_pick"] = pick
            kept = int(pick.get("kept") or total) if notable else total
            # 只有真少下了才写「12 / 38」；兜底全选或精简重试时仍旧是 N 个。
            # 一个文件都没认出来时别写「1 个文件」——那是把自己也有疑问的事说成事实。
            if not files:
                label = "已投递迅雷（没拿到文件清单，整个种子全下）"
            else:
                label = ("已投递迅雷（%d / %d 个文件）" % (kept, total)
                         if notable and kept < total
                         else "已投递迅雷（%d 个文件）" % total)
            return DownloadResult(True, self.name, task_id or "ok", label, raw)

        binary = bool(resp.get("_binary"))
        # 二进制/乱码里抠出来的数字不可信（可能是压缩流的字节巧合），宁可不报 code
        code = "" if binary else extract_code(resp)
        # 失败时把原始响应留一份在日志里：默认 INFO 就能看到（截断），
        # 调成 DEBUG 则能看到完整的请求体与响应体。
        orig = resp.get("_raw_text") or resp
        status = resp.get("_http_status") or ""
        log.info("迅雷建任务被拒（目录:%s%s）原始响应：%s",
                 (folder or {}).get("parent_folder_id") or "默认",
                 ("，HTTP %s" % status) if status else "",
                 self._redact(orig, 400) if not binary else
                 "(非文本响应，hex=%s)" % (resp.get("_raw_hex") or "")[:120])
        base_raw = {"_stage": "task", "response": _snippet(resp, 400)}
        if binary:
            base_raw["_binary"] = True
            base_raw["_raw_hex"] = resp.get("_raw_hex") or ""
        if code:
            base_raw["_error_code"] = code
        # 限流优先：同样的 403，说成「鉴权失败」会把人带去重登迅雷、重抓 pan_auth，
        # 而真正该做的是等额度恢复。并且这一步一旦确认，上层一次都不该再重试。
        if not binary and (resp.get("_quota") or classify_quota(_snippet(resp, 4000))):
            self._quota_hit = True
            base_raw["_quota"] = True
            return DownloadResult(False, self.name, "",
                                  explain_quota(self.cfg), base_raw)
        kind = resp.get("_auth_failed") or ("" if binary else classify_task_error(resp))
        if kind in ("token", "account"):
            base_raw["_auth_failed"] = kind
            return DownloadResult(False, self.name, "",
                                  self._auth_message(kind, resp), base_raw)
        if kind == "folder":
            base_raw["_error_kind"] = "folder"
            base_raw["_folder_id"] = (folder or {}).get("parent_folder_id", "")
            return DownloadResult(False, self.name, "",
                                  self._folder_message(resp, folder), base_raw)
        if code:
            # 只说码不说原因的那种（面板/网关都可能）：查表能翻译就翻译，查不到就讲清
            # 常见原因和自检办法，别让用户对着一个数字发愁
            return DownloadResult(False, self.name, "",
                                  self._code_message(resp, code), base_raw)
        return DownloadResult(False, self.name, "",
                              "迅雷拒绝任务: %s" % _snippet(resp, 200), base_raw)

    def _retry_with_page_token(self, info):
        """Cookie 有效、pan_auth 被拒时，拿套件首页内联的令牌换一次再试。

        1.0.42 的自愈核心：新版套件不把 pan_auth 落盘（文件扫描一无所获就是这原因），
        但首页 HTML 里就写着当前那条。只要登录 Cookie 还是好的，程序就能自己把令牌
        捞回来，不用再让用户开 F12 抄。
        """
        fresh, why = self.fetch_pan_auth()
        if not fresh:
            info["pan_auth_refresh"] = why
            return {}, info
        old = str(self.cfg.get("pan_auth") or "").strip()
        if fresh == old:
            info["pan_auth_refresh"] = "首页里的令牌与现在这条一致（%s）" % why
            return {}, info
        self.cfg["pan_auth"] = fresh
        info["pan_auth_new"] = fresh
        info["pan_auth_refresh"] = why
        log.info("pan_auth 被拒，已从套件首页换用新令牌（%d 字符）", len(fresh))
        try:
            data = self._request("/drive/v1/tasks", method="GET", extra={"limit": "5"})
        except util.HttpError as e:
            info["error"] = str(e)
            return {}, info
        if isinstance(data, dict) and not data.get("_auth_failed") and "tasks" in data:
            info["ok"] = True
            info["tasks_ok"] = True
            info["tasks_sample"] = _snippet(data, 400)
            info["hint"] = "已自动换用套件首页里的新 pan_auth，重连成功"
            info.pop("error", None)
            info.pop("culprit", None)
            info.pop("auth", None)
        return data, info

    def probe(self) -> dict:
        info = {"backend": self.name, "mode": self.cfg.get("mode") or "fnos_gateway",
                "api_base": self.api_base(), "auth": ""}
        # 代理/WireGuard 排障：先讲清这条请求到底走哪条路
        info["route"] = util.route_of(self.api_base(), self.proxy)
        # pan_auth 是 JWT，payload 里就写着过期时间——先把这手牌摆到桌面上
        life = pan_auth_life(self.cfg.get("pan_auth"))
        if life.get("text"):
            info["pan_auth_exp"] = life["text"]
            if life.get("expired"):
                info["pan_auth_expired"] = True
        try:
            data = self._request("/drive/v1/tasks", method="GET", extra={"limit": "5"})
        except util.HttpError as e:
            info["ok"] = False
            info["error"] = str(e)
            info["hint"] = ("检查端口/mode 是否正确，以及迅雷面板是否已登录"
                            if e.status is None else
                            "HTTP %s：%s" % (e.status, explain_auth("token", info["mode"], self.cfg))
                            if e.status in (401, 403) else "HTTP %s" % e.status)
            info["hint"] = _merge_hint(info["hint"], self._route_note(info))
            return info

        info["tasks_sample"] = _snippet(data, 400)
        kind = data.get("_auth_failed") or ""
        if kind:
            info["ok"] = False
            info["tasks_ok"] = False
            info["auth"] = kind
            info["error"] = _snippet(data, 200)
            info["token_age"] = _age_text(self.token_age())
            info["token_source"] = str(self.cfg.get("fnos_token_source") or "")
            culprit = ""
            if kind == "token" and info["mode"].lower() == "fnos_gateway" and self._cookie():
                culprit, evidence = self.auth_culprit(info["error"])
                info["culprit"] = culprit
                if evidence.get("reason"):
                    info["auth_evidence"] = evidence["reason"]
            if culprit == "pan_auth":
                # 登录 Cookie 是好的，只是 pan_auth 被云端作废了——首页里就写着
                # 当前那条，用同一个 Cookie 读回来，换上去再试，用户不用动手。
                data, info = self._retry_with_page_token(info)
                kind = "" if info.get("tasks_ok") else (data.get("_auth_failed") or "token")
            if kind:
                info["hint"] = explain_auth(kind, info["mode"], self.cfg, culprit,
                                            self.token_age(), cloud_ok=self._cloud_ok)
                info["hint"] = _merge_hint(info["hint"], self._route_note(info))
                return info

        info["tasks_ok"] = True
        target = _find_target(data)
        info["target"] = target or self._target
        info["ok"] = bool(info["target"])
        if not info["target"]:
            info["hint"] = ("接口能通但取不到设备空间：通常是迅雷客户端没登录账号，"
                            "或刚装好空间还没激活")
        # 目录 id 填成路径这种错，本地就能看出来，不必等投递失败
        folder, note = self._folder_params()
        if note:
            info["folder"] = "invalid"
            info["folder_id"] = str(self.cfg.get("parent_folder_id") or "")
            extra = note + "（迅雷要的是目录 id，留空即用默认目录）"
            info["hint"] = ("%s；%s" % (info["hint"], extra)) if info.get("hint") else extra
        # 顺手把「迅雷自己用过的下载目录」列出来：投递报目录类错误时，用户可以直接照抄
        self._tasks_data = data
        cands = harvest_folders(data)[:3]
        if cands:
            info["folders"] = cands
        return info


# ---------- 辅助 ----------
def harvest_folders(data) -> list:
    """从任务列表里捞出迅雷自己写过的下载目录（id + 路径），按出现次数排序。

    任务创建成功后，迅雷会把 ``parent_folder_id`` / ``parent_folder_path`` 记在任务的
    params 里。这就是「迅雷亲口承认能用的目录」，比让用户手抄 id 靠谱得多。
    """
    found = {}

    def walk(node, depth=0):
        if depth > 8:
            return
        if isinstance(node, dict):
            fid = node.get("parent_folder_id") or node.get("parent_id")
            if isinstance(fid, str) and fid.strip():
                item = found.setdefault(fid.strip(), {
                    "parent_folder_id": fid.strip(), "parent_folder_path": "", "hits": 0})
                item["hits"] += 1
                path = node.get("parent_folder_path") or node.get("path") or ""
                if isinstance(path, str) and path.strip() and not item["parent_folder_path"]:
                    item["parent_folder_path"] = path.strip()
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:50]:
                walk(v, depth + 1)

    walk(data)
    return sorted(found.values(), key=lambda x: (-x["hits"], x["parent_folder_id"]))


def _join_note(message, note):
    """把一句补丁挂到投递结果后面，用 · 隔开，别套两层括号。"""
    message = (message or "").strip()
    note = (note or "").strip()
    if not note:
        return message
    return "%s· %s" % (message, note) if message else note


def _mark_folder_fallback(raw, bad_id, note):
    """标记「这次是降级到默认目录才成功的」，上层据此清理坏配置。"""
    raw = dict(raw or {})
    raw["_folder_fallback"] = True
    if bad_id:
        raw["_bad_folder_id"] = bad_id
    if note:
        raw["_folder_note"] = note
    return raw


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{32,40}", name):  # 占位 infohash 名
        return ""
    return name[:180]


def _merge_hint(a, b):
    """两段提示拼一起，空的那段不留空档。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    return a + "；" + b


def _find_target(node, depth=0):
    """在任意层级里找形如 device#xxx 的设备标识。"""
    if depth > 6:
        return ""
    if isinstance(node, dict):
        t = node.get("target")
        if isinstance(t, str) and "#" in t and len(t) < 200:
            return t
        for v in node.values():
            r = _find_target(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for v in node[:30]:
            r = _find_target(v, depth + 1)
            if r:
                return r
    return ""


def _children_of(node):
    """节点的子项。迅雷把目录内容放在 ``dir`` 里（不是 ``dir`` 路径字符串）：

    ``{"is_dir": true, "file_count": 6, "dir": {"resources": [ …6 个文件… ]}}``
    """
    kids = node.get("resources") or node.get("children")
    if not kids:
        d = node.get("dir")
        if isinstance(d, dict):
            kids = d.get("resources") or d.get("children")
    if isinstance(kids, dict):
        kids = kids.get("resources")
    return kids if isinstance(kids, list) else []


def _is_dir_node(node) -> bool:
    """这个节点是不是目录（迅雷对裸磁力常常只回一个这样的根目录节点）。"""
    if not isinstance(node, dict):
        return False
    if _children_of(node):
        return True
    if node.get("kind") in ("drive#folder", "folder"):
        return True
    if str(node.get("is_dir")) in ("1", "True", "true"):
        return True
    # 有的版本把 is_dir 省掉，只留 "file_count": "6"：那是目录的「里面有多少个」。
    # 只在 >1 时才这么判——file_count == 1 的真·单文件种子不该被当成目录去钻。
    try:
        return int(str(node.get("file_count") or 0)) > 1
    except (TypeError, ValueError):
        return False


def _flatten_files(resources, depth=0, out=None):
    """把文件树拍平成文件列表（DFS 先序，保持与迅雷 file_index 一致的顺序）。

    注意：**没有子节点的目录节点会被丢掉**（它代表「这个目录我还没展开」），
    不能当成一个文件——那样调用方会以为「这条磁力只有 1 个文件」，
    于是不发 ``sub_file_index``，迅雷就把整个种子全下了。展开见 ``_drill``。
    """
    if out is None:
        out = []
    if depth > 8:
        return out
    for node in resources or []:
        if not isinstance(node, dict):
            continue
        if _is_dir_node(node):
            _flatten_files(_children_of(node), depth + 1, out)
        else:
            out.append(node)
    return out


def _resources_of(parsed):
    """从响应里挖出资源数组。

    真机响应（飞牛网关 + 官方迅雷，2026-11-12）长这样：

    ```json
    {"list_id": "Ah5-…",
     "list": {"page_size": 2000,
              "resources": [{"name": "FC2PPV-5003668", "is_dir": true, "file_count": 6,
                             "dir": {"page_size": 6, "resources": [ …6 个文件… ]}}]}}
    ```

    所以「取 resources」这一步得会剥两层：``list`` 是个对象、目录节点里面的内容在 ``dir``
    里。之前只认 ``resources``/``list`` 平铺的情况，整棵树就被当成了「一个空目录」，
    于是「一个文件都没认出来」——这才是那个 bug 的真身。
    """
    res = parsed
    for _ in range(4):
        if isinstance(res, list):
            break
        if not isinstance(res, dict):
            return []
        nxt = res.get("resources")
        if nxt is None:
            nxt = res.get("list")
        if nxt is None:
            nxt = res.get("dir")
        if nxt is None:
            # 没有 resources / list / dir：只有它自己就是个文件/目录节点时才算一个资源。
            # 错误响应（{"_raw_text": "boom"}）之类不该被当成「一个文件」，那会让
            # 「云端解析成功」的判定失真，进而触发最简参数重试、掩盖真问题。
            return [res] if (res.get("name") or res.get("id")) else []
        res = nxt
    if isinstance(res, dict):
        res = [res]
    return [n for n in (res or []) if isinstance(n, dict)]


def _node_id(node) -> str:
    for k in ("id", "file_id", "gcid", "infohash"):
        v = node.get(k) if isinstance(node, dict) else None
        if isinstance(v, (str, int)) and str(v).strip() not in ("", "0"):
            return str(v).strip()
    return ""


def _same_node(a, b) -> bool:
    """两次响应是不是同一个节点（用来识别「参数没生效、把目录本身又回了一遍」）。"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    ia, ib = _node_id(a), _node_id(b)
    if ia and ib and ia == ib:
        return True
    return (str(a.get("name") or "") == str(b.get("name") or "")
            and str(a.get("file_size") or "") == str(b.get("file_size") or "")
            and bool(a.get("is_dir")))


def _brief(node, limit=8):
    """节点摘要：给面板诊断看，别把整棵树塞进响应。"""
    if not isinstance(node, dict):
        return node
    out = {}
    for k in ("id", "dir", "parent_id", "name", "file_index", "file_size",
              "file_count", "is_dir", "kind"):
        v = node.get(k)
        if isinstance(v, (dict, list)):
            v = _shape(v)          # 目录节点的 dir 里是整棵子树，别整段塞进响应
        if isinstance(v, str) and len(v) > 80:
            v = v[:80] + "…"
        if v not in (None, ""):
            out[k] = v
    extra = sorted(str(k) for k in node.keys() if k not in out)
    if extra:
        out["_other_keys"] = extra[:limit]
    return out


def _child_bodies(node, magnet, list_id, space):
    """往里钻一层要用的「路径 + 请求体 + 查询参数」，按「最可能好使」排序。

    迅雷各版本认的字段不一样（跟 ``FILE_LIST_BODIES`` 同一个毛病），连**接口路径**都可能
    不是一个：``resource/list`` 是「解析这个地址」，``files/list`` 才是「列这个目录」。
    ``device_space`` 也一样——有的版本当查询参数、有的当请求体字段，两种都试。
    谁的子节点多就用谁，并记在进程里下次排最前。
    这些都是**纯本地**接口，每试一套一两百毫秒，比跑去外网下种子便宜得多。

    每项是 ``(标签, 路径, 请求体, 查询参数)``。
    """
    LIST = "/drive/v1/resource/list"
    FILES = "/drive/v1/files/list"
    nid = _node_id(node)
    path = node.get("dir") if isinstance(node.get("dir"), str) else ""
    ds = {"device_space": space} if space else None
    out = []
    if nid:
        out.append(("parent_id", LIST, {"parent_id": nid, "page_size": 2000}, None))
        out.append(("files/list:parent_id", FILES,
                    {"parent_id": nid, "page_size": 2000}, None))
        if ds:
            out.append(("parent_id+device_space", LIST,
                        {"parent_id": nid, "page_size": 2000}, ds))
            out.append(("files/list:parent_id+device_space", FILES,
                        {"parent_id": nid, "page_size": 2000}, ds))
            out.append(("parent_id+space", LIST,
                        {"parent_id": nid, "space": space, "page_size": 2000}, None))
            out.append(("files/list:parent_id+space", FILES,
                        {"parent_id": nid, "space": space, "page_size": 2000}, None))
        if list_id:
            out.append(("parent_id+list_id", LIST,
                        {"parent_id": nid, "list_id": list_id,
                         "page_size": 2000}, None))
        out.append(("parent_id+urls", LIST,
                    {"parent_id": nid, "urls": magnet, "page_size": 2000}, None))
        out.append(("parent_id+urls[]", LIST,
                    {"parent_id": nid, "urls": [magnet], "page_size": 2000}, None))
        out.append(("urls+parent_id", LIST,
                    {"urls": magnet, "parent_id": nid, "page_size": 2000}, None))
        out.append(("id+list_id", LIST,
                    {"id": nid, "list_id": list_id or nid, "page_size": 2000}, None))
    if path:
        out.append(("dir", LIST, {"dir": path, "page_size": 2000}, None))
        if nid:
            out.append(("parent_id+dir", LIST,
                        {"parent_id": nid, "dir": path, "page_size": 2000}, None))
    if list_id and not nid:
        out.append(("list_id", LIST, {"list_id": list_id, "page_size": 2000}, None))
    return out


# 钻目录时哪套参数好使（进程内记住，下次排最前，省掉几次注定失败的本地请求）
_CHILD_VARIANT = {"label": ""}
_MAX_DIR_DEPTH = 3          # 再深的层级一般就是「花絮」目录，没必要为它多打几轮


def _shape(node, depth=0):
    """把一段响应压成「结构摘要」，给面板的解析测试看。

    「迅雷到底回了什么」是这个项目里最贵的信息：只看到 1 个文件时，得能一眼判断
    是它真只有一个文件，还是回了一个没展开的目录节点。
    """
    if depth >= 3:
        return "…"
    if isinstance(node, list):
        head = [_shape(x, depth + 1) for x in node[:3]]
        if len(node) > 3:
            head.append("…共 %d 项" % len(node))
        return head
    if isinstance(node, dict):
        kids = node.get("resources")
        if kids is None:
            kids = node.get("children")
        if kids is None and isinstance(node.get("dir"), dict):
            kids = node["dir"].get("resources")
        item = {"keys": sorted(str(k) for k in node.keys())[:14]}
        for k in ("kind", "is_dir", "name", "file_index"):
            if node.get(k) not in (None, ""):
                item[k] = node.get(k) if k != "name" else str(node.get(k))[:60]
        if isinstance(kids, list):
            item["children"] = len(kids)
            if kids:
                item["child0"] = _shape(kids[0], depth + 1)
        return item
    return repr(node)[:40]

