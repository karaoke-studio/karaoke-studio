"""Compare deterministic effect samples produced on different platforms.

The binary layout mirrors ``native/subtitle_renderer/tests/effect_samples.cpp``; change both
together.  Platforms legitimately differ in the last bits of libm results, so floats are
compared with tolerances, while anything discrete (particle counts, a particle switching
between visible and invisible, generated pixels) must match.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import math
from pathlib import Path
import struct
import sys

GEO_EFFECTS, GEO_INDICES, GEO_TIMES, GEO_FIELDS = 12, 6, 7, 8
PARTICLE_KINDS = ("ripple", "scanline", "sparkle", "twinkle", "twinkle_classic", "assemble",
                  "dissolve", "note", "heart", "flame", "flame_alt", "fireball", "flash",
                  "shockwave", "spark", "flame_glow", "lightning", "lightning_glow", "ember",
                  "smoke", "ash", "petal", "feather", "snow", "snow_solid")
PARTICLE_CASES = 6 * 4 * 3 * 2 * 7  # sprites x sweeps x speeds x seeds x times
PARTICLE_FIELDS = ("x", "y", "rotation", "size", "alpha", "stretch_x", "stretch_y")
NOISE_MASKS, COLUMN_IMAGES, COLUMN_BYTES, DECOR_MODES = 16, 6, 20 * 40 * 4, 8

# Pixel-space tolerances: far below anything visible, far above libm ulp noise.
POSITION_TOLERANCE = 1e-3
ALPHA_TOLERANCE = 1e-4


class Reader:
    def __init__(self, data: bytes, label: str):
        self.data, self.offset, self.label = data, 0, label

    def take(self, size: int) -> bytes:
        if self.offset + size > len(self.data):
            raise ValueError("sample file is truncated or has a different layout")
        chunk = self.data[self.offset:self.offset + size]
        self.offset += size
        return chunk

    def floats(self, count: int) -> tuple[float, ...]:
        start = self.offset
        values = struct.unpack(f"<{count}f", self.take(4 * count))
        for index, value in enumerate(values):
            if not math.isfinite(value):
                raise ValueError(
                    f"{self.label}: non-finite float {value!r} at byte offset {start + 4 * index}")
        return values

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]


@dataclass
class Report:
    geometry_max: float = 0.0
    particle_max: dict[str, float] = field(default_factory=lambda: dict.fromkeys(PARTICLE_FIELDS, 0.0))
    count_mismatches: dict[str, int] = field(default_factory=dict)
    visibility_flips: int = 0
    pixel_bytes_differing: int = 0
    particles: int = 0

    def failures(self) -> list[str]:
        problems = []
        if self.geometry_max > POSITION_TOLERANCE:
            problems.append(f"geometry animation differs by {self.geometry_max:.3g}")
        for name, worst in self.particle_max.items():
            limit = ALPHA_TOLERANCE if name == "alpha" else POSITION_TOLERANCE
            if worst > limit:
                problems.append(f"particle {name} differs by {worst:.3g} (limit {limit:g})")
        if self.count_mismatches:
            problems.append(f"particle counts differ: {self.count_mismatches}")
        if self.visibility_flips:
            problems.append(f"{self.visibility_flips} particles switch between visible and invisible")
        if self.pixel_bytes_differing:
            problems.append(f"{self.pixel_bytes_differing} generated pixel bytes differ")
        return problems


def compare(reference: bytes, candidate: bytes) -> Report:
    a, b = Reader(reference, "reference"), Reader(candidate, "candidate")
    report = Report()
    for _ in range(GEO_EFFECTS * GEO_INDICES * GEO_TIMES):
        for x, y in zip(a.floats(GEO_FIELDS), b.floats(GEO_FIELDS)):
            report.geometry_max = max(report.geometry_max, abs(x - y))
    for kind in PARTICLE_KINDS:
        for _ in range(PARTICLE_CASES):
            count_a, count_b = a.u32(), b.u32()
            first = [a.floats(len(PARTICLE_FIELDS)) for _ in range(count_a)]
            second = [b.floats(len(PARTICLE_FIELDS)) for _ in range(count_b)]
            if count_a != count_b:
                report.count_mismatches[kind] = report.count_mismatches.get(kind, 0) + 1
                continue
            for p, q in zip(first, second):
                report.particles += 1
                for name, x, y in zip(PARTICLE_FIELDS, p, q):
                    report.particle_max[name] = max(report.particle_max[name], abs(x - y))
                if (p[4] > 0) != (q[4] > 0):
                    report.visibility_flips += 1

    def pixels(size: int) -> None:
        report.pixel_bytes_differing += sum(x != y for x, y in zip(a.take(size), b.take(size)))

    for _ in range(NOISE_MASKS):
        size_a, size_b = a.u32(), b.u32()
        if size_a != size_b:
            raise ValueError("noise mask sizes differ")
        pixels(size_a * 4)
    for _ in range(COLUMN_IMAGES):
        pixels(COLUMN_BYTES)
    for _ in range(DECOR_MODES):
        shape_a = (a.i32(), a.i32(), a.i32())
        shape_b = (b.i32(), b.i32(), b.i32())
        if shape_a != shape_b:
            raise ValueError(f"decor image shapes differ: {shape_a} vs {shape_b}")
        pixels(shape_a[0] * shape_a[1] * 4)
    if a.offset != len(a.data) or b.offset != len(b.data):
        raise ValueError("trailing bytes: effect_samples.cpp layout changed without this script")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidates", type=Path, nargs="+")
    options = parser.parse_args(argv)
    reference = options.reference.read_bytes()
    failed = False
    for candidate in options.candidates:
        try:
            report = compare(reference, candidate.read_bytes())
        except ValueError as error:
            print(f"{candidate.name} vs {options.reference.name}: FAIL: {error}")
            failed = True
            continue
        worst = ", ".join(f"{name}={value:.3g}" for name, value in report.particle_max.items())
        print(f"{candidate.name} vs {options.reference.name}: {report.particles} particles; "
              f"geometry max={report.geometry_max:.3g}; particle max: {worst}")
        for problem in report.failures():
            print(f"  FAIL: {problem}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
