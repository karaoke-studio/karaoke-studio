#include "font_shaping.h"
#include "include/core/SkTypeface.h"
#include "modules/skshaper/include/SkShaper_harfbuzz.h"
#include "third_party/externals/harfbuzz/src/hb.h"
#include "third_party/externals/harfbuzz/src/hb-ot.h"
#include <algorithm>
#include <cmath>
#include <memory>
#include <stdexcept>

namespace subtitle::skia::text {
namespace {
struct Scalar { std::uint32_t value, byteStart; };
std::vector<Scalar> scalars(const std::string& utf8, std::uint32_t begin, std::uint32_t end) {
    if (begin > end || end > utf8.size()) throw std::runtime_error("Skia 字体塑形范围无效");
    std::vector<Scalar> out;
    for (std::uint32_t i = begin; i < end;) {
        const auto first = std::uint8_t(utf8[i]);
        const unsigned count = first < 0x80 ? 0 : (first & 0xe0) == 0xc0 ? 1 : (first & 0xf0) == 0xe0 ? 2
            : (first & 0xf8) == 0xf0 ? 3 : 99;
        if (count == 99 || i + 1 + count > end) throw std::runtime_error("Skia 文本不是有效 UTF-8");
        std::uint32_t value = count ? first & ((1u << (6 - count)) - 1) : first;
        for (unsigned j = 1; j <= count; ++j) {
            const auto next = std::uint8_t(utf8[i + j]);
            if ((next & 0xc0) != 0x80) throw std::runtime_error("Skia 文本不是有效 UTF-8");
            value = (value << 6) | (next & 0x3f);
        }
        out.push_back({value, i});
        i += 1 + count;
    }
    return out;
}
double pixelScale(const FontInstance& font) {
    int scale = 0; hb_font_get_scale(font.shapingFace(), &scale, nullptr);
    if (scale <= 0) throw std::runtime_error("Skia HarfBuzz 塑形尺度无效");
    return double(font.shapingFont().getSize())/scale;
}
bool kanaVoice(std::uint32_t c) { return c == 0x3099 || c == 0x309a; }
// DirectWrite retains authored kana voicing glyphs instead of Unicode NFC.
// Override only these normalization pairs; Latin/Hangul normalization stays HB's.
hb_bool_t composeKanaSource(hb_unicode_funcs_t*, hb_codepoint_t a, hb_codepoint_t b,
    hb_codepoint_t* composed, void*) {
    return !kanaVoice(b) && hb_unicode_compose(hb_unicode_funcs_get_default(), a, b, composed);
}
hb_bool_t decomposeKanaSource(hb_unicode_funcs_t*, hb_codepoint_t ab,
    hb_codepoint_t* a, hb_codepoint_t* b, void*) {
    if (!hb_unicode_decompose(hb_unicode_funcs_get_default(), ab, a, b)) return false;
    return !kanaVoice(*b);
}
std::shared_ptr<hb_unicode_funcs_t> authoredKanaUnicode() {
    static const auto funcs = [] {
        auto value = std::shared_ptr<hb_unicode_funcs_t>(
            hb_unicode_funcs_create(hb_unicode_funcs_get_default()), hb_unicode_funcs_destroy);
        hb_unicode_funcs_set_compose_func(value.get(), composeKanaSource, nullptr, nullptr);
        hb_unicode_funcs_set_decompose_func(value.get(), decomposeKanaSource, nullptr, nullptr);
        hb_unicode_funcs_make_immutable(value.get());
        return value;
    }();
    return funcs;
}
}

bool cellUsesOpenTypeShaping(const std::string& utf8) {
    const auto values = scalars(utf8, 0, std::uint32_t(utf8.size()));
    const auto unicode = hb_unicode_funcs_get_default();
    bool joined = false;
    for (const auto& scalar : values) {
        if (scalar.value > 0xffff) return true;
        const auto category = hb_unicode_general_category(unicode, scalar.value);
        joined = joined || scalar.value == 0x200d || category == HB_UNICODE_GENERAL_CATEGORY_NON_SPACING_MARK
            || category == HB_UNICODE_GENERAL_CATEGORY_SPACING_MARK || category == HB_UNICODE_GENERAL_CATEGORY_ENCLOSING_MARK;
    }
    return values.size() > 1 && joined;
}

std::vector<SkGlyphID> nominalGlyphs(const std::string& utf8, const FontInstance& font,
    std::uint32_t begin, std::uint32_t end) {
    std::vector<SkGlyphID> out;
    for (const auto& scalar : scalars(utf8, begin, end)) {
        hb_codepoint_t glyph = 0;
        if (!hb_font_get_nominal_glyph(font.shapingFace(), scalar.value, &glyph)) glyph = 0;
        if (glyph > 65535) throw std::runtime_error("Skia 字体返回无效 nominal glyph");
        out.push_back(SkGlyphID(glyph));
    }
    return out;
}

ShapedRange shapeFontRange(const std::string& utf8, const FontInstance& font,
    std::uint32_t begin, std::uint32_t end, const std::string& language, bool rtl, bool opentype) {
    if (begin >= end || end > utf8.size()) throw std::runtime_error("Skia 字体塑形范围无效");
    ShapedRange out;
    const double pixels = pixelScale(font);
    double cursor = 0;
    if (!opentype) {
        if (rtl) throw std::runtime_error("Skia nominal 字形策略不接受 RTL 塑形方向");
        const auto values = scalars(utf8, begin, end);
        const auto glyphs = nominalGlyphs(utf8, font, begin, end);
        for (std::size_t i = 0; i < values.size(); ++i) {
            // Same pinned hmtx/HVAR advance HB would use, without any GPOS.
            out.glyphs.push_back({glyphs[i], {float(cursor), 0.f}, values[i].byteStart, 0});
            cursor += hb_font_get_glyph_h_advance(font.shapingFace(), glyphs[i])*pixels;
        }
        if (!std::isfinite(cursor)) throw std::runtime_error("Skia nominal 字形返回非有限坐标");
        out.advance = float(cursor);
        return out;
    }
    auto scripts = SkShapers::HB::ScriptRunIterator(utf8.data(), utf8.size());
    if (!scripts) throw std::runtime_error("Skia script iterator 初始化失败");
    std::uint32_t scriptStart = 0;
    while (!scripts->atEnd()) {
        scripts->consume();
        const auto scriptEnd = std::uint32_t(scripts->endOfCurrentRun());
        const auto left = std::max(begin, scriptStart), right = std::min(end, scriptEnd);
        scriptStart = scriptEnd;
        if (left >= right) continue;
        auto buffer = std::shared_ptr<hb_buffer_t>(hb_buffer_create(), hb_buffer_destroy);
        if (!hb_buffer_allocation_successful(buffer.get())) throw std::runtime_error("Skia HB buffer 分配失败");
        const auto sourceScalars = scalars(utf8, left, right);
        const bool kanaMarks = std::any_of(sourceScalars.begin(), sourceScalars.end(),
            [](const auto& c) { return kanaVoice(c.value); });
        if (kanaMarks) hb_buffer_set_unicode_funcs(buffer.get(), authoredKanaUnicode().get());
        hb_buffer_add_utf8(buffer.get(), utf8.data(), int(utf8.size()), left, int(right-left));
        hb_buffer_set_direction(buffer.get(), rtl ? HB_DIRECTION_RTL : HB_DIRECTION_LTR);
        hb_buffer_set_script(buffer.get(), hb_script_from_iso15924_tag(scripts->currentScript()));
        hb_buffer_set_language(buffer.get(), hb_language_from_string(language.c_str(), int(language.size())));
        hb_buffer_set_cluster_level(buffer.get(), HB_BUFFER_CLUSTER_LEVEL_MONOTONE_GRAPHEMES);
        // Match the previous shaper: no BOT flag that inserts dotted circles.
        hb_shape(font.shapingFace(), buffer.get(), nullptr, 0);
        if (!hb_buffer_allocation_successful(buffer.get())) throw std::runtime_error("Skia HB 塑形内存不足");
        unsigned count = 0;
        const auto info = hb_buffer_get_glyph_infos(buffer.get(), &count);
        const auto positions = hb_buffer_get_glyph_positions(buffer.get(), nullptr);
        // Without GPOS, DirectWrite leaves kana marks at nominal positions.
        // HB synthesizes offsets (and mark classes without GDEF). Restore only
        // kana marks, retaining real GDEF zero advance. Refuse unproven GSUB
        // remapping rather than guessing which output glyph came from a mark.
        const auto face = hb_font_get_face(font.shapingFace());
        const bool nominalKana = kanaMarks && !hb_ot_layout_has_positioning(face);
        const auto sourceGlyphs = nominalKana ? nominalGlyphs(utf8, font, left, right) : std::vector<SkGlyphID>{};
        if (nominalKana && (count != sourceGlyphs.size()
            || !std::equal(sourceGlyphs.begin(), sourceGlyphs.end(), info,
                [](auto glyph, const auto& item) { return glyph == item.codepoint; })))
            throw std::runtime_error("Skia 无 GPOS 假名浊音的字形替换尚未验证");
        double vertical = 0;
        for (unsigned i = 0; i < count; ++i) {
            if (info[i].codepoint > 65535 || info[i].cluster < left || info[i].cluster >= right)
                throw std::runtime_error("Skia HarfBuzz 返回无效字形或 cluster");
            const bool nominalMark = nominalKana && kanaVoice(sourceScalars[i].value);
            const double x = cursor+(nominalMark ? 0 : positions[i].x_offset)*pixels;
            const double y = vertical-(nominalMark ? 0 : positions[i].y_offset)*pixels;
            out.glyphs.push_back({SkGlyphID(info[i].codepoint), {float(x), float(y)}, info[i].cluster, 0});
            const auto advance = nominalMark
                ? (hb_ot_layout_get_glyph_class(face, info[i].codepoint) == HB_OT_LAYOUT_GLYPH_CLASS_MARK
                    ? 0 : hb_font_get_glyph_h_advance(font.shapingFace(), info[i].codepoint))
                : positions[i].x_advance;
            cursor += advance*pixels;
            vertical -= positions[i].y_advance*pixels;
        }
    }
    if (!std::isfinite(cursor) || std::any_of(out.glyphs.begin(), out.glyphs.end(), [](const auto& g) {
            return !std::isfinite(g.position.x()) || !std::isfinite(g.position.y());
        })) throw std::runtime_error("Skia HarfBuzz 返回非有限坐标");
    out.advance = float(cursor);
    return out;
}
}
