"""历史字典：扫描指定目录，记住见过的文件夹/文件名，用于「是否已经下过」判断。

三条规则：
* 每次扫描只做快照对比，**从不删记录**；发现消失就把 `deleted_ts` 记成「这次发现的时间」，
  记录留着，下次搜索还能告诉你「曾经有过，X 月 X 日发现没了」。
* 匹配用归一化名（去标点/空白/大小写），比的是名字而不是路径。
* 目录整体不可访问（没挂载、权限没了）时**不**标记删除，避免一次误判清空整本字典。
"""
from __future__ import annotations

import os
import re
import time
import unicodedata

from . import util

KIND_DIR = "dir"
KIND_FILE = "file"
STATUS_PRESENT = "present"
STATUS_DELETED = "deleted"

# 系统/回收站目录，扫进去只会污染字典
DEFAULT_IGNORE_NAMES = [
    "@eaDir", "#recycle", "#snapshot", "lost+found", "System Volume Information",
    ".DS_Store", "Thumbs.db", "$RECYCLE.BIN", ".Trash", "@tmp", "@docker",
]
# 下载中间态，不算库里已有
DEFAULT_IGNORE_EXTS = [
    ".tmp", ".part", ".aria2", ".crdownload", ".downloading", ".!qb", ".!ut", ".bc!",
]

# 副本尾巴：「 (1)」「[2]」「 - 副本」「copy」「复件」
_COPY_TAIL_RE = re.compile(
    r"(?:[\s._\-]*[\(\[]\d{1,3}[\)\]])+$"
    r"|[\s._\-]*(?:副本|复件|拷贝|复制|副本\s*\d+|copy(?:[\s._\-]*\d+)?)[\s._\-]*$",
    re.I)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{2,5}$")
_PUNCT_RE = re.compile(r"[\W_]+", re.U)
# 归一化后至少要有这么多字符才值得拿来比对
_MIN_CJK = 3
_MIN_LATIN = 4


# ---------------- 名字归一化 ----------------
def normalize(text) -> str:
    """小写 + 全角转半角 + 去掉所有标点空白，只留字母数字与汉字。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNCT_RE.sub("", s)


def base_name(name, kind=KIND_FILE) -> str:
    """去掉扩展名与「(1)/副本/copy」这类尾巴，用来认「同一个东西的不同版本」。"""
    s = str(name or "").strip()
    for _ in range(3):  # 「名称.mkv (1)」这类叠加尾巴
        stripped = _COPY_TAIL_RE.sub("", s)
        stripped = _EXT_RE.sub("", stripped) if kind == KIND_FILE else stripped
        if stripped == s:
            break
        s = stripped
    return normalize(s)


def tokens(key: str):
    """把归一化串切成可用于模糊检索的片段（字母数字/汉字连续段）。"""
    return [t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", key or "") if len(t) >= 4]


def key_min_len(key: str) -> int:
    """中文三个字就有意义，英文得四个字母以上。"""
    return _MIN_CJK if re.search(r"[\u4e00-\u9fff]", key or "") else _MIN_LATIN


# ---------------- 扫描 ----------------
def _ignored(name, ignore_names, ignore_exts, kind):
    low = name.lower()
    if low in ignore_names:
        return True
    if kind == KIND_FILE:
        ext = os.path.splitext(low)[1]
        if ext in ignore_exts:
            return True
    return False


def _iter_dir(path, depth):
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return
    entries.sort(key=lambda e: e.name.lower())
    for entry in entries:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        yield entry, is_dir, is_file


def walk(roots, max_depth=3, include_files=True, max_items=20000,
         ignore_names=None, ignore_exts=None, skip_hidden=True, follow_symlinks=False):
    """遍历监控目录，返回 (条目列表, 统计)。深度按相对监控根的层级算。"""
    ignore_names = {n.lower() for n in (ignore_names or DEFAULT_IGNORE_NAMES)}
    ignore_exts = {e.lower() for e in (ignore_exts or DEFAULT_IGNORE_EXTS)}
    rows, stats = [], {"truncated": False, "skipped": 0, "errors": [], "roots_missing": []}

    for root in roots:
        root = os.path.abspath(os.path.expanduser(str(root or "").strip()))
        if not root:
            continue
        if not os.path.isdir(root):
            stats["roots_missing"].append(root)
            continue
        stack = [(root, 0)]
        while stack:
            if len(rows) >= max_items:
                stats["truncated"] = True
                break
            cur, depth = stack.pop()
            if depth >= max_depth:
                continue
            for entry, is_dir, is_file in _iter_dir(cur, depth):
                name = entry.name
                if skip_hidden and name.startswith("."):
                    continue
                if is_dir:
                    if _ignored(name, ignore_names, ignore_exts, KIND_DIR):
                        stats["skipped"] += 1
                        continue
                    rows.append({"root": root, "path": entry.path, "kind": KIND_DIR,
                                 "name": name, "depth": depth + 1, "size": 0, "mtime": 0})
                    if follow_symlinks or not entry.is_symlink():
                        stack.append((entry.path, depth + 1))
                elif is_file and include_files:
                    if _ignored(name, ignore_names, ignore_exts, KIND_FILE):
                        stats["skipped"] += 1
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        size, mtime = st.st_size, st.st_mtime
                    except OSError:
                        size, mtime = 0, 0
                    rows.append({"root": root, "path": entry.path, "kind": KIND_FILE,
                                 "name": name, "depth": depth + 1, "size": size,
                                 "mtime": mtime})
                if len(rows) >= max_items:
                    stats["truncated"] = True
                    break

    _fill_dir_sizes(rows)
    return rows, stats


def _fill_dir_sizes(rows):
    """把子文件体积累加到所属目录上，重复分析时目录的体积才有意义。"""
    sizes = {}
    for r in rows:
        if r["kind"] != KIND_FILE or not r["size"]:
            continue
        parent = os.path.dirname(r["path"])
        while parent and parent != os.path.dirname(parent):
            sizes[parent] = sizes.get(parent, 0) + r["size"]
            parent = os.path.dirname(parent)
    for r in rows:
        if r["kind"] == KIND_DIR:
            r["size"] = sizes.get(r["path"], 0)


def _too_many_missing(before, after):
    """目录还能读，但条目骤减：多半是卷没挂上/权限变了，不要据此标记删除。"""
    if before < 50:
        return False
    return after < before * 0.4


def scan(cfg, store, roots=None, now=None):
    """执行一次快照对比。返回可直接展示的报告。"""
    lib = cfg.data.get("library") or {}
    raw_roots = roots if roots is not None else lib.get("roots") or []
    # 统一成绝对路径：库里的 root 字段与这里的比对必须是同一套写法
    roots = [os.path.abspath(os.path.expanduser(str(r).strip()))
             for r in raw_roots if str(r).strip()]
    t0 = time.time()
    now = now or t0
    report = {
        "ok": True, "ts": now, "roots": roots, "added": 0, "removed": 0,
        "restored": 0, "present": 0, "deleted": 0, "total": 0,
        "warnings": [], "skipped_roots": [], "truncated": False,
        "duration": 0.0, "scanned": 0,
    }
    if not roots:
        report["ok"] = False
        report["error"] = "还没配置监控目录"
        report["text"] = "历史字典\n  还没有监控目录。在「历史字典」里填上要分析的目录（比如 /vol1/1000/Downloads），再点开始分析。"
        return report

    rows, stats = walk(
        roots,
        max_depth=int(lib.get("max_depth") or 3),
        include_files=bool(lib.get("include_files", True)),
        max_items=int(lib.get("max_items") or 20000),
        ignore_names=lib.get("ignore_names") or DEFAULT_IGNORE_NAMES,
        ignore_exts=lib.get("ignore_exts") or DEFAULT_IGNORE_EXTS,
        skip_hidden=bool(lib.get("skip_hidden", True)),
    )
    report["truncated"] = stats["truncated"]
    report["scanned"] = len(rows)
    for missing in stats["roots_missing"]:
        report["skipped_roots"].append(missing)
        report["warnings"].append("目录不存在或不可读：%s" % missing)

    existing = store.library_map(roots)
    seen = set()
    per_root_before, per_root_after = {}, {}
    for item in existing.values():
        if item.get("status") == STATUS_PRESENT:
            per_root_before[item["root"]] = per_root_before.get(item["root"], 0) + 1

    blocked_roots = set(stats["roots_missing"])
    for row in rows:
        per_root_after[row["root"]] = per_root_after.get(row["root"], 0) + 1
    for root, before in per_root_before.items():
        if _too_many_missing(before, per_root_after.get(root, 0)):
            blocked_roots.add(root)
            report["warnings"].append(
                "目录条目从 %d 骤减到 %d，疑似未挂载或权限变化，本次不标记删除：%s"
                % (before, per_root_after.get(root, 0), root))

    to_save = []
    for row in rows:
        seen.add(row["path"])
        old = existing.get(row["path"])
        item = dict(row)
        item["name_key"] = normalize(row["name"])
        item["base_key"] = base_name(row["name"], row["kind"])
        if not old:
            item.update(first_seen=now, last_seen=now, status=STATUS_PRESENT,
                        deleted_ts=0, deleted_count=0)
            report["added"] += 1
        else:
            if old["status"] == STATUS_DELETED:
                report["restored"] += 1
            # 关键：deleted_ts 不清零 —— 「曾经有过、什么时间发现没了」这条信息要留着
            item.update(first_seen=old.get("first_seen") or now, last_seen=now,
                        status=STATUS_PRESENT, deleted_ts=old.get("deleted_ts") or 0,
                        deleted_count=old.get("deleted_count") or 0)
        to_save.append(item)
    if to_save:
        store.library_save_many(to_save)

    gone = []
    for path, old in existing.items():
        if path in seen or old.get("status") != STATUS_PRESENT:
            continue
        # 目录读不到 / 条目骤减的根目录一律不动，宁可漏标也不误清
        if old["root"] in blocked_roots:
            continue
        gone.append(path)
    if gone:
        store.library_mark_deleted_many(gone, now)
        report["removed"] = len(gone)

    counts = store.library_counts(roots)
    report.update(present=counts["present"], deleted=counts["deleted"],
                  total=counts["total"])
    report["duration"] = round(time.time() - t0, 2)
    store.add_library_scan(report)
    if report["removed"]:
        store.add_event("library", "字典扫描：新增 %d，消失 %d（记录保留）"
                        % (report["added"], report["removed"]))
    else:
        store.add_event("library", "字典扫描：新增 %d，无消失" % report["added"])
    report["text"] = format_report(report)
    return report


# ---------------- 查询 ----------------
def _score(term_key, item_key):
    if not term_key or not item_key:
        return 0.0
    if item_key == term_key:
        return 1.0
    if item_key in term_key:
        # 库里的名字被查询词完整包含：越接近整串越可信
        return 0.55 + 0.4 * (len(item_key) / max(len(term_key), 1))
    if term_key in item_key:
        return 0.5 + 0.4 * (len(term_key) / max(len(item_key), 1))
    return 0.0


def lookup(store, terms, cfg=None, limit=8):
    """用一组关键词查字典，返回「已存在 / 曾存在已删除 / 没有」。"""
    lib = ((cfg or {}).get("library") or {})
    text_terms = []
    for t in terms or []:
        t = str(t or "").strip()
        if t and t not in text_terms:
            text_terms.append(t)

    keys = []
    for t in text_terms:
        k = normalize(t)
        if len(k) >= key_min_len(k):
            keys.append(k)
    keys = list(dict.fromkeys(keys))

    out = {"ok": True, "state": "none", "verdict": "库里没有", "strong": False,
           "terms": text_terms, "checked_keys": len(keys), "matches": [],
           "present": 0, "deleted": 0, "text": ""}
    if not keys:
        out["verdict"] = "查询词太短，跳过比对"
        out["text"] = "历史字典\n  查询词太短，没有可比对的关键词。"
        return out

    found = {}
    for k in keys:
        for row in store.library_like(k, 40):
            sc = _score(k, row["name_key"])
            if sc <= 0:
                continue
            prev = found.get(row["path"])
            if prev and prev["score"] >= sc:
                continue
            row = dict(row)
            row["score"] = round(sc, 2)
            found[row["path"]] = row
        # 反向：字典里的名字更长、查询词只是它的一部分（如库有「FC2PPV-1234567 中字」）
        for tok in sorted(tokens(k), key=len, reverse=True)[:1]:
            for row in store.library_like(tok, 120):
                ik = row["name_key"]
                if len(ik) < 8 or len(ik) <= len(k) or ik.find(k) < 0:
                    continue
                sc = _score(k, ik)
                if sc and (row["path"] not in found or found[row["path"]]["score"] < sc):
                    row = dict(row)
                    row["score"] = round(sc, 2)
                    found[row["path"]] = row

    matches = sorted(found.values(),
                     key=lambda r: (r["status"] != STATUS_PRESENT, -r["score"],
                                    r.get("deleted_ts") or 0))
    # 命中的目录已经能代表里面的文件，别把同一个片子报四遍
    matches = _drop_nested(matches)
    present = [m for m in matches if m["status"] == STATUS_PRESENT]
    deleted = [m for m in matches if m["status"] == STATUS_DELETED]
    out["present"], out["deleted"] = len(present), len(deleted)
    out["matches"] = matches[:limit]
    out["strong"] = any(m["score"] >= 0.75 for m in matches)
    # 分开算：一条记录「强命中但在库里」和「强命中但已删除」要区别对待
    out["strong_present"] = any(m["score"] >= 0.75 for m in present)
    out["strong_deleted"] = any(m["score"] >= 0.75 for m in deleted)

    if present:
        out["state"] = "present"
        out["verdict"] = ("库里已有（另有 %d 条曾删除）" % len(deleted) if deleted
                          else "库里已有")
    elif deleted:
        out["state"] = "deleted"
        out["verdict"] = "曾存在，发现已删除"
    out["text"] = format_lookup(out)
    return out


def _fmt_ts(ts):
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def format_lookup(res):
    lines = ["历史字典"]
    lines.append("  结论：%s" % res["verdict"])
    if not res.get("matches"):
        lines.append("  没有比中的记录（已比对 %d 个关键词）" % res.get("checked_keys", 0))
        return "\n".join(lines)
    if res["state"] == "present" and res.get("strong"):
        lines.append("  → 命中已存在的文件，默认不再重复投递")
    elif res["state"] == "deleted" and res.get("strong_deleted", res.get("strong")):
        lines.append("  → 这些记录曾经入库、现在已不在目录里（记录自删除之日起保留）")
        lines.append("  → 与「已存在」同样处理：默认不再重复投递，要用 /force 才继续")
    lines.append("  条目：")
    for m in res["matches"]:
        if m["status"] == STATUS_PRESENT:
            mark = "✔ 存在"
        else:
            mark = "⌫ %s 发现已删除" % (_fmt_ts(m.get("deleted_ts")) or "?")
        lines.append("    [%s] %s" % (mark, m.get("name")))
        lines.append("      %s" % m.get("path"))
        info = []
        if m.get("size"):
            info.append(util.human_size(m["size"]))
        if m.get("first_seen"):
            info.append("首次记录 %s" % _fmt_ts(m["first_seen"]))
        if m["status"] == STATUS_DELETED and m.get("deleted_count"):
            info.append("累计消失 %d 次" % m["deleted_count"])
        if info:
            lines.append("      %s" % " · ".join(info))
    return "\n".join(lines)


def suggest_terms(plan, match):
    """从一次检索里凑出用于查字典的词：查询串、关键词、FC2 编号、标题首行。"""
    if match is not None and not isinstance(match, dict):
        match = match.to_dict()
    match = match or {}
    out = []
    for val in [match.get("query"),
                ((plan or {}).get("fc2") or {}).get("code"),
                ((plan or {}).get("fc2") or {}).get("raw")]:
        if val:
            out.append(str(val))
    for kw in match.get("keywords") or []:
        out.append(str(kw))
    text = (plan or {}).get("text") or ""
    if text:
        from . import intake
        title = intake.guess_title(text, [])
        if title:
            out.append(title[:120])
    return [t for t in dict.fromkeys(x.strip() for x in out if x and x.strip())]


# ---------------- 重复分析 ----------------
def _drop_nested(items):
    """目录与它内部的同名文件不是重复，去掉被包含的那一层。"""
    keep = []
    paths = [i["path"] for i in items]
    for item in items:
        p = item["path"] + os.sep
        if any(p.startswith(other + os.sep) for other in paths if other != item["path"]):
            continue
        keep.append(item)
    return keep


def duplicates(store, cfg=None, min_size_mb=0, limit=50):
    lib = ((cfg or {}).get("library") or {})
    try:
        min_size_mb = float(min_size_mb if min_size_mb is not None
                            else lib.get("dup_min_size_mb") or 0)
    except (TypeError, ValueError):
        min_size_mb = 0.0
    floor = int(min_size_mb * 1024 * 1024)

    groups = {}
    for row in store.library_present():
        key = row.get("base_key") or row.get("name_key") or ""
        if len(key) < 4:
            continue
        if row.get("kind") == KIND_FILE and (row.get("size") or 0) < floor:
            continue
        groups.setdefault(key, []).append(row)

    out = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        items = _drop_nested(items)
        if len(items) < 2:
            continue
        files = [i for i in items if i["kind"] == KIND_FILE]
        sizes = {i.get("size") or 0 for i in items}
        same_size = len(sizes) == 1 and all(s > 0 for s in sizes)
        biggest = max((i.get("size") or 0) for i in items)
        wasted = max(0, (biggest * (len(items) - 1)))
        out.append({
            "key": key,
            "name": items[0]["name"],
            "count": len(items),
            "files": len(files),
            "same_size": bool(same_size),
            "size": biggest,
            "size_human": util.human_size(biggest),
            "wasted": wasted,
            "wasted_human": util.human_size(wasted),
            "reason": ("同名同体积，基本可以确定重复" if same_size
                       else "名字相同，可能只是不同版本/清晰度"),
            "items": [{"path": i["path"], "kind": i["kind"],
                       "size": i.get("size") or 0,
                       "size_human": util.human_size(i.get("size") or 0),
                       "first_seen": i.get("first_seen") or 0} for i in items],
        })
    out.sort(key=lambda g: (g["count"] * g["size"], g["count"]), reverse=True)
    return {"ok": True, "groups": out[:limit], "total": len(out),
            "scanned": len(store.library_present()),
            "text": format_dups({"groups": out[:limit], "total": len(out)})}


def format_dups(res):
    lines = ["历史字典 · 疑似重复"]
    groups = res.get("groups") or []
    if not groups:
        lines.append("  没有发现明显重复。")
        return "\n".join(lines)
    lines.append("  共 %d 组；按可能白占的空间排序" % res.get("total", len(groups)))
    for g in groups:
        lines.append("")
        lines.append("  [%d 份] %s　%s" % (g["count"], g["name"], g["size_human"]))
        lines.append("    %s" % g["reason"])
        for it in g["items"][:6]:
            lines.append("    - %s（%s）" % (it["path"], it["size_human"]))
        if len(g["items"]) > 6:
            lines.append("    …还有 %d 条" % (len(g["items"]) - 6))
    return "\n".join(lines)


def format_report(rep):
    lines = ["历史字典"]
    if rep.get("error"):
        lines.append("  %s" % rep["error"])
        return "\n".join(lines)
    lines.append("  监控目录：%s" % ("、".join(rep.get("roots") or []) or "无"))
    lines.append("  本次扫描 %d 条（%.2fs）%s" % (
        rep.get("scanned", 0), rep.get("duration", 0),
        "，已达条目上限被截断" if rep.get("truncated") else ""))
    lines.append("  新增 %d · 消失 %d · 恢复 %d" % (
        rep.get("added", 0), rep.get("removed", 0), rep.get("restored", 0)))
    lines.append("  字典现有 %d 条：存在 %d，已删除 %d（记录保留）" % (
        rep.get("total", 0), rep.get("present", 0), rep.get("deleted", 0)))
    for w in rep.get("warnings") or []:
        lines.append("  ⚠ %s" % w)
    return "\n".join(lines)
