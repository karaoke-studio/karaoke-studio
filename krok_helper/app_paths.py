"""用户数据目录的解析与跨版本迁移。

刻意保持**零重依赖**（只用标准库 + :mod:`krok_helper.config`）：
:func:`migrate_app_data_dir` 必须在日志系统初始化之前跑，而
``krok_helper.settings`` 会连带拉进整个 PyQt6，放在那里既拖慢启动，
又会让导入期的报错落在日志就绪之前、直接消失。

``settings`` 与 ``logging_config`` 都从这里取路径原语，避免像从前那样
各写一份硬编码的环境变量名而悄悄跑偏。
"""

from __future__ import annotations

import os
from pathlib import Path

from krok_helper.config import APP_NAME


SETTINGS_FILE_NAME = "settings.json"

#: 历史应用名，用于设置读取兜底与 :func:`migrate_app_data_dir` 的迁移来源。
#:
#: **顺序不可颠倒。** ``settings._read_app_settings`` 用
#: ``next(... if legacy.is_file())`` 取第一个存在的路径，所以必须「越新越靠前」。
#: 若把 ``Karaoke Helper`` 排前面，那些机器上还残留 2026-06 合并前旧目录的用户
#: 会被拽回去读古董配置。
LEGACY_APP_NAMES = ("Karaoke Studio", "Karaoke Helper")

SETTINGS_DIR_ENV = "KARAOKE_STUDIO_SETTINGS_DIR"
SETTINGS_APP_NAME_ENV = "KARAOKE_STUDIO_SETTINGS_APP_NAME"


def settings_path_for_app_name(app_name: str) -> Path:
    appdata = os.getenv("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata) / app_name / SETTINGS_FILE_NAME

    config_home = os.getenv("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / app_name.lower().replace(" ", "-") / SETTINGS_FILE_NAME

    return Path.home() / ".config" / app_name.lower().replace(" ", "-") / SETTINGS_FILE_NAME


def effective_app_name() -> str:
    return os.getenv(SETTINGS_APP_NAME_ENV, APP_NAME).strip() or APP_NAME


def get_settings_path() -> Path:
    settings_dir = os.getenv(SETTINGS_DIR_ENV)
    if settings_dir:
        return Path(settings_dir).expanduser() / SETTINGS_FILE_NAME

    return settings_path_for_app_name(effective_app_name())


def get_legacy_settings_paths() -> list[Path]:
    """历史应用名下的 settings.json 候选路径，按「越新越靠前」排列。

    ``SETTINGS_DIR_ENV`` 被显式指定时返回空列表：那个环境变量的语义是
    「**就用这个目录**」，不该在它还没有 settings.json 时偷偷回退到 ``%APPDATA%``
    下的历史目录去。（漏掉这条会让测试隔离形同虚设 —— 隔离目录初始是空的，
    于是每个用例都读到用户真实的配置；改名把 ``Karaoke Studio`` 加进历史名之后
    这个既存缺陷才暴露出来。）
    """

    if os.getenv(SETTINGS_DIR_ENV):
        return []
    return [settings_path_for_app_name(name) for name in LEGACY_APP_NAMES]


def legacy_app_names_for(app_name: str) -> tuple[str, ...]:
    """把有效应用名映射到它对应的历史名。

    源码调试档把应用名后缀成 ``"<APP_NAME> Dev"``（见 :mod:`krok_helper.runtime_profile`），
    它的历史名同样带 ``" Dev"`` 后缀，所以按后缀推导而不是写死两张表。
    """

    suffix = " Dev" if app_name.endswith(" Dev") else ""
    return tuple(f"{name}{suffix}" for name in LEGACY_APP_NAMES)


# migrate_app_data_dir 跑在日志系统初始化之前，没法直接写日志。这里沿用
# settings._LAST_CORRUPTION_BACKUP 的「先记下、日志起来后再取走」模式。
_MIGRATION_NOTES: list[str] = []


def consume_migration_notes() -> list[str]:
    """返回并清空迁移过程中攒下的说明；调用方负责在日志就绪后写出去。"""

    notes = list(_MIGRATION_NOTES)
    _MIGRATION_NOTES.clear()
    return notes


def migrate_app_data_dir() -> Path | None:
    """把改名前的用户数据目录整体搬到当前应用名下，返回实际迁移的旧目录。

    改名会让 ``%APPDATA%\\<应用名>\\`` 整个换位置，而这个目录里不只有
    ``settings.json`` —— 还挂着 ``subtitle_render_recovery/``、
    ``subtitle_render_backups/``（真实的未保存工程）、``lyrics_timing_cache/``、
    ``ai_models/``（可能数 GB）、``msst/``、``logs/``、``video_download/`` 的 Cookie。
    ``LEGACY_APP_NAMES`` 那套读取兜底只管 ``settings.json`` 一个文件，救不了其余目录，
    所以这里做一次目录级搬迁。

    用 :func:`os.rename` 而不是拷贝：同盘原子、瞬时完成，几 GB 的 AI 模型不用重下，
    也不会出现「拷到一半」的中间态。

    **幂等条件是「新目录已存在就跳过」**，刻意不用标记位 —— 标记位本身要存在被迁移的
    ``settings.json`` 里，会形成先有鸡还是先有蛋的问题。

    .. warning::
       必须在 ``configure_application_logging()`` **之前**调用。日志模块会在新目录下
       建 ``logs/``，一旦它先跑，新目录就存在了，本函数会永久跳过，用户数据全留在旧目录。
    """

    if os.getenv(SETTINGS_DIR_ENV):
        # 显式指定了目录，用户自己管，不要动。
        return None

    app_name = effective_app_name()
    new_dir = settings_path_for_app_name(app_name).parent
    if new_dir.exists():
        return None

    for legacy_name in legacy_app_names_for(app_name):
        if legacy_name == app_name:
            continue
        old_dir = settings_path_for_app_name(legacy_name).parent
        if not old_dir.is_dir():
            continue
        try:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old_dir, new_dir)
        except OSError as exc:
            # 目录被占用、跨盘、或另一个实例抢先建了新目录都会走到这里。
            # 不致命：settings.json 仍有 LEGACY_APP_NAMES 读取兜底。
            _MIGRATION_NOTES.append(f"用户数据目录迁移失败（{old_dir} → {new_dir}）：{exc}")
            return None
        _MIGRATION_NOTES.append(f"用户数据目录已迁移：{old_dir} → {new_dir}")
        return old_dir

    return None
