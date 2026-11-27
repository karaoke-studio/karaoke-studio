#!/usr/bin/env bash
# ============================================================================
# build_linux_arm64_skia_only.sh
#
# krok-helper：Linux + arm64 + Vulkan 的 Skia 字幕渲染器一键构建脚本。
# 位于仓库的 scripts/ 目录下（与 build_skia_renderer.py 同层）
#
# 最终产物：
#   ~/.cache/krok-helper/native/linux-arm64-vulkan/
#     ├─ krok_skia_subtitle_renderer
#     └─ qml/Krok/SubtitleSkia/libsubtitle_skia_preview.so
#        （预览库为 QML 模块插件，构建器按 QML 布局放入子目录）
#
# 用法（任意目录均可，脚本会自动定位仓库根）：
#   bash scripts/build_linux_arm64_skia_only.sh
#   bash scripts/build_linux_arm64_skia_only.sh --qt-root ~/Qt/6.11.0/gcc_arm64 --jobs 8
#   bash scripts/build_linux_arm64_skia_only.sh --clean        # 清空 ~/.cache/krok-helper 全部缓存，从零重建
#
# 构建模式：
#   （默认，不带参数） 正常模式：复用 ~/.cache/krok-helper 中已解压的 LLVM 与
#                      既有缓存，跳过下载/解压 tar.xz 这一步（日常构建最快）
#   --clean            清空 ~/.cache/krok-helper 下全部缓存（含 LLVM 安装包与
#                      解压目录、gn/ninja、Skia 源码 skia-deps、构建产物），
#                      然后完全从零重新下载并重建（耗时最长）
#
# 缓存位置说明：
#   缓存根为 ~/.cache/krok-helper，与 Python 构建器的 skia-deps 及运行时
#   resolve_skia_renderer_path 的发现路径保持一致。若存在上一版脚本遗留的
#   ~/.cache/krok-studio，正常模式会自动把其中的 tools/downloads/native
#   迁回 ~/.cache/krok-helper（避免重复下载 LLVM）；--clean 则两处一并清空。
#
# 环境变量：
#   QT_ROOT      Qt 6.11.0 SDK 根目录（gcc_arm64，含 qtbase + qtdeclarative）
#   JOBS         并行编译任务数（默认 nproc）
#   CLEAN=1      等价于 --clean：清空 ~/.cache/krok-helper 全部缓存后从零重建
#   SMOKE_FONT   冒烟测试字体名（存在时透传给构建器）
#
# 系统依赖（脚本只做预检并提示，绝不会自动安装）：
#   Debian/Ubuntu: sudo apt install build-essential cmake git python3 curl wget xz-utils file
#   Qt SDK: 用 Qt 在线安装器安装 6.11.0（linux gcc_arm64）并勾选 qtbase、qtdeclarative
#
# 建议仓库保持 LF 行尾提交 .sh（Windows 侧可配置 git：core.autocrlf=input，
# 或 .gitattributes 写 `*.sh text eol=lf`），直接 ./ 执行时 shebang 才不会被 \r 破坏。
# ============================================================================

# --- 兜底引导：下面两行刻意保持 POSIX 兼容、行尾用注释吸收 \r，容忍 CRLF / sh 调用 ---
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@" # ensure bash
grep -q $'\r' "$0" 2>/dev/null && KROK_ORIGIN="$0" exec bash <(sed 's/\r$//' "$0") "$@" || true # CRLF guard

set -Eeuo pipefail

# ---------------------------- 日志与错误处理 ----------------------------
if [ -t 2 ]; then
  C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
log_info()  { printf '%s[INFO]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
log_warn()  { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
log_error() { printf '%s[ERR ]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; }
die() { log_error "$*"; exit 1; }

# ---------------------------- Qt 环境准备提示 ----------------------------
# 开跑前打印一次；任何非 0 退出（die / ERR trap / Ctrl-C）由 EXIT trap 再打印一次。
show_qt_notice() {
  printf '%s' "$C_WARN" >&2
  cat >&2 <<'EOF'
============================================================
 ⚠ 重要提示：运行本脚本前必须先准备好 Qt 6.11.0 SDK
============================================================
 1. 打开网页 https://www.qt.io/development/download-qt-installer-oss
    下载并安装 Qt 在线安装器（Qt Online Installer）；
 2. 通过安装器安装 Qt 6.11.0 桌面版（Desktop，Linux gcc_arm64），
    并勾选 qtbase 与 qtdeclarative 组件（预览模块依赖 Qt Quick）；
 3. 安装 Qt 私有开发头文件：
        sudo apt install qt6-base-private-dev

安装完成后，用 --qt-root 参数或 QT_ROOT 环境变量指定 SDK 根目录：
    bash scripts/build_linux_arm64_skia_only.sh --qt-root ~/Qt/6.11.0/gcc_arm64

常用构建模式：
  （默认，不带参数） 正常模式：复用 ~/.cache/krok-helper 中已解压的 LLVM 与
                    既有缓存，跳过下载/解压 tar.xz（日常构建最快）
  --clean           彻底清空 ~/.cache/krok-helper 全部缓存后从零重建
                    （LLVM 安装包与解压目录、gn/ninja、Skia 源码 skia-deps
                    全部重新下载/拉取，耗时最长）
============================================================
EOF
  printf '%s' "$C_OFF" >&2
}

trap 'rc=$?; log_error "命令失败 (exit=$rc)，位置：第 ${BASH_LINENO[0]:-?} 行附近：${BASH_COMMAND:-?}"; exit $rc' ERR
# 构建失败退出前，再次给出 Qt 环境准备提示（成功退出 rc=0 不打印）
trap 'rc=$?; if [ "$rc" -ne 0 ]; then show_qt_notice; fi' EXIT

# ---------------------------- 路径与常量 ----------------------------
SELF="${KROK_ORIGIN:-${BASH_SOURCE[0]:-$0}}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SELF")" && pwd -P)"

# 本脚本位于仓库的 scripts/ 目录（与 build_skia_renderer.py 同层），
# 仓库根为其上一级目录；兜底兼容脚本被放回仓库根目录的情况。
if [ -f "$SCRIPT_DIR/build_skia_renderer.py" ]; then
  ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
else
  ROOT="$SCRIPT_DIR"
fi
BUILD_PY="$ROOT/scripts/build_skia_renderer.py"

LOCK_FILE=""
for _c in "$ROOT/skia.lock.json" "$ROOT/scripts/skia.lock.json"; do
  if [ -f "$_c" ]; then LOCK_FILE="$_c"; break; fi
done
if [ -z "$LOCK_FILE" ]; then
  LOCK_FILE="$(find "$ROOT" -maxdepth 3 -type f -name 'skia.lock.json' -print 2>/dev/null | head -n1 || true)"
fi

# 缓存根与 Python 构建器保持一致：~/.cache/krok-helper
# （构建器的 Skia 源码位于 ~/.cache/krok-helper/skia-deps，--clean 可一并清掉）
CACHE="$HOME/.cache/krok-helper"
LEGACY_CACHE="$HOME/.cache/krok-studio"
DOWNLOADS="$CACHE/downloads"
TOOLS_DIR="$CACHE/tools"
LLVM_DIR="$TOOLS_DIR/llvm-21.1.8-linux-arm64"
LLVM_URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-21.1.8/LLVM-21.1.8-Linux-ARM64.tar.xz"
LLVM_SHA256="65ce0b329514e5643407db2d02a5bd34bf33d159055dafa82825c8385bd01993"
NATIVE_OUT="$CACHE/native/linux-arm64-vulkan"
QT_MIN="6.11.0"
EXE_NAME="krok_skia_subtitle_renderer"
SO_NAME="libsubtitle_skia_preview.so"
# 预览库是 QML 模块插件：构建器按 QML 布局输出到 native_output 的
# qml/Krok/SubtitleSkia/ 子目录（见 build_skia_renderer.py 的 elf_paths），而非产物根目录
SO_REL="qml/Krok/SubtitleSkia/libsubtitle_skia_preview.so"
SO_PATH=""

# ---------------------------- 参数解析 ----------------------------
QT_ROOT_ARG="${QT_ROOT:-}"
JOBS_ARG="${JOBS:-}"
DO_CLEAN="${CLEAN:-0}"

usage() {
  cat <<'USAGE'
用法: bash scripts/build_linux_arm64_skia_only.sh [选项]

选项:
  --qt-root <dir>  Qt 6.11.0 SDK 根目录（gcc_arm64）；也可用环境变量 QT_ROOT
  --jobs <N>       并行任务数；也可用环境变量 JOBS（默认 nproc）
  --clean          清空 ~/.cache/krok-helper 下全部缓存后从零重建
                   （LLVM 安装包与解压目录、gn/ninja、Skia 源码 skia-deps、
                   构建产物全部删除后重新下载/拉取/编译；同时清理上一版
                   脚本遗留的 ~/.cache/krok-studio）
  -h, --help       显示帮助

构建模式:
  （默认，不带参数） 正常模式：复用 ~/.cache/krok-helper 中已解压的 LLVM 与
                   既有缓存，跳过下载/解压 tar.xz；若存在旧版遗留的
                   ~/.cache/krok-studio 会自动迁回，避免重复下载

环境变量:
  QT_ROOT / JOBS / CLEAN / SMOKE_FONT
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --qt-root)
      if [ "$#" -lt 2 ]; then die "--qt-root 需要一个目录参数"; fi
      QT_ROOT_ARG="$2"; shift 2 ;;
    --qt-root=*)
      QT_ROOT_ARG="${1#*=}"; shift ;;
    --jobs)
      if [ "$#" -lt 2 ]; then die "--jobs 需要一个数字参数"; fi
      JOBS_ARG="$2"; shift 2 ;;
    --jobs=*)
      JOBS_ARG="${1#*=}"; shift ;;
    --clean)
      DO_CLEAN=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      die "未知参数：$1（--help 查看用法）" ;;
  esac
done

JOBS="${JOBS_ARG:-$(nproc 2>/dev/null || echo 8)}"
case "$JOBS" in
  ""|*[!0-9]*) die "JOBS 必须是正整数（当前：'$JOBS'）" ;;
esac
case "$DO_CLEAN" in
  1|true|TRUE|True|yes|YES|Yes) DO_CLEAN=1 ;;
  0|false|FALSE|False|no|NO|No) DO_CLEAN=0 ;;
  *) die "CLEAN 只接受 0/1/true/false（当前：'$DO_CLEAN'）" ;;
esac

# ---------------------------- 预检 1：宿主机与基础工具 ----------------------------
preflight_host() {
  log_info "预检 1/3：宿主机与基础工具"
  local os arch pyver
  os="$(uname -s)"
  arch="$(uname -m)"
  if [ "$os" != "Linux" ]; then
    die "仅支持 Linux 宿主机（当前 uname -s=$os）"
  fi
  case "$arch" in
    aarch64|arm64) log_info "宿主架构：$arch" ;;
    *) die "目标为 linux-arm64，请在 aarch64 宿主机运行（当前 uname -m=$arch）" ;;
  esac

  local missing=()
  local c
  for c in python3 git cmake tar xz sha256sum; do
    if ! command -v "$c" >/dev/null 2>&1; then missing+=("$c"); fi
  done
  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    missing+=("curl或wget")
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    log_error "缺少基础工具：${missing[*]}"
    log_error "可参考（脚本不会自动执行）：sudo apt install build-essential cmake git python3 curl wget xz-utils file"
    die "请安装缺失工具后重试"
  fi

  pyver="$(python3 -c 'import sys; print("%d.%d" % (sys.version_info[0], sys.version_info[1]))')"
  if [ "$(printf '%s\n' "3.10" "$pyver" | sort -V | head -n1)" != "3.10" ]; then
    die "python3 版本过低（$pyver），需要 >= 3.10"
  fi
  log_info "python3 $pyver OK"

  if ! command -v gcc >/dev/null 2>&1 && ! command -v cc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
    log_warn "未发现 C/C++ 编译器（gcc/cc/clang）；CMake 侧可能失败，建议安装 build-essential"
  fi
}

# ---------------------------- 预检 2：skia.lock.json ----------------------------
preflight_lock() {
  log_info "预检 2/3：skia.lock.json 的 linux-arm64 条目"
  if [ -z "$LOCK_FILE" ] || [ ! -f "$LOCK_FILE" ]; then
    die "未找到 skia.lock.json（已搜索仓库 3 层目录），请确认文件位置"
  fi
  log_info "锁文件：$LOCK_FILE"
  if ! python3 - "$LOCK_FILE" <<'PY'
import json, re, sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read()

try:
    lock = json.loads(raw)
except Exception as exc:
    print("LOCK_ERROR: skia.lock.json 不是合法 JSON: %s" % exc)
    sys.exit(1)

hexre = re.compile(r"^[0-9a-fA-F]{64}$")
uphex = re.compile(r"^[0-9A-F]{64}$")


def iter_strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_strings(value)
    elif isinstance(node, str):
        yield node


problems = 0
for s in iter_strings(lock):
    if "PLACEHOLDER" in s.upper():
        print("LOCK_ERROR: 锁文件仍包含占位符，请先填入真实哈希: %s" % s)
        problems += 1

if "linux-arm64" not in raw:
    print("LOCK_ERROR: 锁文件中没有任何 linux-arm64 条目（gn/ninja 与工具链均未添加）")
    sys.exit(1)


def pick_section(name):
    if isinstance(lock, dict):
        if isinstance(lock.get(name), (dict, list)):
            return lock[name]
        for value in lock.values():
            if isinstance(value, dict) and isinstance(value.get(name), (dict, list)):
                return value[name]
    return None


def pick_arm(section):
    if isinstance(section, dict):
        return section.get("linux-arm64")
    if isinstance(section, list):
        for item in section:
            if isinstance(item, dict) and "linux-arm64" in json.dumps(item, ensure_ascii=False):
                return item
    return None


tools = pick_section("tools")
arm_tools = pick_arm(tools)
if isinstance(arm_tools, dict):
    for tool in ("gn", "ninja"):
        entry = arm_tools.get(tool)
        if not isinstance(entry, dict):
            print("LOCK_WARN: tools.linux-arm64.%s 不是对象，跳过自动校验" % tool)
            continue
        shas = [s for s in iter_strings(entry) if hexre.match(s)]
        upper = [s for s in shas if uphex.match(s)]
        if upper:
            print("LOCK_WARN: tools.linux-arm64.%s 存在大写哈希（构建器通常要求小写）: %s"
                  % (tool, ", ".join(upper)))
        if not shas:
            print("LOCK_ERROR: tools.linux-arm64.%s 未发现 64 位 sha256" % tool)
            problems += 1
        elif len(shas) < 2:
            print("LOCK_WARN: tools.linux-arm64.%s 仅发现 %d 个哈希，binary_sha256 可能尚未填写"
                  % (tool, len(shas)))
        else:
            print("LOCK_OK: tools.linux-arm64.%s 哈希齐备" % tool)
elif tools is not None:
    print("LOCK_WARN: 未能按预期结构在 tools 中识别 linux-arm64，请人工确认 gn/ninja 条目")
else:
    print("LOCK_WARN: 未找到 tools 段，跳过 gn/ninja 详细校验")

toolchains = pick_section("toolchains")
arm_tc = pick_arm(toolchains)
if arm_tc is None:
    print("LOCK_WARN: 未找到 toolchains.linux-arm64 工具链条目")
elif "21.1.8" in json.dumps(arm_tc, ensure_ascii=False):
    print("LOCK_OK: toolchains.linux-arm64 = clang++ 21.1.8")
else:
    print("LOCK_WARN: toolchains.linux-arm64 版本不是 21.1.8，与本脚本下载的 LLVM 不一致")

if problems:
    sys.exit(1)
print("LOCK_OK: skia.lock.json 预检通过")
PY
  then
    die "skia.lock.json 预检未通过（见上方 LOCK_ERROR 详情）"
  fi
}

# ---------------------------- 预检 3：Qt SDK ----------------------------
preflight_qt() {
  log_info "预检 3/3：Qt SDK（Qt6Quick / qtdeclarative，需 >= $QT_MIN）"
  QT_RESOLVED=""
  if [ -n "$QT_ROOT_ARG" ]; then
    if [ ! -d "$QT_ROOT_ARG" ]; then
      die "--qt-root/QT_ROOT 指定的目录不存在：$QT_ROOT_ARG"
    fi
    if [ ! -f "$QT_ROOT_ARG/lib/cmake/Qt6Quick/Qt6QuickConfig.cmake" ]; then
      die "$QT_ROOT_ARG 缺少 lib/cmake/Qt6Quick/Qt6QuickConfig.cmake（qtdeclarative 未安装？）"
    fi
    QT_RESOLVED="$QT_ROOT_ARG"
  else
    local candidates=(
      "$HOME/Qt/6.11.0/gcc_arm64"
      "/opt/Qt/6.11.0/gcc_arm64"
      "$HOME/Qt/"*"6.11.0"*/gcc_arm64
      "$HOME/Qt/"*/gcc_arm64
      "/opt/Qt/"*/gcc_arm64
    )
    local dir
    for dir in "${candidates[@]}"; do
      if [ -f "$dir/lib/cmake/Qt6Quick/Qt6QuickConfig.cmake" ]; then
        QT_RESOLVED="$dir"
        break
      fi
    done
    if [ -z "$QT_RESOLVED" ]; then
      die "未找到 Qt SDK，请到 https://www.qt.io/development/download-qt-installer-oss 下载运行 Qt Online Installer for Linux 已安装好可以用 --qt-root 或环境变量 QT_ROOT 指定 6.11.0 的 gcc_arm64 SDK 根目录"
    fi
  fi

  local ver_file="$QT_RESOLVED/lib/cmake/Qt6/Qt6ConfigVersionImpl.cmake"
  if [ ! -f "$ver_file" ]; then
    die "缺少 $ver_file，无法确认 Qt 版本"
  fi
  local qver
  qver="$(sed -n 's/.*PACKAGE_VERSION[^0-9"]*"\{0,1\}\([0-9][0-9.]*\).*/\1/p' "$ver_file" | head -n1 || true)"
  if [ -z "$qver" ]; then
    die "无法从 Qt6ConfigVersion.cmake 解析出版本号（$ver_file）"
  fi
  if [ "$(printf '%s\n' "$QT_MIN" "$qver" | sort -V | head -n1)" != "$QT_MIN" ]; then
    die "Qt 版本过低：$qver < $QT_MIN（需要 6.11.0 的 qtbase + qtdeclarative）"
  fi
  log_info "Qt SDK：$QT_RESOLVED（$qver）"
}

# ---------------------------- LLVM 21.1.8（固定 sha256，幂等） ----------------------------
# 正常模式（默认）：已解压且校验通过则直接复用，跳过下载/解压 tar.xz；
# 缓存不存在时（如 --clean 之后、首次构建）才下载并解压一次。
ensure_llvm() {
  log_info "工具链：LLVM 21.1.8（Linux ARM64，固定 sha256）"
  mkdir -p "$DOWNLOADS" "$TOOLS_DIR"
  local archive="$DOWNLOADS/LLVM-21.1.8-Linux-ARM64.tar.xz"
  local marker="$LLVM_DIR/.krok-llvm-21.1.8-arm64.ok"

  # 兼容旧版脚本解压过但从未写过标记的 LLVM：验证 clang++ 可用且版本正确后
  # 补写标记。这样正常模式即可直接复用之前解压过的内容，跳过下载与解压 tar.xz。
  if [ ! -f "$marker" ] && [ -f "$LLVM_DIR/bin/clang++" ]; then
    chmod +x "$LLVM_DIR/bin/clang++" 2>/dev/null || true
    if "$LLVM_DIR/bin/clang++" --version 2>/dev/null | grep -q '21.1.8'; then
      log_info "发现已解压且可用的 LLVM 21.1.8：$LLVM_DIR（补写标记，跳过下载/解压）"
      touch "$marker"
    else
      log_warn "$LLVM_DIR 已存在但 clang++ 异常，将重新解压覆盖"
    fi
  fi

  if [ -f "$marker" ]; then
    log_info "已有校验通过的 LLVM 21.1.8：$LLVM_DIR（跳过下载/解压 tar.xz）"
  else
    local need_download=1
    if [ -f "$archive" ]; then
      if printf '%s  %s\n' "$LLVM_SHA256" "$archive" | sha256sum -c - >/dev/null 2>&1; then
        log_info "复用已下载且校验通过的安装包：$archive"
        need_download=0
      else
        log_warn "已存在的安装包 sha256 不匹配，将重新下载：$archive"
        rm -f "$archive"
      fi
    fi
    if [ "$need_download" -eq 1 ]; then
      log_info "开始下载：$LLVM_URL"
      if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 2 -o "$archive" "$LLVM_URL"
      else
        wget -O "$archive" "$LLVM_URL"
      fi
      if ! printf '%s  %s\n' "$LLVM_SHA256" "$archive" | sha256sum -c - >/dev/null 2>&1; then
        rm -f "$archive"
        die "LLVM 安装包 sha256 校验失败（期望 $LLVM_SHA256），已删除损坏文件"
      fi
      log_info "sha256 校验通过"
    fi

    local stage="$TOOLS_DIR/.llvm-stage-$$"
    rm -rf "$stage"
    mkdir -p "$stage"
    log_info "解压中（tar -xJf，可能需要一两分钟）..."
    tar -xJf "$archive" -C "$stage"
    local top src
    top="$(ls -A "$stage" | head -n1 || true)"
    if [ -n "$top" ] && [ "$(ls -A "$stage" | wc -l)" -eq 1 ] && [ -d "$stage/$top" ]; then
      src="$stage/$top"
    else
      src="$stage"
    fi
    rm -rf "$LLVM_DIR"
    mkdir -p "$TOOLS_DIR"
    mv "$src" "$LLVM_DIR"
    rm -rf "$stage"
    touch "$marker"
    log_info "解压完成：$LLVM_DIR（已写标记，后续正常模式跳过下载/解压）"
  fi

  CLANGXX="$(find "$LLVM_DIR" \( -type f -o -type l \) -name 'clang++' 2>/dev/null | head -n1 || true)"
  if [ -z "$CLANGXX" ]; then
    die "在 $LLVM_DIR 中未找到 clang++"
  fi
  chmod +x "$CLANGXX" 2>/dev/null || true

  local clangver
  clangver="$("$CLANGXX" --version 2>/dev/null | head -n1 || true)"
  case "$clangver" in
    *21.1.8*) log_info "clang++：$CLANGXX" ;;
    *) die "clang++ 版本异常：'$clangver'（期望 21.1.8）" ;;
  esac

  # 系统 clang 提醒（必须在把 LLVM_BIN 插到 PATH 之前检测，否则测到的是下载版）
  if command -v clang++ >/dev/null 2>&1; then
    local sysver
    sysver="$(clang++ --version 2>/dev/null | head -n1 || true)"
    case "$sysver" in
      *21.1.8*) log_info "系统 clang++ 也是 21.1.8" ;;
      *) log_warn "系统 clang++ 非 21.1.8（$sysver）；构建将优先使用本脚本下载的 LLVM 21.1.8（已置于 PATH 最前）" ;;
    esac
  fi

  LLVM_BIN="$(dirname -- "$CLANGXX")"
  export PATH="$LLVM_BIN:$PATH"
  log_info "PATH 注入：$LLVM_BIN"
}

# ---------------------------- CLEAN=1 清缓存 ----------------------------
# 清空 ~/.cache/krok-helper 全部缓存（含 LLVM 安装包与解压目录、gn/ninja、
# Skia 源码 skia-deps、构建产物），之后走正常流程重新下载、解压、拉源码、
# 从零编译。上一版脚本遗留的 ~/.cache/krok-studio 一并清掉。
clean_cache() {
  log_warn "CLEAN=1：清空全部构建缓存 $CACHE（从头开始重新构建）"
  rm -rf -- "$CACHE"
  if [ -e "$LEGACY_CACHE" ]; then
    log_warn "同时清理上一版脚本遗留的旧缓存：$LEGACY_CACHE"
    rm -rf -- "$LEGACY_CACHE"
  fi
  log_info "缓存清理完成：LLVM 安装包/解压目录、gn/ninja、Skia 源码（skia-deps）、构建产物已全部删除"
  log_info "本次构建将重新下载 LLVM 并完整重建（耗时较长）"
}

# ---------------------------- 旧缓存位置迁移（krok-studio -> krok-helper） ----------------------------
# 上一版脚本曾把缓存放在 ~/.cache/krok-studio。若那里已有下载好的 LLVM 安装包、
# 解压目录或构建产物，而 ~/.cache/krok-helper 对应子目录尚不存在，则整体搬回，
# 避免正常模式重复下载 GB 级的 LLVM tar.xz。搬运失败不致命（大不了重新下载）。
migrate_legacy_cache() {
  if [ ! -d "$LEGACY_CACHE" ]; then
    return 0
  fi
  log_info "发现上一版脚本遗留的缓存目录：$LEGACY_CACHE，尝试迁回 $CACHE"
  local sub moved=0
  for sub in tools downloads native; do
    if [ -d "$LEGACY_CACHE/$sub" ] && [ ! -e "$CACHE/$sub" ]; then
      mkdir -p "$CACHE"
      if mv "$LEGACY_CACHE/$sub" "$CACHE/$sub"; then
        log_info "已迁移：$LEGACY_CACHE/$sub -> $CACHE/$sub"
        moved=1
      else
        log_warn "迁移失败（忽略，将按需重新下载/构建）：$LEGACY_CACHE/$sub"
      fi
    fi
  done
  if [ "$moved" -eq 1 ]; then
    log_info "旧缓存迁移完成"
  fi
}

# ---------------------------- 调用 Python 构建器 ----------------------------
run_build() {
  if [ -n "${SMOKE_FONT:-}" ]; then
    export SMOKE_FONT
    log_info "SMOKE_FONT=$SMOKE_FONT（透传给构建器）"
  fi
  cd -- "$ROOT"
  log_info "调用构建器：scripts/build_skia_renderer.py --platform linux --arch arm64 --gpu-api vulkan --llvm-root $LLVM_DIR --jobs $JOBS"
  # 显式传入固定 LLVM：避免构建器按 PATH 摸到无法执行的第三方 clang 包装脚本
  python3 "$BUILD_PY" \
    --platform linux \
    --arch arm64 \
    --gpu-api vulkan \
    --qt-root "$QT_RESOLVED" \
    --llvm-root "$LLVM_DIR" \
    --jobs "$JOBS"
}

# ---------------------------- 产物校验 ----------------------------
validate_artifacts() {
  log_info "校验产物（预期目录：$NATIVE_OUT）"
  local exe="$NATIVE_OUT/$EXE_NAME"
  local so="$NATIVE_OUT/$SO_REL"
  if [ ! -f "$exe" ]; then
    # 兜底：native 产物的实际落盘路径由 Python 构建器决定；若与预期不一致，
    # 在整个缓存根（~/.cache/krok-helper）内搜索同名可执行文件。
    exe="$(find "$CACHE" -type f -name "$EXE_NAME" -print -quit 2>/dev/null || true)"
  fi
  if [ -z "$exe" ] || [ ! -f "$exe" ]; then
    die "缺少可执行产物 $EXE_NAME（已在 $NATIVE_OUT 与 $CACHE 全树搜索未找到）；请检查上方构建日志"
  fi
  if [ "$exe" != "$NATIVE_OUT/$EXE_NAME" ]; then
    log_warn "可执行文件不在预期路径 $NATIVE_OUT/$EXE_NAME，实际位于：$exe"
  fi
  if [ ! -f "$so" ]; then
    so="$(find "$CACHE" -type f -path "*qml/Krok/SubtitleSkia/$SO_NAME" -print -quit 2>/dev/null || true)"
  fi
  if [ -z "$so" ] || [ ! -f "$so" ]; then
    so="$(find "$CACHE" -type f -name "$SO_NAME" -print -quit 2>/dev/null || true)"
  fi
  if [ -z "$so" ] || [ ! -f "$so" ]; then
    die "缺少预览动态库 $SO_NAME（已在 $NATIVE_OUT 与 $CACHE 全树搜索未找到）；请检查上方 CMake 日志确认 KROK_BUILD_SKIA_PREVIEW 是否真的编出了 QML 插件"
  fi
  SO_PATH="$so"
  if [ ! -x "$exe" ]; then
    chmod +x "$exe" 2>/dev/null || true
  fi
  if command -v file >/dev/null 2>&1; then
    local fexe fso
    fexe="$(file -b "$exe" || true)"
    fso="$(file -b "$so" || true)"
    case "$fexe" in
      *aarch64*|*ARM64*|*arm64*) log_info "可执行文件架构：$fexe" ;;
      *) log_warn "可执行文件架构非 aarch64：$fexe" ;;
    esac
    log_info "动态库：$fso"
  fi
  log_info "产物清单："
  ls -lh -- "$exe" "$so" || true
  ls -lh -- "$(dirname -- "$exe")" || true
}

# ---------------------------- 主流程 ----------------------------
main() {
  show_qt_notice
  log_info "============================================================"
  log_info " krok-helper: Linux arm64 Skia 渲染器构建（skia-only）"
  log_info " 脚本位置   : build_linux_arm64_skia_only.sh"
  log_info " 仓库根目录 : $ROOT"
  log_info " 缓存目录   : $CACHE"
  log_info " 产物目录   : $NATIVE_OUT"
  log_info " 并行任务数 : $JOBS"
  if [ "$DO_CLEAN" -eq 1 ]; then
    log_info " 构建模式   : --clean（清空 ~/.cache/krok-helper 全部缓存，从零重建）"
  else
    log_info " 构建模式   : 正常模式（复用已解压的 LLVM，跳过下载/解压）"
  fi
  log_info "============================================================"

  if [ ! -f "$BUILD_PY" ]; then
    die "未找到构建入口：$BUILD_PY（请把本脚本放在仓库的 scripts/ 目录下）"
  fi

  if [ "$DO_CLEAN" -eq 1 ]; then
    clean_cache
  else
    migrate_legacy_cache
  fi

  preflight_host
  preflight_lock
  preflight_qt
  ensure_llvm
  run_build
  validate_artifacts

  log_info "============================================================"
  log_info " 构建完成 ✔"
  log_info " 渲染器: $NATIVE_OUT/$EXE_NAME"
  # 预览库是 QML 模块插件，位于 qml/Krok/SubtitleSkia/ 子目录
  # （validate_artifacts 已解析出实际路径存入 SO_PATH）
  log_info " 预览库: ${SO_PATH:-$NATIVE_OUT/$SO_REL}"
  log_info " 运行时会由 krok_helper/subtitle_render/native/skia_process.py"
  log_info " 的 resolve_skia_renderer_path 自动发现（缓存根与运行时一致：~/.cache/krok-helper）。"
  log_info "============================================================"
}

main "$@"
