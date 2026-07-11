# N3 Glow Concentration and Style Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve NicoKaraMaker3's three-level `BlurLevel` glow concentration across import, persistence, UI, and every QPainter path while retaining the sample project's disabled second-outline semantics, blue/white after colors, ruby styling, and 7px letter spacing.

**Architecture:** Add a clamped `0..2` concentration value to the main, ruby-override, scheme, and title models. Centralize N3's radius sequence and multi-pass compositing in `engine/painter.py`, then thread the effective main/ruby/title value into direct and cached glow paths. Keep existing brush/spacing import intact, repair omitted ruby scheme overrides, and add importer/model/UI/pixel/cache regressions before each production change.

**Tech Stack:** Python 3.14, dataclasses, PyQt6/QPainter/QImage, pytest, zip/JSON `.n3proj` import.

## Global Constraints

- Work only on `feat/subtitle-render`; do not modify the SUG submodule source.
- User-facing strings are Chinese.
- `UseEdge2=null` with no boolean in the N3 fallback chain remains disabled; the sample must keep main/ruby `stroke2_width_px == 0`.
- Concentration levels map directly: `0=low`, `1=medium`, `2=high`.
- The blur sequence is `DecorSize - floor(i * DecorSize / (BlurLevel + 1))`.
- Existing projects missing the fields retain current one-pass glow (`level=0`).
- Tests must be written and observed failing before production changes.

---

### Task 1: Persist clamped concentration fields

**Files:**
- Modify: `krok_helper/subtitle_render/models.py`
- Test: `tests/test_subtitle_render_painter.py`

**Interfaces:**
- Produces: `normalize_glow_concentration_level(value: object, fallback: int = 0) -> int`
- Produces: `Style.glow_concentration_level: int`
- Produces: `Style.ruby_glow_concentration_level: Optional[int]`
- Produces: matching optional fields on `SubtitleStyleScheme`
- Produces: `TitleOverlay.glow_concentration_level: int`

- [ ] **Step 1: Write failing persistence tests**

Add tests that construct a `Style` containing main level 2, ruby level 1, a custom scheme with both values, and a title level 2; assert `style_from_dict(style_to_dict(style))` retains all values. Add invalid payload assertions proving `-1` clamps to 0 and `8` clamps to 2, while a missing ruby override remains `None`.

```python
def test_style_dict_roundtrip_keeps_glow_concentration_levels():
    style = Style(
        glow_concentration_level=2,
        ruby_glow_concentration_level=1,
        custom_style_schemes={
            "B": SubtitleStyleScheme(
                glow_concentration_level=1,
                ruby_glow_concentration_level=2,
            )
        },
        title_overlay=TitleOverlay(glow_concentration_level=2),
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.glow_concentration_level == 2
    assert restored.ruby_glow_concentration_level == 1
    assert restored.custom_style_schemes["B"].ruby_glow_concentration_level == 2
    assert restored.title_overlay.glow_concentration_level == 2
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_painter.py -k "glow_concentration" -q`

Expected: FAIL because the dataclasses do not accept the new fields.

- [ ] **Step 3: Add fields and validation**

Add the four model fields and:

```python
def normalize_glow_concentration_level(value: object, fallback: int = 0) -> int:
    try:
        return max(0, min(2, int(value)))
    except (TypeError, ValueError):
        return max(0, min(2, int(fallback)))
```

Use it in `style_from_dict()`, `subtitle_style_scheme_from_dict()`, and `title_overlay_from_dict()`. Preserve `None` only for optional scheme/ruby fields. Add the title field to `title_overlay_to_dict()`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_painter.py -k "glow_concentration or style_dict_roundtrip" -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add krok_helper/subtitle_render/models.py tests/test_subtitle_render_painter.py
git commit -m "feat: persist N3 glow concentration"
```

### Task 2: Import BlurLevel without changing outline semantics

**Files:**
- Modify: `krok_helper/subtitle_render/n3proj_import.py`
- Modify: `tests/test_subtitle_render_n3proj_import.py`

**Interfaces:**
- Consumes: `normalize_glow_concentration_level()`
- Produces: concentration entries from `_scheme_changes()`
- Produces: title concentration from `_build_title_overlay()`

- [ ] **Step 1: Write failing fixture and real-sample regressions**

Extend `_lyrics_font()` with `blur_level`. Assert a blur scheme maps level 2 into main and ruby values and no longer warns. Add a skipped-when-absent smoke test for `D:\カラオケ\songs\TACTIC\1.n3proj` asserting:

```python
assert style.glow_concentration_level == 1
assert style.ruby_glow_concentration_level == 1
assert style.letter_spacing_px == 7
assert style.stroke_width_px == 2
assert style.stroke2_width_px == 0
assert style.ruby_font_size_px == 45
assert style.ruby_stroke_width_px == 2
assert style.ruby_stroke2_width_px == 0
assert style.karaoke_colors.after.text.color == "#FFF1FB"
assert style.karaoke_colors.after.stroke.color == "#4EAADE"
assert style.karaoke_colors.after.stroke2.color == "#000000"
assert style.karaoke_colors.after.shadow.color == "#4EAADE"
```

- [ ] **Step 2: Run importer tests and verify RED**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_n3proj_import.py -k "blur or tactic" -q`

Expected: FAIL because concentration is missing and the unsupported warning remains.

- [ ] **Step 3: Map BlurLevel**

For `DecorKind == 2`, clamp `font["BlurLevel"]`, set main/ruby concentration, and remove the unsupported warning. In `_build_title_overlay()`, set the title value from its selected font scheme. Do not change `_resolve_use_edge2()`.

- [ ] **Step 4: Run all importer tests and verify GREEN**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_n3proj_import.py -q`

Expected: PASS, including the local TACTIC sample.

- [ ] **Step 5: Commit**

```powershell
git add krok_helper/subtitle_render/n3proj_import.py tests/test_subtitle_render_n3proj_import.py
git commit -m "fix: import N3 glow concentration"
```

### Task 3: Render N3 multi-pass glow and repair ruby role overrides

**Files:**
- Modify: `krok_helper/subtitle_render/engine/painter.py`
- Modify: `tests/test_subtitle_render_painter.py`

**Interfaces:**
- Produces: `_glow_blur_radii(radius: int, concentration_level: int) -> tuple[int, ...]`
- Produces: `_ruby_glow_concentration_level(style: Style) -> int`
- Changes: `_paint_glow_path(..., concentration_level: int = 0, ...)`

- [ ] **Step 1: Write RED tests**

Assert:

```python
assert _glow_blur_radii(13, 0) == (13,)
assert _glow_blur_radii(13, 1) == (13, 7)
assert _glow_blur_radii(13, 2) == (13, 9, 5)
```

Render the same path at levels 0/1/2 and assert hashes differ and total alpha increases. Assert cached run-glow keys differ by level. Build a role scheme with all ruby stroke/decor/radius/concentration/offset fields and assert `_style_for_role()` returns every override.

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_painter.py -k "glow_blur_radii or glow_concentration or role_ruby" -q`

Expected: FAIL because helpers/effective fields are missing.

- [ ] **Step 3: Implement exact multi-pass compositing**

```python
def _glow_blur_radii(radius: int, concentration_level: int) -> tuple[int, ...]:
    radius = max(int(radius), 1)
    passes = normalize_glow_concentration_level(concentration_level) + 1
    return tuple(radius - (index * radius // passes) for index in range(passes))
```

Build the source outline once using original radius, then blur/draw the same source for each radius. Thread main values to title, direct after glow, cached before/after glow, and animations. Thread effective ruby values to horizontal/vertical ruby paths. Include concentration in layer/cache signatures.

- [ ] **Step 4: Include all ruby role fields**

Add main concentration plus ruby stroke/decor/glow/shadow fields from the design to `_SUBTITLE_SCHEME_STYLE_FIELDS`. Make `_ruby_paint_style()` carry effective decoration and concentration.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_painter.py -k "glow or ruby or role" -q`

Expected: PASS.

- [ ] **Step 6: Run complete painter tests**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_painter.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add krok_helper/subtitle_render/engine/painter.py tests/test_subtitle_render_painter.py
git commit -m "feat: render N3 glow concentration levels"
```

### Task 4: Expose low/medium/high in the property panel

**Files:**
- Modify: `krok_helper/subtitle_render/frontend/property_panel.py`
- Modify: `tests/test_subtitle_render_property_panel.py`

**Interfaces:**
- Produces: `_glow_concentration_combo` with item data 0, 1, 2

- [ ] **Step 1: Write failing UI tests**

Assert the control is hidden outside the glow decoration target, visible for glow, emits main level 2 for the main subject, emits ruby level 1 for the ruby subject, returns ruby to inherited main behavior after “应用主文字配色”, and survives a custom-scheme roundtrip.

- [ ] **Step 2: Run tests and verify RED**

Run: `$env:QT_QPA_PLATFORM='offscreen'; C:\Python314\python.exe -m pytest tests\test_subtitle_render_property_panel.py -k "glow_concentration" -q`

Expected: FAIL because the combo does not exist.

- [ ] **Step 3: Add UI and mappings**

Add both concentration names to `_SCHEME_FIELDS`; map the main name to the ruby name in `_RUBY_COLOR_SUBJECT_FIELDS`. Add a compact “发光浓度” combo with `低/中/高`, show it only for glow decoration, synchronize via `_color_subject_value()`, include values in `_scheme_from_current()`, and clear the ruby override in `_apply_main_colors_to_ruby()`.

- [ ] **Step 4: Run property-panel tests and verify GREEN**

Run: `$env:QT_QPA_PLATFORM='offscreen'; C:\Python314\python.exe -m pytest tests\test_subtitle_render_property_panel.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add krok_helper/subtitle_render/frontend/property_panel.py tests/test_subtitle_render_property_panel.py
git commit -m "feat: add three N3 glow concentration levels"
```

### Task 5: Full verification and visual audit

**Files:**
- Modify only if factual corrections are needed: `docs/superpowers/specs/2026-07-10-n3-glow-and-style-parity-design.md`

- [ ] **Step 1: Run subtitle-render tests**

Run: `C:\Python314\python.exe -m pytest tests\test_subtitle_render_*.py -q`

Expected: PASS.

- [ ] **Step 2: Run complete tests**

Run: `C:\Python314\python.exe -m pytest tests\`

Expected: exit code 0.

- [ ] **Step 3: Run embedded Qt smoke**

Run the AGENTS.md offscreen smoke command and instantiate the subtitle-render page.

Expected: no exception.

- [ ] **Step 4: Render TACTIC reference frames**

Render 14000/15050/17000ms to temporary PNGs. Verify red/gold before colors, blue/white completed target line, denser medium than forced low, `letter_spacing_px == 7`, and disabled main/ruby second outlines.

- [ ] **Step 5: Inspect final state**

```powershell
git diff --check
git status --short
git log -6 --oneline
```

Expected: no whitespace errors and only intentional commits.

