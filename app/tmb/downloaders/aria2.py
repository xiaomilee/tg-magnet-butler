"""aria2 JSON-RPC 适配器（兜底用，接口稳定、无账号门槛）。"""
from __future__ import annotations

import json
import logging

from .. import util
from .base import BaseDownloader, DownloadResult

log = logging.getLogger("tmb.aria2")


class Aria2Downloader(BaseDownloader):
    name = "aria2"

    def __init__(self, cfg: dict, proxy: str = ""):
        self.cfg = cfg or {}
        self.rpc = self.cfg.get("rpc_url") or "http://127.0.0.1:6800/jsonrpc"
        if "://" not in self.rpc:
            self.rpc = "http://" + self.rpc
        # aria2 基本都跑在 NAS 本机，别让代理接管内网请求
        self.proxy = "" if util.is_local_url(self.rpc) else str(
            self.cfg.get("proxy") or proxy or "").strip()

    def _call(self, method, params):
        secret = self.cfg.get("secret") or ""
        payload = {"jsonrpc": "2.0", "id": "tmb", "method": method, "params": params}
        if secret:
            payload["params"] = ["token:" + secret] + list(params)
        status, text, _ = util.http_request(
            self.rpc, method="POST", data=payload, timeout=20,
            proxy=self.proxy, retries=1)
        return json.loads(text) if text else {}

    def add(self, magnet, title="", options=None):
        opts = {}
        if self.cfg.get("dir"):
            opts["dir"] = self.cfg["dir"]
        if title:
            opts["out"] = title if not title.endswith(".torrent") else None
            opts = {k: v for k, v in opts.items() if v}
        try:
            resp = self._call("aria2.addUri", [[magnet], opts])
        except Exception as e:
            return DownloadResult(False, self.name, "", "aria2 请求失败: %s" % e)
        if resp.get("error"):
            return DownloadResult(False, self.name, "",
                                  "aria2 拒绝: %s" % resp["error"].get("message"), resp)
        gid = resp.get("result") or ""
        return DownloadResult(bool(gid), self.name, gid, "已投递 aria2", resp)

    def probe(self) -> dict:
        try:
            ver = self._call("aria2.getVersion", [])
            if ver.get("error"):
                return {"ok": False, "backend": self.name, "rpc": self.rpc,
                        "error": (ver["error"] or {}).get("message", "aria2 返回错误"),
                        "hint": "RPC 通了但被拒：多半是 secret 令牌不对"}
            return {"ok": bool(ver.get("result")), "backend": self.name,
                    "version": (ver.get("result") or {}).get("version", ""),
                    "rpc": self.rpc}
        except Exception as e:
            msg = str(e)
            if not self.proxy:
                cut = msg.find("。配了代理的话")
                if cut != -1:
                    msg = msg[:cut] + "。"
            return {"ok": False, "backend": self.name, "error": msg, "rpc": self.rpc,
                    "hint": "确认 aria2 已开 --enable-rpc、端口（默认 6800）与地址正确"}
