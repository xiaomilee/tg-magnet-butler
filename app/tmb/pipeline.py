"""业务主流程：解析 -> (直接投递 | 检索) -> 分析 -> 投递 -> 组织回复。

消息进来先过 intake：
* 自带磁力 → 直接投递，不联网；
* 有 FC2 → 只用编号去搜；
* 都没有 → 走 rules.json 的关键词分拣再搜。
"""
from __future__ import annotations

import logging
import random
import string
import time

from . import analyzer, downloaders, intake, library, nyaa, util
from .classifier import Match, Router
from .downloaders.xunlei import pan_auth_life, pick_note

log = logging.getLogger("tmb.pipeline")


def _rid(n=8):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


class Pipeline(object):
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.router = Router(cfg.rules, max_rules=1)
        # 令牌寿命相关的节流状态：别让每次投递都去扫一遍本机文件
        self._token_next_try = 0.0        # 下次允许「提前换新」的时间
        self._token_watch_at = 0.0        # 下次心跳检查时间
        self._token_warn_exp = None       # 已经为哪条令牌记过提醒（防止刷屏）

    def reload_rules(self):
        self.router.reload(self.cfg.rules)

    # ---------------- 检索 ----------------
    def search(self, query, category="0_0", filt="0", sort="seeders",
               rule=None, keywords=None, limit=None):
        nyaa_cfg = self.cfg.data.get("nyaa", {})
        search_cfg = self.cfg.data.get("search", {})
        rule = rule or {}
        limit = int(limit or rule.get("limit") or nyaa_cfg.get("page_size_hint") or 20)
        # 代理必须在这里解析：nyaa 段看不到 telegram.proxy，而 Config.get 支持点号路径
        proxy = self.cfg.get("nyaa.proxy") or self.cfg.get("telegram.proxy") or ""
        items = nyaa.search(nyaa_cfg, query,
                            category=category or nyaa_cfg.get("default_category", "0_0"),
                            filt=filt or nyaa_cfg.get("default_filter", "0"),
                            sort=sort or nyaa_cfg.get("default_sort", "seeders"),
                            limit=limit, proxy=proxy)
        result = analyzer.analyze(items, rule=rule, search_cfg=search_cfg,
                                  keywords=keywords or [],
                                  base_url=nyaa_cfg.get("base_url"))
        return result

    # ---------------- 消息定性 ----------------
    @staticmethod
    def _plan_brief(plan):
        """bundle 里只留判定摘要，磁力全文另存，别把整段磁力塞进历史记录。"""
        return {
            "mode": plan.get("mode"),
            "reason": plan.get("reason"),
            "fc2": plan.get("fc2"),
            "has_fc2": plan.get("has_fc2"),
            "bad_magnets": plan.get("bad_magnets") or [],
            "magnet_count": len(plan.get("magnets") or []),
        }

    def _rule_for(self, word):
        """找一条提到 word 的规则（FC2 用），好让它的分类/过滤条件继续生效。"""
        for rule in (self.cfg.rules.get("rules") or []):
            if not rule.get("enabled", True):
                continue
            m = rule.get("match") or {}
            blob = " ".join([rule.get("id") or "", rule.get("name") or ""]
                            + list(m.get("any") or []) + list(m.get("all") or [])
                            + list(rule.get("match_regex") or []))
            if word.lower() in blob.lower():
                return rule
        return None

    def fc2_match(self, plan):
        """FC2 分支的检索计划：query 只用编号，绝不把整段话丢给站点。"""
        code = (plan.get("fc2") or {}).get("code") or ""
        found = self._rule_for("fc2")
        rule = dict(found or {})
        if not found:
            rule.setdefault("id", "fc2")
            rule.setdefault("name", "FC2 编号检索")
        tpl = rule.get("query_template") or "{keywords}"
        query = tpl.replace("{kw1}", code).replace("{keywords}", code)
        if "{raw}" in query or not query.strip():
            # 模板想把整段原文丢出去：FC2 场景没意义，直接换成编号
            query = code
        query = " ".join(query.split()) or code
        matched = ["FC2"] + ([plan["fc2"]["raw"]] if plan.get("fc2") else [])
        return Match(rule, 1.0, [code], query, matched, is_default=not found)

    def match_for(self, plan, text):
        """判定完之后该用哪条规则、发什么查询。磁力直达不检索，由调用方先挡掉。"""
        if plan["mode"] == intake.MODE_FC2:
            return self.fc2_match(plan)
        return self.router.route(text)[0]

    def plan_parsed(self, text):
        """面板「解析后搜」用：先判定消息长得像什么，再算出真正要发给站点的查询。

        返回 ``{"plan": ..., "match": Match|None}``；自带磁力时 ``match`` 为 None
        （这条消息一步都不该往站点跑）。与 ``handle_text`` 共用同一套判定，
        免得「面板上试出来的结果」和「机器人真跑出来的结果」对不上。
        """
        plan = intake.parse(text, int(self.cfg.get("download.max_magnets") or 3))
        match = None if plan["mode"] == intake.MODE_MAGNET else self.match_for(plan, text)
        return {"plan": plan, "match": match}

    # ---------------- 历史字典 ----------------
    def library_check(self, plan, match):
        """检索前先问一句「这本下过吗」。没配目录或关掉开关就完全不查。"""
        if not self.cfg.get("library.check_search", True):
            return None
        if not (self.cfg.get("library.roots") or []):
            return None
        terms = library.suggest_terms(plan, match)
        if not terms:
            return None
        try:
            return library.lookup(self.store, terms, self.cfg.data)
        except Exception:
            log.exception("历史字典查询失败，本次忽略")
            return None

    def library_skip_state(self, hit):
        """命中字典后要不要拦。「已存在」和「曾存在已删除」各有一个开关。

        返回 None 表示放行；否则返回 "present" / "deleted" / "present,deleted"。
        两者都要求强匹配（名字基本对得上），弱匹配只提示不拦。
        """
        if not hit:
            return None
        hit = hit or {}
        reasons = []
        if hit.get("strong_present", hit.get("strong")) \
                and self.cfg.get("library.skip_download", True):
            reasons.append(library.STATUS_PRESENT)
        if hit.get("strong_deleted", hit.get("strong")) \
                and self.cfg.get("library.skip_deleted", True):
            reasons.append(library.STATUS_DELETED)
        return ",".join(reasons) or None

    # ---------------- 从一段话走完整流程 ----------------
    def auto_enabled(self, auto=None):
        """这一条消息该不该自动投递。auto 显式给值优先（/force、面板手动用）。"""
        return self.cfg.get("download.auto_download", True) if auto is None else auto

    def handle_text(self, text, chat_id=0, auto=None, force=False):
        plan = intake.parse(text, int(self.cfg.get("download.max_magnets") or 3))
        want = self.auto_enabled(auto)
        if plan["mode"] == intake.MODE_MAGNET:
            return self.handle_magnets(plan, chat_id, want=want)

        match = self.match_for(plan, text)
        rule = match.rule
        bundle = {
            "rid": _rid(),
            "ts": time.time(),
            "text": util.truncate(text, 500),
            "mode": plan["mode"],
            "plan": self._plan_brief(plan),
            "match": match.to_dict(),
            "query": match.query,
            "keywords": match.keywords,
            "items": [],
            "submitted": [],
            "error": None,
            "relaxed": False,
            "total": 0,
            "dropped": {},
            # 「有结果但没投」的两种原因，回复里要分得清是哪一种
            "auto_off": not want,
            "rule_auto_off": not rule.get("auto_download", True),
        }

        # 历史字典：库里已经有的、或者曾经有但已删掉的，都先告一声，
        # 省下一次检索和一次重复下载。force=True（/force 命令）时整段跳过。
        hit = None if force else self.library_check(plan, match)
        if hit:
            bundle["library"] = hit
        skip_state = self.library_skip_state(hit)
        if skip_state:
            bundle["skipped_library"] = True
            bundle["skipped_state"] = skip_state
            self.store.save_search(bundle["rid"], chat_id, match.query, match.rule_id, {
                "items": [], "query": bundle["query"], "match": bundle["match"],
                "library": {"state": hit["state"], "matches": [
                    {"path": m["path"], "status": m["status"]} for m in hit["matches"][:5]]},
            })
            first = hit["matches"][0]["path"] if hit["matches"] else ""
            if skip_state == library.STATUS_DELETED:
                self.store.add_event("library_hit", "%s 曾存在已删除，跳过投递（需 /force）：%s" % (
                    match.query, first))
            else:
                self.store.add_event("library_hit", "%s 已在库里，跳过投递：%s" % (
                    match.query, first))
            return bundle
        try:
            result = self.search(match.query,
                                 category=rule.get("category"),
                                 filt=rule.get("filter"),
                                 sort=rule.get("sort"),
                                 rule=rule,
                                 keywords=match.keywords)
            bundle["items"] = result["items"]
            bundle["relaxed"] = result["relaxed"]
            bundle["total"] = result["total"]
            bundle["dropped"] = result["dropped"]
        except Exception as e:
            log.exception("检索失败")
            bundle["error"] = "%s: %s" % (type(e).__name__, e)
            self.store.add_event("search_error", "%s | %s" % (match.query, e))
            return bundle

        # 自动投递
        want = self.auto_enabled(auto)
        if want and rule.get("auto_download", True) and bundle["items"]:
            pick_no = int(rule.get("pick") or self.cfg.get("download.auto_pick") or 1)
            item = analyzer.pick(bundle["items"], pick_no)
            if item:
                res = self.submit(item)
                res["index"] = pick_no
                bundle["submitted"].append(res)

        self.store.save_search(bundle["rid"], chat_id, match.query,
                               match.rule_id, {
                                   "items": bundle["items"][:10],
                                   "query": bundle["query"],
                                   "match": bundle["match"],
                               })
        self.store.add_event("search", "%s -> %d 条" % (match.query, len(bundle["items"])))
        return bundle

    # ---------------- 消息自带磁力：一步到位 ----------------
    def handle_magnets(self, plan, chat_id=0, want=True):
        """消息里已经有磁力，就别再去站点跑一趟，直接投递。

        ``want=False``（面板里关了「自动投递」）时**不投**，只把链接收下、挂出按钮。
        开关关着还往迅雷里塞任务，等于这个开关名不副实——用户关它的意思就是
        「别替我下」，而不是「检索结果别自动下、别人发的磁力照下」。
        """
        text = plan.get("text") or ""
        magnets = plan.get("magnets") or []
        fallback_title = intake.guess_title(text, magnets)
        wanted = []
        for i, mag in enumerate(magnets, 1):
            wanted.append({
                "index": i,
                "title": intake.title_from_magnet(mag) or fallback_title or "未命名磁力",
                "magnet": mag,
                "infohash": nyaa.magnet_hash(mag),
            })
        bundle = {
            "rid": _rid(),
            "ts": time.time(),
            "text": util.truncate(text, 500),
            "mode": intake.MODE_MAGNET,
            "plan": self._plan_brief(plan),
            "match": {"rule_id": "direct", "rule_name": "直接投递", "score": 0.0,
                      "keywords": [], "query": "", "matched": ["磁力"],
                      "is_default": False, "category": "0_0"},
            "query": "",
            "keywords": [],
            # 投出去了就不挂「⬇️ N」按钮（免得同一条被点第二次）；
            # 没投才挂，按钮就是「现在下这一条」的入口。
            "items": [] if want else wanted,
            "submitted": [],
            "auto_off": not want,
            "error": None,
            "relaxed": False,
            "total": len(magnets),
            "dropped": {},
            "ok": False,
        }
        if want:
            for it in wanted:
                res = self.submit(it)
                res["index"] = it["index"]
                bundle["submitted"].append(res)
            bundle["ok"] = any(s.get("ok") for s in bundle["submitted"])

        self.store.save_search(bundle["rid"], chat_id, "(磁力直达)", "direct", {
            "items": [{"title": it["title"], "magnet": it["magnet"],
                       "infohash": it["infohash"]} for it in wanted],
            "query": "",
            "match": bundle["match"],
            "plan": bundle["plan"],
        })
        self.store.add_event("direct", "磁力直达 %d 条 -> %s" % (
            len(magnets), "成功" if bundle["ok"] else
            ("自动投递已关闭，等确认" if not want else "失败")))
        return bundle

    # ---------------- 投递 ----------------
    # ---------------- 迅雷令牌寿命与自愈 ----------------
    def xunlei_token_state(self):
        """pan_auth 还剩多久——JWT 里写着，面板、心跳、投递前都看这一份。"""
        xcfg = (self.cfg.data.get("download") or {}).get("xunlei") or {}
        raw = str(xcfg.get("pan_auth") or "").strip()
        pan = util.clean_pan_auth(raw)
        if pan != raw:
            xcfg["pan_auth"] = pan        # 就地纠正，落盘交给 Config.save
            raw = pan
        life = pan_auth_life(raw)
        if not life:
            note = ("没读到 pan_auth（不是 JWT 或还没填）" if not raw else
                    "pan_auth 形状不对：迅雷令牌是 JWT（三段、点分隔），"
                    "现在这串像一整条地址——保存一次会自动掐出其中那段")
            return {"ok": False, "level": "", "text": "", "note": note}
        if life.get("exp") is None:
            return {"ok": True, "level": "unknown", "text": "",
                    "note": "这串令牌里没写过期时间，判断不了"}
        warn_h = float(xcfg.get("token_warn_hours") or 72)
        left = life.get("left")
        level = "ok"
        if life.get("expired"):
            level = "expired"
        elif left is not None and left <= warn_h * 3600:
            level = "soon"
        return {"ok": True, "level": level, "text": life.get("text") or "",
                "left": left, "exp": life.get("exp"),
                "expired": bool(life.get("expired")),
                "mode": xcfg.get("mode") or "fnos_gateway"}

    def rescan_xunlei_token(self, only_if_newer=False, timeout=2):
        """在本机重扫令牌并写回配置，返回 (是否换了, 一句话说明)。

        只在本机重扫（迅雷跟本程序装在同一台 NAS 上才有意义），拿到可用令牌才写配置，
        捞不到就保持原样——宁可报错，也不能把本来能用的配置覆盖成空的。
        only_if_newer=True 时还要求新令牌的过期时间更晚，避免把刚续好的换回旧的。
        """
        xcfg = (self.cfg.data.get("download") or {}).get("xunlei") or {}
        try:
            from . import xunlei_detect
            rep = xunlei_detect.scan(fnos_token=xcfg.get("fnos_token") or "",
                                     cfg=self.cfg.data, timeout=timeout)
        except Exception:
            log.exception("重扫迅雷令牌失败，忽略")
            return False, "本机重扫失败"
        if (rep.get("best") or {}).get("state") != "ok":
            log.warning("重扫令牌没成功：%s",
                        "；".join(rep.get("notes") or []) or "没有可用候选")
            return False, "本机没扫到能用的令牌"
        patch = xunlei_detect.patch_for(rep)
        # patch_for 会带出空字符串的 api_base / target；这是主动换令牌，不是重置配置，
        # 空值滤掉，免得顺手把用户填的设备空间 id 清空
        patch = {k: v for k, v in patch.items() if v != ""}
        # 只比 patch 真正给出的键：网关模式没有 port、面板模式没有 fnos_port，
        # 拿缺的键去比会把「令牌没变」误判成「变了」，白白多打一次请求。
        keys = [k for k in ("mode", "host", "pan_auth", "fnos_token",
                            "port", "fnos_port") if k in patch]
        if keys and all((xcfg.get(k) or "") == (patch.get(k) or "") for k in keys):
            log.info("重扫到的令牌与现有配置一致，不再重试")
            return False, "本机令牌与现有配置一样（迅雷那边还没换新的）"
        if only_if_newer:
            old = pan_auth_life(xcfg.get("pan_auth")).get("exp")
            new = pan_auth_life(patch.get("pan_auth")).get("exp")
            if old and new and new <= old:
                log.info("扫到的令牌过期时间没有更晚，保持原样")
                return False, "扫到的令牌不比现在的新"
        self.cfg.apply_patch({"download": {"xunlei": patch}})
        self.store.add_event("xunlei_relogin", "已自动换用本机令牌：%s %s:%s" % (
            xunlei_detect.mode_label(patch.get("mode")), patch.get("host"),
            patch.get("port") or patch.get("fnos_port")))
        life = pan_auth_life(patch.get("pan_auth"))
        note = "已换用本机找到的令牌" + ("（%s）" % life["text"] if life.get("text") else "")
        log.info("迅雷令牌已自动更新：%s", note)
        return True, note

    def heal_xunlei(self):
        """投递被拒「令牌失效」时的自愈：本机重抓一次并写回配置。返回是否真的更新了。"""
        xcfg = (self.cfg.data.get("download") or {}).get("xunlei") or {}
        if not xcfg.get("auto_refresh_token", True):
            return False
        changed, _ = self.rescan_xunlei_token(timeout=2)
        return changed

    def refresh_xunlei_token(self, force=False):
        """快到期时提前换新令牌，返回 (是否换了, 一句话说明)。

        迅雷不会把「续期」这个动作开放给我们，能做的只是：迅雷套件自己把令牌换新之后，
        从本机把它捞回来。所以在窗口内每隔 token_retry_hours 试一次，捞不到就静默等下次。
        """
        xcfg = (self.cfg.data.get("download") or {}).get("xunlei") or {}
        if not xcfg.get("auto_refresh_token", True) and not force:
            return False, ""
        st = self.xunlei_token_state()
        if not st.get("ok") or st.get("exp") is None:
            return False, ""
        now = time.time()
        if not force:
            watch_h = float(xcfg.get("token_watch_hours") or 48)
            left = st.get("left")
            if not st.get("expired") and left is not None and left > watch_h * 3600:
                return False, ""                      # 还早，别去打扰迅雷
            if now < self._token_next_try:
                return False, ""                      # 刚试过，别刷请求
        retry_h = float(xcfg.get("token_retry_hours") or 6)
        self._token_next_try = now + retry_h * 3600
        return self.rescan_xunlei_token(only_if_newer=True, timeout=3)

    def token_watch(self):
        """心跳里定时看一眼：快过期就提前提醒并试着换新，别等投递失败才发现。"""
        now = time.time()
        if now < self._token_watch_at:
            return
        self._token_watch_at = now + 1800
        st = self.xunlei_token_state()
        level = st.get("level") or ""
        if level not in ("soon", "expired"):
            self._token_warn_exp = None
            return
        expired = level == "expired"
        changed, note = self.refresh_xunlei_token()
        if changed:
            log.info("令牌%s，已提前换新：%s", "已过期" if expired else "快到期", note)
            self.store.add_event("xunlei_token_refreshed", "令牌%s，%s" % (
                st.get("text") or "", note))
            self._token_warn_exp = None
            return
        # 换不到新的：同一条令牌只提醒一次，别每分钟刷屏
        if st.get("exp") == self._token_warn_exp:
            return
        self._token_warn_exp = st.get("exp")
        log.warning("迅雷令牌%s（%s）：本机没捞到更新的令牌%s", "已过期" if expired else "快到期",
                    st.get("text"), "，下载会失败" if expired else "，到期后下载会失败")
        self.store.add_event(
            "xunlei_token_expired" if expired else "xunlei_token_soon",
            "迅雷令牌%s（%s）。本机没捞到更新的令牌，建议在飞牛桌面打开迅雷套件"
            "确认账号仍在线，再回面板点「自动侦测并连接」" % (
                st.get("text") or "", note or "重扫无结果"))

    def learn_xunlei_folder(self, cand):
        """投递用「已有任务里的目录」成功了：把这个 id 写进配置，下次不用再撞一遍。

        只做「记住一个迅雷刚刚接受过的值」，不在失败时改配置——
        之前那版会在降级后清空 id，等于替用户删设置，反而可能让本来能用的配置变空。
        """
        if isinstance(cand, str):
            cand = {"parent_folder_id": cand}
        if not isinstance(cand, dict):
            return False
        fid = str(cand.get("parent_folder_id") or "").strip()
        if not fid:
            return False
        xcfg = (self.cfg.data.get("download") or {}).get("xunlei") or {}
        if (xcfg.get("parent_folder_id") or "").strip() == fid:
            return False
        patch = {"parent_folder_id": fid}
        path = str(cand.get("parent_folder_path") or "").strip()
        if path:
            patch["parent_folder_path"] = path
        self.cfg.apply_patch({"download": {"xunlei": patch}})
        self.store.add_event("xunlei_folder",
                             "已记下迅雷可用的下载目录：%s" % (path or fid))
        log.info("已把迅雷可用的下载目录写进配置：%s", path or fid)
        return True

    def xunlei_filter_on(self):
        """文件筛选现在生效吗（= 没勾全选）。缺字段按「生效」算，和下载器一致。"""
        return not bool(self.cfg.get("download.xunlei.select_all_files", False))

    def _xunlei_in_use(self, backend=""):
        """这次投递会不会用到迅雷——只有用到才值得为令牌做预检。"""
        if backend:
            return str(backend).lower() == "xunlei"
        dl = self.cfg.data.get("download") or {}
        names = [dl.get("backend")]
        fallback = dl.get("fallback")
        if fallback is None:
            fallback = ["aria2", "qbittorrent"]
        names += list(fallback or [])
        return "xunlei" in [str(n).lower() for n in names if n]

    def torrent_url_for(self, infohash):
        """按 infohash 去站点找回 .torrent 地址（拿到了才能算出真实文件清单）。

        只有迅雷用得上：迅雷的 ``resource/list`` 有的版本对裸磁力只回一个目录节点，
        没有种子文件就又回到「1 个文件 → 全下」。查不到就算了，绝不影响投递。
        """
        infohash = (infohash or "").strip().upper()
        if len(infohash) != 40:
            return ""
        cache = self.__dict__.setdefault("_torrent_url_cache", {})
        if infohash in cache:
            return cache[infohash]
        cache[infohash] = ""
        proxy = self.cfg.get("nyaa.proxy") or self.cfg.get("telegram.proxy") or ""
        try:
            url = nyaa.torrent_url_by_hash(self.cfg.data, infohash, proxy=proxy)
        except Exception as e:  # noqa: BLE001 - 锦上添花，别拖垮投递
            log.info("按 infohash 找种子文件失败：%s", e)
            return ""
        if url:
            log.info("按 infohash 找到了种子文件：%s", url)
            cache[infohash] = url
        return cache[infohash]

    def submit(self, item, backend=None):
        magnet = item.get("magnet") or ""
        title = item.get("title") or ""
        infohash = item.get("infohash") or ""
        out = {
            "ok": False,
            "title": title,
            "infohash": infohash,
            "magnet": magnet,
            "backend": "",
            "task_id": "",
            "message": "",
            "deduped": False,
        }
        if not magnet:
            out["message"] = "缺少磁力链接"
            return out

        dedupe_days = int(self.cfg.get("app.dedupe_days") or 0)
        if infohash and self.store.seen_recently(infohash, dedupe_days):
            out["deduped"] = True
            out["message"] = "%d 天内已投递过同一资源，跳过" % dedupe_days
            return out

        # 投递前先看令牌还剩多久：快到期的先试着换新，省得第一条就因为 invalid token 失败
        token_note = ""
        if self._xunlei_in_use(backend):
            changed, token_note = self.refresh_xunlei_token()
            if not changed:
                token_note = ""

        chain = downloaders.chain(self.cfg.data)
        if backend:
            cfg_copy = dict(self.cfg.data)
            cfg_copy["download"] = dict(cfg_copy.get("download") or {})
            cfg_copy["download"]["backend"] = backend
            cfg_copy["download"]["fallback"] = []
            chain = downloaders.chain(cfg_copy)
        if not chain:
            out["message"] = "未配置下载器（download.backend）"
            return out

        errors = []
        healed = False
        i = 0
        # .torrent 地址是「文件筛选」的前提：迅雷对裸磁力有时只回一个目录节点，
        # 认出 1 个文件就等于没筛。搜索结果里现成带着；裸磁力则交给下载器按需回调
        # （只在它自己认不出清单时才去站点找，免得每条投递都多两次网络往返）。
        torrent_url = item.get("torrent_url") or ""
        resolver = None
        if infohash and self._xunlei_in_use(backend):
            resolver = lambda: self.torrent_url_for(infohash)   # noqa: E731
        dl_opts = {"torrent_url": torrent_url, "torrent_resolver": resolver}
        while i < len(chain):
            dl = chain[i]
            i += 1
            try:
                res = dl.add(magnet, title, dl_opts)
            except Exception as e:
                log.exception("投递异常")
                errors.append("%s: %s" % (dl.name, e))
                continue
            if res.ok:
                msg = res.message
                if getattr(dl, "_healed", False) is True:   # 只认明确标记过的那次重试
                    msg = "%s（本次令牌已自动更新）" % msg
                elif token_note and dl.name == "xunlei":
                    msg = "%s（%s）" % (msg, token_note)
                raw = res.raw or {}
                if dl.name == "xunlei":
                    if (raw.get("_folder_recovered")
                            and self.learn_xunlei_folder(raw["_folder_recovered"])):
                        msg = "%s（已把这个目录写进设置，下次直接用）" % msg
                    elif raw.get("_folder_fallback"):
                        msg = "%s（建议到面板把「下载目录 id」清空或改成实际目录）" % msg
                out.update(ok=True, backend=res.backend, task_id=res.task_id,
                           message=msg)
                # 多文件种子只下了其中几个：这句话必须跟到回执里，
                # 否则「筛选到底生效没有」只能去迅雷里数文件才看得出来
                if raw.get("_pick"):
                    out["pick"] = raw["_pick"]
                    out["pick_note"] = pick_note(raw["_pick"])
                # 筛选是配在迅雷下面的，换 aria2/qb 接管时它管不着——
                # 这种时候得说出来，不然「我明明设了只下视频」就成了空话
                if dl.name != "xunlei" and self.xunlei_filter_on():
                    msg += "（这次是 %s 下的，文件筛选只对迅雷生效，种子里有什么就下什么）" % dl.name
                    out["message"] = msg
                self.store.add_download(infohash, title, magnet, res.backend,
                                        res.task_id, "ok", msg)
                self.store.add_event("download", "%s -> %s" % (title[:60], res.backend))
                return out
            errors.append("%s: %s" % (res.backend or dl.name, res.message))
            log.warning("后端 %s 投递失败: %s", dl.name, res.message)
            # 令牌失效（最典型的是服务端只回一句 invalid token）：本机重抓一次令牌，
            # 拿新配置立刻重试同一个后端。只做一次，避免来回打请求。
            if (not healed and dl.name == "xunlei"
                    and (res.raw or {}).get("_auth_failed")):
                healed = True
                if self.heal_xunlei():
                    again = downloaders.make("xunlei", self.cfg.data,
                                             self.cfg.get("telegram.proxy") or "")
                    if again:
                        again._healed = True
                        chain.insert(i, again)

        out["message"] = "；".join(errors) or "未知错误"
        self.store.add_download(infohash, title, magnet, "", "", "failed", out["message"])
        self.store.add_event("download_failed", "%s | %s" % (title[:60], out["message"]))
        return out

    # ---------------- 文本组织 ----------------
    def format_direct(self, bundle):
        """消息自带磁力时的回复：不列站点结果，只报投递结果（或待确认）。"""
        plan = bundle.get("plan") or {}
        subs = bundle.get("submitted") or []
        pending = bundle.get("items") or []
        auto_off = bool(bundle.get("auto_off"))
        head = "🧲 <b>直接投递</b>　<i>消息自带磁力，跳过检索</i>"
        if plan.get("fc2"):
            head += "\n<i>另有 %s，本次用磁力、不用编号</i>" % util.esc(
                plan["fc2"].get("raw") or plan["fc2"].get("code") or "")
        if auto_off:
            head += ("\n⏸ <b>自动投递已关闭</b>，这几条<b>还没进迅雷</b>，点下方按钮才下")
        lines = [head, "📥 共 %d 条磁力" % (len(subs) or len(pending)), ""]
        if auto_off:
            for i, it in enumerate(pending, 1):
                lines.append("⏸ <b>%d.</b> %s" % (
                    i, util.esc(util.truncate(it.get("title"), 100)) or "未命名磁力"))
                if it.get("infohash"):
                    lines.append("　　<code>%s</code>" % util.esc(it["infohash"]))
                lines.append("　　↳ 待确认，点 <b>⬇️ %d</b> 投递" % i)
            lines.append("")
            lines.append("<i>想恢复「收到就下」，去面板概览页把「自动投递」打开。</i>")
        else:
            for i, s in enumerate(subs, 1):
                mark = "✅" if s.get("ok") else ("⏭ " if s.get("deduped") else "⚠️")
                lines.append("%s<b>%d.</b> %s" % (
                    mark, i, util.esc(util.truncate(s.get("title"), 100)) or "未命名磁力"))
                if s.get("infohash"):
                    lines.append("　　<code>%s</code>" % util.esc(s["infohash"]))
                if s.get("ok"):
                    lines.append("　　↳ <b>已投递</b> %s%s" % (
                        util.esc(s.get("backend") or ""),
                        (" · #" + util.esc(s.get("task_id"))) if s.get("task_id") else ""))
                    if s.get("pick_note"):
                        lines.append("　　↳ %s" % util.esc(s["pick_note"]))
                else:
                    lines.append("　　↳ %s" % util.esc(s.get("message") or "投递失败"))
            lines.append("")
            lines.append("成功 %d / %d" % (sum(1 for s in subs if s.get("ok")), len(subs)))
        for bad in plan.get("bad_magnets") or []:
            lines.append("⚠️ <i>有一条磁力没认出来（infohash 不完整）：</i><code>%s</code>"
                         % util.esc(bad))
        return "\n".join(lines)

    def force_hint(self, bundle):
        """拼一句能真正复现本次判定的 /force 命令。

        FC2 场景的 query 只剩一串数字，用户照抄回来会被当成普通搜索（数字还会被丢弃），
        所以要把 FC2 前缀带上；其余情况用 query 即可，太长就退回原文首句。
        """
        plan = bundle.get("plan") or {}
        if bundle.get("mode") == intake.MODE_FC2:
            code = (plan.get("fc2") or {}).get("code")
            if code:
                return "FC2 %s" % code
        q = (bundle.get("query") or "").strip()
        if not q:
            q = (bundle.get("text") or "").strip()
        return util.truncate(q, 60) or "关键词"

    def _format_library(self, lib, bundle):
        """把字典命中摊成三五行，别把整本字典吐出来。"""
        out = []
        present = [m for m in lib["matches"] if m.get("status") == "present"]
        gone = [m for m in lib["matches"] if m.get("status") != "present"]
        if present:
            out.append("📚 <b>库里已有</b>　<i>历史字典命中 %d 条</i>" % len(present))
            for m in present[:2]:
                out.append("　· %s" % util.esc(util.truncate(m.get("name"), 80)))
                out.append("　　<code>%s</code>" % util.esc(m.get("path")))
        if gone:
            out.append("🗑 <b>曾经有，现在没了</b>　<i>记录保留</i>")
            for m in gone[:2]:
                when = time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(m.get("deleted_ts") or 0)) \
                    if m.get("deleted_ts") else "?"
                out.append("　· %s　<i>%s 发现已删除</i>" % (
                    util.esc(util.truncate(m.get("name"), 80)), util.esc(when)))
                out.append("　　<code>%s</code>" % util.esc(m.get("path")))
        if bundle.get("skipped_library"):
            out.append("")
            hint = "确认要重下"
            if "deleted" in (bundle.get("skipped_state") or "") and not present:
                hint = "确认不是同一个文件、要重新下载"
            out.append("⏭ <i>已跳过检索与自动投递。%s，发</i> "
                       "<code>/force %s</code>" % (hint, util.esc(self.force_hint(bundle))))
        return out

    def format_bundle(self, bundle, max_items=None):
        if bundle.get("mode") == intake.MODE_MAGNET:
            return self.format_direct(bundle)

        m = bundle.get("match") or {}
        plan = bundle.get("plan") or {}
        lines = []
        if bundle.get("mode") == intake.MODE_FC2:
            head = "🧲 <b>FC2 · %s</b>" % util.esc(m.get("rule_name") or "编号检索")
        else:
            head = "🎯 <b>%s</b>" % util.esc(m.get("rule_name") or "分拣")
        if m.get("matched"):
            head += "　<i>命中：%s</i>" % util.esc("、".join(m["matched"][:3]))
        lines.append(head)
        lines.append("🔍 查询：<code>%s</code>" % util.esc(bundle.get("query") or "-"))
        if bundle.get("keywords"):
            lines.append("🧩 关键词：%s" % util.esc(" / ".join(bundle["keywords"][:6])))
        for bad in plan.get("bad_magnets") or []:
            lines.append("⚠️ <i>磁力没认出来（infohash 不完整），已按文本检索：</i>"
                         "<code>%s</code>" % util.esc(bad))

        lib = bundle.get("library") or {}
        if lib.get("matches"):
            lines.extend(self._format_library(lib, bundle))
        if bundle.get("skipped_library"):
            return "\n".join(lines)

        if bundle.get("error"):
            lines.append("")
            lines.append("❌ 检索失败：%s" % util.esc(bundle["error"]))
            return "\n".join(lines)

        items = bundle.get("items") or []
        lines.append("📦 命中 <b>%d</b> / 抓取 %d" % (len(items), bundle.get("total", 0)))
        if bundle.get("relaxed"):
            lines.append("<i>过滤条件偏严，已放宽展示</i>")
        # 有结果却一条没投：把「为什么没下」写在结果上方，别让人以为程序坏了
        if items and not bundle.get("submitted"):
            if bundle.get("auto_off"):
                lines.append("⏸ <b>自动投递已关闭</b>：只列结果，要点下方 ⬇️ 按钮才下载。")
            elif bundle.get("rule_auto_off"):
                lines.append("⏸ 规则「%s」关掉了自动投递：只列结果，要点下方 ⬇️ 按钮才下载。"
                             % util.esc(m.get("rule_name") or "当前规则"))
        if not items:
            lines.append("")
            lines.append("没有符合条件的结果。可以换个更短的关键词，或在管理面板里放宽规则。")
            return "\n".join(lines)

        limit = int(max_items or self.cfg.get("telegram.max_message_len", 3500) // 120)
        limit = max(1, min(len(items), limit, 8))
        sub_map = {s.get("index"): s for s in (bundle.get("submitted") or [])}
        lines.append("")
        for i, it in enumerate(items[:limit], 1):
            tag = "✅ " if sub_map.get(i, {}).get("ok") else ""
            # 🟢 对应站点列表里的绿底行（trusted 可信发布者）
            green = "🟢 " if it.get("trusted") else ""
            lines.append("%s<b>%d.</b> %s%s" % (
                tag, i, green, util.esc(util.truncate(it.get("title"), 110))))
            meta = "⬆️%d ⬇️%d · %s · %s" % (
                it.get("seeders", 0), it.get("downloads", 0),
                it.get("size_human") or "?", it.get("age_human") or "?")
            extra = ("　<i>%s</i>" % util.esc("，".join(it.get("reasons") or []))
                     if it.get("reasons") else "")
            lines.append("　　%s%s" % (meta, extra))
            s = sub_map.get(i)
            if s:
                if s.get("ok"):
                    lines.append("　　↳ <b>已投递</b> %s%s" % (
                        util.esc(s.get("backend")),
                        (" · #" + util.esc(s.get("task_id"))) if s.get("task_id") else ""))
                    if s.get("pick_note"):
                        lines.append("　　↳ %s" % util.esc(s["pick_note"]))
                elif s.get("deduped"):
                    lines.append("　　↳ ⏭ %s" % util.esc(s.get("message")))
                else:
                    lines.append("　　↳ ⚠️ %s" % util.esc(s.get("message")))
        if len(items) > limit:
            lines.append("")
            lines.append("…还有 %d 条，点下方按钮查看/投递" % (len(items) - limit))
        return "\n".join(lines)

    def keyboard(self, bundle, max_buttons=5):
        items = bundle.get("items") or []
        rid = bundle.get("rid")
        rows = []
        row = []
        for i in range(1, min(len(items), max_buttons) + 1):
            row.append({"text": "⬇️ %d" % i, "callback_data": "dl:%s:%d" % (rid, i)})
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        # 磁力直达的按钮只用来「补投」，没有可重搜的查询词，别把「重搜」挂上去
        if items and bundle.get("mode") != intake.MODE_MAGNET:
            rows.append([
                {"text": "🔁 重搜", "callback_data": "rs:%s" % rid},
                {"text": "📋 全部磁力", "callback_data": "mg:%s" % rid},
            ])
        return {"inline_keyboard": rows} if rows else None

    def magnet_list_text(self, bundle, max_items=6):
        items = (bundle.get("items") or [])[:max_items]
        if not items:
            return "没有可用的磁力链接"
        out = ["<b>磁力链接</b>（点一下即可复制/在迅雷 App 打开）"]
        for i, it in enumerate(items, 1):
            out.append("\n%d. %s" % (i, util.esc(util.truncate(it.get("title"), 80))))
            out.append("<code>%s</code>" % util.esc(it.get("magnet")))
        return "\n".join(out)
