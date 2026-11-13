"""关键词分拣：把转发进来的一段话，拆成「规则 + 检索词」。

设计原则
--------
* 规则是数据（rules.json），不是代码，方便在 Web UI 里改；
* 命中多条规则时按分值排序，默认只用最高分的那条，避免一次搜索互相污染；
* 检索词 = 原文去掉指令词/URL/停用词后的实词，避免把「帮我下个 xxx 谢谢」整句丢给搜索引擎。
"""
from __future__ import annotations

import re

# 常见的中文指令词/语气词，不参与检索
STOPWORDS = {
    "下载", "帮我", "帮忙", "帮", "求", "找", "找一下", "一下", "来个", "来一个", "有没有",
    "资源", "链接", "磁力", "磁力链", "种子", "谢谢", "麻烦", "我要", "想看", "想下",
    "那个", "这个", "一个", "可以", "能不能", "请", "给我", "吧", "啊", "吗", "呢", "呀",
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "和", "与", "及", "就", "都",
    "有", "没", "不", "很", "太", "还", "也", "再", "把", "被", "让", "对", "从",
    "download", "please", "pls", "thx", "thanks", "magnet", "torrent", "link",
    "帮下", "来下", "找找", "整一个", "弄一个", "求个", "给我", "麻烦帮我",
    # 「动词+找」这类复合口语，逐字拆会把「我找」当成关键词
    "帮我找", "帮我搜", "帮我下", "帮找", "我找", "我搜", "我下", "找下", "搜下",
    "搜索", "搜", "查", "查一下", "看看", "找找看", "有没有资源", "求资源",
}

# 中文里常见的「意图前缀」：粘在实词前面时要剥掉，否则会把整段丢进搜索引擎
INTENT_PREFIXES = sorted({
    "求个", "求一个", "求一", "来个", "来一个", "来点", "弄个", "整个", "给我下", "给我找",
    "帮我下", "帮我找", "帮我搜", "帮忙下", "帮忙找", "想看", "想要", "有没有", "有没有人", "谁有",
    "能不能", "可不可以", "能否", "麻烦帮", "麻烦", "请帮", "想找", "想下",
}, key=len, reverse=True)

# 口语尾巴，剥掉不影响检索
INTENT_SUFFIXES = sorted({
    "一下", "一哈", "谢谢", "多谢", "感谢", "拜托", "呗", "吧", "啊", "呀", "呢",
}, key=len, reverse=True)

_URL_RE = re.compile(r"(https?://\S+|magnet:\?\S+|www\.\S+|t\.me/\S+)", re.I)
_BRACKET_RE = re.compile(r"[【\[（(]([^】\]）)]{1,40})[】\]）)]")
_SPLIT_RE = re.compile(r"[\s,，、。.．!！?？;；:：/\\|~～\-—+*#@\"'“”‘’《》<>()（）\[\]【】]+")
# Vol.2 / v1.5 这类点号是名字的一部分，切词前先换成占位符，切完再还原
_INNER_DOT_RE = re.compile(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])")
# ABC-123 / FC2PPV-4999948 / ABC_456 这类番号，中间的连接符属于编号本身
_INNER_HYPHEN_RE = re.compile(r"(?<=[A-Za-z])[-_](?=[0-9])")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\uFE0F\u200d]+")


def normalize(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\u3000", " ")
    # 全角转半角（ASCII 范围）
    out = []
    for ch in t:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    t = "".join(out)
    t = _EMOJI_RE.sub(" ", t)
    t = _URL_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _strip_intent(tok: str) -> str:
    """剥掉「求个 / 帮我下 / 来个」这类意图前缀与「一下 / 谢谢」这类尾巴。"""
    t = tok
    for _ in range(3):
        hit = False
        for p in INTENT_PREFIXES:
            if t.startswith(p) and len(t) - len(p) >= 2:
                t = t[len(p):]
                hit = True
                break
        if not hit:
            break
    for _ in range(2):
        hit = False
        for s in INTENT_SUFFIXES:
            if t.endswith(s) and len(t) - len(s) >= 2:
                t = t[:-len(s)]
                hit = True
                break
        if not hit:
            break
    return t.strip()


def extract_keywords(text: str, strip_words=None, limit=8):
    """从一段自然语言里抽出检索词，保留原顺序并去重。"""
    raw = normalize(text)
    if not raw:
        return []

    strip = {s.lower() for s in (strip_words or []) if s}
    terms = []

    # 1) 【】/[] 里的内容通常是作品名，优先保留
    for m in _BRACKET_RE.finditer(raw):
        cand = m.group(1).strip()
        if 1 <= len(cand) <= 40:
            terms.append(cand)
    body = _BRACKET_RE.sub(" ", raw)

    # 2) 按标点/空白切词
    body = _INNER_DOT_RE.sub("\x00", body)
    body = _INNER_HYPHEN_RE.sub("\x01", body)
    for tok in _SPLIT_RE.split(body):
        tok = tok.replace("\x00", ".").replace("\x01", "-").strip(" .-_")
        if not tok:
            continue
        terms.append(tok)

    seen, out = set(), []
    for tok in terms:
        tok = _strip_intent(tok.strip())
        low = tok.lower()
        if len(tok) < 1 or len(tok) > 48:
            continue
        if low in STOPWORDS or low in strip:
            continue
        # 长数字串（QQ 号、订单号）不参与检索；但 7 位左右的番号要留
        if tok.isdigit() and len(tok) > 8:
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(tok)
        if len(out) >= limit:
            break
    if not out:
        # 全是停用词时退回原文（截断），至少别搜空
        fallback = raw.strip()[:60]
        if fallback:
            out = [fallback]
    return out


def _hits(needles, haystack_low: str):
    n = 0
    for w in needles or []:
        w = (w or "").strip().lower()
        if w and w in haystack_low:
            n += 1
    return n


class Match(object):
    """一次分拣结果。"""

    def __init__(self, rule, score, keywords, query, matched_words, is_default=False):
        self.rule = rule
        self.score = score
        self.keywords = keywords
        self.query = query
        self.matched_words = matched_words
        self.is_default = is_default

    @property
    def rule_id(self):
        return self.rule.get("id") or ("default" if self.is_default else "?")

    @property
    def rule_name(self):
        return self.rule.get("name") or self.rule_id

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "score": self.score,
            "keywords": self.keywords,
            "query": self.query,
            "matched": self.matched_words,
            "is_default": self.is_default,
            "category": self.rule.get("category") or "0_0",
        }


def _build_query(rule: dict, text: str, keywords):
    tpl = rule.get("query_template") or "{keywords}"
    kw_str = " ".join(keywords) if keywords else ""
    q = (tpl.replace("{keywords}", kw_str)
            .replace("{kw1}", keywords[0] if keywords else "")
            .replace("{raw}", normalize(text)))
    return re.sub(r"\s+", " ", q).strip()


class Router(object):
    def __init__(self, rules: dict, max_rules: int = 1):
        self.rules = rules or {}
        self.max_rules = max(1, int(max_rules or 1))

    def reload(self, rules: dict):
        self.rules = rules or {}

    def route(self, text: str):
        """返回 Match 列表（按分值降序），永不返回空。"""
        raw = normalize(text)
        low = raw.lower()
        cands = []

        for rule in (self.rules.get("rules") or []):
            if not rule.get("enabled", True):
                continue
            score = 0.0
            matched = []
            any_words = rule.get("match", {}).get("any") or []
            all_words = rule.get("match", {}).get("all") or []
            for w in any_words:
                if w and w.lower() in low:
                    score += 1.0
                    matched.append(w)
            if all_words:
                ok = [w for w in all_words if w and w.lower() in low]
                if len(ok) == len(all_words):
                    score += 2.0 + 0.5 * len(ok)
                    matched.extend(ok)
                else:
                    score -= 1.0
            for pat in (rule.get("match_regex") or []):
                try:
                    if re.search(pat, raw, re.I):
                        score += 1.5
                        matched.append(pat)
                except re.error:
                    continue
            if score <= 0:
                continue

            kw = extract_keywords(raw, rule.get("strip_words"))
            query = _build_query(rule, raw, kw)
            if not query:
                continue
            cands.append(Match(rule, score, kw, query, matched))

        cands.sort(key=lambda m: m.score, reverse=True)
        if cands:
            return cands[: self.max_rules]

        # 兜底规则
        rule = dict(self.rules.get("default") or {})
        rule.setdefault("id", "default")
        rule.setdefault("name", "通用兜底")
        kw = extract_keywords(raw, rule.get("strip_words"))
        query = _build_query(rule, raw, kw)
        return [Match(rule, 0.0, kw, query, [], is_default=True)]


def title_match_ratio(keywords, title: str) -> float:
    """标题命中率：检索词里有多少落进了标题。"""
    if not keywords:
        return 1.0
    low = (title or "").lower()
    hit = 0
    counted = 0
    for kw in keywords:
        k = (kw or "").strip().lower()
        if len(k) < 2 and not re.search(r"[a-zA-Z]", k):
            continue
        counted += 1
        if k and k in low:
            hit += 1
    if counted == 0:
        return 1.0
    return hit / float(counted)
