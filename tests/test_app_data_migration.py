"""改名（Karaoke Studio → Lin-K Lyrics）后的用户数据目录迁移。

这些用例是 :func:`krok_helper.app_paths.migrate_app_data_dir` 的规格说明。
目录里不只有 settings.json，还挂着未保存工程恢复、备份、AI 模型和 Cookie，
所以迁移必须是**整目录**的，不能只靠 ``LEGACY_APP_NAMES`` 那套单文件读取兜底。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper import app_paths
from krok_helper.app_paths import (
    LEGACY_APP_NAMES,
    consume_migration_notes,
    migrate_app_data_dir,
)
from krok_helper.config import APP_NAME


@pytest.fixture(autouse=True)
def _isolated_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """把 %APPDATA% 指到 tmp_path，并清掉两个会短路迁移的环境变量。"""

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(app_paths.SETTINGS_DIR_ENV, raising=False)
    monkeypatch.setenv(app_paths.SETTINGS_APP_NAME_ENV, APP_NAME)
    consume_migration_notes()
    yield
    consume_migration_notes()


def _seed(root: Path, marker: str) -> Path:
    """造一个装着各类用户数据的旧目录。"""

    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(f'{{"marker": "{marker}"}}', encoding="utf-8")
    (root / "subtitle_render_backups").mkdir()
    (root / "subtitle_render_backups" / "unsaved.yurika").write_text(marker, encoding="utf-8")
    (root / "ai_models").mkdir()
    (root / "ai_models" / "model.bin").write_bytes(b"\x00" * 512)
    (root / "video_download").mkdir()
    (root / "video_download" / "bilibili_cookies.txt").write_text(marker, encoding="utf-8")
    return root


def test_migrates_whole_directory_not_just_settings_json(tmp_path: Path) -> None:
    old = _seed(tmp_path / "Karaoke Studio", "OLD")

    moved = migrate_app_data_dir()

    new = tmp_path / APP_NAME
    assert moved == old
    assert not old.exists()
    # 关键点：未保存工程、AI 模型、Cookie 都必须跟着走，不能只搬 settings.json
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"marker": "OLD"}'
    assert (new / "subtitle_render_backups" / "unsaved.yurika").read_text(encoding="utf-8") == "OLD"
    assert (new / "ai_models" / "model.bin").stat().st_size == 512
    assert (new / "video_download" / "bilibili_cookies.txt").read_text(encoding="utf-8") == "OLD"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    _seed(tmp_path / "Karaoke Studio", "OLD")
    assert migrate_app_data_dir() is not None
    # 第二次没有旧目录可搬，必须安静地什么都不做
    assert migrate_app_data_dir() is None


def test_existing_new_directory_is_never_clobbered(tmp_path: Path) -> None:
    """新目录已存在 → 用户已经在新版上跑过了，旧目录原样留着不动。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")
    _seed(tmp_path / APP_NAME, "CURRENT")

    assert migrate_app_data_dir() is None
    assert (tmp_path / APP_NAME / "settings.json").read_text(encoding="utf-8") == '{"marker": "CURRENT"}'
    assert (tmp_path / "Karaoke Studio" / "settings.json").read_text(encoding="utf-8") == '{"marker": "OLD"}'


def test_settings_dir_env_override_skips_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指定了设置目录 → 用户自己管，绝不去动 %APPDATA%。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")
    monkeypatch.setenv(app_paths.SETTINGS_DIR_ENV, str(tmp_path / "custom"))

    assert migrate_app_data_dir() is None
    assert (tmp_path / "Karaoke Studio").is_dir()
    assert not (tmp_path / APP_NAME).exists()


def test_rename_failure_is_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录被占用 / 跨盘时不能崩，settings.json 仍有读取兜底。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")

    def boom(*args, **kwargs):
        raise OSError("directory in use")

    monkeypatch.setattr(app_paths.os, "rename", boom)

    assert migrate_app_data_dir() is None
    assert (tmp_path / "Karaoke Studio").is_dir()
    notes = consume_migration_notes()
    assert any("迁移失败" in note for note in notes)


def test_newest_legacy_name_wins(tmp_path: Path) -> None:
    """同时残留两代旧目录时，必须搬最近的那个。

    ``LEGACY_APP_NAMES`` 的顺序是「越新越靠前」。若顺序写反，那些机器上还留着
    2026-06 合并前 ``Karaoke Helper`` 目录的用户会被拽回去读古董配置。
    """

    assert LEGACY_APP_NAMES.index("Karaoke Studio") < LEGACY_APP_NAMES.index("Karaoke Helper")

    _seed(tmp_path / "Karaoke Helper", "ANCIENT")
    _seed(tmp_path / "Karaoke Studio", "RECENT")

    migrate_app_data_dir()

    assert (tmp_path / APP_NAME / "settings.json").read_text(encoding="utf-8") == '{"marker": "RECENT"}'
    # 更古老的那个原样留着，不合并也不删
    assert (tmp_path / "Karaoke Helper" / "settings.json").read_text(encoding="utf-8") == '{"marker": "ANCIENT"}'


def test_dev_profile_migrates_from_dev_legacy_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源码调试档的旧名同样带 " Dev" 后缀，按后缀推导而不是写死两张表。"""

    monkeypatch.setenv(app_paths.SETTINGS_APP_NAME_ENV, f"{APP_NAME} Dev")
    _seed(tmp_path / "Karaoke Studio Dev", "DEV")

    migrate_app_data_dir()

    assert (tmp_path / f"{APP_NAME} Dev" / "settings.json").read_text(encoding="utf-8") == '{"marker": "DEV"}'


def test_legacy_settings_read_fallback_still_covers_settings_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移失败后的兜底：settings.json 仍能从旧目录读到。"""

    from krok_helper.settings import load_app_settings

    (tmp_path / "Karaoke Studio").mkdir()
    (tmp_path / "Karaoke Studio" / "settings.json").write_text(
        '{"ffmpeg_dir": "D:/legacy-ffmpeg"}', encoding="utf-8"
    )

    def boom(*args, **kwargs):
        raise OSError("directory in use")

    monkeypatch.setattr(app_paths.os, "rename", boom)
    migrate_app_data_dir()

    assert load_app_settings().ffmpeg_dir == "D:/legacy-ffmpeg"


def test_settings_dir_override_disables_the_legacy_read_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""``SETTINGS_DIR_ENV`` 的语义是「就用这个目录」，不许回退到 %APPDATA% 历史目录。

    这条曾经被违反过：隔离目录初始是空的，于是 ``_read_app_settings`` 一路回退到
    ``%APPDATA%\<历史名>\settings.json``，让整个测试套件读到用户的真实配置
    （改名把 ``Karaoke Studio`` 加进 ``LEGACY_APP_NAMES`` 之后才暴露出来）。
    """

    from krok_helper.settings import load_app_settings

    # %APPDATA% 下摆一份「真实」配置
    legacy = tmp_path / "Karaoke Studio"
    legacy.mkdir()
    (legacy / "settings.json").write_text('{"ffmpeg_dir": "D:/REAL"}', encoding="utf-8")

    # 覆盖目录存在但还是空的 —— 最容易踩坑的状态
    override = tmp_path / "isolated"
    override.mkdir()
    monkeypatch.setenv(app_paths.SETTINGS_DIR_ENV, str(override))

    assert app_paths.get_legacy_settings_paths() == []
    assert load_app_settings().ffmpeg_dir == ""


def test_legacy_paths_are_offered_when_no_override_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有覆盖时，历史路径照常参与兜底（顺序仍是越新越靠前）。"""

    monkeypatch.delenv(app_paths.SETTINGS_DIR_ENV, raising=False)
    paths = app_paths.get_legacy_settings_paths()

    assert [p.parent.name for p in paths] == list(LEGACY_APP_NAMES)
