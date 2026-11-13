"""消息前置解析：先看有没有磁力，再看是不是 FC2，最后才交给关键词分拣。

判定顺序（用户定的优先级，不能换）
--------------------------------
1. 消息里有 ``magnet:?xt=urn:btih:...`` → 直接投递给迅雷，**一次搜索都不发**
2. 没有磁力、但出现 ``FC2`` 字样 → 取 FC2 后面那串编号（通常是 7 位），
   只拿编号去站点检索
3. 两样都没有 → 整段消息走 rules.json 的关键词分拣再检索

为什么写死在代码里而不是放进 rules.json：这是「消息长什么样」的语法判断，
不是可以随手调的偏好；规则表仍然负责第 3 条的检索词、过滤条件与分类。
"""
from __future__ import annotations

import html
import re
import urllib.parse

from . import nyaa

MODE_MAGNET = "magnet"
MODE_FC2 = "fc2"
MODE_SEARCH = "search"

MODE_LABEL = {
    MODE_MAGNET: "自带磁力 · 直接投递",
    MODE_FC2: "FC2 编号检索",
    MODE_SEARCH: "整段检索",
}

# 磁力链接一直写到空白或引号/全角标点为止；后面常粘着中文句子，必须切掉
_MAGNET_RE = re.compile(
    r"magnet:\?[^\s<>\"'“”‘’「」『』【】（）()《》，。；：！？、]+", re.I)
# 允许出现在末尾但一定不属于链接的字符（用户复制时常带上）
_TAIL_JUNK = ".,;:!?)]}>'\"，。；：！？、…~"
_NON_ASCII_TAIL_RE = re.compile(r"[^\x21-\x7e]+$")

_FC2_FLAG_RE = re.compile(r"fc2", re.I)
# FC2PPV-4999948 / FC2-PPV-1234567 / fc2 ppv 1234567 / FC2 1234567
_FC2_CODE_RE = re.compile(r"fc2[^0-9\n]{0,12}?(\d{5,8})", re.I)


# ---------------------------------------------------------------- 磁力
def _clean_magnet(raw: str) -> str:
    """把从文本里切出来的一段收拾成能直接投递的磁力链接。"""
    s = html.unescape((raw or "").strip())
    # 末尾粘的中文（「磁力」两个字之类）先整段裁掉
    s = _NON_ASCII_TAIL_RE.sub("", s)
    s = s.rstrip(_TAIL_JUNK).rstrip("&")
    if not s.lower().startswith("magnet:?"):
        return ""
    rest = s[8:]
    # 统一小写 scheme 与 xt=urn:btih:（有的客户端认，有的不认大写），哈希本身保持原样
    m = re.match(r"(?i)xt=urn:btih:([0-9a-z]+)", rest)
    if m:
        rest = "xt=urn:btih:" + m.group(1) + rest[m.end():]
    return "magnet:?" + rest


def scan_magnets(text: str, limit: int = 3):
    """扫描磁力，返回 (有效列表, 无效原始串列表)。

    截断或拼错的磁力直接判无效：投递一个没有 infohash 的任务只会换来一堆报错，
    但要把原文留下来，回复里告诉用户「这一条没认出来」，否则会误以为程序没看见。
    """
    out, bad, seen = [], [], set()
    for m in _MAGNET_RE.finditer(text or ""):
        mag = _clean_magnet(m.group(0))
        ih = nyaa.magnet_hash(mag) if mag else ""
        if not ih:
            bad.append((m.group(0) or "").strip()[:80])
            continue
        if ih in seen:
            continue
        seen.add(ih)
        if len(out) < max(1, int(limit or 3)):
            out.append(mag)
    return out, bad


def find_magnets(text: str, limit: int = 3):
    """取消息里的磁力链接，按出现顺序去重（按 infohash）。无效的丢弃。"""
    return scan_magnets(text, limit)[0]


def title_from_magnet(magnet: str) -> str:
    """磁力里的 dn 参数通常就是资源名，拿它当标题最准。"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    except Exception:
        return ""
    return (q.get("dn") or [""])[0].strip()


def guess_title(text: str, magnets=()) -> str:
    """没有 dn 时，用消息里的文字当标题（去掉磁力本体）。

    多行拼成一串：转发过来的消息常见「FC2PPV-4999948」一行、标题一行，
    只取第一行会把编号当标题，全拼起来才看得出是什么资源。
    """
    body = _MAGNET_RE.sub(" ", text or "")
    parts = []
    for line in body.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" -—_·•：:，,、\t")
        if len(line) >= 2 and line not in parts:
            parts.append(line)
        if sum(len(p) for p in parts) >= 120:
            break
    if parts:
        return " ".join(parts)[:120]
    if magnets:
        return "磁力 " + (nyaa.magnet_hash(magnets[0])[:12] or "")
    return ""


# ---------------------------------------------------------------- FC2
def has_fc2(text: str) -> bool:
    return bool(_FC2_FLAG_RE.search(text or ""))


def find_fc2(text: str):
    """找 FC2 编号。没有 FC2 字样返回 None；有字样但没编号也返回 None。"""
    hits = [(m.start(), m.group(1).strip(), m.group(0).strip())
            for m in _FC2_CODE_RE.finditer(text or "")]
    if not hits:
        return None
    # FC2 编号常见 7 位：多条命中时优先 7 位那条，其次取最先出现的
    best = next((h for h in hits if len(h[1]) == 7), hits[0])
    return {"code": best[1], "raw": best[2], "count": len(hits)}


# ---------------------------------------------------------------- 汇总
def parse(text: str, max_magnets: int = 3) -> dict:
    """给一段消息定性，返回 {mode, magnets, fc2, has_fc2, bad_magnets, reason, text}。"""
    raw = text or ""
    magnets, bad = scan_magnets(raw, max_magnets)
    fc2 = find_fc2(raw)
    plan = {
        "mode": MODE_SEARCH,
        "magnets": magnets,
        "bad_magnets": bad,
        "fc2": fc2,
        "has_fc2": has_fc2(raw),
        "reason": "",
        "text": raw,
    }
    if magnets:
        plan["mode"] = MODE_MAGNET
        plan["reason"] = "消息自带 %d 条磁力，直接投递" % len(magnets)
        if fc2:
            plan["reason"] += "（另有 FC2 %s，本次不用）" % fc2["code"]
    elif fc2:
        plan["mode"] = MODE_FC2
        plan["reason"] = "命中 FC2「%s」，用编号 %s 检索" % (fc2["raw"], fc2["code"])
    elif plan["has_fc2"]:
        plan["reason"] = "有 FC2 字样但没跟编号，按整段检索"
    else:
        plan["reason"] = "没有磁力也没有 FC2，按整段检索"
    return plan


def describe(plan: dict) -> str:
    """人话版判定结果，CLI 与面板都用它。"""
    lines = ["判定：%s　（%s）" % (MODE_LABEL.get(plan.get("mode"), plan.get("mode")),
                                plan.get("reason") or "")]
    for i, mag in enumerate(plan.get("magnets") or [], 1):
        ih = nyaa.magnet_hash(mag)
        lines.append("  磁力 %d：%s…" % (i, mag[:60]))
        lines.append("           infohash %s" % ih)
    if plan.get("fc2"):
        lines.append("  FC2 编号：%s（原文「%s」）"
                     % (plan["fc2"]["code"], plan["fc2"]["raw"]))
    for bad in plan.get("bad_magnets") or []:
        lines.append("  忽略：%s（infohash 不完整，当普通文本处理）" % bad[:60])
    return "\n".join(lines)
