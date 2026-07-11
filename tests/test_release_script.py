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
    assert "gh release edit v3.2.0 --notes-file" in capsys.readouterr().out


def test_notes_missing_section_raises(release_mod):
    with pytest.raises(SystemExit):
        release_mod.cmd_notes("9.9.9")
