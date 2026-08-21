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
    # 惰性触发的「只跑一次」标记是进程级的，用例之间必须归零，否则第二个用例开始
    # 就再也触发不了迁移（测出来的绿是假的）。
    monkeypatch.setattr(app_paths, "_migration_attempted", False)
    monkeypatch.setattr(app_paths, "_migration_running", False)
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
    """新目录已存在 → 里面的东西一律不动（补空缺是另一条用例的事）。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")
    _seed(tmp_path / APP_NAME, "CURRENT")

    assert migrate_app_data_dir() is None
    assert (tmp_path / APP_NAME / "settings.json").read_text(encoding="utf-8") == '{"marker": "CURRENT"}'
    assert (tmp_path / "Karaoke Studio" / "settings.json").read_text(encoding="utf-8") == '{"marker": "OLD"}'
    assert (tmp_path / APP_NAME / "ai_models" / "model.bin").stat().st_size == 512


def test_directory_created_by_logging_does_not_strand_the_old_data(tmp_path: Path) -> None:
    """「更新完一版，配置全没了」的正解。

    只要有谁先一步把新应用名目录建了出来（日志初始化、独立入口、一次设置保存…），
    ``new_dir.exists()`` 就成立、整轮迁移被永久跳过，用户几 GB 数据留在旧目录里
    再没人看。现在这种情况下要把旧目录的内容接回来。
    """

    _seed(tmp_path / "Karaoke Studio", "OLD")
    (tmp_path / APP_NAME / "logs").mkdir(parents=True)  # 日志抢先建目录

    migrate_app_data_dir()

    new = tmp_path / APP_NAME
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"marker": "OLD"}'
    assert (new / "video_download" / "bilibili_cookies.txt").read_text(encoding="utf-8") == "OLD"
    assert (new / "ai_models" / "model.bin").stat().st_size == 512
    assert (new / "subtitle_render_backups" / "unsaved.yurika").is_file()
    assert (new / "logs").is_dir()  # 抢先建出来的那个原样保留
    assert any("接回" in note for note in consume_migration_notes())


def test_adoption_never_overwrites_what_the_new_directory_already_has(tmp_path: Path) -> None:
    """只补不覆盖：用户在新版里已经存过的配置绝不能被旧的盖回去。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")
    new = tmp_path / APP_NAME
    new.mkdir()
    (new / "settings.json").write_text('{"marker": "CURRENT"}', encoding="utf-8")

    migrate_app_data_dir()

    assert (new / "settings.json").read_text(encoding="utf-8") == '{"marker": "CURRENT"}'
    # 但 settings.json 兜底救不了的那些目录还是接回来了
    assert (new / "video_download" / "bilibili_cookies.txt").read_text(encoding="utf-8") == "OLD"
    assert (new / "ai_models" / "model.bin").stat().st_size == 512
    # 没被接走的那份留在旧目录，不删
    assert (tmp_path / "Karaoke Studio" / "settings.json").is_file()


def test_no_note_when_the_new_directory_was_already_complete(tmp_path: Path) -> None:
    """正常启动（新旧目录条目一致）不该每次都往日志里灌同一段话。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")
    _seed(tmp_path / APP_NAME, "CURRENT")

    migrate_app_data_dir()

    assert consume_migration_notes() == []


def test_settings_path_triggers_migration_without_an_entry_point(tmp_path: Path) -> None:
    """迁移不再押在「谁先跑」上：解析设置路径这一下自己就会把它带起来。"""

    _seed(tmp_path / "Karaoke Studio", "OLD")

    path = app_paths.get_settings_path()

    assert path == tmp_path / APP_NAME / "settings.json"
    assert path.read_text(encoding="utf-8") == '{"marker": "OLD"}'
    assert not (tmp_path / "Karaoke Studio").exists()


def test_lazy_migration_runs_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        app_paths, "migrate_app_data_dir", lambda: calls.append(1)
    )

    app_paths.ensure_app_data_migrated()
    app_paths.ensure_app_data_migrated()

    assert len(calls) == 1


def test_migration_notes_also_land_in_the_startup_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移跑在日志之前，而日志本身也出过整轮没起来的现场，所以要双份记录。"""

    trace = tmp_path / "startup-trace.log"
    monkeypatch.setenv("KARAOKE_STUDIO_STARTUP_TRACE", str(trace))
    _seed(tmp_path / "Karaoke Studio", "OLD")

    migrate_app_data_dir()

    assert "appdata.migration" in trace.read_text(encoding="utf-8")
    assert "用户数据目录已迁移" in trace.read_text(encoding="utf-8")


def test_rename_that_lands_nowhere_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.rename`` 成功不等于文件真在我们以为的位置。

    有文件重定向的运行环境（MSIX 沙箱、安全软件虚拟化、同步盘）会把整个目录物化
    到别处，应用下次以正常身份启动就什么都找不到 —— 这种「静默成功」必须留下话。
    """

    _seed(tmp_path / "Karaoke Studio", "OLD")
    monkeypatch.setattr(app_paths.os, "rename", lambda *a, **k: None)  # 假装成功、其实没动

    assert migrate_app_data_dir() is None
    assert any("可能被文件重定向接管" in note for note in consume_migration_notes())


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
