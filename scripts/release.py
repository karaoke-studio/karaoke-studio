"""Karaoke Studio 发版辅助工具。

``prepare`` 同步版本号并创建 CHANGELOG 占位段；``notes`` 从 CHANGELOG
抽取中文 release body。构建和上传仍由 tag 触发的 GitHub Actions 完成。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path


def _force_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "krok_helper" / "config.py"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
RELEASE_DIST = ROOT / "dist"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")
_CHANGELOG_PLACEHOLDER = """## [{version}] — {date}

*（请用一句中文概述本次发布的用户可见变化。）*

### 新增功能
- *（待补充；没有则删除本节）*

### 特性改变
- *（待补充；没有则删除本节）*

### 修复项目
- *（待补充；没有则删除本节）*

---

"""


def _check_version_format(value: str) -> str:
    if not VERSION_RE.fullmatch(value):
        raise SystemExit(f"版本号必须形如 X.Y.Z 或 X.Y.Z.N（收到 {value!r}）")
    return value


def _replace_once(path: Path, pattern: str, replacement: str, description: str) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise SystemExit(f"无法在 {path} 中找到{description}")
    old = match.group(1)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"未能在 {path} 中唯一替换{description}")
    if updated != text:
        # newline="" 关掉换行翻译：默认会把整份文件写成 CRLF，本仓库源文件一律 LF，
        # 那样 diff 会变成"整文件重写"，真正的一行改动被埋掉。
        path.write_text(updated, encoding="utf-8", newline="")
    return old, updated


def _read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit(f"无法在 {VERSION_FILE} 中解析 APP_VERSION")
    return match.group(1)


def _write_version(version: str) -> str:
    old, _ = _replace_once(
        VERSION_FILE,
        r'(?m)^APP_VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
        f'APP_VERSION = "{version}"',
        " APP_VERSION",
    )
    return old


def _write_readme_version(version: str) -> str:
    old, _ = _replace_once(
        README,
        r"(?m)^当前版本：`([^`]+)`[ \t]*$",
        f"当前版本：`{version}`",
        "顶部‘当前版本’",
    )
    return old


def _has_version_section(content: str, version: str) -> bool:
    return bool(re.search(rf"(?m)^##\s*\[{re.escape(version)}\](?:\s|$)", content))


def _insert_changelog_placeholder(version: str) -> bool:
    content = CHANGELOG.read_text(encoding="utf-8")
    if _has_version_section(content, version):
        return False
    unreleased = re.search(r"(?m)^##\s*\[Unreleased\][^\n]*\n", content)
    if not unreleased:
        raise SystemExit(f"无法在 {CHANGELOG} 中找到 [Unreleased] 段")
    next_section = re.search(r"(?m)^##\s", content[unreleased.end() :])
    insert_at = unreleased.end() + (next_section.start() if next_section else len(content))
    placeholder = _CHANGELOG_PLACEHOLDER.format(version=version, date=dt.date.today().isoformat())
    CHANGELOG.write_text(content[:insert_at] + placeholder + content[insert_at:], encoding="utf-8")
    return True


#: 每份更新公告开头的固定横幅。
#:
#: 更新器用 ``QTextBrowser.setMarkdown()`` 渲染 release body，Qt 的 markdown
#: 会放行内联 HTML，所以这里的颜色在更新弹窗里是真的红字。用 ``<b>`` 而不是
#: markdown 的 ``**``：内联 HTML 里的 ``**`` 不会被当成强调解析，加粗会丢。
#: GitHub 的网页版 release 会过滤掉 ``style``，那边降级成黑色粗体——文字本身
#: 两边都在。
ANNOUNCEMENT_BANNER = (
    '<span style="color:#d64545"><b>Lin-K/SUG 反馈交流群 QQ 1108437280</b></span>'
)


def _with_announcement_banner(body: str) -> str:
    """Put the standing announcement above every release body.

    Current CHANGELOG sections include the banner explicitly.  Keep this
    fallback for older or manually prepared sections so CI cannot publish a
    release body without the standing feedback notice.
    """

    text = body.lstrip("\n")
    if text.startswith(ANNOUNCEMENT_BANNER):
        return body
    return f"{ANNOUNCEMENT_BANNER}\n\n{text}"


#: 第一个不再随包分发旧名主程序副本（``Karaoke Studio.exe``）的版本；4.3.x 整条
#: 线仍双名分发，自该版本起只打 ``Lin-K Lyrics.exe``。与 ``scripts/build_parts.py``
#: 的 ``FIRST_LEGACY_FREE_VERSION`` 同源，护栏测试保证两处一致。改这个名字必须
#: 连同 build_parts 的版本闸一起改。
LEGACY_FREE_SINCE_VERSION = "4.4.0"

#: 旧版更新提示：改名前安装、长期未更新而错过迁移版（4.2.8.9+）的用户，
#: 自动更新到无旧名包会失败（全量被拒）或更新后无法启动（增量删了旧名 EXE）。
#: 两条可走的路：重新下载完整包，或直接打开安装目录里已装好的 Lin-K Lyrics.exe
#: （增量路径其实已经装上了新名主程序，只是旧名入口失效）。渲染口径与
#: ANNOUNCEMENT_BANNER 相同：HTML span 在更新弹窗里是真红字，GitHub 网页降级为黑色粗体。
MANUAL_UPDATE_NOTICE = (
    '<span style="color:#d64545"><b>老版本用户请注意：若自动更新失败或更新后无法'
    '启动，可重新下载下方完整包，或直接打开安装目录里的 Lin-K Lyrics.exe'
    '（新版安装包已不再包含旧主程序名 Karaoke Studio.exe）。</b></span>'
)


def _needs_manual_update_notice(version: str) -> bool:
    """首个无旧名副本的版本（含）起加手动更新提示；四段语义，4.3.1 不触发、4.4.0 触发。"""
    since = tuple(int(part) for part in LEGACY_FREE_SINCE_VERSION.split("."))
    return tuple(int(part) for part in version.split(".")) >= since


def _with_manual_update_notice(body: str) -> str:
    """Insert the manual-update notice right below the standing banner.

    与横幅一样在唯一出口生成（本地 ``notes`` 与 CI 的 release body 都经过这里），
    重复调用不叠加。
    """

    if MANUAL_UPDATE_NOTICE in body:
        return body
    text = body.lstrip("\n")
    if text.startswith(ANNOUNCEMENT_BANNER):
        rest = text[len(ANNOUNCEMENT_BANNER) :].lstrip("\n")
        return f"{ANNOUNCEMENT_BANNER}\n\n{MANUAL_UPDATE_NOTICE}\n\n{rest}"
    return f"{MANUAL_UPDATE_NOTICE}\n\n{text}"


def _extract_section(version: str) -> str:
    content = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^##\s*\[{re.escape(version)}\][^\n]*\n(?P<body>.*?)(?=^##\s|\Z)",
        content,
    )
    if not match:
        raise SystemExit(f"CHANGELOG.md 中未找到 [{version}] 段落")
    body = match.group("body").strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md 的 [{version}] 段落为空")
    return body + "\n"


def cmd_prepare(version: str) -> int:
    version = _check_version_format(version)
    # 先验证三个入口标记都存在，避免某个文件格式变化时只改成功一半。
    old_version = _read_version()
    readme_text = README.read_text(encoding="utf-8")
    if not re.search(r"(?m)^当前版本：`([^`]+)`[ \t]*$", readme_text):
        raise SystemExit(f"无法在 {README} 中找到顶部‘当前版本’")
    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    if not _has_version_section(changelog_text, version) and not re.search(
        r"(?m)^##\s*\[Unreleased\][^\n]*\n", changelog_text
    ):
        raise SystemExit(f"无法在 {CHANGELOG} 中找到 [Unreleased] 段")

    _write_version(version)
    old_readme = _write_readme_version(version)
    inserted = _insert_changelog_placeholder(version)
    print(f"APP_VERSION：{old_version} → {version}" if old_version != version else f"APP_VERSION 已是 {version}")
    print(f"README 当前版本：{old_readme} → {version}" if old_readme != version else f"README 当前版本已是 {version}")
    if inserted:
        print(f"已在 CHANGELOG.md 中插入 [{version}] 中文占位段，请补全并删除空分类")
    else:
        print(f"CHANGELOG.md 已存在 [{version}] 段落，未重复插入")
    print(f"准备完成。补全日志后运行：python scripts/release.py notes {version}")
    return 0


def cmd_notes(version: str, output: Path | None = None) -> int:
    version = _check_version_format(version)
    notes = _with_announcement_banner(_extract_section(version))
    if _needs_manual_update_notice(version):
        notes = _with_manual_update_notice(notes)
    path = output or RELEASE_DIST / f"release-notes-v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notes, encoding="utf-8")
    try:
        display_path = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        display_path = path
    print(f"已写入中文 release notes：{display_path}")
    print("CI 会从 CHANGELOG.md 自动提取同一版本段并写入 GitHub Release。")
    print(f"发布后验证：gh release view v{version} --json body --jq .body")
    return 0


def cmd_check_notes(version: str, path: Path) -> int:
    """校验 release body 与旧名停发口径一致，CI 发布前运行。

    无旧名版本（自 ``LEGACY_FREE_SINCE_VERSION`` 起，含）的正文必须带手动更新提示，
    仍在分发旧名副本的版本不得带（提示与包内容错位会误导用户）。用
    ``MANUAL_UPDATE_NOTICE`` 原文匹配而非关键词，改提示措辞不需要同步改这里。
    """
    _check_version_format(version)
    body = path.read_text(encoding="utf-8")
    needs = _needs_manual_update_notice(version)
    has = MANUAL_UPDATE_NOTICE in body
    if needs != has:
        raise SystemExit(
            f"{path} 与停发口径不一致：v{version} "
            f"{'必须携带' if needs else '不得携带'}手动更新提示"
            "（docs/auto_update.md §8.1，判定与 scripts/build_parts.py 同源）。"
        )
    print(f"release body 提示校验通过：v{version} {'携带' if has else '不带'}手动更新提示")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="同步版本号并创建 CHANGELOG 占位段")
    prepare.add_argument("version", help="目标版本号 X.Y.Z 或 X.Y.Z.N")
    notes = subparsers.add_parser("notes", help="从 CHANGELOG 生成中文 release notes")
    notes.add_argument("version", help="目标版本号 X.Y.Z 或 X.Y.Z.N")
    notes.add_argument("-o", "--output", type=Path, help="自定义输出文件")
    check = subparsers.add_parser(
        "check-notes", help="校验 release body 的手动更新提示与停发口径一致"
    )
    check.add_argument("version", help="目标版本号 X.Y.Z 或 X.Y.Z.N")
    check.add_argument("file", type=Path, help="待校验的 release body 文件")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        return cmd_prepare(args.version)
    if args.command == "notes":
        return cmd_notes(args.version, args.output)
    if args.command == "check-notes":
        return cmd_check_notes(args.version, args.file)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
