"""下载器适配层。"""
from __future__ import annotations


class DownloadResult(object):
    def __init__(self, ok, backend="", task_id="", message="", raw=None):
        self.ok = ok
        self.backend = backend
        self.task_id = task_id
        self.message = message
        self.raw = raw or {}

    def to_dict(self):
        return {
            "ok": self.ok,
            "backend": self.backend,
            "task_id": self.task_id,
            "message": self.message,
        }

    def __repr__(self):
        return "<DownloadResult ok=%s backend=%s task=%s msg=%s>" % (
            self.ok, self.backend, self.task_id, self.message)


class BaseDownloader(object):
    name = "base"

    def add(self, magnet: str, title: str = "", options=None) -> DownloadResult:
        raise NotImplementedError

    def probe(self) -> dict:
        raise NotImplementedError
