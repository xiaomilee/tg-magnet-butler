#!/usr/bin/env python3
"""把 fnOS 安装/配置向导填写的 wizard_* 环境变量合并进 config.json。

用法：python3 wizard_apply.py <etc_dir> <var_dir>
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tmb import util                     # noqa: E402
from tmb.config import Config            # noqa: E402

# 向导字段 -> 配置路径
MAPPING = {
    "wizard_bot_token": "telegram.bot_token",
    "wizard_chat_ids": "telegram.allowed_chat_ids",
    "wizard_admin_ids": "telegram.admin_user_ids",
    "wizard_proxy": "telegram.proxy",
    "wizard_download_backend": "download.backend",
    "wizard_xunlei_mode": "download.xunlei.mode",
    "wizard_xunlei_host": "download.xunlei.host",
    "wizard_xunlei_port": "download.xunlei.port",
    "wizard_pan_auth": "download.xunlei.pan_auth",
    "wizard_fnos_token": "download.xunlei.fnos_token",
    "wizard_nyaa_base": "nyaa.base_url",
    "wizard_access_token": "app.access_token",
}

LIST_FIELDS = ("telegram.allowed_chat_ids", "telegram.admin_user_ids")
INT_FIELDS = ("download.xunlei.port",)


def main():
    etc = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TRIM_PKGETC", "./etc")
    var = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TRIM_PKGVAR", "./var")
    cfg = Config(etc, var).load()
    changed = []

    for env_key, path in MAPPING.items():
        # 向导字段可能带前缀大小写差异，这里两种都试
        raw = os.environ.get(env_key)
        if raw is None:
            raw = os.environ.get(env_key.upper())
        if raw is None or str(raw).strip() == "":
            continue
        val = str(raw).strip()
        if path in LIST_FIELDS:
            val = [x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
        elif path in INT_FIELDS:
            try:
                val = int(val)
            except ValueError:
                continue
        cfg.set(path, val)
        changed.append(path)

    if changed:
        cfg.save()
        print("已写入配置：" + ", ".join(changed))
    else:
        cfg.save()  # 至少保证默认配置文件落盘
        print("未检测到向导参数，已生成默认配置")

    if not os.path.exists(cfg.rules_path):
        util.save_json(cfg.rules_path, cfg.rules, 0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main())
