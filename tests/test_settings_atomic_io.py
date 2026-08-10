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
