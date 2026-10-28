"""KS ``scripts/release.py`` 的版本同步与中文 notes 测试。"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def release_mod(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("ks_release", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "VERSION_FILE", tmp_path / "krok_helper" / "config.py")
    monkeypatch.setattr(module, "README", tmp_path / "README.md")
    monkeypatch.setattr(module, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(module, "RELEASE_DIST", tmp_path / "dist")
    (tmp_path / "krok_helper").mkdir()
    module.VERSION_FILE.write_text('APP_NAME = "Karaoke Studio"\nAPP_VERSION = "3.1.7.4"\n', encoding="utf-8")
    module.README.write_text("# Karaoke Studio\n\n当前版本：`3.1.7.4`\n\n正文\n", encoding="utf-8")
    module.CHANGELOG.write_text(textwrap.dedent("""
        # Changelog

        ## [Unreleased]

        ---

        ## [3.1.7.4] — 2026-07-11

        ### 修复项目
        - 旧说明
        """).lstrip(), encoding="utf-8")
    return module


@pytest.mark.parametrize("version", ["3.2.0", "3.1.7.5"])
def test_prepare_accepts_three_and_four_segment_versions(release_mod, version):
    assert release_mod.cmd_prepare(version) == 0
    assert release_mod._read_version() == version
    assert f"当前版本：`{version}`" in release_mod.README.read_text(encoding="utf-8")
    assert f"## [{version}]" in release_mod.CHANGELOG.read_text(encoding="utf-8")


def test_prepare_is_idempotent(release_mod):
    release_mod.cmd_prepare("3.2.0")
    release_mod.cmd_prepare("3.2.0")
    assert release_mod.CHANGELOG.read_text(encoding="utf-8").count("## [3.2.0]") == 1


@pytest.mark.parametrize("version", ["v3.2.0", "3.2", "3.2.0.1.2", "3.2.0-beta"])
def test_prepare_rejects_versions_outside_release_contract(release_mod, version):
    with pytest.raises(SystemExit):
        release_mod.cmd_prepare(version)


def test_prepare_aborts_if_readme_marker_is_missing(release_mod):
    release_mod.README.write_text("# no version marker\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        release_mod.cmd_prepare("3.2.0")
    assert release_mod._read_version() == "3.1.7.4"


def test_notes_extracts_only_requested_chinese_section(release_mod, tmp_path, capsys):
    release_mod.cmd_prepare("3.2.0")
    content = release_mod.CHANGELOG.read_text(encoding="utf-8").replace(
        "*（请用一句中文概述本次发布的用户可见变化。）*",
        "补齐工作台自动更新发版流程。",
    )
    release_mod.CHANGELOG.write_text(content, encoding="utf-8")
    output = tmp_path / "notes.md"
    assert release_mod.cmd_notes("3.2.0", output) == 0
    notes = output.read_text(encoding="utf-8")
    assert "补齐工作台" in notes
    assert "[3.1.7.4]" not in notes
    output_text = capsys.readouterr().out
    assert "CI 会从 CHANGELOG.md 自动提取同一版本段" in output_text
    assert "gh release view v3.2.0" in output_text


def test_notes_missing_section_raises(release_mod):
    with pytest.raises(SystemExit):
        release_mod.cmd_notes("9.9.9")


def test_every_release_body_opens_with_the_qq_group_banner(release_mod, tmp_path):
    """公告横幅在唯一出口生成，CI 直接拿这份文件当 release body。"""

    out = tmp_path / "notes.md"
    assert release_mod.cmd_notes("3.1.7.4", out) == 0
    text = out.read_text(encoding="utf-8")

    assert text.startswith(release_mod.ANNOUNCEMENT_BANNER)
    assert "1108437280" in text.splitlines()[0]
    # 横幅之后仍然是这一版真正的更新内容。
    assert release_mod._extract_section("3.1.7.4").strip() in text


def test_the_banner_uses_html_bold_so_qt_markdown_keeps_it(release_mod):
    """内联 HTML 里的 ``**`` 不会被解析成强调，用它加粗会静默丢掉。"""

    banner = release_mod.ANNOUNCEMENT_BANNER
    assert "<b>" in banner and "**" not in banner
    assert "color:#d64545" in banner


def test_adding_the_banner_twice_does_not_duplicate_it(release_mod):
    once = release_mod._with_announcement_banner("## 更新\n- 条目\n")
    twice = release_mod._with_announcement_banner(once)

    assert once == twice
    assert twice.count("1108437280") == 1


def _prepare_filled_section(release_mod, version: str) -> None:
    release_mod.cmd_prepare(version)
    content = release_mod.CHANGELOG.read_text(encoding="utf-8").replace(
        "*（请用一句中文概述本次发布的用户可见变化。）*",
        "停发旧名主程序副本。",
    )
    release_mod.CHANGELOG.write_text(content, encoding="utf-8")


def test_notes_from_the_first_legacy_free_version_add_the_notice(release_mod, tmp_path):
    """首个无旧名副本版本（4.4.0，含）起，正文头部在横幅下方插入手动更新提示。"""

    _prepare_filled_section(release_mod, "4.4.0")
    out = tmp_path / "notes.md"
    assert release_mod.cmd_notes("4.4.0", out) == 0
    lines = out.read_text(encoding="utf-8").splitlines()

    assert lines[0] == release_mod.ANNOUNCEMENT_BANNER
    assert lines[2] == release_mod.MANUAL_UPDATE_NOTICE
    assert "Lin-K Lyrics.exe" in lines[2]
    assert "停发旧名主程序副本" in "\n".join(lines)


@pytest.mark.parametrize("version", ["4.3.0", "4.3.1", "4.3.9.9"])
def test_notes_while_still_shipping_the_legacy_exe_keep_the_body_unchanged(
    release_mod, tmp_path, version
):
    """4.3.x 整条线仍随包携带旧名副本，正文不得出现手动更新提示（提示与包内容
    错位会误导用户）。"""

    _prepare_filled_section(release_mod, version)
    out = tmp_path / "notes.md"
    assert release_mod.cmd_notes(version, out) == 0
    text = out.read_text(encoding="utf-8")

    assert release_mod.MANUAL_UPDATE_NOTICE not in text
    assert text.startswith(release_mod.ANNOUNCEMENT_BANNER)


def test_adding_the_manual_update_notice_twice_does_not_duplicate_it(release_mod):
    bannered = release_mod._with_announcement_banner("## 更新\n- 条目\n")
    once = release_mod._with_manual_update_notice(bannered)
    twice = release_mod._with_manual_update_notice(once)

    assert once == twice
    assert once.count(release_mod.MANUAL_UPDATE_NOTICE) == 1
    # 提示必须位于横幅之下、正文之上。
    assert (
        once.index("1108437280")
        < once.index(release_mod.MANUAL_UPDATE_NOTICE)
        < once.index("## 更新")
    )


def test_check_notes_enforces_the_notice_policy(release_mod, tmp_path):
    """CI 发布前的护栏：无旧名版本的 body 必须带提示，双名版本不得带。"""

    with_notice = (
        f"{release_mod.ANNOUNCEMENT_BANNER}\n\n"
        f"{release_mod.MANUAL_UPDATE_NOTICE}\n\n## 更新\n- 条目\n"
    )
    without_notice = f"{release_mod.ANNOUNCEMENT_BANNER}\n\n## 更新\n- 条目\n"
    noticed = tmp_path / "noticed.md"
    plain = tmp_path / "plain.md"
    noticed.write_text(with_notice, encoding="utf-8")
    plain.write_text(without_notice, encoding="utf-8")

    # 4.4.0：首个无旧名副本版本，必须带提示。
    assert release_mod.cmd_check_notes("4.4.0", noticed) == 0
    with pytest.raises(SystemExit):
        release_mod.cmd_check_notes("4.4.0", plain)
    # 4.3.1：仍双名分发，不得带提示（提示与包内容错位会误导用户）。
    assert release_mod.cmd_check_notes("4.3.1", plain) == 0
    with pytest.raises(SystemExit):
        release_mod.cmd_check_notes("4.3.1", noticed)
