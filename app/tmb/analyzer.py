"""结果分析：过滤 -> 打分 -> 排序。

打分维度：做种数（能不能下动）、下载量（资源热度）、发布时间（新鲜度）、
标题关键词命中率（是不是同一个东西）、是否可信组/重制、偏好词加分。
"""
from __future__ import annotations

import math
import re
import time

from . import util
from .classifier import title_match_ratio


def _contains_any(title_low: str, words):
    hits = []
    for w in words or []:
        w = (w or "").strip().lower()
        if w and w in title_low:
            hits.append(w)
    return hits


def analyze(items, rule=None, search_cfg=None, keywords=None, base_url=""):
    rule = rule or {}
    search_cfg = search_cfg or {}
    keywords = keywords or []

    min_seeders = _num(rule.get("min_seeders", search_cfg.get("min_seeders", 1)), 1)
    min_size = _num(rule.get("min_size_mb", search_cfg.get("min_size_mb", 0)), 0) * 1024 * 1024
    max_size = _num(rule.get("max_size_mb", search_cfg.get("max_size_mb", 0)), 0) * 1024 * 1024
    ratio_gate = _num(rule.get("title_match_ratio", search_cfg.get("title_match_ratio", 0)), 0)
    exclude = rule.get("exclude_words") or []
    prefer = rule.get("prefer_words") or []
    prefer_cat = (rule.get("prefer_category") or "").strip()

    dropped = {"seeders": 0, "size": 0, "exclude": 0, "title": 0}
    kept = []
    now = time.time()

    for it in items:
        title = it.get("title") or ""
        low = title.lower()

        if min_seeders and it.get("seeders", 0) < min_seeders:
            dropped["seeders"] += 1
            continue
        size = it.get("size_bytes") or 0
        if size and min_size and size < min_size:
            dropped["size"] += 1
            continue
        if size and max_size and size > max_size:
            dropped["size"] += 1
            continue
        if _contains_any(low, exclude):
            dropped["exclude"] += 1
            continue
        ratio = title_match_ratio(keywords, title)
        if ratio_gate and ratio < ratio_gate:
            dropped["title"] += 1
            continue

        score = 0.0
        reasons = []

        seeders = it.get("seeders", 0)
        s_seed = min(seeders, 300) / 300.0 * 40.0
        score += s_seed
        if seeders >= 20:
            reasons.append("做种充足")

        dls = it.get("downloads", 0)
        score += math.log10(1 + max(0, dls)) * 6.0

        age_days = (now - it.get("pub_ts", 0)) / 86400.0 if it.get("pub_ts") else 999
        if age_days < 120:
            score += (1 - age_days / 120.0) * 20.0
            if age_days < 7:
                reasons.append("新发布")
        score += ratio * 25.0
        if ratio >= 0.99 and keywords:
            reasons.append("标题完全命中")

        ph = _contains_any(low, prefer)
        if ph:
            score += min(len(ph), 4) * 8.0
            reasons.append("偏好词:" + "/".join(ph[:3]))

        if it.get("trusted"):
            # 绿底行 = 站点认证的可信发布者，通常是自己压片/校验过的资源
            score += _num(search_cfg.get("trusted_bonus", 10), 10.0)
            reasons.append("可信发布")
        if it.get("remake"):
            score -= 3.0
            reasons.append("重制")
        if it.get("hidden"):
            score -= 15.0
            reasons.append("站点隐藏")

        if prefer_cat and it.get("category_id") == prefer_cat:
            score += 6.0
            reasons.append("分类匹配")

        item = dict(it)
        item["score"] = round(score, 2)
        item["reasons"] = reasons
        item["match_ratio"] = round(ratio, 2)
        item["size_human"] = it.get("size_text") or util.human_size(size)
        item["age_human"] = util.human_age(it.get("pub_ts"))
        if not item.get("view_url") and item.get("id") and base_url:
            item["view_url"] = "%s/view/%s" % (base_url.rstrip("/"), item["id"])
        kept.append(item)

    kept.sort(key=lambda x: (-x["score"], -x.get("seeders", 0)))

    relaxed = False
    if not kept and items and not dropped["exclude"]:
        # 过滤太狠时退回「原始排序」，让用户至少看到能下的东西；
        # 但如果是被「排除词」清空，说明是用户的明确意图，不做放宽。
        relaxed = True
        for it in items[:5]:
            item = dict(it)
            item["score"] = float(it.get("seeders", 0))
            item["reasons"] = ["过滤条件过严，已放宽"]
            item["match_ratio"] = round(title_match_ratio(keywords, it.get("title")), 2)
            item["size_human"] = it.get("size_text") or util.human_size(it.get("size_bytes"))
            item["age_human"] = util.human_age(it.get("pub_ts"))
            kept.append(item)

    return {
        "items": kept,
        "total": len(items),
        "kept": len(kept),
        "dropped": dropped,
        "relaxed": relaxed,
    }


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pick(items, index=1):
    """按序号取候选（1 起），越界返回 None。"""
    try:
        i = int(index) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= i < len(items):
        return items[i]
    return None
