"""从 .torrent 文件里读出真实文件清单。

为什么要绕这一道：迅雷的 ``POST /drive/v1/resource/list`` 在部分版本上对**磁力**只回一个
「根目录」节点，子文件要另外取，甚至根本取不到（磁力还没被云端缓存时）。那种时候程序
只看到 1 个条目，就不敢发 ``sub_file_index``，迅雷就把整个种子全下了——用户看到的现象
是「文件筛选完全没生效，全下载了」。

种子文件里的 ``info.files`` 是权威清单，顺序就是 BT 的文件顺序，也就是 ``sub_file_index``
用的序号。所以：先问迅雷（本地、快），它认不出完整清单时再拿 .torrent 顶上。

只用标准库：bencode 自己解，不引第三方包。
"""
from __future__ import annotations

import logging

from . import util

log = logging.getLogger("tmb.torrentfile")

MAX_TORRENT_BYTES = 8 * 1024 * 1024      # 正常种子几十 KB，8 MB 已经离谱


class BencodeError(ValueError):
    """种子文件不是合法 bencode。"""


def bdecode(data, _depth=0):
    """最小 bencode 解码：int / bytes / list / dict。

    返回的字符串一律是 ``bytes``（BT 里文件名常是 Shift-JIS / GBK，交给调用方决定怎么解）。
    """
    if _depth > 64:
        raise BencodeError("嵌套太深")

    def parse(i):
        if i >= len(data):
            raise BencodeError("数据提前结束")
        c = data[i:i + 1]
        if c == b"i":
            end = data.index(b"e", i)
            try:
                return int(data[i + 1:end]), end + 1
            except ValueError:
                raise BencodeError("整数格式不对")
        if c == b"l":
            i += 1
            out = []
            while data[i:i + 1] != b"e":
                v, i = parse(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i:i + 1] != b"e":
                k, i = parse(i)
                if not isinstance(k, bytes):
                    raise BencodeError("字典的键不是字符串")
                v, i = parse(i)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            sep = data.index(b":", i)
            try:
                n = int(data[i:sep])
            except ValueError:
                raise BencodeError("长度前缀不是数字")
            start = sep + 1
            if n < 0 or start + n > len(data):
                raise BencodeError("字符串越界")
            return data[start:start + n], start + n
        raise BencodeError("看不懂的标记 %r" % c)

    value, _end = parse(0)
    return value


def _text(raw) -> str:
    """种子里的名字解码：先 UTF-8（BT 规范），再 Shift-JIS / GB18030 兜底。

    顺序别乱：GB18030 几乎什么字节都能解，放前面会把 CP932 的名字解得半对半错
    （能出字、但是乱码），所以 CP932 先来，它解不动的才交给 GB18030。
    """
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw or "")
    for enc in ("utf-8", "cp932", "gb18030"):
        try:
            return bytes(raw).decode(enc)
        except UnicodeDecodeError:
            continue
    return bytes(raw).decode("utf-8", "replace")


def files_of(blob) -> list:
    """把种子内容解成 ``[{"name": "目录/文件.mkv", "file_size": 123}, …]``。

    顺序就是种子里的顺序（单文件种子返回一条）。解析不出来时抛 ``BencodeError``。
    """
    if isinstance(blob, str):
        blob = blob.encode("latin-1", "ignore")
    meta = bdecode(bytes(blob or b""))
    if not isinstance(meta, dict):
        raise BencodeError("顶层不是字典")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("没有 info 段")

    out = []
    multi = info.get(b"files")
    if isinstance(multi, list):
        for f in multi:
            if not isinstance(f, dict):
                continue
            parts = [_text(p) for p in (f.get(b"path") or []) if p not in (None, "")]
            size = f.get(b"length") or 0
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = 0
            name = "/".join([p for p in parts if p])
            if name:
                out.append({"name": name, "file_size": size})
    else:
        if b"length" not in info:
            raise BencodeError("既没有 files 也没有 length，不是合法种子")
        try:
            size = int(info.get(b"length") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"name": _text(info.get(b"name") or "未命名"), "file_size": size})
    if not out:
        raise BencodeError("种子里没有文件列表")
    return out


def fetch(url: str, proxy: str = "", timeout: int = 15):
    """下载 .torrent 并解出文件清单。失败抛异常，由调用方降级。

    这条路只在「迅雷自己认不出清单」时才走：宁可多花几秒把它认准，也别一声不响全下。

    两件事必须同时做到，否则这条路等于没用（实测）：

    * **超时给够 15 秒**：走代理连 nyaa 建连就要 10 秒上下，之前设 4~6 秒是必然失败；
    * **断了能续**（``util.http_download``）：常在半截断流
      （``IncompleteRead(16087 read, 5674 more expected)``），整下重来多少次都断在同一处，
      用 ``Range`` 把剩下的补上才拿得到完整种子。
    """
    if not url:
        raise ValueError("没有种子地址")
    raw = util.http_download(url, timeout=timeout, proxy=proxy, attempts=4,
                             max_bytes=MAX_TORRENT_BYTES)
    if not raw:
        raise BencodeError("种子文件是空的")
    return files_of(raw)


def files_from_url(url: str, proxy: str = "", timeout: int = 15) -> list:
    """``fetch`` 的不抛异常版本：拿不到就返回空列表，调用方按「没有清单」处理。"""
    try:
        return fetch(url, proxy=proxy, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - 这是锦上添花的路径，绝不该拖垮投递
        log.warning("读种子文件失败（%s）：%s", url[:80], e)
        return []
