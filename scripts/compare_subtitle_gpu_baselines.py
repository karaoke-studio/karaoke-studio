"""Strict before/after native GPU comparison; differences are evidence, never waived."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import zlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.subtitle_gpu_baseline.artifacts import pixel_difference, write_json


def compare(before: Path, after: Path) -> dict:
    old = json.loads((before / "manifest.json").read_text(encoding="utf-8"))
    new = json.loads((after / "manifest.json").read_text(encoding="utf-8"))
    for key in ("width", "height", "fps", "reference_backend", "gpu_api", "render_settings"):
        if old[key] != new[key]:
            raise ValueError(f"Incomparable capture setting: {key}")
    old_cases = {case["id"]: case for case in old["cases"]}
    new_cases = {case["id"]: case for case in new["cases"]}
    if old_cases.keys() != new_cases.keys():
        raise ValueError("Capture cases differ")
    differences = []
    count = 0
    for key, left in old_cases.items():
        right = new_cases[key]
        if left["scene_sha256"] != right["scene_sha256"]:
            raise ValueError(f"Render IR differs: {key}")
        frames = {frame["t_ms"]: frame for frame in right["frames"]}
        if set(frames) != {frame["t_ms"] for frame in left["frames"]}:
            raise ValueError(f"Sample times differ: {key}")
        for a in left["frames"]:
            b = frames[a["t_ms"]]
            count += 1
            if a["pixel_sha256"] == b["pixel_sha256"]:
                continue
            raw_a = zlib.decompress((before / "cases" / key / a["raw_file"]).read_bytes())
            raw_b = zlib.decompress((after / "cases" / key / b["raw_file"]).read_bytes())
            differences.append({"case": key, "t_ms": a["t_ms"],
                "before_path": a["native_event"].get("realization_path_ready"),
                "after_path": b["native_event"].get("realization_path_ready"),
                "difference": pixel_difference(raw_a, raw_b, old["width"], old["height"])})
    return {"status": "failed" if differences else "passed", "frames": count,
            "identical_frames": count - len(differences), "differences": differences,
            "before_source": old["source"], "after_source": new["source"],
            "before_seek_differences": sum(len(c["seek_mismatches"]) for c in old["cases"]),
            "after_seek_differences": sum(len(c["seek_mismatches"]) for c in new["cases"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.before, args.after)
    write_json(args.output, result)
    print(json.dumps({k: result[k] for k in ("status", "frames", "identical_frames", "before_seek_differences", "after_seek_differences")}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
