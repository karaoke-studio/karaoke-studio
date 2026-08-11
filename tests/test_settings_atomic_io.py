from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from krok_helper.settings import (
    AppSettings,
    consume_corruption_backup,
    load_app_settings,
    save_app_settings,
)


@pytest.fixture(autouse=True)
def _isolated_appdata(monkeypatch, tmp_path: Path):
    """每个测试都获得自己的 %APPDATA%/Karaoke Studio/ 目录，避免污染真实 settings.json。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # POSIX 兜底
    # 配置目录名也要钉住：``configure_source_debug_settings_profile`` 是产品代码里
    # 直接 ``os.environ.setdefault`` 的，测完不会自己还原；由 monkeypatch 先设一次，
    # 它的还原才兜得住，否则那条用例之后的测试全被带到 "Karaoke Studio Dev" 去。
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio")
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_DIR", raising=False)
    # 清掉模块级 corruption 状态，避免上一个测试污染本测试
    consume_corruption_backup()
    yield


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / "Karaoke Studio" / "settings.json"


def test_save_uses_atomic_replace_and_writes_complete_json(tmp_path: Path):
    settings = AppSettings(
        ffmpeg_dir="D:/ffmpeg",
        lyrics_timing={"audio": {"default_volume": 42}},
        subtitle_render={"selected_scheme_key": "custom:图像方案"},
    )
    save_app_settings(settings)

    target = _settings_path(tmp_path)
    assert target.is_file()
    # 不允许残留 .tmp（成功路径里 os.replace 已经把 .tmp 重命名为本体）
    assert not (target.parent / "settings.json.tmp").exists()
    # 内容应该是完整 JSON
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["ffmpeg_dir"] == "D:/ffmpeg"
    assert data["lyrics_timing"]["audio"]["default_volume"] == 42
    assert data["subtitle_render"]["selected_scheme_key"] == "custom:图像方案"
    assert load_app_settings().subtitle_render["selected_scheme_key"] == "custom:图像方案"


def test_save_preserves_existing_file_when_write_fails_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """模拟「写 .tmp 阶段崩」——本体不应被破坏。

    这是 v3.0.x 真实事故的回归保护：先把 settings.json 直接 truncate 再写的
    路径会让进程被杀时留下空文件；改成 tmp+os.replace 后，写失败只会留下
    未完成的 .tmp，本体仍是上一次的完整版本。
    """
    # 先正常写一份完整的
    good = AppSettings(ffmpeg_dir="D:/ffmpeg", lyrics_timing_dictionary=[{"word": "猫"}])
    save_app_settings(good)
    target = _settings_path(tmp_path)
    good_text = target.read_text(encoding="utf-8")

    # 模拟「写 .tmp 中途失败」：第二次 save 时让 Path.write_text 在 .tmp 上抛
    from pathlib import Path as _Path
    real_write_text = _Path.write_text

    def fail_when_tmp(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise OSError("simulated mid-write crash")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "write_text", fail_when_tmp)

    bad = AppSettings(ffmpeg_dir="D:/nope", lyrics_timing_dictionary=[])
    with pytest.raises(OSError):
        save_app_settings(bad)

    # 本体应该还是第一次的完整内容（绝不能是空文件 / 半截）
    assert target.read_text(encoding="utf-8") == good_text


def test_load_backs_up_corrupt_file_and_returns_defaults(tmp_path: Path):
    target = _settings_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 写入一个无效 JSON（模拟真实事故里 truncate 完没写完的残骸）
    target.write_text("{not valid json", encoding="utf-8")

    loaded = load_app_settings()

    # 应该回落到默认 AppSettings
    assert loaded.ffmpeg_dir == ""
    assert loaded.lyrics_timing == {}
    assert loaded.lyrics_timing_dictionary == []
    assert loaded.lyrics_timing_migrated_v1 is False

    # 损坏文件应该被备份到 settings.json.corrupt-<ts>，而**不是**被默默删掉
    backups = list(target.parent.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"

    # consume_corruption_backup 第一次返回备份路径并清零；第二次返回 None
    first = consume_corruption_backup()
    assert first == backups[0]
    assert consume_corruption_backup() is None


def test_load_backs_up_non_object_top_level(tmp_path: Path):
    target = _settings_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # JSON 合法但顶层是数组——也算损坏（schema 期望 object）
    target.write_text("[1, 2, 3]", encoding="utf-8")

    loaded = load_app_settings()
    assert loaded.lyrics_timing == {}
    backups = list(target.parent.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "[1, 2, 3]"


def test_load_does_not_emit_backup_for_missing_file(tmp_path: Path):
    """settings.json 根本不存在 → 是首次运行 / 全新安装，不该误报损坏。"""
    loaded = load_app_settings()
    assert loaded == AppSettings()
    backups = list((tmp_path / "Karaoke Studio").glob("settings.json.corrupt-*")) if (tmp_path / "Karaoke Studio").exists() else []
    assert backups == []
    assert consume_corruption_backup() is None


def test_load_migrates_legacy_dot_ffmpeg_directory_to_system_path(tmp_path: Path):
    target = _settings_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"ffmpeg_dir": "."}), encoding="utf-8")

    loaded = load_app_settings()

    assert loaded.ffmpeg_dir == ""


def test_settings_app_name_env_uses_separate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio Dev")

    save_app_settings(AppSettings(ffmpeg_dir="D:/dev-ffmpeg"))

    dev_settings = tmp_path / "Karaoke Studio Dev" / "settings.json"
    release_settings = tmp_path / "Karaoke Studio" / "settings.json"
    assert dev_settings.is_file()
    assert not release_settings.exists()
    assert load_app_settings().ffmpeg_dir == "D:/dev-ffmpeg"


def test_settings_dir_env_overrides_app_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom_dir = tmp_path / "custom-settings"
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio Dev")
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(custom_dir))

    save_app_settings(AppSettings(ffmpeg_dir="D:/custom-ffmpeg"))

    assert (custom_dir / "settings.json").is_file()
    assert not (tmp_path / "Karaoke Studio Dev" / "settings.json").exists()
    assert load_app_settings().ffmpeg_dir == "D:/custom-ffmpeg"


def test_source_debug_profile_defaults_to_dev_settings(monkeypatch: pytest.MonkeyPatch):
    from krok_helper.runtime_profile import configure_source_debug_settings_profile

    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_DIR", raising=False)

    configure_source_debug_settings_profile()

    assert os.environ["KARAOKE_STUDIO_SETTINGS_APP_NAME"] == "Karaoke Studio Dev"


# ─────────────────────────────────────────────────────────────────────────────
# 多开时的命名空间保护
#
# 打轴与字幕渲染的配置由各自的设置桥读写（读盘 → 只改自己那段 → 写盘），而外壳、
# 全局设置对话框、更新器写的是**本进程启动时读到的那份整份快照**。同时开着两个
# 工作台时，后写的会把先写的整份顶掉：在 A 里存进预设库的配色方案，B 随手换个
# 主题就没了；等 A 退出再写一次才回来 —— 用户看到的现象是"要关掉应用才保存"。
# ─────────────────────────────────────────────────────────────────────────────


def test_a_whole_snapshot_write_keeps_another_instances_module_settings(tmp_path: Path):
    """B 写自己的设置，不该把 A 刚存进去的模块配置顶掉。"""
    first = load_app_settings()
    first.subtitle_render = {"style_presets": [{"name": "旧"}]}
    save_app_settings(first)

    # 两个实例各自启动，都读到"旧"
    instance_a = load_app_settings()
    instance_b = load_app_settings()

    # A 通过设置桥存了一条新预设（桥写的就是这一段，所以不合并）
    instance_a.subtitle_render = {"style_presets": [{"name": "旧"}, {"name": "新"}]}
    save_app_settings(instance_a, merge_module_namespaces=False)

    # B 这时候做任何会写设置的事：换主题、折叠步骤条、关窗……
    instance_b.ui_theme = "dark"
    save_app_settings(instance_b)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    names = [item["name"] for item in saved["subtitle_render"]["style_presets"]]
    assert names == ["旧", "新"], "B 的整份写回把 A 存的预设顶掉了"
    assert saved["ui_theme"] == "dark", "B 自己那份改动也要写进去"


def test_the_bridge_can_still_write_its_own_namespace(tmp_path: Path):
    """桥要写的正是那几段，合并回盘上的旧值等于把自己的改动丢掉。"""
    settings = load_app_settings()
    settings.subtitle_render = {"style_presets": [{"name": "旧"}]}
    save_app_settings(settings, merge_module_namespaces=False)

    settings.subtitle_render = {"style_presets": [{"name": "新"}]}
    save_app_settings(settings, merge_module_namespaces=False)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert [i["name"] for i in saved["subtitle_render"]["style_presets"]] == ["新"]


def test_module_namespaces_survive_across_every_wholesale_writer(tmp_path: Path):
    """打轴那几段和字幕渲染一样受保护 —— 它们也各有一个桥。"""
    first = load_app_settings()
    save_app_settings(first)

    writer = load_app_settings()

    fresh = load_app_settings()
    fresh.lyrics_timing = {"a": 1}
    fresh.lyrics_timing_dictionary = [{"b": 2}]
    fresh.lyrics_timing_singers = [{"name": "歌手"}]
    fresh.lyrics_timing_network_dictionary = {"c": 3}
    save_app_settings(fresh, merge_module_namespaces=False)

    save_app_settings(writer)  # 另一个实例的整份写回

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["lyrics_timing"] == {"a": 1}
    assert saved["lyrics_timing_dictionary"] == [{"b": 2}]
    assert saved["lyrics_timing_singers"] == [{"name": "歌手"}]
    assert saved["lyrics_timing_network_dictionary"] == {"c": 3}


# ─────────────────────────────────────────────────────────────────────────────
# 多开时的普通字段保护
#
# 命名空间只是冰山一角：导出目录、命名模板、下载设置、``pymss``、``updater`` 这些
# 顶层字段没有设置桥，写的全是本进程启动时读到的那份快照。在 A 里改完导出目录，
# B 随手换个主题（或者干脆只是关个窗）就把它顶回去了。
# ─────────────────────────────────────────────────────────────────────────────


def _two_instances() -> tuple:
    save_app_settings(load_app_settings())
    return load_app_settings(), load_app_settings()


def test_another_instance_does_not_revert_ordinary_fields(tmp_path: Path):
    """B 只改了主题，不该把 A 改过的任何东西带回去。"""
    instance_a, instance_b = _two_instances()

    instance_a.align_output_custom_dir = r"D:\A 的导出目录"
    instance_a.video_download_save_dir = r"D:\A 的下载目录"
    instance_a.pymss = {"output_format": "flac"}
    instance_a.updater = {"channel": "beta"}
    save_app_settings(instance_a)

    instance_b.ui_theme = "dark"
    save_app_settings(instance_b)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["align_output_custom_dir"] == r"D:\A 的导出目录"
    assert saved["video_download_save_dir"] == r"D:\A 的下载目录"
    assert saved["pymss"] == {"output_format": "flac"}
    assert saved["updater"] == {"channel": "beta"}
    assert saved["ui_theme"] == "dark", "B 自己那份改动也要写进去"


def test_a_later_save_from_the_same_instance_still_yields(tmp_path: Path):
    """判据是"和基线比有没有变"，所以"改过一次"不能变成"永远压着别人"。

    外壳每次写盘前都会把界面上的值收一遍，B 的界面里还是启动时那份 —— 收完仍然
    等于基线，于是继续让位给 A。
    """
    instance_a, instance_b = _two_instances()
    instance_a.align_output_custom_dir = r"D:\A 的导出目录"
    save_app_settings(instance_a)

    instance_b.ui_theme = "dark"
    save_app_settings(instance_b)
    instance_b.workflow_compact = True
    save_app_settings(instance_b)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["align_output_custom_dir"] == r"D:\A 的导出目录"


def test_an_instance_can_still_change_a_field_twice(tmp_path: Path):
    """让位的前提是"本实例没动过"—— 动过就得写下去，否则就成了存不了。"""
    settings = load_app_settings()

    settings.ffmpeg_dir = "D:/一"
    save_app_settings(settings)
    settings.ffmpeg_dir = "D:/二"
    save_app_settings(settings)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["ffmpeg_dir"] == "D:/二"


def test_both_instances_changing_one_field_is_last_write_wins(tmp_path: Path):
    """同一个字段两边都改，跨进程没有更好的答案。"""
    instance_a, instance_b = _two_instances()

    instance_a.ffmpeg_dir = "D:/A"
    save_app_settings(instance_a)
    instance_b.ffmpeg_dir = "D:/B"
    save_app_settings(instance_b)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["ffmpeg_dir"] == "D:/B"


def test_a_hand_built_settings_object_still_writes_everything(tmp_path: Path):
    """没有基线的 ``AppSettings()`` 走老路子整份写 —— 不然就没人写得进去了。"""
    save_app_settings(load_app_settings())

    save_app_settings(AppSettings(ffmpeg_dir="D:/手搓"))

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["ffmpeg_dir"] == "D:/手搓"


def test_a_field_missing_from_the_file_keeps_this_instances_value(tmp_path: Path):
    """老版本写的文件里没有的字段，合并时不能当成"盘上是默认值"。"""
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ffmpeg_dir": "D:/老版本"}), encoding="utf-8")

    settings = load_app_settings()
    settings.workflow_compact = True
    save_app_settings(settings)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["workflow_compact"] is True
    assert saved["ffmpeg_dir"] == "D:/老版本"


# ─────────────────────────────────────────────────────────────────────────────
# 命名空间的保护不能反过来咬住"故意要写命名空间"的人
#
# 一次性迁移、「导入旧版 SUG 配置」、字幕渲染独立运行时的兜底保存，写的正是那几段
# 命名空间。无条件换成盘上的值，等于这次保存什么都没干 —— 而迁移那条还会把
# ``lyrics_timing_migrated_v1`` 标记写下去，于是永远不再重试。
#
# 现有的迁移测试只断言内存里的对象，正是这样漏过去的：这里一律断言**盘上**。
# ─────────────────────────────────────────────────────────────────────────────


def _legacy_sug_dir(tmp_path: Path) -> Path:
    legacy = tmp_path / "legacy-sug"
    legacy.mkdir()
    (legacy / "singers.json").write_text(
        json.dumps([{"name": "老演唱者"}]), encoding="utf-8"
    )
    (legacy / "dictionary.json").write_text(
        json.dumps([{"word": "老词条"}]), encoding="utf-8"
    )
    return legacy


def test_the_one_shot_sug_migration_lands_on_disk(tmp_path: Path):
    from krok_helper.settings import migrate_strange_uta_game_settings

    legacy = _legacy_sug_dir(tmp_path)
    save_app_settings(load_app_settings())  # 老用户：settings.json 早就存在

    settings = load_app_settings()
    assert migrate_strange_uta_game_settings(settings, legacy) is True
    save_app_settings(settings)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["lyrics_timing_singers"] == [{"name": "老演唱者"}]
    assert saved["lyrics_timing_migrated_v1"] is True


def test_importing_legacy_sug_settings_lands_on_disk(tmp_path: Path):
    from krok_helper.settings import import_legacy_sug_settings

    legacy = _legacy_sug_dir(tmp_path)
    save_app_settings(load_app_settings())

    settings = load_app_settings()
    import_legacy_sug_settings(legacy, settings)
    save_app_settings(settings)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["lyrics_timing_dictionary"] == [{"word": "老词条"}]


def test_a_namespace_the_instance_never_touched_still_yields(tmp_path: Path):
    """放行"改过的"不能顺手把"没改过的"也放行了 —— 那就是原来的多开事故。"""
    instance_a, instance_b = _two_instances()

    instance_a.subtitle_render = {"style_presets": [{"name": "A 存的"}]}
    save_app_settings(instance_a, merge_module_namespaces=False)

    instance_b.ui_theme = "dark"
    save_app_settings(instance_b)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["subtitle_render"] == {"style_presets": [{"name": "A 存的"}]}


def test_a_bridge_sync_does_not_make_the_host_claim_the_namespace(tmp_path: Path):
    """桥把值同步回宿主之后，宿主不能反过来拿它去压别人。

    宿主的 ``AppSettings`` 是启动时那一个，桥每次写完都会把新值塞回去。不打招呼
    的话，宿主下次整份写盘就认为"这段是我改的"，于是把另一个实例后写的顶掉。
    """
    from krok_helper.settings import sync_baseline_field

    host, other = _two_instances()

    # 本实例的桥写了一版，并同步回宿主
    host.subtitle_render = {"style_presets": [{"name": "本实例"}]}
    save_app_settings(host, merge_module_namespaces=False)
    sync_baseline_field(host, "subtitle_render")

    # 另一个实例随后又写了一版
    other.subtitle_render = {"style_presets": [{"name": "另一个实例"}]}
    save_app_settings(other, merge_module_namespaces=False)

    host.ui_theme = "dark"
    save_app_settings(host)

    saved = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["subtitle_render"] == {"style_presets": [{"name": "另一个实例"}]}


def test_the_first_ever_save_still_works_without_a_file(tmp_path: Path):
    """盘上还没有文件时合并要安静地跳过，不能把首次保存搞挂。"""
    settings = AppSettings()
    settings.subtitle_render = {"style_presets": []}

    save_app_settings(settings)

    assert _settings_path(tmp_path).is_file()


def test_a_corrupt_file_does_not_wipe_module_namespaces(tmp_path: Path):
    """坏文件读不出来时保留内存里的值，别把好好的命名空间清成空的。"""
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")
    settings = AppSettings()
    settings.subtitle_render = {"style_presets": [{"name": "内存里的"}]}

    save_app_settings(settings)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [i["name"] for i in saved["subtitle_render"]["style_presets"]] == ["内存里的"]
