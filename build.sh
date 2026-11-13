#!/bin/bash
# 一键构建 fpk：./build.sh
set -e
cd "$(dirname "$0")"

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

if [ ! -f ICON_256.PNG ]; then
  echo "==> 生成图标"
  "$PY" tools/make_icons.py || echo "图标生成失败（缺少 Pillow？），跳过"
fi

echo "==> 打包"
"$PY" tools/build_fpk.py "$@"
