"""系统字体补注册（``n3.system_font_inventory``）的回归测试。

覆盖三件事：

1. 旧字体名字表的两种错标编码都能读（标 Shift-JIS 实为 UTF-16BE / 真的 Shift-JIS）；
2. 修复只补 Windows Unicode 名字记录，不动轮廓表、不丢原记录；
3. 注册后该族真的进 Qt 字体库、能被 ``QFontInfo``/字形能力解析命中，且重复
   调用幂等、已可见字体不会被重复注册。

注意：测试进程默认跑在 ``offscreen`` 平台，该平台的字体库是 FreeType 实现，
非 ASCII 族名会被替换成 ``?``（生产用的 Windows/DirectWrite 平台没有这个问题，
CJK 族名的注册由 ``test_cjk_family_registers_on_windows_platform`` 在真实平台
上守）。因此注册类用例统一用 ASCII 族名；CJK 名字的解码/修复由纯函数用例覆盖。
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import pytest
from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.n3 import system_font_inventory as inventory

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="系统字体补注册只对 Windows 有意义"
)

#: 注册类用例的族名：必须是 ASCII，见模块 docstring 的平台说明。
ASCII_FAMILY = "LegacyFixture Sans"


def _system_fonts_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _base_font() -> Path:
    for name in ("arial.ttf", "segoeui.ttf", "tahoma.ttf", "times.ttf", "verdanab.ttf"):
        path = _system_fonts_dir() / name
        if path.is_file():
            return path
    pytest.skip("找不到可用于构造测试字体的系统字体")


def _legacy_font_bytes(source: Path, family: str) -> bytes:
    """造一个「名字表只有 ja-JP 旧编码记录」的字体，模拟旧日文字体。

    与调试用的 FG平成明朝体一致：platform 3 标成 Shift-JIS（encoding 2），
    字节实际是 UTF-16BE——Qt 因此完全看不见这个族。
    """

    from fontTools.ttLib import TTFont

    font = TTFont(str(source), lazy=False)
    table = font["name"]
    table.names = []
    for name_id, text in (
        (1, family),
        (2, "Regular"),
        (3, f"{family}-Regular-1"),
        (4, family),
        (6, "LegacyFixture-Regular"),
    ):
        # setName 接受已编码的 bytes（platform 3 / encoding 2 按 UTF-16BE 存储，
        # 正是旧日文字体「标错编码」的样子）。
        table.setName(text.encode("utf-16-be"), name_id, 3, 2, 0x0411)
    buffer = io.BytesIO()
    font.save(buffer)
    return buffer.getvalue()


def _name_records(data: bytes) -> dict[tuple[int, int, int, int], bytes]:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(data), lazy=True)
    return {
        (record.nameID, record.platformID, record.platEncID, record.langID): record.string
        for record in font["name"].names
    }


def _system_ttc() -> Path:
    """找一个体积不大、至少两个 face 的系统 TTC 当夹具。"""

    from fontTools.ttLib import TTCollection

    candidates = sorted(
        (path for path in _system_fonts_dir().glob("*.?tc") if path.stat().st_size < 4_000_000),
        key=lambda path: path.stat().st_size,
    )
    for path in candidates:
        try:
            if len(TTCollection(str(path), lazy=True).fonts) >= 2:
                return path
        except Exception:  # noqa: BLE001 - 坏字体继续找下一个
            continue
    pytest.skip("找不到两个 face 的系统 TTC 用于夹具")


def _set_family(table, family: str, *, unicode_names: bool) -> None:
    table.names = []
    for name_id, text in (
        (1, family),
        (2, "Regular"),
        (4, family),
        (6, "LegacyFixture-Regular"),
    ):
        if unicode_names:
            table.setName(text, name_id, 3, 1, 0x0409)
        else:
            table.setName(text.encode("utf-16-be"), name_id, 3, 2, 0x0411)


@pytest.fixture()
def clean_registry_state():
    inventory.reset_registration_state()
    yield inventory
    inventory.reset_registration_state()


# ---------------------------------------------------------------------------
# 名字解码 / 注册表值名
# ---------------------------------------------------------------------------


def test_decode_name_text_handles_both_legacy_encodings() -> None:
    # 字节真是 Shift-JIS（platform 3 / encoding 2 的规范用法）
    assert (
        inventory.decode_name_text(3, 2, 0x0411, "平成明朝体".encode("cp932"))
        == "平成明朝体"
    )
    # 字节其实是 UTF-16BE（旧日文字体的错标法，Qt 看不见的那类）
    assert (
        inventory.decode_name_text(3, 2, 0x0411, "平成明朝体".encode("utf-16-be"))
        == "平成明朝体"
    )
    # 正常 Unicode 记录
    assert (
        inventory.decode_name_text(3, 1, 0x0411, "游明朝".encode("utf-16-be"))
        == "游明朝"
    )
    # Mac 记录不作为族名来源（旧字体里常是错标字节）
    assert inventory.decode_name_text(1, 0, 0x0411, b"\xff\xfe") is None


def test_registry_value_name_splits_collection_members() -> None:
    assert inventory.parse_registry_value_name(
        "FG平成明朝体W9 & FGP平成明朝体W9 (TrueType)"
    ) == ("FG平成明朝体W9", "FGP平成明朝体W9")
    assert inventory.parse_registry_value_name("汉仪跑男体 45U (TrueType)") == (
        "汉仪跑男体 45U",
    )
    assert inventory.parse_registry_value_name("Some Font (Demo) (OpenType)") == (
        "Some Font (Demo)",
    )


# ---------------------------------------------------------------------------
# name 表解析 / 修复（纯 fontTools，不依赖 Qt 平台）
# ---------------------------------------------------------------------------


def test_parse_font_faces_reads_legacy_names(tmp_path: Path) -> None:
    path = tmp_path / "legacy.ttf"
    path.write_bytes(_legacy_font_bytes(_base_font(), "测试孤僻体 Legion"))

    faces = inventory.parse_font_faces(path)

    assert len(faces) == 1
    assert faces[0].face_index == 0
    assert "测试孤僻体 Legion" in faces[0].family_texts
    assert faces[0].style_text == "Regular"


def test_repaired_font_bytes_adds_unicode_names_and_keeps_outlines(
    tmp_path: Path,
) -> None:
    from fontTools.ttLib import TTFont

    source = _base_font()
    path = tmp_path / "legacy.ttf"
    path.write_bytes(_legacy_font_bytes(source, "测试孤僻体 Legion"))

    repaired = inventory.repaired_font_bytes(path)
    records = _name_records(repaired)

    # 补上 Qt 认的两个 locale，且沿用原族名（CJK 原文逐字保真）
    for language in (0x0409, 0x0804):
        text = inventory.decode_name_text(
            3, 1, language, records[(1, 3, 1, language)]
        )
        assert text == "测试孤僻体 Legion"
    assert inventory.decode_name_text(3, 1, 0x0409, records[(2, 3, 1, 0x0409)]) == "Regular"
    # 原有旧编码记录仍在（对旧工程保存的族名零影响）
    assert (1, 3, 2, 0x0411) in records
    # 轮廓表按原始字节复制，修复不改字形
    repaired_font = TTFont(io.BytesIO(repaired), lazy=True)
    original = TTFont(str(source), lazy=True)
    assert bytes(repaired_font.reader["glyf"]) == bytes(original.reader["glyf"])


def test_repairing_one_face_of_a_collection_extracts_only_that_face(
    tmp_path: Path,
) -> None:
    from fontTools.ttLib import TTFont, TTCollection

    collection = TTCollection(str(_system_ttc()), lazy=False)
    face_count = len(collection.fonts)
    # face 0 用正常 Unicode 名字（Qt 可见），最后一个 face 只有旧编码名字
    _set_family(collection.fonts[0]["name"], "LegacyTTC Visible", unicode_names=True)
    _set_family(
        collection.fonts[face_count - 1]["name"], "LegacyTTC Hidden", unicode_names=False
    )
    path = tmp_path / "two-faces.ttc"
    collection.save(str(path))

    # 只缺最后一个 face：必须抽成单 face 字体，不能把整包（含已可见 face）注册
    single = inventory.repaired_font_bytes(path, (face_count - 1,))
    face = TTFont(io.BytesIO(single), lazy=True)  # 仍是 TTC 的话这里会抛错
    texts = {
        inventory.decode_name_text(3, 1, language, record.string)
        for record in face["name"].names
        for language in (0x0409, 0x0804)
        if record.nameID == 1 and record.platformID == 3 and record.platEncID == 1
    }
    assert texts == {"LegacyTTC Hidden"}

    # 整文件修复仍产出同样 face 数的 TTC
    whole = inventory.repaired_font_bytes(path, None)
    assert len(TTCollection(io.BytesIO(whole), lazy=True).fonts) == face_count


# ---------------------------------------------------------------------------
# 注册链路（offscreen 平台也能跑的部分）
# ---------------------------------------------------------------------------


def test_register_missing_system_fonts_makes_family_usable(
    tmp_path: Path, clean_registry_state
) -> None:
    path = tmp_path / "legacy.ttf"
    path.write_bytes(_legacy_font_bytes(_base_font(), ASCII_FAMILY))
    assert ASCII_FAMILY not in QFontDatabase.families()

    report = inventory.register_missing_system_fonts(
        entries=(inventory.InstalledFontFile(path, (ASCII_FAMILY,)),)
    )

    assert ASCII_FAMILY in report.registered
    assert report.app_font_ids
    assert ASCII_FAMILY in QFontDatabase.families()
    assert QFontInfo(QFont(ASCII_FAMILY)).family() == ASCII_FAMILY

    # 渲染链路的入口也能解析到它：字形能力 + 字重决策 + GPU 快照路径
    from krok_helper.subtitle_render.engine.text.font_capabilities import (
        get_capabilities,
    )
    from krok_helper.subtitle_render.engine.text.font_weight import (
        resolve_font_instance,
    )

    capabilities = get_capabilities(ASCII_FAMILY)
    assert capabilities is not None
    assert capabilities.faces
    instance = resolve_font_instance(ASCII_FAMILY, 400)
    assert instance.reason is None
    assert instance.resource_path
    assert Path(instance.resource_path).is_file()


def test_registration_is_idempotent(tmp_path: Path, clean_registry_state) -> None:
    path = tmp_path / "legacy.ttf"
    path.write_bytes(_legacy_font_bytes(_base_font(), ASCII_FAMILY))
    entries = (inventory.InstalledFontFile(path, (ASCII_FAMILY,)),)

    first = inventory.register_missing_system_fonts(entries=entries)
    second = inventory.register_missing_system_fonts(entries=entries)

    assert ASCII_FAMILY in first.registered
    assert second.registered == ()
    assert second.app_font_ids == ()


def test_declared_names_visible_uses_prefix_and_requires_all() -> None:
    names = frozenset({"arial", "华康圆体 std w3"})
    assert inventory.declared_names_visible(("Arial",), names)
    # 全名去掉尾部修饰词后再比
    assert inventory.declared_names_visible(("Arial Bold",), names)
    assert inventory.declared_names_visible(("华康圆体 Std W3 Regular",), names)
    # TTC 经 parse_registry_value_name 拆成两个名字：只要有一个族不可见就必须
    # 打开文件确认（真机上的名字不会带 " & "，拆分发生在解析注册表时）
    assert not inventory.declared_names_visible(("Arial", "Foo"), names)
    # 空声明名不能当作可见
    assert not inventory.declared_names_visible((), names)


def test_register_does_not_open_files_whose_names_qt_already_resolves(
    tmp_path: Path, clean_registry_state, monkeypatch
) -> None:
    """第一层名字比对命中后不再解析文件（347 个文件只打开差集）。"""

    source = _base_font()
    font_id = QFontDatabase.addApplicationFont(str(source))
    assert font_id >= 0
    calls: list[Path] = []

    def _must_not_parse(path):
        calls.append(Path(path))
        raise AssertionError(f"不应该解析已经可见的字体的文件：{path}")

    try:
        monkeypatch.setattr(inventory, "parse_font_faces", _must_not_parse)
        report = inventory.register_missing_system_fonts(
            entries=(
                inventory.InstalledFontFile(source, ("Arial",)),
                inventory.InstalledFontFile(source, ("Arial Bold",)),
            )
        )
        assert calls == []
        assert report.registered == ()
        assert report.failed == ()
    finally:
        QFontDatabase.removeApplicationFont(font_id)


def test_register_skips_font_family_qt_already_sees(
    tmp_path: Path, clean_registry_state
) -> None:
    source = _base_font()
    # offscreen 平台不枚举系统字体，先把这份字体注册成应用字体，模拟
    # 「Qt 已经能看见它」的状态。声明名留空，走「解析后确认已可见」这条路径。
    font_id = QFontDatabase.addApplicationFont(str(source))
    assert font_id >= 0
    try:
        report = inventory.register_missing_system_fonts(
            entries=(inventory.InstalledFontFile(source, ()),)
        )
        assert report.registered == ()
        assert str(source) in report.covered
    finally:
        QFontDatabase.removeApplicationFont(font_id)


def test_registration_is_skipped_outside_gui_thread(
    tmp_path: Path, clean_registry_state
) -> None:
    path = tmp_path / "legacy.ttf"
    path.write_bytes(_legacy_font_bytes(_base_font(), ASCII_FAMILY))
    entries = (inventory.InstalledFontFile(path, (ASCII_FAMILY,)),)

    reports: list[inventory.RegistrationReport] = []

    def worker() -> None:
        reports.append(inventory.register_missing_system_fonts(entries=entries))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30.0)

    assert reports
    assert reports[0].registered == ()
    assert reports[0].skipped_reason
    # 后台线程没有偷偷注册字体
    assert ASCII_FAMILY not in QFontDatabase.families()


def test_cjk_family_registers_on_windows_platform(
    tmp_path: Path, clean_registry_state
) -> None:
    """真实 Windows 平台下 CJK 族名逐字保真（生产路径的最终守门）。"""

    application = QApplication.instance()
    if application is None or application.platformName().lower() != "windows":
        pytest.skip(
            "CJK 族名注册需要 Windows/DirectWrite 字体库"
            "（offscreen 平台用 FreeType，非 ASCII 族名会退化成 ?）"
        )

    family = "测试孤僻体 Legion"
    path = tmp_path / "legacy.ttf"
    path.write_bytes(_legacy_font_bytes(_base_font(), family))

    report = inventory.register_missing_system_fonts(
        entries=(inventory.InstalledFontFile(path, (family,)),)
    )

    assert family in report.registered
    assert family in QFontDatabase.families()
    assert QFontInfo(QFont(family)).family() == family
