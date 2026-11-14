"""配置加载/保存。

配置文件位于 fnOS 的应用配置目录（TRIM_PKGETC，即 etc/），
运行数据（sqlite、日志）位于 TRIM_PKGVAR（即 var/）。
"""
from __future__ import annotations

import os

from . import util

APP_NAME = "tgmagnet"
APP_TITLE = "磁力管家"

DEFAULT_CONFIG = {
    "telegram": {
        # 在 @BotFather 处创建机器人后拿到的 token
        "bot_token": "",
        # 允许使用的 chat_id 白名单；留空表示不限制（不推荐）
        "allowed_chat_ids": [],
        # 允许执行管理命令的用户 id；留空表示所有人可管理（不推荐）
        "admin_user_ids": [],
        "poll_timeout": 50,
        # 代理线路的第一条。面板上「代理」框里一行一条，第一行存在这里，
        # 后面的行存在 proxy_channels。留空 = 直连。
        "proxy": "",
        # 是否把搜索结果详情也发出来（关闭后只发最终投递结果）
        "verbose": True,
        "max_message_len": 3500,
        # 轮询撞 409 时，若查出这个 bot 被设过 webhook，自动清掉改回长轮询
        "auto_clear_webhook": True,
    },
    # 代理线路（第一行之后的那些）：面板上「代理」框一行一个地址，从上往下试。
    # 每条：{"name": "书房PC", "url": "http://192.168.1.10:7897", "enabled": true}
    # 不分主备——第一条不通就轮第二条，谁先通用谁。内网/本机地址永远直连，不进这个队列。
    "proxy_channels": [],
    # 自动切换：后台定期把整串线路测一遍，谁通用谁（只改内存里的顺序，不动配置）
    "proxy_auto": True,
    "nyaa": {
        "base_url": "https://sukebei.nyaa.si",
        "timeout": 15,
        # 1.0.40 起并入上面那一串线路，面板不再单独填（留空 = 跟线路表走）。
        # 老配置里还留着的值会被当成一条线路，检索站优先走它。
        "proxy": "",
        "page_size_hint": 20,
        "default_category": "0_0",
        # 0=全部 1=仅非重制 2=仅可信
        "default_filter": "0",
        # id/title/seeders/leechers/downloads/size/date
        "default_sort": "seeders",
        "trackers": [
            "http://nyaa.tracker.wf:7777/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://exodus.desync.com:6969/announce",
            "udp://tracker.torrent.eu.org:451/announce",
            "udp://open.demonii.com:1337/announce",
            "udp://tracker.tiny-vps.com:6969/announce",
            "udp://tracker.openbittorrent.com:6969/announce",
        ],
    },
    "search": {
        # 结果硬性过滤
        "min_seeders": 1,
        "min_size_mb": 0,
        "max_size_mb": 0,
        # 标题必须命中的关键词比例（0~1），0 表示不校验
        "title_match_ratio": 0.0,
        # 最多投递几个候选
        "max_candidates": 3,
        # 站点标记为可信发布者（列表里的绿底行）加多少分。
        # 绿底是「trusted 上传者」，不等于热门；做种仍占 40 分权重，这里只做微调。
        "trusted_bonus": 10,
    },
    "download": {
        # xunlei | aria2 | qbittorrent | none
        "backend": "xunlei",
        # 主用失败时依次尝试的兜底后端
        "fallback": ["aria2", "qbittorrent"],
        # 自动投递，否则只把磁力链接回传到 Telegram 由用户点开
        "auto_download": True,
        # 自动投递第几个候选（1 起）
        "auto_pick": 1,
        # 消息自带磁力时最多直投几条（一条消息里粘了多条磁力的情况）
        "max_magnets": 3,
        "xunlei": {
            # 只走飞牛官方迅雷套件：经 fnOS 网关 /cgi/ThirdParty/xunlei/index.cgi。
            # 1.0.31 起不再提供「2345 面板」这个选项——那台机器上根本没有独立面板，
            # 留着只会让人选错（选了就是 errno 111）。网关端口由启动时的自动侦测决定，
            # 所以这里只是「还没侦测过」的兜底值，不写进面板界面。
            "mode": "fnos_gateway",
            "host": "127.0.0.1",
            "fnos_port": 5666,
            # 迅雷的 pan_auth（浏览器开发者工具里拷，见文档）
            "pan_auth": "",
            "xla_ci": "",
            # 1.0.42：新版 fnOS 网关（迅雷套件 3.23.5 起）认的是 Cookie 里
            # fnwg_token + ost 这一对，老的 fnos-token 现在一出现就被整条拒
            # （回 13 字节 invalid token，静态页也不给）。整条 Cookie 串存这里，
            # 发请求时原样带上；fnos_token 留给还没升级的老飞牛。
            "fnos_cookie": "",
            # mode=fnos_gateway 时需要飞牛登录 token（老版飞牛用，新版见 fnos_cookie）
            "fnos_token": "",
            # 这串令牌是什么时候、从哪儿来的：browser（面板读到的浏览器 Cookie）/
            # detect（自动侦测写入）/ manual（手填）。只用来判断它是不是早过期了。
            "fnos_token_ts": 0,
            "fnos_token_source": "",
            # 设备空间标识，留空自动从 /drive/v1/tasks?type=user#runner 解析
            "target": "",
            # 目标目录 id / 路径，留空用迅雷默认下载目录
            "parent_folder_id": "",
            "parent_folder_path": "",
            # 多文件种子是否全选。false = 按下面的 file_filter 挑文件。
            # 这是文件筛选的唯一总闸，**对所有投递生效**：自动投递、按钮补投、
            # 直接发磁力链接都走同一份规则，没有哪条路能绕过去。
            "select_all_files": False,
            # 关掉全选后按什么挑文件。缺字段时按 downloaders.xunlei.DEFAULT_FILE_FILTER
            # 补齐（那边有一份同样的默认值，测试会比对两份是否一致）
            "file_filter": {
                # 类别开关：一个文件按扩展名只归一类
                "video": True,
                "subtitle": True,
                "image": False,
                "doc": False,
                "archive": False,
                "other": False,
                # 大小门槛（MB），0 = 不限。拿不到大小的文件不会被误删
                "min_size_mb": 100,
                "max_size_mb": 0,
                # 名称包含 / 排除，逗号分隔的片段，不区分大小写
                "name_include": "",
                "name_exclude": "",
                # 字幕和说明不受大小门槛限制（字幕天生几十 KB）
                "subtitle_exempt_size": True,
                # 规则把文件全筛掉时改回全选；关掉则宁可不投递
                "fallback_all": True,
            },
            "timeout": 30,
            # 令牌失效（invalid token）时，在本机自动重抓一次并重试；同机装迅雷才有效果
            "auto_refresh_token": True,
            # pan_auth 是 JWT，里面写着过期时间。剩不到这么多小时就在面板/日志提示
            "token_warn_hours": 72,
            # 剩不到这么多小时，每次投递前（及每半小时心跳里）试着从本机捞一次更新的令牌
            "token_watch_hours": 48,
            # 上面那个尝试的间隔：捞不到就隔这么久再试，别一直扫文件
            "token_retry_hours": 6,
            # 目录类失败时依次改试「默认目录 → 迅雷用过的目录」；成功不改配置，失败也不改
            "auto_fallback_folder": True,
            # 令牌被验证有效（磁力能解析）却还被拒时，去掉可选字段再投一次
            "auto_minimal_body": True,
        },
        "aria2": {
            "rpc_url": "http://127.0.0.1:6800/jsonrpc",
            "secret": "",
            "dir": "",
            "proxy": "",
        },
        "qbittorrent": {
            # 默认指向本机 WebUI；跨机器访问改成 http://NAS的IP:8080
            "url": "http://127.0.0.1:8080",
            "username": "admin",
            "password": "",
            "save_path": "",
            "category": "",
            "tags": "",
            # 添加后暂停（真正下载要手动开始），批量投递时用
            "paused": False,
            # 本机/内网地址会自动跳过代理，只有 qB 在外网时才需要填
            "proxy": "",
        },
    },
    "library": {
        # 要分析的历史目录，可填多个；子目录按 max_depth 往下走
        "roots": [],
        "max_depth": 3,
        "include_files": True,
        "max_items": 20000,
        "skip_hidden": True,
        # 这些名字的目录直接跳过（系统目录/回收站）
        "ignore_names": [],
        # 下载中间态不算入库
        "ignore_exts": [],
        # 检索前先查字典：命中「已存在」时提示，并可选择不重复投递
        "check_search": True,
        # 命中「已存在」→ 不检索不投递，要用 /force 才继续
        "skip_download": True,
        # 命中「曾存在、已删除」→ 同样拦下，要用 /force 才继续
        "skip_deleted": True,
        # 弱匹配（只是名字部分重合）只提示，不拦投递
        "dup_min_size_mb": 0,
    },
    "app": {
        "port": 8470,
        "bind": "0.0.0.0",
        # Web 管理面板访问口令，留空表示不校验（依赖 fnOS 网关鉴权）
        "access_token": "",
        "log_level": "INFO",
        # 同一 infohash 多少天内不重复投递
        "dedupe_days": 30,
    },
}


# 规则文件版本：内置规则有增减时 +1，老用户的 rules.json 会自动补齐缺的内置规则
RULES_VERSION = 2


def default_rules() -> dict:
    """内置规则示例。关键词分拣的核心：命中哪条规则，就用哪条规则的检索参数。"""
    return {
        "version": RULES_VERSION,
        "default": {
            "name": "通用兜底",
            "enabled": True,
            "query_template": "{keywords}",
            "category": "0_0",
            "filter": "0",
            "sort": "seeders",
            "limit": 5,
            "min_seeders": 1,
            "min_size_mb": 0,
            "max_size_mb": 0,
            "exclude_words": [],
            "prefer_words": [],
            "prefer_category": "",
            "auto_download": True,
            "pick": 1,
        },
        "rules": [
            {
                # FC2 走编号检索（见 intake.py），这条规则负责它的分类/过滤/去重口味
                "id": "fc2",
                "name": "FC2 编号",
                "enabled": True,
                "match": {"any": ["fc2"]},
                "match_regex": [r"fc2[\s\-_]*ppv?[\s\-_]*\d{5,8}"],
                "strip_words": ["fc2", "ppv", "求", "下载", "资源", "看看"],
                "query_template": "{kw1}",
                "category": "0_0",
                "filter": "0",
                "sort": "seeders",
                "limit": 8,
                "min_seeders": 1,
                "min_size_mb": 0,
                "max_size_mb": 0,
                "exclude_words": [],
                "prefer_words": ["fc2", "ppv"],
                "auto_download": True,
                "pick": 1,
            },
            {
                "id": "show",
                "name": "影视/剧集",
                "enabled": True,
                "match": {"any": ["剧", "电影", "动画", "番", "综艺", "纪录片"]},
                "match_regex": [],
                "strip_words": ["帮我下", "下载", "找一下", "求", "资源", "番剧", "新番",
                                "剧集", "电影", "动画", "综艺", "纪录片", "看看"],
                "query_template": "{keywords}",
                "category": "0_0",
                "filter": "0",
                "sort": "seeders",
                "limit": 5,
                "min_seeders": 2,
                "min_size_mb": 30,
                "max_size_mb": 0,
                "exclude_words": ["sample", "预告", "花絮"],
                "prefer_words": ["1080p", "中字", "简中", "合集", "BDRip"],
                "auto_download": True,
                "pick": 1,
            },
            {
                "id": "book",
                "name": "图书/漫画",
                "enabled": True,
                "match": {"any": ["书", "漫画", "本子", "画集", "杂志"]},
                "match_regex": [],
                "strip_words": ["帮我下", "下载", "找一下", "求", "漫画", "本子", "画集", "杂志"],
                "query_template": "{keywords}",
                "category": "0_0",
                "filter": "0",
                "sort": "seeders",
                "limit": 6,
                "min_seeders": 1,
                "min_size_mb": 0,
                "max_size_mb": 30000,
                "exclude_words": [],
                "prefer_words": ["vol", "全集", "中文"],
                "auto_download": True,
                "pick": 1,
            },
        ],
    }


def _migrate_rules(rules: dict):
    """老 rules.json 缺新内置规则时补上，用户自己的规则与顺序一律不动。"""
    try:
        ver = int(rules.get("version") or 1)
    except (TypeError, ValueError):
        ver = 1
    if ver >= RULES_VERSION:
        return rules, False
    existing = rules.setdefault("rules", [])
    have = {r.get("id") for r in existing if isinstance(r, dict)}
    added = []
    for rule in default_rules()["rules"]:
        if rule.get("id") not in have:
            existing.append(dict(rule))
            added.append(rule.get("id"))
    rules["version"] = RULES_VERSION
    return rules, bool(added)


class Config(object):
    def __init__(self, etc_dir: str, var_dir: str):
        self.etc_dir = etc_dir
        self.var_dir = var_dir
        self.config_path = os.path.join(etc_dir, "config.json")
        self.rules_path = os.path.join(etc_dir, "rules.json")
        self.data = util.deep_merge(DEFAULT_CONFIG, {})
        self.rules = default_rules()

    # ---------- 加载/保存 ----------
    def load(self):
        os.makedirs(self.etc_dir, exist_ok=True)
        os.makedirs(self.var_dir, exist_ok=True)
        raw = util.load_json(self.config_path, {}) or {}
        self.data = util.deep_merge(DEFAULT_CONFIG, raw)
        fixed = self._migrate_xunlei()
        # 首次启动就把默认配置落盘：否则 install_callback 里的 chmod 600 会落空，
        # 用户想手工改配置也找不到文件。此处只写 config，rules 还没开始解析。
        # 迁移真的改了东西（比如令牌形状不对被收拾干净）也要立刻落盘，
        # 否则下次重启又要重算一遍，面板上也一直显示那串坏值。
        if not raw or fixed:
            util.save_json(self.config_path, self.data, 0o600)
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
        rules = util.load_json(self.rules_path, None)
        if not isinstance(rules, dict) or "rules" not in rules:
            rules = default_rules()
            util.save_json(self.rules_path, rules, 0o644)
        else:
            rules, added = _migrate_rules(rules)
            if added:
                util.save_json(self.rules_path, rules, 0o644)
        self.rules = rules
        return self

    def save(self):
        util.save_json(self.config_path, self.data, 0o600)
        util.save_json(self.rules_path, self.rules, 0o644)

    def _migrate_xunlei(self):
        """1.0.31：删掉「2345 面板」这个模式。

        以前 ``mode`` 的默认值就是 ``panel``，很多人从没选过、却一直在打
        ``127.0.0.1:2345``——而机器上只有官方套件，那个端口没人监听，于是
        「连接被拒绝（errno 111）」→「未取到设备空间」，看起来像令牌坏了。
        现在只有一种接法（飞牛网关），端口交给启动时的自动侦测。

        1.0.41：``pan_auth`` 只留 JWT 本身。从浏览器 F12 里抄到的常常是整条
        ``…/device/info/watch?pan_auth=eyJ…&device_space=``；存成整条之后，
        网关会把这一串当 token 转给迅雷云，回
        ``token contains an invalid number of segments``——看着像过期，
        其实是形状不对。返回「有没有改动」，交给 :meth:`load` 决定要不要落盘。
        """
        x = (self.data.get("download") or {}).get("xunlei")
        if not isinstance(x, dict):
            return False
        changed = False
        if str(x.get("mode") or "").lower() != "fnos_gateway":
            x["mode"] = "fnos_gateway"
            changed = True
        if "port" in x:
            x.pop("port", None)
            changed = True
        raw_pan = str(x.get("pan_auth") or "").strip()
        pan = util.clean_pan_auth(raw_pan)
        if pan != raw_pan:
            x["pan_auth"] = pan
            changed = True
        # 1.0.42：新版飞牛（迅雷套件 3.23.5 起）不再认 fnos-token，改认
        # Cookie 里的 fnwg_token + ost；而且只要 fnos-token 这个名字还在，
        # 网关就整条拒。两件事都要在这里收干净。
        raw_xla = str(x.get("xla_ci") or "").strip()
        if ";" in raw_xla or "fnwg_token=" in raw_xla:
            # 1.0.41 的救急法：把 fnwg_token/ost 塞在 xla_ci 里（它也拼进 Cookie）。
            # 现在有正经字段了，拆出来、把 xla_ci 还原成纯值。
            ci, jar = "", []
            for bit in (b.strip() for b in raw_xla.split(";")):
                if not bit or "=" not in bit:
                    if bit and not ci:
                        ci = bit                     # 老写法：第一段就是 XLA_CI 的值
                    continue
                name = bit.split("=", 1)[0].strip().lower()
                if name == "xla_ci":
                    ci = bit.split("=", 1)[1].strip()
                elif name in ("fnwg_token", "ost", "osrt", "xtoken"):
                    jar.append(bit)
            x["xla_ci"] = ci
            if jar and not str(x.get("fnos_cookie") or "").strip():
                x["fnos_cookie"] = "; ".join(jar)
            changed = True
        raw_ck = str(x.get("fnos_cookie") or "")
        ck = util.clean_fnos_cookie(raw_ck)
        if ck != raw_ck:
            x["fnos_cookie"] = ck
            changed = True
        return changed

    def save_rules(self, rules: dict):
        self.rules = rules
        util.save_json(self.rules_path, rules, 0o644)

    # ---------- 取值 ----------
    def get(self, dotted: str, default=None):
        return util.get_path(self.data, dotted, default)

    def set(self, dotted: str, value):
        util.set_path(self.data, dotted, value)

    @property
    def log_dir(self) -> str:
        d = os.path.join(self.var_dir, "logs")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def db_path(self) -> str:
        return os.path.join(self.var_dir, "tmb.sqlite3")

    def public_view(self) -> dict:
        """给 Web UI 的配置视图，敏感字段脱敏为布尔标记。"""
        import copy

        view = copy.deepcopy(self.data)

        def mask(container, key):
            if isinstance(container, dict) and container.get(key):
                container[key] = "******"
                container[key + "_set"] = True

        mask(view.get("telegram", {}), "bot_token")
        mask(view.get("download", {}).get("xunlei", {}), "pan_auth")
        mask(view.get("download", {}).get("xunlei", {}), "fnos_token")
        mask(view.get("download", {}).get("xunlei", {}), "fnos_cookie")
        mask(view.get("download", {}).get("xunlei", {}), "xla_ci")
        mask(view.get("download", {}).get("aria2", {}), "secret")
        mask(view.get("download", {}).get("qbittorrent", {}), "password")
        mask(view.get("app", {}), "access_token")
        return view

    def apply_patch(self, patch: dict):
        """Web UI 提交的局部配置合并；'******' 视为保持原值不变。"""
        def clean(node):
            if isinstance(node, dict):
                return {k: clean(v) for k, v in node.items() if k != "_set"}
            if node == "******":
                return _KEEP
            return node

        patch = clean(patch or {})
        self._apply(self.data, patch)
        # 保存时顺手把令牌收拾成合法形状（整条 watch 地址 → 里面的 JWT）：
        # 面板上贴进去的那一刻就修好，不用等下次重启。
        self._migrate_xunlei()
        self.save()

    def _apply(self, target: dict, patch):
        for k, v in patch.items():
            if v is _KEEP:
                continue
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                self._apply(target[k], v)
            else:
                target[k] = v


class _Keep(object):
    def __repr__(self):
        return "<keep>"


_KEEP = _Keep()
