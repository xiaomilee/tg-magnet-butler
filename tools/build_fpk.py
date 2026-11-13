#!/usr/bin/env python3
"""把工程目录打包成飞牛 fnOS 可安装的 fpk（tar.gz）。

为什么不用 fnpack：fnpack 只能在飞牛机器上跑，且会校验一堆模板字段。
这里按官方包结构直接组装，跨平台、可进 CI：

    <fpk>
    ├── manifest
    ├── ICON.PNG / ICON_256.PNG
    ├── cmd/            (生命周期脚本，0755)
    ├── config/         (privilege / resource)
    ├── wizard/         (install / config)
    ├── tgmagnet.sc     (防火墙端口声明)
    └── app.tgz         (解压到 target/，即应用负载)

用法：python tools/build_fpk.py [--out dist]
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXECUTABLES = {"cmd/main", "cmd/install_init", "cmd/install_callback",
               "cmd/uninstall_init", "cmd/uninstall_callback",
               "cmd/upgrade_init", "cmd/upgrade_callback",
               "cmd/config_init", "cmd/config_callback",
               "app/main.py", "app/wizard_apply.py"}
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "dist", "build", "tests"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")


def read_manifest():
    data = {}
    with open(os.path.join(ROOT, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


# ---------------------------------------------------------------------------
# manifest 体检
#
# 这一节的阈值不是拍脑袋：1.0.11（changelog 单行 5775 字节）能正常覆盖安装，
# 1.0.12（6582 字节）起被飞牛应用中心拒收，同一份代码只改了 version 与 changelog，
# 所以上限就在这中间。取 1500 留足余量，别再靠「每版往后追加」把包写坏。
# ---------------------------------------------------------------------------
MAX_CHANGELOG_BYTES = 1500
MAX_MANIFEST_BYTES = 4096
REQUIRED_FIELDS = ["appname", "version", "display_name", "desc", "platform",
                   "service_port", "ctl_stop", "desktop_uidir",
                   "desktop_applaunchname"]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def check_manifest(manifest, raw):
    """返回问题列表。空列表才算过关。"""
    bad = []

    for key in REQUIRED_FIELDS:
        if not manifest.get(key):
            bad.append("缺少必填字段 %s" % key)

    ver = manifest.get("version", "")
    if not VERSION_RE.match(ver):
        # fnOS 不认后缀版本：1.0.2b6 → 1.0.2b7 会被判成同一版，直接拒绝升级
        bad.append("version=%r 不是纯数字三段式（x.y.z），飞牛会判成同一版本" % ver)
    else:
        parts = [int(x) for x in ver.split(".")]
        if parts[2] > 9999:
            bad.append("version 第三段 %d 过大" % parts[2])

    cl = (manifest.get("changelog") or "").encode("utf-8")
    if len(cl) > MAX_CHANGELOG_BYTES:
        bad.append("changelog 有 %d 字节，超过 %d 的安全上限；"
                   "只写最近两版摘要，完整历史放 docs/更新日志.md"
                   % (len(cl), MAX_CHANGELOG_BYTES))

    if len(raw) > MAX_MANIFEST_BYTES:
        bad.append("manifest 共 %d 字节，超过 %d" % (len(raw), MAX_MANIFEST_BYTES))

    if raw.startswith(b"\xef\xbb\xbf"):
        bad.append("manifest 带 UTF-8 BOM，安装器解析第一个字段会失败")
    if b"\r\n" in raw:
        bad.append("manifest 是 CRLF 行尾，飞牛按行取字段可能带上 \\r")
    for line in raw.decode("utf-8", "replace").split("\n"):
        if len(line.encode("utf-8")) > MAX_CHANGELOG_BYTES:
            bad.append("有一行长达 %d 字节（%s…），会被按行读取的实现截断"
                       % (len(line.encode("utf-8")), line[:24]))
            break
    return bad


def verify_package(path, manifest):
    """打完再验一遍：文件在不在、app.tgz 能不能解、入口有没有执行位。"""
    bad = []
    want = {"manifest", "app.tgz", "ICON.PNG", "ICON_256.PNG",
            "config/privilege", "config/resource", "tgmagnet.sc", "cmd/main"}
    with tarfile.open(path, "r:gz") as tar:
        names = set(tar.getnames())
        for w in sorted(want - names):
            bad.append("包里缺少 %s" % w)
        for mode_name in ("cmd/main", "cmd/install_init", "cmd/install_callback"):
            m = tar.getmember(mode_name) if mode_name in names else None
            if m is not None and not (m.mode & 0o111):
                bad.append("%s 没有执行权限（%o）" % (mode_name, m.mode))
        if "app.tgz" in names:
            blob = tar.extractfile("app.tgz").read()
            try:
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as inner:
                    inner_names = set(inner.getnames())
                    if "main.py" not in inner_names:
                        bad.append("app.tgz 里没有 main.py")
                    else:
                        ent = inner.getmember("main.py")
                        if not (ent.mode & 0o111):
                            bad.append("app.tgz/main.py 没有执行权限（%o）" % ent.mode)
                    # 面板/日志显示的版本号来自这个文件，必须和 manifest 一致，
                    # 否则又会出现「装了新版却显示旧版」的假象
                    if "tmb/_build_version.py" not in inner_names:
                        bad.append("app.tgz 里没有 tmb/_build_version.py，面板会显示 0.0.0-dev")
                    else:
                        got = inner.extractfile("tmb/_build_version.py").read().decode("utf-8")
                        want = manifest.get("version", "")
                        if ('"%s"' % want) not in got:
                            bad.append("app.tgz 里的版本号与 manifest（%s）不一致：%s"
                                       % (want, got.strip().splitlines()[-1]))
            except tarfile.TarError as e:
                bad.append("app.tgz 解不开：%s" % e)
    size = os.path.getsize(path)
    if size > 20 * 1024 * 1024:
        bad.append("包体 %.1f MiB 偏大，确认没把 dist/ 或日志打进去" % (size / 1048576.0))
    return bad


def _filter(tarinfo):
    name = os.path.basename(tarinfo.name)
    if name in SKIP_DIRS:
        return None
    if tarinfo.isdir():
        tarinfo.mode = 0o755
        tarinfo.uid = tarinfo.gid = 0
        tarinfo.uname = tarinfo.gname = "root"
        return tarinfo
    if any(name.endswith(s) for s in SKIP_SUFFIX):
        return None
    # app.tgz 内的路径是相对 app/ 的（如 main.py），而 EXECUTABLES 里写的是
    # app/main.py。两边都归一化后比对，否则解释器入口拿不到 0755。
    rel = tarinfo.name.replace("\\", "/").lstrip("./")
    cand = {rel, "app/" + rel}
    if any(c in EXECUTABLES for c in cand) or name.endswith(".sh"):
        tarinfo.mode = 0o755
    else:
        tarinfo.mode = 0o644
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = "root"
    return tarinfo


def add_tree(tar, base_dir, arc_prefix=""):
    for entry in sorted(os.listdir(base_dir)):
        if entry in SKIP_DIRS or entry.startswith(".tmp_"):
            continue
        full = os.path.join(base_dir, entry)
        arc = (arc_prefix + "/" + entry) if arc_prefix else entry
        if os.path.isdir(full):
            tar.add(full, arcname=arc, filter=_filter, recursive=True)
        else:
            tar.add(full, arcname=arc, filter=_filter)


def add_file(tar, rel_path):
    full = os.path.join(ROOT, rel_path)
    tar.add(full, arcname=rel_path, filter=_filter)


def write_build_version(version):
    """把 manifest 的 version 注入成 app/tmb/_build_version.py。

    版本号只能有一个来源。之前 ``tmb/__init__.py`` 里硬编码 1.0.11，之后每版都忘了改，
    面板、日志、HTTP Server 头全在骗人——用户看到旧版本号就以为覆盖安装没生效。
    """
    path = os.path.join(ROOT, "app", "tmb", "_build_version.py")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write('# 自动生成，勿手改（由 tools/build_fpk.py 按 manifest 写入）\n'
                'VERSION = "%s"\n' % version)
    return path


def build_app_tgz(out_path):
    """app/ 目录 -> app.tgz（解压后即 target/）"""
    app_dir = os.path.join(ROOT, "app")
    with tarfile.open(out_path, "w:gz", compresslevel=9) as tar:
        add_tree(tar, app_dir)
    return out_path


def build_fpk(out_dir):
    manifest = read_manifest()
    with open(os.path.join(ROOT, "manifest"), "rb") as f:
        raw_manifest = f.read()
    problems = check_manifest(manifest, raw_manifest)
    if problems:
        sys.exit("manifest 不合格，打包中止：\n  - " + "\n  - ".join(problems))

    appname = manifest.get("appname", "tgmagnet")
    version = manifest.get("version", "0.0.0")
    platform = manifest.get("platform", "x86")

    required = ["manifest", "ICON.PNG", "ICON_256.PNG", "app", "cmd",
                "config/privilege", "config/resource", "tgmagnet.sc"]
    missing = [r for r in required if not os.path.exists(os.path.join(ROOT, r))]
    if missing:
        sys.exit("缺少打包必需文件：%s" % ", ".join(missing))

    os.makedirs(out_dir, exist_ok=True)
    tmp_app_tgz = os.path.join(out_dir, "app.tgz")
    out_name = "%s_%s_%s.fpk" % (appname, version, platform)
    out_path = os.path.join(out_dir, out_name)

    ver_file = write_build_version(version)
    try:
        build_app_tgz(tmp_app_tgz)

        with tarfile.open(out_path, "w:gz", compresslevel=9) as tar:
            for rel in ("manifest", "ICON.PNG", "ICON_256.PNG", "tgmagnet.sc", "LICENSE"):
                if os.path.exists(os.path.join(ROOT, rel)):
                    add_file(tar, rel)
            for d in ("cmd", "config", "wizard"):
                if os.path.isdir(os.path.join(ROOT, d)):
                    add_tree(tar, os.path.join(ROOT, d), d)
            tar.add(tmp_app_tgz, arcname="app.tgz", filter=_filter)
    finally:
        # 生成物不进仓库：删掉后源码态回退到 0.0.0-dev
        try:
            os.unlink(ver_file)
        except OSError:
            pass
        try:
            os.unlink(tmp_app_tgz)
        except OSError:
            pass

    problems = verify_package(out_path, manifest)
    if problems:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        sys.exit("包体自检不通过，已删除该产物：\n  - " + "\n  - ".join(problems))

    size = os.path.getsize(out_path)
    print("构建完成：%s（%.1f KiB）" % (out_path, size / 1024.0))
    print("安装：飞牛 → 应用中心 → 手动安装 → 选择该文件")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = ap.parse_args()
    t0 = time.time()
    path = build_fpk(args.out)
    print("耗时 %.2fs" % (time.time() - t0))
    return 0 if path else 1


if __name__ == "__main__":
    sys.exit(main())
