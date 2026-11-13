"""下载器工厂。"""
from __future__ import annotations

from .aria2 import Aria2Downloader
from .qbittorrent import QbittorrentDownloader
from .xunlei import XunleiDownloader

BACKENDS = {
    "xunlei": XunleiDownloader,
    "aria2": Aria2Downloader,
    "qbittorrent": QbittorrentDownloader,
}


def make(name: str, cfg: dict, proxy: str = ""):
    name = (name or "none").lower()
    if name == "none":
        return None
    klass = BACKENDS.get(name)
    if not klass:
        return None
    sub = (cfg.get("download") or {}).get(name) or {}
    return klass(sub, proxy=proxy)


def chain(cfg: dict) -> list:
    """按主用 + 兜底顺序返回下载器实例列表。

    代理只作为「下载器在外网」时的候选：各下载器自己会判断目标是本机/内网
    就直接连（见 qbittorrent.py / util.http_request），避免把 NAS 内网请求
    交给机场代理绕一圈再失败。
    """
    dl = cfg.get("download") or {}
    proxy = (cfg.get("telegram") or {}).get("proxy") or ""
    order = [dl.get("backend") or "xunlei"]
    fallback = dl.get("fallback")
    # 显式清空（[]）表示「不要兜底」；字段缺失才用默认兜底链
    if fallback is None:
        fallback = ["aria2", "qbittorrent"]
    for extra in fallback:
        if extra and extra not in order:
            order.append(extra)
    out = []
    for name in order:
        inst = make(name, cfg, proxy)
        if inst:
            out.append(inst)
    return out
