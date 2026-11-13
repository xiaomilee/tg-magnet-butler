"""日志：文件 + 内存环形缓冲（供 Web UI 查看）。"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity=800):
        super().__init__()
        self.capacity = capacity
        self._buf = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            # 注意：Handler.formatTime 在部分 Python 版本里已移除，这里自己格式化
            fmt = self.formatter
            if fmt is not None:
                ts = fmt.formatTime(record, "%m-%d %H:%M:%S")
            else:
                ts = time.strftime("%m-%d %H:%M:%S", time.localtime(record.created))
            msg = "%s [%s] %s" % (ts, record.levelname, record.getMessage())
            if record.exc_info:
                msg += "\n" + (fmt.formatException(record.exc_info)
                               if fmt is not None else "见文件日志")
            with self._lock:
                self._buf.append(msg)
        except Exception:
            self.handleError(record)

    def snapshot(self, lines=200):
        with self._lock:
            data = list(self._buf)
        return data[-lines:]

    def tail_text(self, lines=200) -> str:
        return "\n".join(self.snapshot(lines))


RING = RingBufferHandler()


def setup(log_dir: str, level: str = "INFO"):
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, "app.log"),
                                 maxBytes=4 * 1024 * 1024, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass

    RING.setFormatter(fmt)
    root.addHandler(RING)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
