"""把 aria2c 拉到 ``build/vendor/aria2/``，供 build_windows.bat 随包分发。

为什么要随包带 aria2c
    B 站分给海外客户端的 upos CDN 会间歇性卡死单条 TCP 连接。yt-dlp 内建下载器
    是单连接顺序拉整段视频，那条连接一死整个下载就死。aria2c 多连接分块下载既
    抗断流又提速（对齐 BBDown 默认的 ``-mt``）。找不到 aria2c 时
    ``YtDlpService`` 会自动退回 HTTP 分块 Range，能救断连但拿不到提速。

产物落点
    ``build/vendor/aria2/aria2c.exe`` 与同目录的 ``COPYING``（GPLv2，随包分发
    必须附带）。build_windows.bat 用 ``--add-binary`` 把它放进
    ``_internal/tools/aria2/``，也就是 runtime part。

校验
    只信任 ``ARIA2C_SHA256``——对**解压出来的 aria2c.exe** 做校验，不是对 zip。
    上游重打 zip（时间戳/压缩参数变化）不该让构建失败，真正要钉死的是最终进包
    的那个二进制。哈希对不上直接 SystemExit，宁可断构建也不能发出未经核对的
    可执行文件。

幂等
    目标文件已存在且哈希正确就直接跳过，不重复下载。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path


ARIA2_VERSION = "1.37.0"
ARIA2_ARCHIVE = f"aria2-{ARIA2_VERSION}-win-64bit-build1"
ARIA2_URL = (
    f"https://github.com/aria2/aria2/releases/download/release-{ARIA2_VERSION}/{ARIA2_ARCHIVE}.zip"
)
# 解压后 aria2c.exe 的 SHA-256（见模块 docstring 的「校验」一节）
ARIA2C_SHA256 = ""
ARIA2C_EXPECTED_SIZE = 5_649_408

VENDOR_SUBDIR = Path("build") / "vendor" / "aria2"
# aria2 是 GPLv2+，随包分发必须附带许可全文；LICENSE.OpenSSL 是它的链接例外声明。
PAYLOAD_NAMES = ("aria2c.exe", "COPYING", "LICENSE.OpenSSL", "AUTHORS")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _up_to_date(vendor_dir: Path) -> bool:
    if any(not (vendor_dir / name).is_file() for name in PAYLOAD_NAMES):
        return False
    return _sha256((vendor_dir / "aria2c.exe").read_bytes()) == ARIA2C_SHA256


def _download(url: str) -> bytes:
    print(f"  下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "karaoke-studio-build"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - 固定的官方 release 地址
        return response.read()


def _extract(archive_bytes: bytes, vendor_dir: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        for name in PAYLOAD_NAMES:
            member = f"{ARIA2_ARCHIVE}/{name}"
            try:
                payload = zf.read(member)
            except KeyError:
                raise SystemExit(f"aria2 压缩包里找不到 {member}") from None
            (vendor_dir / name).write_bytes(payload)


def fetch(project_root: Path, *, force: bool = False) -> Path:
    if not ARIA2C_SHA256:
        raise SystemExit(
            "ARIA2C_SHA256 未填写。请先核对官方 release 的 aria2c.exe 哈希并写进 "
            "scripts/fetch_aria2.py，构建脚本不接受未经校验的二进制。"
        )

    vendor_dir = project_root / VENDOR_SUBDIR
    if not force and _up_to_date(vendor_dir):
        print(f"  aria2c 已就位且哈希一致，跳过下载：{vendor_dir}")
        return vendor_dir / "aria2c.exe"

    vendor_dir.mkdir(parents=True, exist_ok=True)
    _extract(_download(ARIA2_URL), vendor_dir)

    exe = vendor_dir / "aria2c.exe"
    digest = _sha256(exe.read_bytes())
    if digest != ARIA2C_SHA256:
        # 先删掉，避免下次运行把坏文件当成「已就位」
        shutil.rmtree(vendor_dir, ignore_errors=True)
        raise SystemExit(
            "aria2c.exe 哈希校验失败，已删除下载内容。\n"
            f"  期望: {ARIA2C_SHA256}\n"
            f"  实际: {digest}"
        )

    print(f"  aria2c {ARIA2_VERSION} 就位：{exe}（{exe.stat().st_size:,} 字节）")
    return exe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="忽略本地缓存，强制重新下载")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="项目根目录（默认：脚本所在仓库）",
    )
    args = parser.parse_args()

    fetch(args.project_root, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
