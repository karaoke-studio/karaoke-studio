"""改名后仍必须成立的发布契约。

这些不是普通的字符串断言，而是**防止后人「顺手清理」**的护栏。执行自动更新的是
用户机器上的旧版代码，新版怎么写都救不了它 —— 一旦下面任何一条被破坏，存量用户
要么彻底断更，要么更新完拉不起来。详见 docs/auto_update.md §8。
"""

from __future__ import annotations

import scripts.build_parts as build_parts
from krok_helper.updater.installer import DEFAULT_APP_EXE_NAME, LEGACY_APP_EXE_NAME
from krok_helper.updater.worker import current_asset_name


def test_release_asset_names_keep_the_pre_rename_prefix() -> None:
    """资产名不可改：旧 worker 硬编码全量 zip 名，并从 zip 名派生 manifest 名。

    ``pick_primary_asset`` 找不到精确名时会回退成「文件名含 windows 且以 .zip
    结尾」，而这条规则同时匹配 ``-app.zip`` 与 ``-runtime.zip`` —— 改名有让旧客户端
    把分包当全量包、装出不可启动安装的风险。
    """

    assert build_parts.ASSET_BASE == "KaraokeStudio-windows"
    assert current_asset_name() in {
        "KaraokeStudio-windows.zip",
        "KaraokeStudio-macos.zip",
    }


def test_legacy_named_exe_ships_alongside_the_new_one() -> None:
    """兼容副本必须随包分发，且必须在 app part 的 targets 里。

    - 少了文件本身：旧 Updater 的 ``apply_update`` 找不到 ``--app-exe``，整包失败。
    - 少了 targets 条目：增量更新的 orphan cleanup 会把用户安装目录里的旧名 EXE
      删掉，更新虽然装上了但 ``launch_main_app`` 再也拉不起来。
    """

    assert LEGACY_APP_EXE_NAME == "Karaoke Studio.exe"
    assert DEFAULT_APP_EXE_NAME != LEGACY_APP_EXE_NAME
    assert build_parts.LEGACY_APP_EXE_NAME == LEGACY_APP_EXE_NAME
    assert build_parts.APP_EXE_NAME in build_parts.APP_TARGETS
    assert build_parts.LEGACY_APP_EXE_NAME in build_parts.APP_TARGETS


def test_onedir_layout_names_are_unchanged() -> None:
    """``_internal/`` 布局与更新器文件名同样被存量客户端硬编码。"""

    from krok_helper.updater.installer import (
        LOCAL_MANIFEST_FILENAME,
        TMP_DIR_NAME,
        UPDATER_EXE_NAME,
    )

    assert UPDATER_EXE_NAME == "Updater.exe"
    assert LOCAL_MANIFEST_FILENAME == ".installed_manifest.json"
    # 三份副本（installer / updater_app / separation.runtime 的目的地校验）必须一致
    assert TMP_DIR_NAME == "KaraokeStudioUpdater"


def test_full_update_payload_names_match_installer() -> None:
    """全量回退路径回写根目录负载用的双主程序名必须与 installer 口径一致。

    2026-09 事故根因之一：全量 ``_apply_workbench_update`` 的回写清单与包内容
    脱节，sidecar 与另一份主程序名被静默跳过（详见
    tests/test_workbench_updater_lock_guard.py 的回归测试）。
    """

    from krok_helper.updater_app.main import (
        LEGACY_APP_EXE_NAME as UPDATER_APP_LEGACY,
        PRIMARY_APP_EXE_NAME,
    )
    from krok_helper.updater.installer import LEGACY_APP_EXE_NAME

    assert UPDATER_APP_LEGACY == LEGACY_APP_EXE_NAME
    assert PRIMARY_APP_EXE_NAME == DEFAULT_APP_EXE_NAME


def test_updater_sessions_always_use_the_canonical_exe_name(
    tmp_path, monkeypatch
) -> None:
    """新版主程序无论以哪个文件名启动，更新会话都按新名走（2026-09 迁移机制）。

    旧名快捷方式启动的用户也会传 ``Lin-K Lyrics.exe`` 给 Updater：按新名校验/
    回写/重启，并触发旧名副本清理（docs/auto_update.md §8.1）。回到「传实际
    启动名」的旧逻辑会让旧名安装永远收敛不到新名，停发旧名副本时全部断更。
    """
    import sys

    from krok_helper.updater import installer

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable", str(tmp_path / LEGACY_APP_EXE_NAME), raising=False
    )
    assert installer.find_app_exe_name() == DEFAULT_APP_EXE_NAME


def test_updater_app_names_migration_constants_match_installer() -> None:
    """清理旧名副本用的双名常量必须与 installer 口径一致，改一处漏一处会误删/漏删。"""

    from krok_helper.updater.installer import LEGACY_APP_EXE_NAME
    from krok_helper.updater_app.main import (
        LEGACY_APP_EXE_NAME as UPDATER_APP_LEGACY,
        PRIMARY_APP_EXE_NAME,
    )

    assert UPDATER_APP_LEGACY == LEGACY_APP_EXE_NAME
    assert PRIMARY_APP_EXE_NAME == DEFAULT_APP_EXE_NAME


def test_updater_temp_dir_name_is_consistent_across_copies() -> None:
    """同一个临时目录名散在三处，改一处漏两处会让更新交接直接错位。"""

    from krok_helper.updater.installer import TMP_DIR_NAME

    runtime_source = (
        __import__("krok_helper.audio_processing.separation.runtime", fromlist=["runtime"])
    )
    import inspect

    assert TMP_DIR_NAME in inspect.getsource(runtime_source.preflight_install_destination)
