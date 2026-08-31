from __future__ import annotations

import sys

from krok_helper.settings import AppSettings
from krok_helper.updater.settings import UpdaterSettings, ensure_updater_settings
from krok_helper.updater.sources import build_api_urls, build_release_urls, normalize_order
from krok_helper.updater.installer import (
    DEFAULT_APP_EXE_NAME,
    LEGACY_APP_EXE_NAME,
    TMP_DIR_NAME,
    LaunchPlan,
)
from krok_helper.updater.worker import LatestRelease, ReleaseAsset, current_asset_name, is_newer_version


def test_workbench_updater_uses_workbench_repo_urls() -> None:
    api_urls = build_api_urls(["github"])
    release_urls = build_release_urls(["github"], "v3.0.1", "KaraokeStudio-windows.zip")

    assert api_urls[0][1] == "https://api.github.com/repos/karaoke-studio/karaoke-studio/releases/latest"
    assert release_urls[0][1] == (
        "https://github.com/karaoke-studio/karaoke-studio/"
        "releases/download/v3.0.1/KaraokeStudio-windows.zip"
    )


def test_workbench_updater_settings_roundtrip_defaults() -> None:
    settings = AppSettings()

    updater = ensure_updater_settings(settings)

    assert updater.enabled is True
    assert updater.check_on_startup is True
    assert settings.updater["source_order"] == ["github", "gh-proxy"]
    assert UpdaterSettings.load(settings).min_check_interval_hours == 8


def test_workbench_updater_normalizes_source_order() -> None:
    # 旧设置里遗留的 ghfast.top / ghproxy.net 源 id 会被过滤，收敛到现行清单。
    assert normalize_order(["ghproxy", "bogus", "github", "ghproxy-net"]) == [
        "github",
        "gh-proxy",
    ]


def test_workbench_updater_source_urls_expand_gh_proxy_nodes() -> None:
    """gh-proxy 源展开为每个镜像节点一条候选，调用方按列表接力。"""
    from krok_helper.updater.sources import (
        GH_PROXY_PREFIXES,
        build_api_urls,
        build_release_urls,
    )

    release_urls = build_release_urls(["github", "gh-proxy"], "v3.0.2", "app.zip")
    assert release_urls[0] == (
        "github",
        "https://github.com/karaoke-studio/karaoke-studio/releases/download/v3.0.2/app.zip",
    )
    mirrors = release_urls[1:]
    assert [source for source, _ in mirrors] == ["gh-proxy"] * len(GH_PROXY_PREFIXES)
    assert [url for _, url in mirrors] == [
        f"{prefix}/https://github.com/karaoke-studio/karaoke-studio/releases/download/v3.0.2/app.zip"
        for prefix in GH_PROXY_PREFIXES
    ]

    api_urls = build_api_urls(["github", "gh-proxy"])
    assert api_urls[0] == (
        "github",
        "https://api.github.com/repos/karaoke-studio/karaoke-studio/releases/latest",
    )
    assert api_urls[1][1].endswith(
        "/https://api.github.com/repos/karaoke-studio/karaoke-studio/releases/latest"
    )


def test_workbench_updater_version_and_asset_selection(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    release = LatestRelease(
        tag="v3.0.1",
        version="3.0.1",
        name="v3.0.1",
        body="",
        html_url="",
        prerelease=False,
        published_at="",
        assets=[ReleaseAsset("KaraokeStudio-windows.zip", 10, "https://example.invalid/app.zip")],
    )

    assert is_newer_version("3.0.1", "3.0.0")
    assert current_asset_name() == "KaraokeStudio-windows.zip"
    assert release.pick_primary_asset("KaraokeStudio-windows.zip") is not None


def test_workbench_updater_launcher_is_workbench_scoped(tmp_path) -> None:
    plan = LaunchPlan(
        app_dir=tmp_path,
        app_exe_name=DEFAULT_APP_EXE_NAME,
        target_version="3.0.1",
        target_tag="v3.0.1",
        asset_name="KaraokeStudio-windows.zip",
        download_urls=[("github", "https://example.invalid/KaraokeStudio-windows.zip")],
        proxy_url="http://127.0.0.1:7890",
    )

    args = plan.command_args(tmp_path / "Updater.exe", current_pid=1234)

    # 更新器临时目录名刻意保持改名前的写法：三处副本（installer / updater_app /
    # separation.runtime 的目的地校验）必须一致，且它是存量客户端的既定行为。
    assert TMP_DIR_NAME == "KaraokeStudioUpdater"
    assert DEFAULT_APP_EXE_NAME == "Lin-K Lyrics.exe"
    # 改名前的 EXE 名必须继续存在：存量客户端会把它当 --app-exe 传进来。
    assert LEGACY_APP_EXE_NAME == "Karaoke Studio.exe"
    assert "--app-exe" in args
    assert args[args.index("--app-exe") + 1] == DEFAULT_APP_EXE_NAME
    assert "KaraokeStudio-windows.zip" in args
    assert "StrangeUtaGame" not in " ".join(args)
