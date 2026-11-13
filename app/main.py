#!/usr/bin/env python3
"""磁力管家入口。

fnOS 生命周期脚本 cmd/main 会以 `python3 main.py` 启动本文件。
也支持本地调试：

    python3 main.py --env .            # 用当前目录当数据目录
    python3 main.py --once "求 某某 1080p"   # 只跑一次分拣+搜索，打印结果
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tmb import (__version__, downloaders, intake, library,  # noqa: E402
                 log as logmod, netdiag, nyaa, proxychan, util, xunlei_detect)
from tmb.bot import BotService                               # noqa: E402
from tmb.config import APP_NAME, Config                      # noqa: E402
from tmb.pipeline import Pipeline                            # noqa: E402
from tmb.store import Store                                  # noqa: E402
from tmb.webui import AppContext, WebUI                      # noqa: E402

import logging                                               # noqa: E402

log = logging.getLogger("tmb.main")


def resolve_dirs(env_root=None):
    """确定 etc / var / target 目录。

    优先使用 fnOS 注入的 TRIM_* 环境变量；本地调试用 --env 指定根目录。
    """
    if env_root:
        root = os.path.abspath(env_root)
        etc = os.path.join(root, "etc")
        var = os.path.join(root, "var")
        dest = os.path.abspath(HERE)
    else:
        etc = os.environ.get("TRIM_PKGETC") or os.path.join("/var/apps", APP_NAME, "etc")
        var = os.environ.get("TRIM_PKGVAR") or os.path.join("/var/apps", APP_NAME, "var")
        dest = os.environ.get("TRIM_APPDEST") or HERE
    for d in (etc, var):
        os.makedirs(d, exist_ok=True)
    return etc, var, dest


class App(object):
    def __init__(self, env_root=None):
        self.etc, self.var, self.dest = resolve_dirs(env_root)
        self.cfg = Config(self.etc, self.var).load()
        logmod.setup(self.cfg.log_dir, self.cfg.get("app.log_level") or "INFO")
        self.store = Store(self.cfg.db_path)
        self.pipeline = Pipeline(self.cfg, self.store)
        self.bot = BotService(self.cfg, self.store, self.pipeline)
        self.ui_dir = os.path.join(self.dest, "ui")
        self.ctx = AppContext(self.cfg, self.store, self.pipeline, self.bot, self.ui_dir)
        self.web = WebUI(self.ctx)
        self._stop = threading.Event()
        self._hb = None
        self.port_taken = False

    def start(self):
        log.info("磁力管家 v%s 启动，etc=%s var=%s", __version__, self.etc, self.var)
        try:
            self.web.start()
        except OSError as e:
            if self._port_taken(e):
                # 端口是天然的「单实例锁」。抢不到就说明已经有一个在跑——这时候再起
                # 机器人，两个进程会拿同一个 Bot Token 抢 getUpdates，Telegram 回 HTTP 409，
                # 消息还会被两边随机分掉。宁可这次不起，等人把旧的停掉。
                log.error(
                    "管理面板端口已被占用（%s）。本机很可能已经有一个磁力管家在跑——"
                    "多半是旧进程没退干净。为避免两个实例抢同一个 Bot Token（会报 HTTP 409、"
                    "消息随机丢给其中一个），本次不启动。请在应用中心「停止」再「启动」；"
                    "确实要同时跑两个，请给其中一个改 app.port。", e)
                self.port_taken = True
                return False
            log.error("管理面板端口占用或无法绑定: %s", e)

        if not self.bot.start():
            log.warning("机器人未启动：%s（可在面板里补配 token 后点重连）",
                        self.bot.last_error)

        self._hb = threading.Thread(target=self._heartbeat, name="hb", daemon=True)
        self._hb.start()
        # 开机就把迅雷接上：端口被改过、令牌换过，都不用等人进设置点按钮。
        threading.Thread(target=self._auto_connect_xunlei, name="xunlei-auto",
                         daemon=True).start()
        # 代理线路也自动选：主代理断电就顶到备用通道，恢复了再切回来
        threading.Thread(target=self._proxy_autotest, name="proxy-auto",
                         daemon=True).start()
        return True

    def _proxy_autotest(self):
        """定期测一遍主代理与备用通道，选出这次该走谁。

        只改内存里的顺序（``proxychan.Router``），**不碰配置**：用户填的主代理永远原样留着，
        它一恢复下一轮就会把它选回来。没配代理、或者关了「自动切换」，就什么都不做。
        """
        self._stop.wait(8)          # 先让服务起来；启动瞬间网络和磁盘都在忙
        while not self._stop.is_set():
            try:
                if self.cfg.get("proxy_auto") is not False and proxychan.has_lines(self.cfg):
                    rep = proxychan.autotest_config(self.cfg)
                    if rep.get("picked_kind"):
                        log.info("代理线路：%s", rep.get("text"))
            except Exception as e:   # noqa: BLE001 - 后台选路失败不该影响服务
                log.debug("自动测速失败：%s: %s", type(e).__name__, e)
            self._stop.wait(proxychan.AUTO_INTERVAL)

    def _auto_connect_xunlei(self):
        """启动时自动侦测并连接飞牛官方迅雷套件。

        只写「真打通」的结果，打不通时什么都不改，只在日志里留一句为什么——
        把原本能用的配置覆盖掉，比连不上更难查。失败不影响启动，用户还可以点「立即重连」。
        """
        if (self.cfg.get("download.backend") or "xunlei") != "xunlei":
            return
        self._stop.wait(3)          # 让面板/机器人先起来，别抢启动时的 IO
        try:
            res = xunlei_detect.autoconnect(self.cfg, self.ctx.fnos_token,
                                            self.ctx.fnos_port or None)
        except Exception as e:       # noqa: BLE001 - 自动连接失败不该拖垮服务
            log.warning("启动时自动连接迅雷失败：%s: %s", type(e).__name__, e)
            return
        port = res.get("port") or 0
        notes = (res.get("report") or {}).get("notes") or []
        if res.get("ok"):
            self.ctx.xunlei_conn = {"ok": True, "port": port,
                                    "text": "已连接 · 飞牛网关 :%s" % port,
                                    "ts": time.time()}
            log.info("迅雷自动连接成功：飞牛网关 :%s%s", port,
                     "，配置已更新" if res.get("applied") else "")
        else:
            tip = notes[0] if notes else "没有候选地址响应"
            self.ctx.xunlei_conn = {"ok": False, "port": port, "text": tip,
                                    "ts": time.time()}
            log.warning("迅雷自动连接没成功：%s（可在面板点「立即重连」）", tip)

    @staticmethod
    def _port_taken(err):
        """这个 OSError 是不是「端口被别人占了」。

        Linux 上是 EADDRINUSE（98）；Windows 上占用端口常报的是 WSAEACCES（10013，
        消息写着「访问权限不允许」），也是占用。注意别把 Linux 上「绑定 1024 以下端口
        没权限」的 EACCES 当成占用——那个是我们自己用错了端口，不是有别人在跑。
        """
        if getattr(err, "errno", None) in (errno.EADDRINUSE, 10048):
            return True
        if getattr(err, "winerror", None) in (10013, 10048):
            return True
        text = str(err).lower()
        return any(k in text for k in ("address already in use",
                                       "only one usage of each socket address",
                                       "以一种访问权限不允许的方式", "10013"))

    def _heartbeat(self):
        path = os.path.join(self.var, "status.json")
        while not self._stop.is_set():
            # 迅雷令牌快到期就提前提醒/换新：别等投递失败才发现（内部有节流）
            try:
                self.pipeline.token_watch()
            except Exception:
                log.exception("检查迅雷令牌寿命失败，忽略")
            try:
                util.save_json(path, {
                    "pid": os.getpid(),
                    "version": __version__,
                    "ts": time.time(),
                    "bot_running": bool(self.bot._thread and self.bot._thread.is_alive()),
                    "bot_username": (self.bot.me or {}).get("username", ""),
                    "bot_error": self.bot.last_error,
                    "uptime": int(time.time() - self.ctx.started_at),
                }, 0o644)
            except OSError:
                pass
            self._stop.wait(20)

    def run_forever(self):
        if not self.start():
            log.error("启动中止（端口被占用），退出。")
            return 3

        def _sig(signum, frame):
            log.info("收到信号 %s，准备退出", signum)
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _sig)
            except (ValueError, OSError):
                pass

        while not self._stop.is_set():
            self._stop.wait(1)

        self.shutdown()

    def shutdown(self):
        log.info("正在停止…")
        try:
            self.bot.stop()
        except Exception:
            pass
        try:
            self.web.stop()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        log.info("已退出")


def main(argv=None):
    ap = argparse.ArgumentParser(description="磁力管家 tmb")
    ap.add_argument("--env", default=None, help="本地调试数据目录（含 etc/ 和 var/）")
    ap.add_argument("--once", default=None, help="只跑一次：输入一段文字，打印分栏结果")
    ap.add_argument("--no-auto", action="store_true", help="配合 --once，不自动投递")
    ap.add_argument("--doctor", action="store_true",
                    help="网络诊断：逐层检查 DNS / TCP / 代理 / TLS，打印结论")
    ap.add_argument("--grab", default=None,
                    help="直接检索关键词并打印磁力链接（不走规则、不做过滤，用来核对解析）")
    ap.add_argument("--limit", type=int, default=5, help="--grab 时打印几条，默认 5")
    ap.add_argument("--parse", default=None, metavar="TEXT",
                    help="只看一条消息会被怎么处理：磁力直达 / FC2 编号 / 整段检索")
    ap.add_argument("--force", action="store_true",
                    help="配合 --parse --send：无视历史字典强制检索投递")
    ap.add_argument("--send", action="store_true",
                    help="配合 --parse：真的执行（会检索并投递，不加只解析不联网）")
    ap.add_argument("--probe", nargs="?", const="__default__", default=None,
                    help="测试下载器连通：--probe qbittorrent / aria2 / xunlei（省略名字则测主用后端）")
    ap.add_argument("--detect", action="store_true",
                    help="自动侦测飞牛官方迅雷套件（网关），打印候选与缺失的令牌")
    ap.add_argument("--apply", action="store_true", help="配合 --detect：把最佳候选写进配置并复测")
    ap.add_argument("--xunlei-folder", action="store_true",
                    help="列出迅雷自己用过的下载目录 id（配合 --apply 写进配置）")
    ap.add_argument("--lib-scan", action="store_true",
                    help="分析历史字典目录：记录文件/文件夹名，标记消失的（记录保留）")
    ap.add_argument("--lib-lookup", default=None, metavar="TEXT",
                    help="查历史字典：这个关键词是不是已经下过 / 曾下过")
    ap.add_argument("--lib-dups", action="store_true", help="分析历史字典里疑似重复的文件")
    ap.add_argument("--roots", default=None,
                    help="配合 --lib-scan：临时指定监控目录，逗号分隔")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)

    if args.version:
        print("tmb " + __version__)
        return 0

    if args.doctor:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        report = netdiag.run(cfg.data)
        print(netdiag.format_report(report))
        ok = not any(level == "error" for level, _ in report.get("verdict") or [])
        return 0 if ok else 1

    if args.detect:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        rep = xunlei_detect.scan(cfg=cfg.data)
        print(xunlei_detect.format_report(rep))
        if args.apply and rep.get("best"):
            patch = xunlei_detect.patch_for(rep)
            shown = {k: (xunlei_detect.mask(v) if k in ("pan_auth", "fnos_token") else v)
                     for k, v in patch.items()}
            if rep["best"]["state"] != "ok":
                # 没打通就别覆盖现有配置，只把建议打出来
                print("\n未写入（探测没打通）。建议配置：%s"
                      % json.dumps(shown, ensure_ascii=False))
                return 1
            cfg.apply_patch({"download": {"xunlei": patch}})
            print("\n已写入配置：%s" % json.dumps(shown, ensure_ascii=False))
            inst = downloaders.make("xunlei", cfg.data, cfg.get("telegram.proxy") or "")
            res = inst.probe() if inst else {}
            print("复测：%s" % json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        return 0 if rep.get("ok") else 1

    if args.xunlei_folder:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        inst = downloaders.make("xunlei", cfg.data, cfg.get("telegram.proxy") or "")
        if not inst:
            print("迅雷下载器不可用")
            return 2
        cur = str(cfg.get("download.xunlei.parent_folder_id") or "")
        print("当前配置的下载目录 id：%s" % (cur or "(空，用迅雷默认目录)"))
        cands = inst.folder_candidates(refresh=True)
        if not cands:
            print("没从任务列表里找到目录 id。")
            print("可以在迅雷面板 → 文件 → 选中目录，从地址栏 parent_folder_id= 后面复制；"
                  "或者留空，让任务落到迅雷默认目录。")
            return 1
        print("迅雷自己用过的下载目录（按出现次数）：")
        for i, c in enumerate(cands, 1):
            print("  %d. %s    id=%s    %d 次" % (
                i, c.get("parent_folder_path") or "(无路径)",
                c["parent_folder_id"], c["hits"]))
        if args.apply:
            best = cands[0]
            cfg.apply_patch({"download": {"xunlei": {
                "parent_folder_id": best["parent_folder_id"],
                "parent_folder_path": best.get("parent_folder_path") or ""}}})
            print("已写入配置：%s（id=%s）" % (
                best.get("parent_folder_path") or "-", best["parent_folder_id"]))
            return 0
        print("加 --apply 就把第一条写进配置。")
        return 0

    if args.probe:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        name = cfg.get("download.backend") or "xunlei"
        if args.probe != "__default__":
            name = args.probe
        inst = downloaders.make(name, cfg.data, cfg.get("telegram.proxy") or "")
        if not inst:
            print("未知下载器：%s（可选 xunlei / aria2 / qbittorrent / none）" % name)
            return 2
        res = inst.probe()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("hint"):
            # 连通正常但目录 id 填错这类问题也要提示，不能只在 ok=false 时才说
            print("\n建议：%s" % res["hint"])
            if name == "xunlei" and not res.get("ok"):
                print("也可以跑一次 --detect --apply：重新识别迅雷并抓取最新令牌。")
        return 0 if res.get("ok") else 1

    if args.lib_scan or args.lib_lookup is not None or args.lib_dups:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        store = Store(cfg.db_path)
        try:
            if args.lib_lookup is not None:
                res = library.lookup(store, [args.lib_lookup], cfg.data)
                print(res["text"])
                return 0 if res["state"] != "none" else 1
            if args.lib_dups:
                res = library.duplicates(store, cfg.data)
                print(res["text"])
                return 0
            roots = None
            if args.roots:
                roots = [r.strip() for r in args.roots.replace("，", ",").split(",")
                         if r.strip()]
                cfg.set("library.roots", roots)
                cfg.save()
            rep = library.scan(cfg, store, roots=roots)
            print(rep["text"])
            return 0 if rep.get("ok") else 1
        finally:
            store.close()

    if args.parse is not None:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        plan = intake.parse(args.parse, int(cfg.get("download.max_magnets") or 3))
        print(intake.describe(plan))
        store = Store(cfg.db_path)
        pipe = Pipeline(cfg, store)
        try:
            if plan["mode"] == intake.MODE_FC2:
                print("将发出的查询：%s" % pipe.fc2_match(plan).query)
            if not args.send:
                print("\n（只解析，不联网。加 --send 会真的检索并投递）")
                return 0
            # --send 是人明确要求「现在就投」，不受面板里那个自动投递开关约束
            bundle = pipe.handle_text(args.parse, 0, force=args.force, auto=True)
            print("\n" + pipe.format_bundle(bundle))
            if bundle.get("mode") == intake.MODE_MAGNET:
                return 0 if bundle.get("ok") else 1
            return 1 if bundle.get("error") else 0
        finally:
            store.close()

    if args.grab:
        etc, var, _dest = resolve_dirs(args.env)
        cfg = Config(etc, var).load()
        try:
            items = nyaa.search(cfg.data, args.grab, limit=max(1, args.limit))
        except Exception as e:
            print("检索失败：%s" % e)
            return 1
        print("查询：%s　拿到 %d 条（🟢 = 站点绿底行 / 可信发布）\n" % (args.grab, len(items)))
        for i, it in enumerate(items, 1):
            print("%s%d. %s" % ("🟢" if it.get("trusted") else "  ", i, it.get("title")))
            print("    做种 %d　吸种 %d　完成 %d　%s　%s" % (
                it.get("seeders", 0), it.get("leechers", 0), it.get("downloads", 0),
                it.get("size_text") or "?", it.get("category_id") or "-"))
            print("    磁力 %s" % (it.get("magnet") or "(无)"))
            if it.get("torrent_url"):
                print("    种子 %s" % it["torrent_url"])
            print()
        return 0

    if args.once:
        app = App(args.env)
        bundle = app.pipeline.handle_text(args.once, chat_id=0,
                                          auto=False if args.no_auto else None)
        print(app.pipeline.format_bundle(bundle))
        if bundle.get("submitted"):
            for s in bundle["submitted"]:
                print("\n[投递] %s" % s)
        return 0

    app = App(args.env)
    rc = app.run_forever()
    return rc if rc else 0


if __name__ == "__main__":
    sys.exit(main())
