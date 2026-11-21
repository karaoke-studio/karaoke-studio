"""One-command Skia hardware check on a real Mac, using a bundle built by CI.

Usage (from the repository root, with a logged-in desktop session):
    python3 scripts/mac_hardware_check.py <mac-validation-ARCH.zip from GitHub Actions>

Nothing is compiled here: the bundle carries the Skia smoke tools, backend tests and the Qt Quick
preview module built by the pinned CI toolchain. This script only checks the machine's real
Metal GPU and writes build/mac-hardware-check-results.zip to send back. It installs PyQt6 into
build/mac-hardware-check/venv and leaves the rest of the system untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / "build/mac-hardware-check"
FONT = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
PYQT = ["PyQt6==6.11.0", "PyQt6-Qt6==6.11.0", "pytest", "numpy", "fonttools", "charset-normalizer", "PyQt6-Fluent-Widgets", "imageio-ffmpeg==0.6.0"]
# Only these system_profiler display fields are kept: no serial numbers or hardware UUIDs.
GPU_FIELDS = ("sppci_model", "sppci_cores", "spdisplays_mtlgpufamilysupport", "spdisplays_vendor",
              "sppci_device_type", "spdisplays_metal")


RENDER_FEATURE_CASES = (
    "tests/test_subtitle_skia_vertical.py::test_vertical_gpu_raster_and_out_of_order_seek",
    "tests/test_subtitle_skia_vertical.py::test_vertical_ruby_gpu_raster_and_out_of_order_seek",
    "tests/test_subtitle_skia_vertical_ruby.py::test_vertical_ruby_units_split_one_span_and_keep_the_pause",
    "tests/test_subtitle_skia_vertical_ruby.py::test_vertical_singing_scale_keeps_column_and_reading_wipe",
    "tests/test_subtitle_skia_vertical_ruby.py::test_vertical_viewport_rotates_body_and_reading_wipes",
    "tests/test_subtitle_skia_vertical_ruby.py::test_vertical_reverse_line_preserves_other_readings",
    "tests/test_subtitle_skia_vector_guides.py::test_vector_guide_gpu_raster_and_out_of_order_seek",
    "tests/test_subtitle_skia_secondary_outline.py::test_outer_ring_preserves_inner_alpha",
    "tests/test_subtitle_skia_line_fade.py::test_fade_g5_preview",
    "tests/test_subtitle_skia_line_fade.py::test_fade_raster_export",
    "tests/test_subtitle_skia_line_motion.py::test_motion_relative_to_static_line",
    "tests/test_subtitle_skia_character_fade.py::test_character_fade_matches_fixed_stagger",
    "tests/test_subtitle_skia_character_transform.py::test_transform_wipe_keeps_same_clock_and_supports_out_of_order_seek",
    "tests/test_subtitle_skia_character_transform.py::test_transformed_outline_keeps_width_across_targets_and_seeks",
    "tests/test_subtitle_skia_scanline.py::test_gpu_and_raster_draw_the_same_band_under_out_of_order_seek",
    "tests/test_subtitle_skia_scanline.py::test_feather_gpu_and_raster_match_under_repeated_seek",
    "tests/test_subtitle_skia_scanline.py::test_plain_scanline_uses_the_authored_color_sides",
    "tests/test_subtitle_skia_sing_particles.py::test_singing_twinkle_agrees_between_gpu_and_raster",
    "tests/test_subtitle_skia_sing_particles.py::test_singing_star_speed_preserves_birth_and_respects_display_window",
    "tests/test_subtitle_skia_sing_particles.py::test_singing_symbols_keep_sprite_resources_and_replay_tail",
    "tests/test_subtitle_skia_sing_particles.py::test_singing_ripple_uses_back_ring_layers_and_replays",
    "tests/test_subtitle_skia_sing_particles.py::test_singing_falling_spritemap_replays_each_shape",
    "tests/test_subtitle_skia_sing_particles.py::test_singing_energy_layers_replay_in_burst_order",
    "tests/test_subtitle_skia_scanline.py::test_scanline_particles_preserve_emit_clock_and_extend_tail",
    "tests/test_subtitle_skia_scanline.py::test_reverse_scanline_band_and_particle_anchors_follow_singing_order",
    "tests/test_subtitle_skia_character_transform.py::test_geometry_duration_preserves_canonical_pivots_and_replays",
    "tests/test_subtitle_skia_character_transform.py::test_character_halo_replays_behind_geometry",
    "tests/test_subtitle_skia_stroke_flash.py::test_flash_follows_each_cell_clock_and_replays",
    "tests/test_subtitle_skia_stroke_flash.py::test_composed_flash_preserves_ruby_clocks_and_replays",
    "tests/test_subtitle_skia_scanline.py::test_combined_particle_layers_keep_both_clocks",
    "tests/test_subtitle_skia_particle_transitions.py::test_falling_transitions_keep_prelude_and_tail_outside_text",
    "tests/test_subtitle_skia_particle_transitions.py::test_particle_transitions_keep_symbol_windows_and_replay",
    "tests/test_subtitle_skia_particle_transitions.py::test_energy_transitions_keep_companion_layers_and_outside_tail",
    "tests/test_subtitle_skia_noise.py::test_noise_transitions_preserve_final_text_and_are_seekable",
    "tests/test_subtitle_skia_gradients.py::test_gradient_uses_one_domain_and_replays",
    "tests/test_subtitle_skia_gradients.py::test_outline_gradients_declare_capability_and_replay",
    "tests/test_subtitle_skia_image_fills.py::test_image_fills_declare_capability_seek_and_reconfigure",
    "tests/test_subtitle_skia_outline_ruby.py::test_outlined_upper_ruby_declares_capability_and_replays",
    "tests/test_subtitle_skia_outline_ruby.py::test_multi_lane_outline_keeps_clocks_and_seeks",
    "tests/test_subtitle_skia_outline_ruby.py::test_annotated_solid_outlines_preserve_final_wipe_and_replay",
    "tests/test_subtitle_skia_outline_ruby.py::test_outlined_lower_ruby_replays_all_three_bands",
    "tests/test_subtitle_skia_line_motion.py::test_line_animation_keeps_ruby_layout_and_replays",
    "tests/test_subtitle_skia_line_motion.py::test_glowing_ruby_fade_preserves_first_nonzero_alpha_on_both_targets",
    "tests/test_subtitle_skia_static_shadow.py::test_shadow_palette_keeps_geometry_and_replays",
    "tests/test_subtitle_skia_static_glow.py::test_static_glow_palette_preserves_geometry_and_replay",
    "tests/test_subtitle_skia_singing_scale.py::test_singing_scale_preserves_clocks_and_replays",
    "tests/test_subtitle_skia_singing_scale.py::test_utopia_transition_releases_following_glyph_and_replays",
    "tests/test_subtitle_skia_outline_ruby.py::test_ruby_utopia_singing_preserves_units_and_replay",
    "tests/test_subtitle_skia_static_shadow.py::test_ruby_shadow_keeps_clocks_and_seeks",
    "tests/test_subtitle_skia_static_glow.py::test_ruby_glow_keeps_clocks_and_seeks",
    "tests/test_subtitle_skia_scanline.py::test_upper_ruby_band_keeps_own_clock_and_seek",
    "tests/test_subtitle_skia_scanline.py::test_glow_band_capability_and_out_of_order_seek",
    "tests/test_subtitle_skia_sing_particles.py::test_upper_ruby_particles_keep_clocks_and_replay",
)


def run(command, log: Path, env=None, timeout=900) -> int:
    with log.open("w", encoding="utf-8") as out:
        out.write("$ " + " ".join(str(c) for c in command) + "\n")
        out.flush()
        try:
            return subprocess.run([str(c) for c in command], stdout=out, stderr=subprocess.STDOUT,
                                  env=env, timeout=timeout, cwd=REPO).returncode
        except subprocess.TimeoutExpired:
            out.write(f"\nTIMEOUT after {timeout}s\n")
            return 124


def output(command) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"unavailable: {error}"


def unpack(archive: Path, destination: Path) -> Path:
    """Accept the GitHub artifact zip (which wraps the bundle) or the inner .tar.gz itself."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as outer:
            inner = [n for n in outer.namelist() if n.endswith(".tar.gz")]
            if len(inner) != 1:
                raise SystemExit(f"Expected one .tar.gz inside {archive.name}, found {inner}")
            archive = destination / Path(inner[0]).name
            archive.write_bytes(outer.read(inner[0]))
    with tarfile.open(archive) as bundle:
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        bundle.extractall(destination / "bundle", **kwargs)
    # Files from a browser download may carry Gatekeeper's quarantine flag.
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(destination / "bundle")],
                       capture_output=True)
    except OSError:
        pass  # not macOS (unit tests)
    return destination / "bundle"


def gpu_summary(displays_json: str) -> list[dict]:
    try:
        return [{k: gpu[k] for k in GPU_FIELDS if k in gpu}
                for gpu in json.loads(displays_json).get("SPDisplaysDataType", [])]
    except (json.JSONDecodeError, AttributeError):
        return [{"error": "system_profiler output unreadable"}]


def verify(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"Bundle file corrupted or modified: {name}")
    return manifest


def environment() -> dict:
    gpus = gpu_summary(output(["system_profiler", "-json", "SPDisplaysDataType"]))
    return {"macos": output(["sw_vers", "-productVersion"]), "build": output(["sw_vers", "-buildVersion"]),
            "machine": platform.machine(), "cpu": output(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "python": sys.version.split()[0], "gpus": gpus,
            "repo_commit": output(["git", "-C", str(REPO), "rev-parse", "HEAD"])}


def archive_results(archive: Path, summary: Path, logs: Path, *, compact: bool) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        out.write(summary, "summary.json")
        candidates = logs.iterdir() if compact else logs.rglob("*")
        for path in sorted(candidates):
            if path.is_file() and (not compact or path.suffix in {".json", ".log"}):
                out.write(path, f"logs/{path.relative_to(logs).as_posix()}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path, help="mac-validation-ARCH.zip (or the .tar.gz inside it)")
    parser.add_argument("--allow-virtual-gpu", action="store_true",
                        help="CI only: a virtual Metal device is not a failure (hardware gate stays open)")
    parser.add_argument("--compact-results", action="store_true",
                        help="Save only logs and reports when the check passes")
    options = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("Run this on the Mac being checked")
    if not (REPO / "krok_helper/lyrics_timing/src").is_dir():
        parser.error("Submodule missing: run `git submodule update --init --recursive` first")
    WORK.mkdir(parents=True, exist_ok=True)
    logs = WORK / "logs"
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir()
    bundle = unpack(options.bundle.expanduser().resolve(), WORK / "unpacked")
    manifest = verify(bundle)
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    if manifest["arch"] != arch:
        parser.error(f"This Mac is {arch}; download mac-validation-{arch} instead of {manifest['arch']}")
    env_info = environment()
    if env_info["repo_commit"] != manifest["commit"]:
        print(f"WARNING: repository is at {env_info['repo_commit'][:10]}, bundle was built from "
              f"{manifest['commit'][:10]}; run `git checkout {manifest['commit']}` for matching tests.")

    venv = WORK / "venv"
    python = venv / "bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    if run([python, "-m", "pip", "install", "--quiet", *PYQT], logs / "pip.log") != 0:
        raise SystemExit("pip install failed; see build/mac-hardware-check/logs/pip.log")

    native = bundle / "native"
    gpu = native / f"macos-{arch}-metal"
    cpu = native / f"macos-{arch}-raster"
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"} | {
        "KROK_SKIA_NATIVE_TEST_ROOT": str(native), "KROK_SKIA_NATIVE_TEST_ARCH": arch,
        "KROK_SKIA_NATIVE_TEST_FONT": str(FONT), "PYTHONPATH": str(REPO),
        # Adds the preview test under Apple's Metal API validation, with its own positive control.
        "KROK_METAL_VALIDATION": "1",
        "KROK_TEST_SKIA_SIDECAR": str(gpu / "krok_skia_subtitle_renderer"),
        "KROK_SUBTITLE_SKIA_REQUIRE_HARDWARE": "0" if options.allow_virtual_gpu else "1"}
    steps = {
        "metal_hardware_smoke": [gpu / "krok_skia_gpu_smoke", FONT, logs / "metal", "--require-hardware", "--frames", "120"],
        "raster_smoke": [cpu / "krok_skia_raster_smoke", FONT, logs / "raster", "--frames", "120"],
        "owned_metal_backend": [gpu / "subtitle_skia_backend_tests", "--gpu", "--allow-software"],
        "quick_preview": [python, "-m", "pytest", "--noconftest", "-rs", "-v", "tests/test_subtitle_skia_preview.py",
                          "tests/test_subtitle_skia_preview_lifecycle.py", "--basetemp", logs / "preview-tests"],
        "native_capabilities": [python, "-m", "pytest", "--noconftest", "-rs", "-v", "tests/test_subtitle_skia_native.py"],
    }
    if (gpu / "krok_skia_subtitle_renderer").is_file():
        steps["subtitle_protocol"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                      "tests/test_subtitle_skia_protocol.py", "tests/test_subtitle_skia_export_formats.py",
                                      "--basetemp", logs / "protocol-tests"]
    if manifest.get("text_metrics_schema") == 1:
        steps["text_metrics"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                 "tests/test_subtitle_skia_text.py", "--basetemp", logs / "text-tests"]
    if manifest.get("resolved_layout_schema", 0) >= 1:
        steps["resolved_layout"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                    "tests/test_subtitle_skia_resolved.py", "--basetemp", logs / "resolved-tests"]
    if manifest.get("resolved_layout_schema", 0) >= 2:
        steps["font_slots"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                              "tests/test_subtitle_skia_font_slots.py", "--basetemp", logs / "font-slot-tests"]
    if manifest.get("single_line_placement", 0) >= 1:
        steps["single_line_placement"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                         "tests/test_subtitle_skia_placement.py", "tests/test_subtitle_skia_outline.py",
                                         "--basetemp", logs / "placement-tests"]
    if manifest.get("inline_roles", 0) >= 1:
        steps["inline_roles"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                 "tests/test_subtitle_skia_roles.py", "--basetemp", logs / "role-tests"]
    if manifest.get("fixed_row_grid", 0) >= 1:
        steps["fixed_row_grid"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                   "tests/test_subtitle_skia_grid.py", "--basetemp", logs / "grid-tests"]
    if manifest.get("page_collisions", 0) >= 1:
        steps["page_collisions"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                    "tests/test_subtitle_skia_collision.py", "--basetemp", logs / "collision-tests"]
    if manifest.get("smart_horizontal", 0) >= 1:
        steps["smart_horizontal"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                     "tests/test_subtitle_skia_smart.py", "--basetemp", logs / "smart-tests"]
    if manifest.get("ruby_metrics", 0) >= 1:
        steps["ruby_metrics"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                 "tests/test_subtitle_skia_ruby_metrics.py", "--basetemp", logs / "ruby-metrics-tests"]
    if manifest.get("ruby_geometry", 0) >= 1:
        steps["ruby_geometry"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                  "tests/test_subtitle_skia_ruby_geometry.py", "--basetemp", logs / "ruby-geometry-tests"]
    if manifest.get("font_resource_contracts", 0) >= 1:
        steps["font_resource_contracts"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
            "tests/test_subtitle_render_font_resolution.py", "-k",
            "ir_font_slots or ruby_latin_resource_hint or partial_ruby_role"]
    if manifest.get("segmented_wipe", 0) >= 1:
        steps["segmented_wipe"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                   "tests/test_subtitle_skia_wipe.py", "--basetemp", logs / "segmented-wipe-tests"]
    if manifest.get("ruby_pages", 0) >= 1:
        steps["ruby_pages"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                               "tests/test_subtitle_skia_ruby_pages.py", "--basetemp", logs / "ruby-page-tests"]
    if manifest.get("ruby_groups", 0) >= 1:
        steps["ruby_groups"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                "tests/test_subtitle_skia_ruby_groups.py", "--basetemp", logs / "ruby-group-tests"]
    if manifest.get("upper_ruby_product", 0) >= 1:
        steps["upper_ruby_product"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                       "tests/test_subtitle_skia_upper_product.py", "--basetemp", logs / "upper-ruby-tests"]
    if manifest.get("lower_ruby_preparation", 0) >= 1:
        steps["lower_ruby_preparation"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                          "tests/test_subtitle_skia_lower.py", "--basetemp", logs / "lower-ruby-tests"]
    if manifest.get("lower_ruby_geometry", 0) >= 1:
        steps["lower_ruby_geometry"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                       "tests/test_subtitle_skia_lower_geometry.py", "--basetemp", logs / "lower-geometry-tests"]
    if manifest.get("lower_ruby_product", 0) >= 1:
        steps["lower_ruby_product"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                      "tests/test_subtitle_skia_lower_product.py", "--basetemp", logs / "lower-product-tests"]
    if manifest.get("ruby_markers", 0) >= 1:
        steps["ruby_markers"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                "tests/test_subtitle_skia_ruby_markers.py", "--basetemp", logs / "ruby-marker-tests"]
    if manifest.get("ruby_units", 0) >= 1:
        steps["ruby_units"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                              "tests/test_subtitle_skia_ruby_units.py", "--basetemp", logs / "ruby-unit-tests"]
    if manifest.get("owned_posix_rings", 0) >= 1:
        steps["owned_posix_rings"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                     "tests/test_subtitle_skia_ipc.py", "--basetemp", logs / "posix-ring-tests"]
    if manifest.get("font_fallback", 0) >= 1:
        steps["font_fallback"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                  "tests/test_subtitle_skia_fallback.py", "--basetemp", logs / "font-fallback-tests"]
    if manifest.get("mixed_fonts", 0) >= 1:
        steps["mixed_fonts"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                "tests/test_subtitle_skia_mixed_fonts.py", "--basetemp", logs / "mixed-font-tests"]
    if manifest.get("standalone_marks", 0) >= 1:
        steps["standalone_marks"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                     "tests/test_subtitle_skia_standalone_marks.py", "--basetemp", logs / "standalone-mark-tests"]
    if manifest.get("no_wipe", 0) >= 1:
        steps["no_wipe"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                            "tests/test_subtitle_skia_no_wipe.py", "--basetemp", logs / "no-wipe-tests"]
    if manifest.get("reverse_wipe", 0) >= 1:
        steps["reverse_wipe"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                 "tests/test_subtitle_skia_reverse.py", "--basetemp", logs / "reverse-wipe-tests"]
    if manifest.get("reverse_ruby", 0) >= 1:
        steps["reverse_ruby"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                 "tests/test_subtitle_skia_reverse_ruby.py", "--basetemp", logs / "reverse-ruby-tests"]
    if manifest.get("forward_endpoints", 0) >= 1:
        steps["forward_endpoints"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                      "tests/test_subtitle_skia_forward_endpoints.py", "--basetemp", logs / "forward-endpoint-tests"]
    if manifest.get("font_registry", 0) >= 1:
        steps["font_registry"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                  "tests/test_subtitle_skia_font_registry.py", "--basetemp", logs / "font-registry-tests"]
    if manifest.get("zero_ruby_no_wipe", 0) >= 1:
        steps["zero_ruby_no_wipe"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                       "tests/test_subtitle_skia_zero_ruby.py", "--basetemp", logs / "zero-ruby-tests"]
    if manifest.get("horizontal_rtl", 0) >= 1:
        steps["horizontal_rtl"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                    "tests/test_subtitle_skia_rtl_flow.py", "--basetemp", logs / "rtl-flow-tests"]
    if manifest.get("cell_glyphs", 0) >= 1:
        steps["cell_glyphs"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                 "tests/test_subtitle_skia_cell_glyphs.py", "--basetemp", logs / "cell-glyph-tests"]
    if manifest.get("kana_marks", 0) >= 1:
        steps["kana_marks"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                               "tests/test_subtitle_skia_kana_marks.py", "--basetemp", logs / "kana-mark-tests"]
    if manifest.get("render_feature_suite", 0) >= 1:
        steps["render_feature_suite"] = [python, "-m", "pytest", "--noconftest", "-rs", "-v",
                                        *RENDER_FEATURE_CASES, "--basetemp", logs / "render-feature-tests"]
    results = {name: run(command, logs / f"{name}.log", env) for name, command in steps.items()}

    metal_report = logs / "metal.json"
    hardware = json.loads(metal_report.read_text()).get("hardware") if metal_report.exists() else None
    hardware_steps = ("metal_hardware_smoke", "native_capabilities")
    functional_ok = all(code == 0 for name, code in results.items() if name not in hardware_steps)
    hardware_ok = hardware is True and all(results[name] == 0 for name in hardware_steps)
    summary = {"bundle": manifest, "environment": env_info, "exit_codes": results,
               "metal_hardware": hardware, "functional_passed": functional_ok, "hardware_gate_passed": hardware_ok}
    summary_path = WORK / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    archive = REPO / "build/mac-hardware-check-results.zip"
    passed = functional_ok and (hardware_ok or options.allow_virtual_gpu)
    archive_results(archive, summary_path, logs, compact=options.compact_results and passed)
    print(json.dumps({k: summary[k] for k in ("exit_codes", "metal_hardware", "functional_passed",
                                               "hardware_gate_passed")}, indent=2))
    print(f"\nSend back: {archive}")
    if not functional_ok:
        return 1
    return 0 if hardware_ok or options.allow_virtual_gpu else 2


if __name__ == "__main__":
    sys.exit(main())
