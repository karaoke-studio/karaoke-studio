#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace krok::subtitle::native {

struct VectorPathCommand {
    char kind = 'M';
    std::vector<float> values;
    bool operator==(const VectorPathCommand &) const = default;
};

struct VectorGlyph {
    std::vector<VectorPathCommand> commands;
    float unitsPerEm = 1000.0f;
    float advanceWidth = 1000.0f;
    // Parsed content fingerprint used only for backend resource identity.  It
    // is deliberately excluded from render equality: commands and metrics are
    // the rendering semantics, while the fingerprint is their derived key.
    std::string resourceKey;
    bool operator==(const VectorGlyph &other) const {
        return commands == other.commands
            && unitsPerEm == other.unitsPerEm
            && advanceWidth == other.advanceWidth;
    }
};

struct BitmapGuide {
    std::wstring beforePath;
    std::wstring afterPath;
    float zoomPercent = 100.0f;
    bool fixSize = false;
    bool noDecor = false;
    bool forceWipeDecor = false;
    float marginLeft = 0.0f;
    float marginRight = 0.0f;
    float marginBottom = 0.0f;
    std::uint64_t beforeModifiedMs = 0;
    std::uint64_t beforeSize = 0;
    std::uint64_t afterModifiedMs = 0;
    std::uint64_t afterSize = 0;
    // 动图（GIF）循环锚点：行显示窗口起点（Python 侧单一事实源写入 IR，
    // 投影时补 sourceTimingOffset）。渲染帧时间减锚点后按累积延时表选帧。
    int animAnchorMs = 0;
    bool operator==(const BitmapGuide &) const = default;
};
struct FontInstanceHint {
    std::wstring resourcePath;
    float scale = 1.0f;
    bool present = false;
    std::wstring style;
    int faceWeight = 0;
    bool syntheticBold = false;
    // Family has no real italic face: the requested italic is drawn by the
    // DirectWrite oblique simulation, mirroring QFont's synthetic oblique.
    bool syntheticItalic = false;
    // True variable-font decision: instantiate the wght axis at ``axis``.
    bool variable = false;
    float axis = 0.0f;
    // Real italic axis (``ital`` / ``slnt``) when the family expresses italic
    // through an axis instead of an italic face: applied together with wght on
    // the same axis instance, with no oblique simulation on top.
    std::wstring italicAxisTag;
    float italicAxisValue = 0.0f;
    // Full axis snapshot (every axis of the face at its value, wght included
    // when variable).  Qt and DirectWrite can pick *different* default
    // instances when an axis is unspecified (measured: Segoe UI Variable ->
    // Qt 'Small' vs DWrite fvar default), so pinning the whole set is what
    // keeps both backends on one instance.
    std::vector<std::pair<std::wstring, float>> axes;
    bool operator==(const FontInstanceHint &) const = default;
};

}  // namespace krok::subtitle::native
