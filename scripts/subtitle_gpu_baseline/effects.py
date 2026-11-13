"""Enumerated effect and interaction cases, evaluated at explicit project times."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
from typing import get_args
from krok_helper.subtitle_render.domain.models import ScanlineEffect

from krok_helper.subtitle_render.domain.timing import (
    EntryAnimation, ExitAnimation, KaraokeAnimation, SingFx, LineAnimationOverride,
)
from .model import BaselineCase


def effect_cases(track, base, asset_dir: Path) -> list[BaselineCase]:
    cases = []
    for phase, annotation in (("entry", EntryAnimation), ("exit", ExitAnimation)):
        for effect in get_args(annotation):
            changes = {"entry_anim" if phase == "entry" else "exit_anim": effect}
            times = (299, 301, 450, 650, 1000, 1500) if phase == "entry" else (4000, 4500, 4900, 5100, 5499, 5900)
            feature = "F14" if effect in ("noise", "assemble_in", "dissolve_out") else (
                "F15" if effect in ("sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid", "flame", "feather", "lightning") else (
                "F12" if effect == "utopia" else "F11" if effect.startswith(("tracking", "wave", "scatter", "converge", "stretch", "glow")) else "F10"))
            cases.append(BaselineCase(f"{phase}_{effect}", (feature,), track, replace(base, **changes), times))
    for effect in get_args(KaraokeAnimation):
        cases.append(BaselineCase(f"karaoke_{effect}", ("F09", "F12", "F13", "F17"), track,
            replace(base, karaoke_anim=effect, karaoke_stroke_flash=True), (1200, 1500, 2300, 3400, 4499)))
    for effect in get_args(SingFx):
        cases.append(BaselineCase(f"sing_{effect}", ("F15", "F16"), track,
            replace(base, sing_fx=effect, fx_particle_count=24), (1300, 1750, 2500, 4000, 4700)))

    gif = asset_dir / "three-frame.gif"
    source = Path(__file__).resolve().parents[2] / "tests/data/guide_anim_3frames.gif"
    shutil.copyfile(source, gif)
    texture = asset_dir / "four-color.png"
    for shape, image in (("band", ""), ("flame", ""), ("bubble", ""), ("image", str(texture.resolve())), ("image", str(gif.resolve()))):
        suffix = "animated" if image.endswith(".gif") else shape
        cases.append(BaselineCase(f"scanline_{suffix}", ("F17", "F18", "F19"), track,
            replace(base, karaoke_anim="utopia_scanline", scanline_enabled=True,
                scanline_shape=shape, scanline_image_path=image,
                scanline_image_color_layers="stroke,decor"), (1300, 1500, 1580, 2600, 3700)))
    for mode in ("color", "brighten", "follow_before", "follow_after", "role"):
        cases.append(BaselineCase(f"scanline_mode_{mode}", ("F17",), track,
            replace(base, karaoke_anim="scanline", scanline_mode=mode,
                scanline_role_name="__global__"), (1500, 2600)))
    for effect in get_args(ScanlineEffect):
        cases.append(BaselineCase(f"scanline_particles_{effect}", ("F20",), track,
            replace(base, karaoke_anim="scanline", scanline_effect=effect), (1300, 1750, 2600, 4700)))
    for speed in (50, 200):
        cases.append(BaselineCase(f"particles_speed_{speed}", ("F16", "F20"), track,
            replace(base, entry_anim="petal", exit_anim="note", sing_fx="flame",
                anim_fx_speed_pct=speed, fx_particle_speed_pct=speed,
                karaoke_anim="utopia_scanline", scanline_effect="star"),
            (100, 600, 1500, 2600, 5300, 5900)))
    combined = replace(base, entry_anim="petal", exit_anim="note", sing_fx="flame",
        karaoke_anim="utopia_scanline", scanline_enabled=True, scanline_shape="image",
        scanline_image_path=str(gif.resolve()), line_band_enabled=True,
        line_band_image="preset:staff_scroll", fx_particle_count=36)
    cases.append(BaselineCase("combined_effects", ("F04", "F12", "F15", "F16", "F19", "F24"),
        track, combined, (100, 400, 1500, 2600, 3700, 5300, 5900)))
    overridden = replace(track, lines=[replace(track.lines[0], animation_override=LineAnimationOverride(
        entry_anim="wave_in", exit_anim="scatter_out", entry_duration_ms=600, exit_duration_ms=600))])
    cases.append(BaselineCase("line_override", ("F10", "F11"), overridden,
        replace(base, entry_anim="fade", exit_anim="fade"), (400, 650, 1500, 5100)))
    return cases
