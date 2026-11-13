"""磁力管家 tmb —— Telegram 关键词分拣 + sukebei 检索 + 迅雷投递。

版本号只有一个来源：打包时由 ``tools/build_fpk.py`` 按 ``manifest`` 的 version
生成本目录下的 ``_build_version.py``。以前这里是硬编码常量，1.0.12 之后忘了同步，
结果面板、日志、HTTP Server 头一直显示 1.0.11——用户以为新包没装上，其实早就是新的了。
"""

try:                                    # 打好的包：构建时注入的真实版本
    from ._build_version import VERSION as __version__
except ImportError:                     # 源码直接跑（开发态）
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]
