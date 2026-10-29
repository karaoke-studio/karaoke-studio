"""系统字体补注册：让 Qt 字体库看不见的已安装字体也能渲染。

Qt 6 的 Windows 字体库登记系统字体族时只认两个 locale 的名字
（``GetUserDefaultLocaleName()`` 与 ``en-us``，见
``qwindowsdirectwritefontdatabase.cpp`` 的 ``populateFontDatabase`` /
``localeString``）。名字表里只有其它 locale 的字体——典型是一些只有 ja-JP
记录的旧日文字体——整族不会进 ``m_populatedFonts``，之后 ``QFontDatabase`` /
``QFont`` 一律解析不到，``addApplicationFont`` 也对它返回 -1。而 DirectWrite /
GDI 本身完全能渲染这些 face（``QRawFont`` 直接从文件读得到字形），缺的只是
「让 Qt 认领它」。

本模块把这类字体补成应用字体：

1. 从注册表（HKLM + HKCU）收集已安装字体文件——安装语义的权威来源，也覆盖
   Win10 1809+ 的每用户安装。字体枚举本身复用仓库既有口径：Qt 快照取
   ``QFontDatabase.families()``（与 ``font_cache.installed_families()`` 同源），
   注册表声明名用来做第一层比对；
2. **先用名字比对，再开文件**：注册表声明名（含去掉 ``Bold``/``Italic`` 等尾部
   修饰词后的前缀）全部已能被 Qt 解析的文件直接跳过——常见机器上 347 个文件
   只剩几十个需要打开。只有活下来的文件才解析 ``name`` 表确认是否真不可见。
   旧字体常见两种编码错标：platform 3 标成 Shift-JIS 但字节其实是 UTF-16BE
   （如调试用的 FG平成明朝体），以及字节确实是 Shift-JIS/GBK——两种都要能读；
3. 对不可见的 face 补写 Windows Unicode（platform 3 / encoding 1）名字记录。
   保留原族名、原记录与全部轮廓表：fontTools 懒加载只反编译 ``name``，
   其余表保存时按原始字节复制（``glyf`` 逐字节不变）；
4. ``QFontDatabase.addApplicationFontFromData`` 注册，最后失效字体目录 /
   字形能力 / 缺字回退三层缓存。

为什么不直接复用 SUG 的 ``font_names.localized_alias_map``：那份统一扫描只把
**有英文名**的字体收进映射（``_merge_records`` 以英文名为键），本文要救的
FG平成明朝体正好没有英文名，会被整条丢弃；而且它不返回文件路径，无法据此
注册应用字体。所以这里沿用同一套「注册表 + name 表」口径，只补它没覆盖的
两个能力（路径与无英文名字体），并把解析量压到名字比对后的差集。


注册后的字体对 Qt 是一等公民：``families()`` 可见、``QFontInfo`` 精确命中、
``QRawFont`` 拿得到真实 face，字幕渲染的 ``font_capabilities``、字重解析、
内容寻址快照与 GPU sidecar 全部沿用现有链路（快照里就是原始轮廓字节）。

线程约定：文件解析与 name 表改写不碰 Qt，可以放在后台线程；注册与
``QFontDatabase`` 查询必须留在 GUI 线程（EMBEDDING §8），非 GUI 线程调用
直接返回 ``skipped_reason``。
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import unicodedata
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

log = logging.getLogger(__name__)

#: 可解析的字体文件后缀；``.ttf`` 里也可能装的是 TTC（见 ``_is_collection``）。
_FONT_EXTENSIONS = frozenset({".ttf", ".ttc", ".otf", ".otc"})

_FONTS_REG_SUBKEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

#: OpenType name 表：Windows platform 与 Unicode 编码。
_PLATFORM_WINDOWS = 3
_UNICODE_ENCODINGS = frozenset({0, 1, 10})

#: platform 3 的旧编码记录按 languageID 猜 ANSI 代码页。
_ANSI_CODECS = {
    0x0411: "cp932",  # 日语
    0x0412: "cp949",  # 韩语
    0x0804: "cp936",  # 简体中文
    0x0404: "cp950",  # 繁体中文
    0x0C04: "cp950",
    0x1004: "cp936",
}

#: 族名候选 nameID：legacy / typographic / WWS 三套模型。
_FAMILY_NAME_IDS = (1, 16, 21)

#: 补写的 locale：en-US 是 Qt 的固定兜底，zh-CN 让中文界面显示可读族名。
_TARGET_LANGUAGES = (0x0409, 0x0804)

#: 注册表值名尾部的类型标注，如 ``(TrueType)`` / ``(OpenType)``。
_TYPE_SUFFIX = re.compile(r"\s*[（(][^（()）]*[）)]\s*$")

#: 本进程注册过的应用字体 id（``reset_registration_state`` 用）。
_APP_FONT_IDS: list[int] = []


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstalledFontFile:
    """一个已安装字体文件，以及注册表里声明的名字（可空）。"""

    path: Path
    declared_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class FontFaceNames:
    """字体文件中一个 face 的 name 表摘要（只收族名/样式名相关记录）。"""

    path: Path
    face_index: int
    family_ids: tuple[tuple[int, str], ...]
    """``(nameID, 文本)`` 列表；``nameID`` 为 1/16/21 之一。"""

    style_text: str | None = None
    full_text: str | None = None
    postscript_text: str | None = None

    @property
    def family_texts(self) -> tuple[str, ...]:
        return tuple(text for _name_id, text in self.family_ids)


@dataclass(frozen=True)
class RegistrationReport:
    """一次补注册的结果（干净地失败好过静默回退）。"""

    registered: tuple[str, ...] = ()
    """本次注册成功的族名。"""

    covered: tuple[str, ...] = ()
    """文件解析后确认 Qt 本来就看得见的文件（无需注册）。"""

    failed: tuple[tuple[str, str], ...] = ()
    """``(文件, 原因)``；原因只用于日志/诊断，不弹窗。"""

    app_font_ids: tuple[int, ...] = ()
    """本次通过 ``addApplicationFontFromData`` 拿到的 id（可用于撤销）。"""

    skipped_reason: str | None = None
    """没有执行枚举的原因（无 QGuiApplication / 非 GUI 线程 / 非 Windows）。"""


# ---------------------------------------------------------------------------
# 名字解码
# ---------------------------------------------------------------------------


def _decode_utf16(raw: bytes) -> str | None:
    if not raw:
        return None
    if len(raw) % 2:
        raw = raw[:-1]
    try:
        text = raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    # 旧字体偶有把 7bit 名打成 UTF-16 却混入 NUL 的情况，去掉后仍要非空。
    text = text.replace("\x00", "").strip()
    return text or None


def _looks_broken(text: str) -> bool:
    """控制符/未分配/代理/私用区——解码猜错时的典型产物。"""

    if not text:
        return True
    return any(
        unicodedata.category(char) in {"Cc", "Cf", "Cn", "Cs", "Co"}
        for char in text
    )


def decode_name_text(
    platform: int, encoding: int, language: int, raw: bytes
) -> str | None:
    """``name`` 记录字节 → 文本（只处理 Windows platform，其它返回 None）。

    先按记录声明的 ANSI 代码页解码，出现控制字符等碎码时再退回 UTF-16BE——
    这样「标成 Shift-JIS 实际是 UTF-16BE」的旧日文字体与真正的 Shift-JIS
    记录都能读出来。platform 1（Mac）记录在旧字体里常是错标编码的字节，
    不作为族名来源，避免把乱码写进修复后的名字表。
    """

    if platform != _PLATFORM_WINDOWS or not raw:
        return None
    if encoding in _UNICODE_ENCODINGS:
        return _decode_utf16(raw)
    codec = _ANSI_CODECS.get(language)
    if codec is not None:
        try:
            text = raw.decode(codec).strip()
        except UnicodeDecodeError:
            text = ""
        if text and not _looks_broken(text):
            return text
    text = _decode_utf16(raw)
    if text is None or _looks_broken(text):
        return None
    return text


# ---------------------------------------------------------------------------
# 字体文件枚举
# ---------------------------------------------------------------------------


def _windows_fonts_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _user_fonts_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return None
    return Path(local) / "Microsoft" / "Windows" / "Fonts"


def _font_directories() -> tuple[Path, ...]:
    directories = [_windows_fonts_dir()]
    user = _user_fonts_dir()
    if user is not None:
        directories.append(user)
    return tuple(directory for directory in directories if directory.is_dir())


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def parse_registry_value_name(value_name: str) -> tuple[str, ...]:
    """注册表值名 → 声明的字体名（TTC 会写成 ``A & B (TrueType)``）。"""

    text = _TYPE_SUFFIX.sub("", str(value_name or "")).strip()
    return tuple(part.strip() for part in text.split(" & ") if part.strip())


def _resolve_font_path(raw: str) -> Path | None:
    text = str(raw or "").strip().strip('"')
    if not text:
        return None
    candidate = Path(text)
    if candidate.suffix.lower() not in _FONT_EXTENSIONS:
        return None
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for directory in _font_directories():
        path = directory / text
        if path.is_file():
            return path
    return None


def registry_font_entries() -> tuple[InstalledFontFile, ...]:
    """``HKLM`` + ``HKCU`` 字体注册表里的全部已安装字体文件。"""

    if not sys.platform.startswith("win"):
        return ()
    try:
        import winreg
    except ImportError:  # pragma: no cover - 仅非 Windows
        return ()

    entries: list[InstalledFontFile] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, _FONTS_REG_SUBKEY)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    value_name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if not isinstance(value, str) or not value:
                    continue
                names = parse_registry_value_name(str(value_name))
                for part in value.split(","):
                    path = _resolve_font_path(part)
                    if path is None:
                        continue
                    entries.append(InstalledFontFile(path, names))
    return tuple(entries)


def installed_font_files() -> tuple[InstalledFontFile, ...]:
    """去重后的已安装字体文件（注册表优先，保留 HKLM 先于 HKCU 的顺序）。"""

    seen: dict[str, InstalledFontFile] = {}
    for entry in registry_font_entries():
        seen.setdefault(_path_key(entry.path), entry)
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# name 表解析与修复
# ---------------------------------------------------------------------------


@contextmanager
def _quiet_fonttools() -> Iterator[None]:
    """fontTools 对 1970 附近时间戳的告警与本次修复无关，不刷控制台。

    这条告警走的是 ``logging``（``fontTools.ttLib.tables._h_e_a_d``）而不是
    ``warnings``，所以得临时抬高该 logger 的级别；退出时恢复，避免影响其它
    调用方。``warnings`` 过滤保留，兜住 fontTools 其它走 warnings 的提示。
    """

    head_logger = logging.getLogger("fontTools.ttLib.tables._h_e_a_d")
    previous_level = head_logger.level
    head_logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*timestamp seems very low.*")
            yield
    finally:
        head_logger.setLevel(previous_level)


def _is_collection(path: Path) -> bool:
    """按 sfnt 头判断，不信后缀（有 ``.ttf`` 里装 TTC 的字体）。"""

    try:
        with open(path, "rb") as stream:
            return stream.read(4) == b"ttcf"
    except OSError:
        return path.suffix.lower() in {".ttc", ".otc"}


def _open_fonts(path: Path) -> list[object]:
    """按真实容器类型打开字体（只反编译 ``name``，其它表保持懒加载）。"""

    if _is_collection(path):
        from fontTools.ttLib import TTCollection

        return list(TTCollection(str(path), lazy=True).fonts)
    from fontTools.ttLib import TTFont

    return [TTFont(str(path), lazy=True)]


def parse_font_faces(path: Path) -> tuple[FontFaceNames, ...]:
    """读取字体文件中每个 face 的族名/样式名（只反编译 ``name`` 表）。"""

    path = Path(path)
    faces: list[FontFaceNames] = []
    with _quiet_fonttools():
        fonts = _open_fonts(path)
    for index, font in enumerate(fonts):
        try:
            name_table = font["name"]  # type: ignore[index]
        except (KeyError, RuntimeError, TypeError):
            continue
        families: list[tuple[int, str]] = []
        seen_texts: set[str] = set()
        style_text: str | None = None
        full_text: str | None = None
        postscript_text: str | None = None
        for record in name_table.names:
            text = decode_name_text(
                record.platformID, record.platEncID, record.langID, record.string
            )
            if text is None:
                continue
            name_id = int(record.nameID)
            if name_id in _FAMILY_NAME_IDS:
                if text not in seen_texts:
                    seen_texts.add(text)
                    families.append((name_id, text))
            elif name_id == 17:
                style_text = text
            elif name_id == 2 and style_text is None:
                style_text = text
            elif name_id == 4 and full_text is None:
                full_text = text
            elif name_id == 6 and postscript_text is None:
                postscript_text = text
        if not families:
            continue
        faces.append(
            FontFaceNames(
                path=path,
                face_index=index,
                family_ids=tuple(families),
                style_text=style_text,
                full_text=full_text,
                postscript_text=postscript_text,
            )
        )
    return tuple(faces)


def _postscript_name(family: str, style: str) -> str:
    def _ascii(value: str) -> str:
        return "".join(
            char for char in value if 0x20 < ord(char) < 0x7F and char != " "
        )

    family_part = _ascii(family) or "Font"
    style_part = _ascii(style) or "Regular"
    return f"{family_part}-{style_part}"


def _repair_name_table(font: object, fallback_family: str) -> str:
    """给一个 face 补写 Windows Unicode 名字记录，返回采用的族名。

    只增补/覆盖 platform 3 / encoding 1 的目标 locale 记录，原有记录全部保留
    （其它 locale、其它 platform、以及旧的错标编码记录都不动），因此对旧工程
    里保存的族名零影响。
    """

    name_table = font["name"]  # type: ignore[index]
    existing: dict[int, str] = {}
    for record in name_table.names:
        text = decode_name_text(
            record.platformID, record.platEncID, record.langID, record.string
        )
        if text is None:
            continue
        name_id = int(record.nameID)
        if name_id not in (*_FAMILY_NAME_IDS, 2, 3, 4, 6, 17):
            continue
        if record.platEncID in _UNICODE_ENCODINGS:
            # 已经是 Qt 能读的 Unicode 记录：以它为准，别用旧编码的解码猜测。
            existing[name_id] = text
        else:
            existing.setdefault(name_id, text)

    family = (
        existing.get(1)
        or existing.get(16)
        or existing.get(21)
        or fallback_family
    )
    style = existing.get(17) or existing.get(2) or "Regular"
    full = existing.get(4) or (
        family if style.casefold() in {"regular", "normal"} else f"{family} {style}"
    )
    postscript = existing.get(6) or _postscript_name(family, style)

    records: dict[int, str] = {}
    for name_id in (1, 16, 21):
        if name_id in existing:
            records[name_id] = existing[name_id]
    records.setdefault(1, family)
    records[2] = style
    records[4] = full
    records[6] = postscript
    if 17 in existing:
        records[17] = style

    for name_id, text in records.items():
        for language in _TARGET_LANGUAGES:
            name_table.setName(text, name_id, _PLATFORM_WINDOWS, 1, language)
    if 3 not in existing:
        name_table.setName(
            f"{_postscript_name(family, style)}-1",
            3,
            _PLATFORM_WINDOWS,
            1,
            _TARGET_LANGUAGES[0],
        )
    return family


def repaired_font_bytes(
    path: Path,
    face_indexes: Sequence[int] | None = None,
    *,
    fallback_family: str | None = None,
) -> bytes:
    """返回补齐名字记录后的字体字节；``face_indexes`` 为空表示整个文件。

    懒加载保存：只有 ``name`` 表被反编译，其余表按原始字节复制，所以轮廓、
    cmap、变体轴等都与源文件逐字节一致（``head`` 时间戳除外）。
    ``fallback_family`` 用于 name 表读不出族名的字体（注册表声明的名字）。
    """

    path = Path(path)
    family = fallback_family or path.stem
    targets = None if face_indexes is None else tuple(int(i) for i in face_indexes)
    buffer = io.BytesIO()
    with _quiet_fonttools():
        if _is_collection(path):
            from fontTools.ttLib import TTFont, TTCollection

            if targets is not None and len(targets) == 1:
                # 只缺一个 face：把它抽成独立字体再注册。整包注册会把同一
                # 文件里 Qt 本来就看得见的 face 再登记一遍（重复族、匹配歧义）。
                font = TTFont(str(path), fontNumber=targets[0], lazy=True)
                _repair_name_table(font, family)
                font.save(buffer)
            else:
                collection = TTCollection(str(path), lazy=True)
                indexes = (
                    range(len(collection.fonts)) if targets is None else targets
                )
                for index in indexes:
                    if 0 <= int(index) < len(collection.fonts):
                        _repair_name_table(collection.fonts[int(index)], family)
                collection.save(buffer)
        else:
            from fontTools.ttLib import TTFont

            font = TTFont(str(path), lazy=True)
            _repair_name_table(font, family)
            font.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def qt_visible_families() -> frozenset[str]:
    """当前 Qt 字体库里的全部族名（含应用字体，小写用于比较）。"""

    from PyQt6.QtGui import QFontDatabase

    return frozenset(
        text
        for text in (str(name).strip().casefold() for name in QFontDatabase.families())
        if text
    )


def _face_visible(face: FontFaceNames, names: frozenset[str]) -> bool:
    return any(text.casefold() in names for text in face.family_texts)


#: 注册表全名尾部可去掉的样式词（只允许白名单，避免把 ``Arial Nova`` /
#: ``Arial Narrow`` / ``SF Pro Display`` 这类「真族名以可见族名开头」的字体
#: 误判为已可见）。只收字重/倾斜这类几乎总是修饰词的词，不收 Condensed /
#: Display / Text 等可能本身就是族名一部分的设计词。
_STYLE_WORDS = frozenset(
    {
        "regular", "normal", "roman", "book", "medium", "light", "extralight",
        "ultralight", "semilight", "demilight", "thin", "hairline", "bold",
        "semibold", "demibold", "demi", "black", "heavy", "ultra", "extra",
        "italic", "oblique",
    }
)


def _declared_name_visible(name: str, names: frozenset[str]) -> bool:
    """单个注册表声明名是否已被 Qt 解析（允许去掉尾部样式词）。

    注册表值名往往是「全名」（``Arial Bold`` / ``Arial Bold Italic``），不是族名：
    从尾部逐个去掉白名单里的样式词后再比。只去白名单词，不做任意截断——
    ``Arial Nova`` 这种「以可见族名开头但自己是另一个族」的字体必须留给文件解析
    判断，否则会漏掉不叫 FG 的同类字体。
    """

    parts = str(name).strip().split()
    if not parts:
        return False
    if " ".join(parts).casefold() in names:
        return True
    while parts and parts[-1].casefold().strip("()（）") in _STYLE_WORDS:
        parts.pop()
        if parts and " ".join(parts).casefold() in names:
            return True
    return False


def declared_names_visible(declared_names: Sequence[str], names: frozenset[str]) -> bool:
    """注册表声明的名字是否**全部**已被 Qt 解析（第一层廉价比对）。

    全命中才跳过文件解析。TTC 的 ``A & B`` 只要有一个族 Qt 解析不到，就必须
    打开文件确认——否则会漏掉「一个 face 可见、另一个 face 不可见」的集合。
    """

    return bool(declared_names) and all(
        _declared_name_visible(name, names) for name in declared_names
    )


@dataclass(frozen=True)
class _RegistrationResult:
    families: tuple[str, ...] = ()
    font_ids: tuple[int, ...] = ()
    error: str | None = None


def _register_bytes(data: bytes) -> _RegistrationResult:
    from PyQt6.QtGui import QFontDatabase

    font_id = QFontDatabase.addApplicationFontFromData(data)
    if font_id < 0:
        return _RegistrationResult(
            error="Qt 拒绝该字体（addApplicationFontFromData 返回 -1）"
        )
    families = tuple(
        str(name) for name in QFontDatabase.applicationFontFamilies(font_id) if name
    )
    if not families:
        try:
            QFontDatabase.removeApplicationFont(font_id)
        except Exception:  # noqa: BLE001 - 清理失败不影响结论
            log.debug("撤销空的应用程序字体失败", exc_info=True)
        return _RegistrationResult(error="Qt 注册后没有给出任何族名")
    _APP_FONT_IDS.append(font_id)
    return _RegistrationResult(families=families, font_ids=(font_id,))


def _register_faces(
    path: Path,
    faces: Sequence[FontFaceNames],
    missing: Sequence[FontFaceNames],
    fallback_family: str | None = None,
) -> _RegistrationResult:
    # 整个文件都缺：整包注册（TTC 一次把每个 face 都登记，内存只占一份）。
    if len(missing) == len(faces):
        return _register_bytes(
            repaired_font_bytes(path, None, fallback_family=fallback_family)
        )
    # 部分缺：只提取缺的 face，避免把已可见的同族重复注册一遍。
    families: list[str] = []
    font_ids: list[int] = []
    for face in missing:
        result = _register_bytes(
            repaired_font_bytes(
                path, (face.face_index,), fallback_family=fallback_family
            )
        )
        if result.error is not None:
            return _RegistrationResult(
                families=tuple(families), font_ids=tuple(font_ids), error=result.error
            )
        families.extend(result.families)
        font_ids.extend(result.font_ids)
    return _RegistrationResult(families=tuple(families), font_ids=tuple(font_ids))


def _invalidate_font_caches() -> None:
    """注册改变字体库后，三层缓存必须一起失效。"""

    try:
        from krok_helper.subtitle_render.n3.font_catalog import (
            invalidate_n3_font_caches,
        )

        invalidate_n3_font_caches()
    except Exception:  # noqa: BLE001 - 缓存失效失败不阻断注册结果
        log.debug("N3 字体目录缓存失效失败", exc_info=True)
    try:
        from krok_helper.subtitle_render.engine.text.font_weight import (
            clear_font_weight_cache,
        )

        clear_font_weight_cache()
    except Exception:  # noqa: BLE001
        log.debug("字重能力缓存失效失败", exc_info=True)
    try:
        from krok_helper.subtitle_render.engine.text.font_fallback import (
            clear_font_caches,
        )

        clear_font_caches()
    except Exception:  # noqa: BLE001
        log.debug("缺字回退缓存失效失败", exc_info=True)


def _gui_application() -> object | None:
    from PyQt6.QtGui import QGuiApplication

    return QGuiApplication.instance()


def register_missing_system_fonts(
    entries: Iterable[InstalledFontFile] | None = None,
) -> RegistrationReport:
    """把 Qt 看不见的已安装字体补注册成应用字体（GUI 线程调用）。

    ``entries`` 仅供测试注入自定义字体清单；生产路径传 ``None`` 走注册表。
    幂等：已注册/已可见的族不会重复注册，适合在启动与字体装卸后重复调用。
    """

    if not sys.platform.startswith("win"):
        return RegistrationReport(skipped_reason="仅 Windows 需要补注册")

    application = _gui_application()
    if application is None:
        return RegistrationReport(skipped_reason="没有 QGuiApplication")
    from PyQt6.QtCore import QThread

    if QThread.currentThread() is not application.thread():
        return RegistrationReport(skipped_reason="不在 GUI 线程")

    qt_names = qt_visible_families()
    # 空字体库说明当前平台根本不枚举系统字体（offscreen / minimal 就是如此），
    # 此时「Qt 看不见」对所有字体都成立，注册等于把整个系统字体目录搬成应用
    # 字体：既慢又改变匹配结果，必须跳过。显式传入 entries 的调用方（测试）
    # 不受此限。
    if not qt_names and entries is None:
        return RegistrationReport(
            skipped_reason="Qt 字体库没有系统字体（当前平台不枚举系统字体）"
        )

    files = tuple(installed_font_files() if entries is None else entries)
    if not files:
        return RegistrationReport()

    registered: list[str] = []
    covered: list[str] = []
    failed: list[tuple[str, str]] = []
    font_ids: list[int] = []

    for entry in files:
        path = Path(entry.path)
        if not path.is_file():
            continue
        if declared_names_visible(entry.declared_names, qt_names):
            # 名字已经全部能被 Qt 解析：不值得为它打开文件。本机 347 个注册表
            # 条目在这一步只剩 34 个需要解析。
            continue
        try:
            faces = parse_font_faces(path)
        except Exception as exc:  # noqa: BLE001 - 单个坏字体不影响其它字体
            failed.append((str(path), f"name 表解析失败：{exc}"))
            continue
        declared_family = entry.declared_names[0] if entry.declared_names else None
        if not faces:
            # name 表读不出族名：用注册表声明的名字兜底修一份；连声明名都没有
            # 才判定不可用（位图 / 损坏字体在这里落进 failed）。
            if not declared_family:
                failed.append((str(path), "没有可读的 name 表"))
                continue
            faces = (
                FontFaceNames(
                    path=path,
                    face_index=0,
                    family_ids=((1, declared_family),),
                ),
            )
        missing = tuple(face for face in faces if not _face_visible(face, qt_names))
        if not missing:
            covered.append(str(path))
            continue
        try:
            result = _register_faces(path, faces, missing, declared_family)
        except Exception as exc:  # noqa: BLE001
            failed.append((str(path), f"注册失败：{exc}"))
            continue
        if result.error is not None:
            failed.append((str(path), result.error))
            continue
        if not result.font_ids:
            failed.append((str(path), "注册没有产生应用字体"))
            continue
        font_ids.extend(result.font_ids)
        registered.extend(result.families)
        # 新族立刻可见：同一字体的第二个副本（例如每用户安装的同一份文件）
        # 会在下一轮被判为 covered，不会重复注册。
        qt_names = qt_visible_families()

    if font_ids:
        log.info("已补注册 %d 个系统字体族：%s", len(registered), "、".join(registered))
        _invalidate_font_caches()
    if failed:
        for path, reason in failed:
            log.debug("系统字体补注册跳过 %s：%s", path, reason)
    return RegistrationReport(
        registered=tuple(dict.fromkeys(registered)),
        covered=tuple(covered),
        failed=tuple(failed),
        app_font_ids=tuple(font_ids),
    )


def reset_registration_state() -> None:
    """撤销本模块注册过的应用字体并清空缓存（测试与字体装卸复用）。"""

    from PyQt6.QtGui import QFontDatabase

    while _APP_FONT_IDS:
        font_id = _APP_FONT_IDS.pop()
        try:
            QFontDatabase.removeApplicationFont(font_id)
        except Exception:  # noqa: BLE001
            log.debug("撤销应用程序字体 %s 失败", font_id, exc_info=True)
    _invalidate_font_caches()


__all__ = [
    "FontFaceNames",
    "InstalledFontFile",
    "RegistrationReport",
    "declared_names_visible",
    "decode_name_text",
    "installed_font_files",
    "parse_font_faces",
    "parse_registry_value_name",
    "qt_visible_families",
    "register_missing_system_fonts",
    "registry_font_entries",
    "repaired_font_bytes",
    "reset_registration_state",
]
