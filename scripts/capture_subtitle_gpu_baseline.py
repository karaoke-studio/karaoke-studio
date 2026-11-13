"""Capture versioned native GPU reference frames for the Skia migration."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", action="append", help="限定场景，可重复指定；默认全部")
    parser.add_argument("--include-real", action="store_true", help="额外加载本地真实 N3 样本，缺失即失败")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--renderer", type=Path, default=ROOT / "build/native-renderer/krok_subtitle_renderer.exe")
    parser.add_argument("--fps", type=int, choices=(60, 120), default=60)
    parser.add_argument("--sequence-seconds", type=float, default=0)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("输出目录必须为空，禁止覆盖已有参考基线。")
    if not math.isfinite(args.sequence_seconds) or args.sequence_seconds < 0 or (args.sequence_seconds and len(args.case or []) != 1):
        parser.error("连续片段时长必须非负，并且必须明确指定一个场景。")
    if not args.inventory_only and not args.renderer.is_file():
        parser.error("未找到用于生成基线的 native GPU renderer。")

    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    # Make font discovery independent of the offscreen plugin and test order.
    for name in ("meiryo.ttc", "times.ttf", "arial.ttf", "seguisym.ttf", "bahnschrift.ttf", "seguiemj.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.is_file() and QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(f"无法注册基线字体：{path}")

    from scripts.subtitle_gpu_baseline.artifacts import write_json
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from scripts.subtitle_gpu_baseline.inventory import inventory
    cases = build_cases(output / "assets", include_real=args.include_real)
    catalog = inventory(cases)
    write_json(output / "inventory.json", catalog)
    if args.case:
        unknown = set(args.case) - {case.case_id for case in cases}
        if unknown:
            parser.error(f"未知场景：{sorted(unknown)}")
        cases = [case for case in cases if case.case_id in args.case]
    if args.inventory_only:
        print(f"能力清单：{len(catalog['features'])} 类，场景：{len(cases)}。")
        return 0
    from scripts.subtitle_gpu_baseline.capture import capture_cases
    result = capture_cases(root=ROOT, output=output, executable=args.renderer.resolve(),
        cases=cases, fps=args.fps, sequence_seconds=args.sequence_seconds)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
