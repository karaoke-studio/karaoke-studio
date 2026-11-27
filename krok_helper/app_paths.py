"""用户数据目录的解析与跨版本迁移。

刻意保持**零重依赖**（只用标准库 + :mod:`krok_helper.config`）：迁移要在日志系统
之前就能跑，而 ``krok_helper.settings`` 会连带拉进整个 PyQt6，放在那里既拖慢启动，
又会让导入期的报错落在日志就绪之前、直接消失。

迁移由 :func:`ensure_app_data_migrated` 在**解析路径时**惰性触发，不再依赖入口点
按正确顺序调用 —— 那种约定破过一次，代价是用户整个数据目录被晾在旧名下面。

``settings`` 与 ``logging_config`` 都从这里取路径原语，避免像从前那样
各写一份硬编码的环境变量名而悄悄跑偏。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
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

    ensure_app_data_migrated()
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
    ensure_app_data_migrated()
    return [settings_path_for_app_name(name) for name in LEGACY_APP_NAMES]


def legacy_app_names_for(app_name: str) -> tuple[str, ...]:
    """把有效应用名映射到它对应的历史名。

    源码调试档把应用名后缀成 ``"<APP_NAME> Dev"``（见 :mod:`krok_helper.runtime_profile`），
    它的历史名同样带 ``" Dev"`` 后缀，所以按后缀推导而不是写死两张表。
    """

    suffix = " Dev" if app_name.endswith(" Dev") else ""
    return tuple(f"{name}{suffix}" for name in LEGACY_APP_NAMES)


def _known_app_dir_names() -> tuple[str, ...]:
    """识别「本应用数据目录」用的全部历史/当前目录名。

    检测旧路径时不仅认 ``LEGACY_APP_NAMES``（含 Dev 后缀档位），也认两个
    当前名（正式 / Dev）——用户可能在两档之间搬过数据（例如 Dev 档配好的
    环境目录后来只在正式档存在），双向都要能接住。
    """

    names: list[str] = []
    for suffix in ("", " Dev"):
        for name in (*LEGACY_APP_NAMES, APP_NAME):
            candidate = f"{name}{suffix}"
            if candidate not in names:
                names.append(candidate)
    effective = effective_app_name()
    if effective not in names:
        names.append(effective)
    return tuple(names)


def _repair_target_names() -> tuple[str, ...]:
    """修复旧路径时的候选目标目录名：当前有效名优先，另一档位次之。"""

    names: list[str] = []
    for name in (effective_app_name(), APP_NAME, f"{APP_NAME} Dev"):
        if name not in names:
            names.append(name)
    return tuple(names)


def settings_text_needs_legacy_repair(text: str) -> bool:
    """粗闸门：``settings.json`` 原文里连一个已知应用数据目录名都没出现时，
    逐值修复遍历必然空手，可以直接跳过。

    修复只可能改写「位于某个已知应用数据目录之下」的绝对路径；目录名一个
    都不在文本里，就不存在这种路径。闸门只做小写子串匹配，把每次读取配置
    都要付的全树遍历（数千个字符串逐个判定）降为文件内容真正可能含旧路径
    时才付。
    """

    if not text:
        return False
    lowered = text.lower()
    return any(name.lower() in lowered for name in _known_app_dir_names())


def repair_legacy_appdata_path(raw: str) -> str | None:
    """把指向旧应用名数据目录且已失效的绝对路径，改写到当前名下确实存在的那份。

    更名（Karaoke Studio → Lin-K Lyrics）搬走的是**整个数据目录**，但
    settings.json 里存的绝对路径（AI 打轴缓存根、模型根、Runtime python.exe
    ……）不会跟着改写；而 SUG 侧「用户显式设置的路径」优先于宿主默认值，
    于是这些设置全部解析到不存在的旧目录——用户看到的就是 AI 打轴环境
    「丢失」。读取配置时对每个疑似路径做一次自愈：

    - 原路径**仍然存在**就绝不动它（用户可能并行保留着旧版目录在用）；
    - 路径不在任何已知应用数据目录之下也不动（那可能是用户自己的
      ``D:\\Videos\\Karaoke Studio\\xxx.mp4``，与应用数据无关）；
    - 把应用目录名换成候选名（当前有效名 → 另一档位，如 ``Lin-K Lyrics``
      / ``Lin-K Lyrics Dev``）后**目标内容确实存在**才改写。

    Returns:
        修复后的路径字符串；无须 / 无法修复时返回 ``None``，调用方保留原值。
    """

    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        path = Path(raw)
    except (OSError, ValueError):
        return None
    if not path.is_absolute():
        return None

    normalized = os.path.normcase(str(path))
    for source_name in _known_app_dir_names():
        root = settings_path_for_app_name(source_name).parent
        root_key = os.path.normcase(str(root))
        # 必须带上分隔符：否则 "Karaoke Studio Backup" 这类兄弟目录会被误认。
        if not normalized.startswith(root_key + os.sep):
            continue
        try:
            if path.exists():
                return None
        except OSError:
            return None
        # str(path) 与 str(root) 的分隔符均已规范化且长度一致，直接按长度切片
        # 得到保留原始大小写的尾部（normcase 会把盘符/整串小写化，不能用它拼）。
        tail = str(path)[len(str(root)) + 1 :]
        for target_name in _repair_target_names():
            if target_name == source_name:
                continue
            repaired = settings_path_for_app_name(target_name).parent / tail
            try:
                if repaired.exists():
                    return str(repaired)
            except OSError:
                continue
        return None
    return None


# migrate_app_data_dir 跑在日志系统初始化之前，没法直接写日志。这里沿用
# settings._LAST_CORRUPTION_BACKUP 的「先记下、日志起来后再取走」模式。
_MIGRATION_NOTES: list[str] = []

_migration_attempted = False
_migration_running = False


def _note(message: str) -> None:
    """记一条迁移说明：既留给日志补写，也当场写进启动面包屑。

    迁移跑在日志就绪之前，而这台机器上出现过「日志系统整轮没起来」的现场；
    面包屑是不依赖日志的那一份，出事时至少还有据可查。
    """

    _MIGRATION_NOTES.append(message)
    try:
        from krok_helper.startup_trace import mark

        mark("appdata.migration", message)
    except Exception:  # noqa: BLE001 —— 记录失败不能反过来影响迁移
        pass


def ensure_app_data_migrated() -> None:
    """幂等地跑一次迁移；解析用户数据路径的地方都先过这里。

    以前迁移只在 ``app.py`` / ``krok_helper.__main__`` 的开头调一次，于是整件事
    押在「谁先跑」上：任何先一步创建了新应用名目录的代码（``python -m
    krok_helper.subtitle_render`` 这类独立入口、日志初始化、乃至一次设置保存），
    都会让 ``new_dir.exists()`` 成立、迁移**永久跳过**，用户几 GB 的数据从此
    留在旧目录里没人再看 —— 表现就是「更新完一版，配置全没了」。

    改成在路径解析时惰性触发，顺序就不再是正确性的前提。
    """

    global _migration_attempted, _migration_running
    if _migration_attempted or _migration_running:
        return
    _migration_running = True
    try:
        migrate_app_data_dir()
    except Exception as exc:  # noqa: BLE001 —— 迁移不该把启动带崩
        _note(f"用户数据目录迁移异常：{type(exc).__name__}: {exc}")
    finally:
        _migration_running = False
        _migration_attempted = True


def _adopt_stranded_legacy_data(new_dir: Path, old_dir: Path) -> None:
    """新目录已存在时，把旧目录里**新目录还没有**的条目搬过来。

    只补不覆盖：新目录里已有的东西一律不动，所以既不会踩掉用户当前的配置，
    也能把 Cookie / AI 模型 / 未保存工程备份这些「settings.json 读取兜底救不了」
    的目录接回来。
    """

    adopted: list[str] = []
    skipped: list[str] = []
    try:
        entries = sorted(old_dir.iterdir())
    except OSError as exc:
        _note(f"旧用户数据目录无法读取（{old_dir}）：{exc}")
        return
    for entry in entries:
        target = new_dir / entry.name
        if target.exists():
            skipped.append(entry.name)
            continue
        try:
            os.rename(entry, target)
        except OSError as exc:
            _note(f"接回 {entry.name} 失败（{entry} → {target}）：{exc}")
            continue
        adopted.append(entry.name)
    if not adopted:
        # 一件都没接回来 = 新目录本来就是齐的（正常情况，每次启动都会走到这里），
        # 这时候不该留话，否则日志里每次开机都是同一段噪声。
        return
    _note(
        f"新用户数据目录已存在，已从 {old_dir} 接回 {len(adopted)} 项：" + "、".join(adopted)
    )
    if skipped:
        _note(
            f"以下条目新目录里已有，保持不动（旧的仍留在 {old_dir}）：" + "、".join(skipped)
        )


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

    **判据是「新目录存不存在」**，刻意不用标记位 —— 标记位本身要存在被迁移的
    ``settings.json`` 里，会形成先有鸡还是先有蛋的问题。新目录已经存在时不再直接放弃：
    那有可能只是日志或某个独立入口抢先把它建了出来，此时按「只补不覆盖」把旧目录里
    还没有的条目接回来（见 :func:`_adopt_stranded_legacy_data`）。

    调用方一般不用直接调它，路径解析会经 :func:`ensure_app_data_migrated` 触发；
    入口点保留显式调用只是为了让迁移尽量早发生，重复调用无副作用。
    """

    if os.getenv(SETTINGS_DIR_ENV):
        # 显式指定了目录，用户自己管，不要动。
        return None

    app_name = effective_app_name()
    new_dir = settings_path_for_app_name(app_name).parent
    legacy_dirs = [
        settings_path_for_app_name(name).parent
        for name in legacy_app_names_for(app_name)
        if name != app_name
    ]

    if new_dir.exists():
        # 新目录已经在了 —— 可能是上一次正常迁移的结果（旧目录早没了，什么都不用做），
        # 也可能是某个入口点抢先把它建了出来、把整轮迁移挤掉了。后一种情况下用户的
        # 数据还全躺在旧目录里，这里补一次「只补不覆盖」的接管。
        stranded = next((item for item in legacy_dirs if item.is_dir()), None)
        if stranded is not None:
            _adopt_stranded_legacy_data(new_dir, stranded)
        return None

    for old_dir in legacy_dirs:
        if not old_dir.is_dir():
            continue
        try:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old_dir, new_dir)
        except OSError as exc:
            # 目录被占用、跨盘、或另一个实例抢先建了新目录都会走到这里。
            # 不致命：settings.json 仍有 LEGACY_APP_NAMES 读取兜底。
            _note(f"用户数据目录迁移失败（{old_dir} → {new_dir}）：{exc}")
            return None
        # 校验落点：``os.rename`` 返回成功不等于文件真的躺在我们以为的位置。
        # 运行在有文件重定向的环境里（MSIX 沙箱、部分安全软件的虚拟化、同步盘），
        # 整个目录可能被物化到别处，而应用下次以正常身份启动时就什么都找不到。
        # 这里把实际观察到的状态记下来，出事时至少有据可查。
        if not new_dir.is_dir():
            _note(
                f"用户数据目录迁移后目标不存在（{old_dir} → {new_dir}）：可能被文件重定向接管"
            )
            return None
        if old_dir.exists():
            _note(
                f"用户数据目录迁移后旧目录仍在（{old_dir}）：运行环境疑似有文件重定向，"
                "请确认数据真的落在 " + str(new_dir)
            )
        _note(f"用户数据目录已迁移：{old_dir} → {new_dir}")
        return old_dir

    return None


# ── 临时目录原语 ─────────────────────────────────────────────────────────────
# %TEMP% 下所有临时产物统一收敛到 ``%TEMP%/LinKLyrics/`` 一个根（面包屑/日志兜底
# 早已在这下面），模块各自再建子目录——不要再往 %TEMP% 顶层撒新根。
# 大缓存放 APPDATA 不合适（漫游/备份污染），Temp 的「系统可随时回收」语义对可再生
# 的缓存恰好正确。

#: %TEMP% 下的应用统一根目录名。
TEMP_ROOT_NAME = "LinKLyrics"

#: 本地缓存根目录环境变量覆盖（测试与排障用）。
CACHE_DIR_ENV = "KARAOKE_STUDIO_CACHE_DIR"

#: 临时根目录环境变量覆盖（测试与排障用）。
TEMP_DIR_ENV = "KARAOKE_STUDIO_TEMP_DIR"

#: 历史上散落在 %TEMP% 顶层的模块自建缓存目录；启动时整体回收。
#: 注意 ``KaraokeStudioUpdater`` **不在**其中——它的路径写在用户支持文档里
#: （updater.log 位置），保持不动。
_LEGACY_TEMP_DIR_NAMES = ("krok_merge_preview", "KaraokeStudioPreviewCache")


def cache_root() -> Path:
    """应用本地缓存根目录（可用 :data:`CACHE_DIR_ENV` 覆盖）。

    存放可再生但在进程运行期间不宜被 ``%TEMP%`` 清理工具随时删除的缓存
    （如字体资源快照）。Windows 落在 ``%LOCALAPPDATA%``（不污染 Roaming），
    macOS 落在 ``~/Library/Caches``，Linux 遵循 ``XDG_CACHE_HOME``。
    """
    override = os.getenv(CACHE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    app_name = effective_app_name()
    if os.name == "nt":
        local_appdata = os.getenv("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / app_name / "cache"
        return temp_root()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / app_name
    xdg_cache = os.getenv("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache).expanduser() / app_name.lower().replace(" ", "-")
    return Path.home() / ".cache" / app_name.lower().replace(" ", "-")


def cache_dir(name: str) -> Path:
    """本地缓存根下的某个模块子目录（如 ``font-resources``）。"""
    return cache_root() / name


def temp_root() -> Path:
    """%TEMP% 下的应用统一根目录（可用 :data:`TEMP_DIR_ENV` 覆盖）。"""
    override = os.getenv(TEMP_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / TEMP_ROOT_NAME


def temp_dir(name: str) -> Path:
    """统一根下的某个模块子目录（如 ``merge-preview`` / ``preview-cache``）。"""
    return temp_root() / name


def purge_legacy_temp_dirs(base: Path | None = None) -> list[Path]:
    """回收旧的散落临时目录（整个目录删除，尽力而为）。

    旧目录在 %TEMP% 顶层，不受 :data:`TEMP_DIR_ENV` 覆盖影响；``base`` 仅供
    测试注入。目录里正被占用的文件（双开实例的播放器/ffmpeg 句柄）删除会
    失败，``ignore_errors`` 静默跳过、留下残余，下次启动再试。返回实际删掉的目录。
    """
    top = Path(tempfile.gettempdir()) if base is None else base
    removed: list[Path] = []
    for name in _LEGACY_TEMP_DIR_NAMES:
        path = top / name
        if not path.is_dir():
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path)
    return removed
