from __future__ import annotations

import json
import logging
import os
import shutil
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from krok_helper.config import APP_NAME
# 路径原语集中在 app_paths（零重依赖），这里重新导出，历史 import 点无需改动。
from krok_helper.app_paths import (  # noqa: F401
    LEGACY_APP_NAMES,
    SETTINGS_APP_NAME_ENV,
    SETTINGS_DIR_ENV,
    SETTINGS_FILE_NAME,
    consume_migration_notes,
    get_legacy_settings_paths,
    get_settings_path,
    settings_path_for_app_name as _settings_path_for_app_name,
)
from krok_helper.audio_alignment import (
    DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE,
    DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE,
    ENCODE_MODE_HARDWARE,
    ENCODE_MODE_SOFTWARE,
)
from krok_helper.pipeline import DEFAULT_OFF_NAME_TEMPLATE, DEFAULT_ON_NAME_TEMPLATE, OUTPUT_NAME_MODE_FIXED
from krok_helper.lyrics import DEFAULT_LYRICS_PROVIDER_IDS, LYRICS_LANGUAGE_ORIGINAL, LYRICS_PREVIEW_LINE
from krok_helper.video_download.download_task import (
    DEFAULT_TIMEOUT,
    NAMING_RULE_TITLE,
    SOURCE_YOUTUBE,
    TIMEOUT_CHOICES,
)


ALIGN_TARGET_VIDEO = "video"
ALIGN_TARGET_AUDIO = "audio"
ALIGN_OUTPUT_DIR_SOURCE_VIDEO = "source_video"
ALIGN_OUTPUT_DIR_CUSTOM = "custom"


# 工作台界面主题：跟随系统 / 强制浅色 / 强制深色。
# 与 SUG ``frontend/theme.py::ThemeMode`` 对应。新值必须同步两边。
UI_THEME_AUTO = "auto"
UI_THEME_LIGHT = "light"
UI_THEME_DARK = "dark"
UI_THEMES = {UI_THEME_AUTO, UI_THEME_LIGHT, UI_THEME_DARK}

# StrangeUtaGame 一次性迁移使用的 marker。
# True 表示已经检测过老路径并完成（或确认无须）导入，不再重复。
LYRICS_TIMING_MIGRATED_KEY = "lyrics_timing_migrated_v1"


@dataclass
class AppSettings:
    output_name_mode: str = OUTPUT_NAME_MODE_FIXED
    on_name_template: str = DEFAULT_ON_NAME_TEMPLATE
    off_name_template: str = DEFAULT_OFF_NAME_TEMPLATE
    align_video_name_template: str = DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
    align_audio_name_template: str = DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
    ffmpeg_dir: str = ""
    align_target: str = ALIGN_TARGET_VIDEO
    align_encode_mode: str = ENCODE_MODE_SOFTWARE
    align_force_1080p60: bool = False
    align_export_use_video_audio: bool = False
    align_output_dir_mode: str = ALIGN_OUTPUT_DIR_SOURCE_VIDEO
    align_output_custom_dir: str = ""
    lyrics_source_ids: list[str] | tuple[str, ...] = DEFAULT_LYRICS_PROVIDER_IDS
    lyrics_preview_mode: str = LYRICS_PREVIEW_LINE
    lyrics_language: str = LYRICS_LANGUAGE_ORIGINAL
    lyrics_strip_intro_lines: bool = True
    video_download_save_dir: str = ""
    video_download_naming_rule: str = NAMING_RULE_TITLE
    video_download_custom_template: str = "{title}"
    video_download_merge_video_audio: bool = True
    video_download_download_thumbnail: bool = False
    video_download_download_subtitle: bool = False
    video_download_concurrent_count: int = 3
    # 5 秒对海外线路太紧：B 站 CDN 抖一下就判超时。10 秒配合分块/多连接重试更稳。
    video_download_timeout: int = DEFAULT_TIMEOUT
    video_download_retry_count: int = 3
    # B 站下载走 aria2c 多连接（检测不到 aria2c 时自动退回 HTTP 分块 Range）
    video_download_use_aria2c: bool = True
    video_download_cookie_path: str = ""
    video_download_source: str = SOURCE_YOUTUBE
    # 视频下载页两组 QSplitter 的像素尺寸。空列表表示用户尚未调整过，页面会使用
    # 当前版本的紧凑默认布局。
    video_download_main_splitter_sizes: list[int] = field(default_factory=list)
    video_download_content_splitter_sizes: list[int] = field(default_factory=list)
    updater: dict = field(default_factory=dict)

    # ── 工作台界面主题 ──
    # ``auto`` 跟随 OS；``light`` / ``dark`` 强制。SUG embedded 嵌入区与所有工作台
    # 页面共享同一份主题状态（由 ``theme_workbench`` 单例驱动）。非法值在
    # ``load_app_settings`` 中 fallback 到 ``auto`` 并 warn。
    ui_theme: str = UI_THEME_AUTO

    # ── 顶部工作流栏紧凑模式 ──
    # True 时把 6 步切换条折叠成一条窄行（只剩编号 + 当前步骤名），所有步骤仍可点。
    # 全局开关（不分页面），用户在工作流栏右上角的折叠按钮里手动切换并持久化。
    workflow_compact: bool = False

    # ── 歌词打轴模块（StrangeUtaGame）的设置 namespace ──
    # 由 frontend/settings/app_settings.py 中 AppSettings 的 dotted-key 树
    # 序列化/反序列化得来，宿主直接以 dict 形式持久化；StrangeUtaGame
    # 内部继续按它自己的 ``get("audio.default_volume")`` 风格读写。
    # 大字典/列表型字段单独建独立 namespace，避免 lyrics_timing 主 dict 过大。
    lyrics_timing: dict = field(default_factory=dict)
    lyrics_timing_dictionary: list = field(default_factory=list)
    lyrics_timing_singers: list = field(default_factory=list)
    lyrics_timing_network_dictionary: dict = field(default_factory=dict)
    # 一次性迁移 marker（见 :func:`migrate_strange_uta_game_settings`）
    lyrics_timing_migrated_v1: bool = False

    # ── 字幕视频渲染模块（subtitle_render）的设置 namespace ──
    # 由模块内部以 dict 形式自由读写，宿主只负责整体持久化；具体字段（字体、
    # 输出目录、硬编偏好、最近样式预设等）在模块 P0-A8 输出 MP4 时落地。
    subtitle_render: dict = field(default_factory=dict)

    # ── 「音视频处理」模块（音频分离 / PyMSS）的设置 namespace ──
    # 需求文档 docs/音视频处理-PyMSS音频分离需求设计.md §10：模块内部以 dict
    # 形式读写，保存安装位置、Runtime 来源、任务模型绑定、输出设置与
    # last_internal_tab 等。API key 不写入本 namespace（每次启动临时生成）。
    pymss: dict = field(default_factory=dict)


# settings.json 解析失败时，``load_app_settings`` 会把坏文件备份并把备份路径
# 记到这里。GUI 在主窗口起来后调 :func:`consume_corruption_backup` 取走（同时清零）
# 并向用户弹窗，避免坏文件被默默丢弃后用户毫不知情就发现配置「全空了」。
_LAST_CORRUPTION_BACKUP: Path | None = None


def consume_corruption_backup() -> Path | None:
    """返回上次 load 的损坏备份路径并清零；调用方负责把它展示给用户。"""
    global _LAST_CORRUPTION_BACKUP
    backup = _LAST_CORRUPTION_BACKUP
    _LAST_CORRUPTION_BACKUP = None
    return backup


def _backup_corrupt_settings(path: Path, reason: str) -> None:
    """把损坏的 settings.json 复制成 ``<name>.corrupt-<ts>`` 备份。

    备份失败本身只 warn 不抛——load_app_settings 总要返回，备份只是兜底。
    """
    global _LAST_CORRUPTION_BACKUP
    log = logging.getLogger(__name__)
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.parent / f"{path.name}.corrupt-{ts}"
        # copy2 保留 mtime 便于追溯出事时间
        shutil.copy2(path, backup)
        _LAST_CORRUPTION_BACKUP = backup
        log.error("settings.json 解析失败（%s），已备份原文件到 %s", reason, backup)
    except Exception:
        log.error("settings.json 解析失败（%s），且备份失败", reason, exc_info=True)


def load_app_settings() -> AppSettings:
    return _stamp_baseline(_read_app_settings())


def _read_app_settings() -> AppSettings:
    path = get_settings_path()
    if not path.is_file():
        path = next((legacy for legacy in get_legacy_settings_paths() if legacy.is_file()), path)
    if not path.is_file():
        return AppSettings()

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        _backup_corrupt_settings(path, "读取失败")
        return AppSettings()

    try:
        payload = json.loads(raw)
    except Exception:
        _backup_corrupt_settings(path, "JSON 解析失败")
        return AppSettings()

    if not isinstance(payload, dict):
        _backup_corrupt_settings(path, "顶层不是 JSON 对象")
        return AppSettings()

    return _settings_from_payload(payload)


def _settings_from_payload(payload: dict) -> AppSettings:
    align_target = str(payload.get("align_target", ALIGN_TARGET_VIDEO))
    if align_target not in {ALIGN_TARGET_VIDEO, ALIGN_TARGET_AUDIO}:
        align_target = ALIGN_TARGET_VIDEO
    align_encode_mode = str(payload.get("align_encode_mode", ENCODE_MODE_SOFTWARE))
    if align_encode_mode not in {ENCODE_MODE_SOFTWARE, ENCODE_MODE_HARDWARE}:
        align_encode_mode = ENCODE_MODE_SOFTWARE
    align_output_dir_mode = str(payload.get("align_output_dir_mode", ALIGN_OUTPUT_DIR_SOURCE_VIDEO))
    if align_output_dir_mode not in {ALIGN_OUTPUT_DIR_SOURCE_VIDEO, ALIGN_OUTPUT_DIR_CUSTOM}:
        align_output_dir_mode = ALIGN_OUTPUT_DIR_SOURCE_VIDEO
    ui_theme_raw = str(payload.get("ui_theme", UI_THEME_AUTO))
    if ui_theme_raw not in UI_THEMES:
        logging.getLogger(__name__).warning(
            "settings.json ui_theme=%r 非法，回落到 %s", ui_theme_raw, UI_THEME_AUTO
        )
        ui_theme_raw = UI_THEME_AUTO

    ffmpeg_dir = str(payload.get("ffmpeg_dir", ""))
    if ffmpeg_dir.strip() == ".":
        ffmpeg_dir = ""

    return AppSettings(
        output_name_mode=str(payload.get("output_name_mode", OUTPUT_NAME_MODE_FIXED)),
        on_name_template=str(payload.get("on_name_template", DEFAULT_ON_NAME_TEMPLATE)),
        off_name_template=str(payload.get("off_name_template", DEFAULT_OFF_NAME_TEMPLATE)),
        align_video_name_template=str(
            payload.get("align_video_name_template", DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE)
        ),
        align_audio_name_template=str(
            payload.get("align_audio_name_template", DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE)
        ),
        ffmpeg_dir=ffmpeg_dir,
        align_target=align_target,
        align_encode_mode=align_encode_mode,
        align_force_1080p60=bool(payload.get("align_force_1080p60", False)),
        align_export_use_video_audio=bool(payload.get("align_export_use_video_audio", False)),
        align_output_dir_mode=align_output_dir_mode,
        align_output_custom_dir=str(payload.get("align_output_custom_dir", "")),
        lyrics_source_ids=tuple(
            str(item)
            for item in payload.get("lyrics_source_ids", DEFAULT_LYRICS_PROVIDER_IDS)
            if str(item).strip()
        )
        or DEFAULT_LYRICS_PROVIDER_IDS,
        lyrics_preview_mode=str(payload.get("lyrics_preview_mode", LYRICS_PREVIEW_LINE)),
        lyrics_language=str(payload.get("lyrics_language", LYRICS_LANGUAGE_ORIGINAL)),
        lyrics_strip_intro_lines=bool(payload.get("lyrics_strip_intro_lines", True)),
        video_download_save_dir=str(payload.get("video_download_save_dir", "")),
        video_download_naming_rule=str(payload.get("video_download_naming_rule", NAMING_RULE_TITLE)),
        video_download_custom_template=str(payload.get("video_download_custom_template", "{title}")),
        video_download_merge_video_audio=bool(payload.get("video_download_merge_video_audio", True)),
        video_download_download_thumbnail=bool(payload.get("video_download_download_thumbnail", False)),
        video_download_download_subtitle=bool(payload.get("video_download_download_subtitle", False)),
        video_download_concurrent_count=min(5, max(1, int(payload.get("video_download_concurrent_count", 3) or 3))),
        video_download_timeout=(
            int(payload.get("video_download_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
            if int(payload.get("video_download_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT) in TIMEOUT_CHOICES
            else DEFAULT_TIMEOUT
        ),
        video_download_retry_count=min(5, max(1, int(payload.get("video_download_retry_count", 3) or 3))),
        video_download_use_aria2c=bool(payload.get("video_download_use_aria2c", True)),
        video_download_cookie_path=str(payload.get("video_download_cookie_path", "")),
        video_download_source=str(payload.get("video_download_source", SOURCE_YOUTUBE)),
        video_download_main_splitter_sizes=_safe_splitter_sizes(
            payload.get("video_download_main_splitter_sizes"), 2
        ),
        video_download_content_splitter_sizes=_safe_splitter_sizes(
            payload.get("video_download_content_splitter_sizes"), 3
        ),
        updater=_safe_dict(payload.get("updater")),
        ui_theme=ui_theme_raw,
        lyrics_timing=_safe_dict(payload.get("lyrics_timing")),
        lyrics_timing_dictionary=_safe_list(payload.get("lyrics_timing_dictionary")),
        lyrics_timing_singers=_safe_list(payload.get("lyrics_timing_singers")),
        lyrics_timing_network_dictionary=_safe_dict(payload.get("lyrics_timing_network_dictionary")),
        lyrics_timing_migrated_v1=bool(payload.get(LYRICS_TIMING_MIGRATED_KEY, False)),
        workflow_compact=bool(payload.get("workflow_compact", False)),
        subtitle_render=_safe_dict(payload.get("subtitle_render")),
        pymss=_safe_dict(payload.get("pymss")),
    )


#: 由各模块自己的设置桥负责读写的命名空间。
#:
#: 打轴（SUG）和字幕渲染都通过设置桥持久化，桥每次都是"读盘 → 只改自己那段 →
#: 写盘"。别的地方（工作台外壳、全局设置对话框、更新器……）写的却是**本进程启动
#: 时读到的那份整份快照**，于是同时开着两个工作台时，后写的会把先写的整份顶掉：
#: 在 A 里存进预设库的配色方案，B 随手换个主题就没了，等 A 退出再写一次才回来
#: ——看起来就像"要关掉应用才保存"。
MODULE_NAMESPACE_FIELDS = (
    "lyrics_timing",
    "lyrics_timing_dictionary",
    "lyrics_timing_singers",
    "lyrics_timing_network_dictionary",
    "subtitle_render",
)


#: 本实例上一次"看到"的整份配置，挂在 :class:`AppSettings` 实例上。
#:
#: 不是 dataclass 字段，所以 :func:`dataclasses.asdict` 不会把它写进 settings.json。
#: 只有 :func:`load_app_settings` 会盖这个章 —— 手搓出来的 ``AppSettings()`` 没有
#: 基线，写盘时按老路子整份覆盖（测试和首次运行都走这条）。
_BASELINE_ATTR = "_krok_disk_baseline"


def _stamp_baseline(settings: AppSettings) -> AppSettings:
    setattr(settings, _BASELINE_ATTR, asdict(settings))
    return settings


def _read_raw_settings() -> dict | None:
    """读盘上的原始 JSON。读不出来（首次保存、损坏、被占用）就返回 ``None``。

    这里不走 :func:`load_app_settings`：后者遇到坏文件会备份并退回默认值，拿那份
    去合并等于把好好的配置清成默认。
    """
    try:
        raw = json.loads(get_settings_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, dict) else None


def _merge_untouched_fields_from_disk(
    settings: AppSettings, payload: dict, raw: dict | None
) -> dict:
    """本实例没动过的字段，一律用盘上的最新值。

    每个工作台写的都是**自己启动时读到的那份快照**。同时开着两个时，后写的那个
    会拿一整份过期数据把先写的顶掉 —— 在 A 里改的导出目录，B 随手换个主题就没了
    （:data:`MODULE_NAMESPACE_FIELDS` 只护住了有设置桥的那几段命名空间，别的
    字段全裸奔）。

    判据是"和基线比有没有变"，不是"和默认值比"：变了说明是本实例改的，该写；
    没变说明本实例只是把启动时读到的值原样端回来，此时盘上如果更新了，那是另一个
    实例的改动，得让给它。同一个字段两边都改就是后写的赢 —— 跨进程没有更好的答案。
    """
    baseline = getattr(settings, _BASELINE_ATTR, None)
    if raw is None or not isinstance(baseline, dict):
        return payload
    disk = asdict(_settings_from_payload(raw))
    for name, value in payload.items():
        # 盘上没有这一项：老版本写的文件，或者本实例是第一个写它的人。
        if name in raw and value == baseline.get(name):
            payload[name] = disk[name]
    return payload


def sync_baseline_field(settings: AppSettings, name: str) -> None:
    """告诉基线：这个字段现在的值是刚从盘上读来的，不算本实例的改动。

    设置桥写完自己那段之后会把值同步回宿主的 ``AppSettings``。不打这个招呼的话，
    宿主下次整份写盘时会认为"这个字段我改过"，于是拿它去压别的实例刚写的值。
    """
    baseline = getattr(settings, _BASELINE_ATTR, None)
    if isinstance(baseline, dict):
        baseline[name] = deepcopy(getattr(settings, name))


def _merge_module_namespaces_from_disk(settings: AppSettings, raw: dict | None) -> None:
    """把模块命名空间就地换成盘上的最新值。

    桥总是先写盘再更新内存，所以盘上的永远不会比内存里旧 —— 取盘上的既能修好跨
    实例覆盖，也不会把本实例刚写的读回旧值。

    **本实例主动改过的那几段除外**：一次性迁移、「导入旧版 SUG 配置」、字幕渲染独
    立运行时的兜底保存，写的正是这些命名空间，无条件换成盘上的等于这次保存什么都
    没干（用户那边看到的是"提示导入成功、其实一条都没进来"）。判据和普通字段一
    样：和基线比变了就是本实例的改动。没有基线（手搓的 ``AppSettings``）时按老
    路子无条件换，那条路上本来也没人有资格声称"我改过"。

    ``raw`` 是盘上的原始 JSON，``None``（首次保存、损坏、被占用）时什么都不做。
    """
    if raw is None:
        return
    baseline = getattr(settings, _BASELINE_ATTR, None)
    for name in MODULE_NAMESPACE_FIELDS:
        if isinstance(baseline, dict) and getattr(settings, name, None) != baseline.get(name):
            continue  # 本实例改过这一段，它就是要写的东西
        if name not in raw:
            continue
        current = getattr(settings, name, None)
        value = raw[name]
        if isinstance(current, dict):
            setattr(settings, name, _safe_dict(value))
        elif isinstance(current, list):
            setattr(settings, name, _safe_list(value))


def save_app_settings(
    settings: AppSettings, *, merge_module_namespaces: bool = True
) -> Path:
    """原子写 settings.json。

    实现：先写到 ``settings.json.tmp``，再 ``os.replace`` 替换。Windows 与 POSIX 上
    ``os.replace`` 都是原子操作——进程被杀只会留下未完成的 ``.tmp``，本体仍是上一次
    完整版本。**绝不能**退回到 ``path.write_text`` 直接覆盖：那条路径里
    ``open(w)`` 先 truncate 再 write，崩在中间的话主文件就是空 / 半截，下次启动
    会触发「全空配置」事故（v3.0.x 真实案例，v3.0.5 修复）。

    写的是**三方合并**的结果而不是内存里那份快照：本实例改过的字段照写，没动过的
    字段取盘上的最新值（见 :func:`_merge_untouched_fields_from_disk`），这样多开时
    两边的改动能各自留下。

    ``merge_module_namespaces`` 额外把 :data:`MODULE_NAMESPACE_FIELDS` 强行换成盘
    上的值。**只有模块自己的设置桥**该传 ``False`` —— 它们要写的正是那几段。
    """
    # 盘上那份只读一次：两处合并都要用它，而写盘会被界面上的每一次改动触发。
    raw = _read_raw_settings()
    if merge_module_namespaces:
        _merge_module_namespaces_from_disk(settings, raw)
    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    snapshot = asdict(settings)
    payload = _merge_untouched_fields_from_disk(settings, dict(snapshot), raw)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    # 基线跟的是**内存里这份**，不是刚写下去的那份：没动过的字段要一直判"没变"，
    # 才能在后续每一次写盘时继续让位给别的实例。
    setattr(settings, _BASELINE_ATTR, snapshot)
    return path


def _safe_dict(value: object) -> dict:
    """Return ``value`` if it's a dict, otherwise an empty dict.

    Used by :func:`load_app_settings` when loading namespace fields that
    must always end up as dicts even if the on-disk JSON contained a
    non-dict value due to corruption or version skew.
    """
    return value if isinstance(value, dict) else {}


def _safe_list(value: object) -> list:
    """Return ``value`` if it's a list, otherwise an empty list."""
    return value if isinstance(value, list) else []


def _safe_splitter_sizes(value: object, expected_count: int) -> list[int]:
    """Return a valid persisted QSplitter size list, or the default marker."""
    if not isinstance(value, (list, tuple)) or len(value) != expected_count:
        return []
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value):
        return []
    sizes = [int(item) for item in value]
    return sizes if sum(sizes) > 0 else []


# ════════════════════════════════════════════════════════════════════
# StrangeUtaGame 一次性配置迁移
# ════════════════════════════════════════════════════════════════════

# StrangeUtaGame 在 standalone 模式下回退到这个目录写 config 当程序目录
# 不可写时。见 ``frontend/settings/app_settings.py:get_config_dir``。
_LEGACY_STRANGE_UTA_GAME_DIR = Path.home() / ".strange_uta_game"
_LEGACY_FILES = {
    "lyrics_timing": "config.json",
    "lyrics_timing_dictionary": "dictionary.json",
    "lyrics_timing_singers": "singers.json",
    "lyrics_timing_network_dictionary": "network_dictionary.json",
}
_LEGACY_LIST_FIELDS = {"lyrics_timing_dictionary", "lyrics_timing_singers"}


def migrate_strange_uta_game_settings(
    settings: AppSettings,
    legacy_dir: Path | None = None,
) -> bool:
    """One-shot import of legacy StrangeUtaGame JSON files into ``settings``.

    Scans ``legacy_dir`` (defaults to ``~/.strange_uta_game``) for the four
    standalone JSON config files and merges their contents into the
    matching namespace fields on the in-memory ``AppSettings``. If the
    marker ``lyrics_timing_migrated_v1`` is already True, returns False
    immediately so re-runs are no-ops.

    The caller is responsible for persisting the result via
    :func:`save_app_settings` — this function mutates ``settings`` in
    place but does not touch disk on the krok-helper side. Reads from the
    legacy files are best-effort: corrupt/missing files are skipped
    silently.

    Returns:
        True if at least one legacy file was imported AND the marker
        flipped to True. False if the marker was already set, the legacy
        dir doesn't exist, or no files were importable.
    """
    if settings.lyrics_timing_migrated_v1:
        return False

    src = legacy_dir if legacy_dir is not None else _LEGACY_STRANGE_UTA_GAME_DIR
    if not src.is_dir():
        # 没有老安装可迁移 —— marker 直接置位避免每次启动都扫盘。
        settings.lyrics_timing_migrated_v1 = True
        return False

    log = logging.getLogger(__name__)
    imported_any = False
    for namespace_field, filename in _LEGACY_FILES.items():
        legacy_path = src / filename
        if not legacy_path.is_file():
            continue
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("legacy StrangeUtaGame file unreadable: %s", legacy_path, exc_info=True)
            continue
        if namespace_field in _LEGACY_LIST_FIELDS:
            if not isinstance(payload, list):
                continue
        elif not isinstance(payload, dict):
            continue
        # 直接覆盖整个 namespace；老用户首次启动我们以"老配置为准"。
        setattr(settings, namespace_field, payload)
        imported_any = True

    settings.lyrics_timing_migrated_v1 = True
    return imported_any


# ════════════════════════════════════════════════════════════════════
# StrangeUtaGame 手动数据导入（用户在工作台「全局设置 → 工具」触发）
# ════════════════════════════════════════════════════════════════════

# 预期文件名 → 工作台 namespace 字段名
_LEGACY_IMPORT_FILES: tuple[tuple[str, str], ...] = (
    ("config.json", "lyrics_timing"),
    ("dictionary.json", "lyrics_timing_dictionary"),
    ("singers.json", "lyrics_timing_singers"),
    ("network_dictionary.json", "lyrics_timing_network_dictionary"),
)


def import_legacy_sug_settings(src_dir: Path, settings: AppSettings) -> dict:
    """从用户指定的旧版 SUG 目录读取四类持久化数据并合并进 ``settings``。

    冲突策略：
    - 主 config（``config.json`` → ``lyrics_timing``）：按 SUG
      ``AppSettings.DEFAULT_SETTINGS`` 树过滤未知 key 后整体覆盖。缺失项不补
      默认值，由 SUG 运行时 ``get()`` 自带的 fallback 处理。
    - 词典（按 ``word`` 去重）、演唱者（按 ``name`` 去重）：合并到现有列表，
      host 已有同 key 的条目优先保留——避免覆盖用户在工作台里改过的数据。
    - 网络词典 cache：整体覆盖（cache 是 last_fetched + entries，无清晰的
      合并语义）。

    调用方负责事后 ``save_app_settings(settings)``。本函数只动内存对象。

    Returns:
        报告 dict，键见实现；GUI 用它生成结果对话框。
    """
    report: dict = {
        "imported": [],
        "missing": [],
        "errors": [],
        "skipped_unknown_keys": [],
        "added_dict_entries": 0,
        "added_singers": 0,
    }
    log = logging.getLogger(__name__)

    for filename, _field in _LEGACY_IMPORT_FILES:
        if not (src_dir / filename).is_file():
            report["missing"].append(filename)

    config_path = src_dir / "config.json"
    if config_path.is_file():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                schema = _sug_default_settings_schema()
                filtered, unknown = _filter_against_schema(payload, schema, prefix="")
                settings.lyrics_timing = filtered
                report["imported"].append("config.json")
                report["skipped_unknown_keys"].extend(sorted(unknown))
            else:
                report["errors"].append(("config.json", "顶层不是 JSON 对象"))
        except Exception as exc:
            log.warning("legacy SUG config.json 导入失败", exc_info=True)
            report["errors"].append(("config.json", str(exc)))

    dict_path = src_dir / "dictionary.json"
    if dict_path.is_file():
        try:
            payload = json.loads(dict_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                added = _merge_list_by_key(
                    existing=settings.lyrics_timing_dictionary,
                    incoming=payload,
                    key_field="word",
                )
                settings.lyrics_timing_dictionary = added["merged"]
                report["added_dict_entries"] = added["added"]
                report["imported"].append("dictionary.json")
            else:
                report["errors"].append(("dictionary.json", "顶层不是 JSON 数组"))
        except Exception as exc:
            log.warning("legacy SUG dictionary.json 导入失败", exc_info=True)
            report["errors"].append(("dictionary.json", str(exc)))

    singers_path = src_dir / "singers.json"
    if singers_path.is_file():
        try:
            payload = json.loads(singers_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                added = _merge_list_by_key(
                    existing=settings.lyrics_timing_singers,
                    incoming=payload,
                    key_field="name",
                )
                settings.lyrics_timing_singers = added["merged"]
                report["added_singers"] = added["added"]
                report["imported"].append("singers.json")
            else:
                report["errors"].append(("singers.json", "顶层不是 JSON 数组"))
        except Exception as exc:
            log.warning("legacy SUG singers.json 导入失败", exc_info=True)
            report["errors"].append(("singers.json", str(exc)))

    network_path = src_dir / "network_dictionary.json"
    if network_path.is_file():
        try:
            payload = json.loads(network_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                settings.lyrics_timing_network_dictionary = payload
                report["imported"].append("network_dictionary.json")
            else:
                report["errors"].append(("network_dictionary.json", "顶层不是 JSON 对象"))
        except Exception as exc:
            log.warning("legacy SUG network_dictionary.json 导入失败", exc_info=True)
            report["errors"].append(("network_dictionary.json", str(exc)))

    return report


def _sug_default_settings_schema() -> dict:
    """读取 SUG ``AppSettings.DEFAULT_SETTINGS`` 作为「合法 key 清单」的事实来源。

    懒加载——只在导入时才 import strange_uta_game，避免无谓启动开销。
    导入失败（比如 submodule 没初始化）时返回空 dict —— 等价于「所有 key 都未知」，
    保险起见整体放行（让后续 _filter_against_schema 直通），调用方仍能拿到字典。
    """
    try:
        import krok_helper  # noqa: F401 — installs bundled SUG src on sys.path
        from strange_uta_game.frontend.settings.app_settings import AppSettings as SugAppSettings
        return SugAppSettings.DEFAULT_SETTINGS
    except Exception:
        logging.getLogger(__name__).warning(
            "无法加载 SUG AppSettings.DEFAULT_SETTINGS，将跳过未知 key 过滤",
            exc_info=True,
        )
        return {}


def _filter_against_schema(
    payload: dict,
    schema: dict,
    prefix: str,
) -> tuple[dict, list[str]]:
    """按 ``schema`` 的**顶层 namespace** 过滤 ``payload``；返回（保留的 dict，丢弃的 key 列表）。

    只比对顶层 key：``audio`` / ``ui`` / ``timing`` / ``export`` / ``shortcuts`` 这些
    namespace 在 SUG 里是稳定的，``DEFAULT_SETTINGS`` 都有声明；但 namespace **内部**
    的子键并不全部出现在 ``DEFAULT_SETTINGS`` 里——SUG 大量运行时通过
    ``s.get("ui.current_line_font_size", 22)`` 这种「裸 key + 硬编码默认值」读取
    未在 schema 中声明的合法值。递归过滤会把这些当作未知项丢掉，导致用户从老版本
    standalone SUG 导入的字体大小、间距、行高系数等界面设置全部失效。

    因此只在顶层做白名单：未知顶层 namespace（如老版本残留或写坏的 JSON）会被丢掉
    并记入 report；已知 namespace 整个 subtree 透传，把判断子键是否合法的责任交回
    SUG 运行时（不认识的 key 也只是不被 ``get()`` 读到而已）。

    schema 为空 dict（submodule 加载失败兜底）→ 整体放行。
    """
    if not schema:
        return deepcopy(payload), []

    kept: dict = {}
    dropped: list[str] = []
    for key, value in payload.items():
        if key in schema:
            kept[key] = deepcopy(value)
        else:
            dropped.append(f"{prefix}.{key}" if prefix else key)
    return kept, dropped


def _merge_list_by_key(
    existing: list,
    incoming: list,
    key_field: str,
) -> dict:
    """合并 ``incoming`` 到 ``existing``，按 ``key_field`` 去重，``existing`` 优先。

    Returns:
        ``{"merged": <合并后的新列表>, "added": <新增条目数>}``
    """
    merged = [deepcopy(item) for item in existing if isinstance(item, dict)]
    seen = {
        item[key_field]
        for item in merged
        if isinstance(item.get(key_field), (str, int))
    }
    added = 0
    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = item.get(key_field)
        if not isinstance(key, (str, int)) or key in seen:
            continue
        merged.append(deepcopy(item))
        seen.add(key)
        added += 1
    return {"merged": merged, "added": added}
