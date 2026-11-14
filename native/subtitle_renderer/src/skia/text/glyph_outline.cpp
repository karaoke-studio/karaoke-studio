#include "text/glyph_outline.h"
#include "text/font_instance.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPathBuilder.h"
#include <ft2build.h>
#include FT_FREETYPE_H
#include FT_OUTLINE_H
#include FT_MULTIPLE_MASTERS_H
#include FT_COLOR_H
#include <cmath>
#include <mutex>
#include <stdexcept>

namespace subtitle::skia::text {
namespace {
void check(FT_Error error) { if (error) throw std::runtime_error("Skia 字体设计轮廓读取失败"); }
struct Builder {
    SkPathBuilder path;
    bool contour = false;
    static SkPoint point(const FT_Vector* p) { return {float(p->x) / 64, -float(p->y) / 64}; }
    static int move(const FT_Vector* to, void* ctx) {
        auto& b = *static_cast<Builder*>(ctx);
        if (b.contour) b.path.close();
        b.path.moveTo(point(to)); b.contour = true; return 0;
    }
    static int line(const FT_Vector* to, void* ctx) {
        static_cast<Builder*>(ctx)->path.lineTo(point(to)); return 0;
    }
    static int conic(const FT_Vector* control, const FT_Vector* to, void* ctx) {
        static_cast<Builder*>(ctx)->path.quadTo(point(control), point(to)); return 0;
    }
    static int cubic(const FT_Vector* a, const FT_Vector* b, const FT_Vector* to, void* ctx) {
        static_cast<Builder*>(ctx)->path.cubicTo(point(a), point(b), point(to)); return 0;
    }
};
}
struct GlyphOutlineSource::Impl {
    std::shared_ptr<const FontResource> resource;
    FT_Library library = nullptr;
    FT_Face face = nullptr;
    float size;
    bool bold, italic;
    std::mutex mutex;
    ~Impl() { if (face) FT_Done_Face(face); if (library) FT_Done_FreeType(library); }
};
GlyphOutlineSource::GlyphOutlineSource(const FontSpec& spec) : impl_(std::make_unique<Impl>()) {
    auto& out = *impl_;
    out.resource = spec.resource; out.size = spec.size;
    out.bold = spec.syntheticBold; out.italic = spec.syntheticItalic;
    check(FT_Init_FreeType(&out.library));
    check(FT_New_Memory_Face(out.library, out.resource->bytes.data(), FT_Long(out.resource->bytes.size()),
                             out.resource->faceIndex, &out.face));
    if (FT_HAS_MULTIPLE_MASTERS(out.face)) {
        FT_MM_Var* variation = nullptr;
        check(FT_Get_MM_Var(out.face, &variation));
        std::vector<FT_Fixed> coordinates;
        for (FT_UInt i = 0; i < variation->num_axis; ++i) {
            auto value = variation->axis[i].def;
            for (const auto& requested : out.resource->axes)
                if (requested.tag == variation->axis[i].tag) value = FT_Fixed(std::llround(requested.value * 65536.0));
            coordinates.push_back(value);
        }
        FT_Done_MM_Var(out.library, variation);
        check(FT_Set_Var_Design_Coordinates(out.face, FT_UInt(coordinates.size()), coordinates.data()));
    }
    if (FT_IS_SCALABLE(out.face)) {
        if (!out.face->units_per_EM) throw std::runtime_error("Skia 字体 em 单位无效");
        // NO_SCALE rounds variable outlines to whole font units. The public FT
        // contract recommends NO_HINTING at units_per_EM ppem to retain 26.6.
        const auto size = FT_F26Dot6(out.face->units_per_EM) * 64;
        check(FT_Set_Char_Size(out.face, size, size, 72, 72));
    }
}
GlyphOutlineSource::~GlyphOutlineSource() = default;
std::optional<GlyphGeometry> GlyphOutlineSource::load(SkGlyphID glyph) const {
    auto& in = *impl_;
    std::lock_guard lock(in.mutex);
    FT_LayerIterator layers{};
    FT_UInt layerGlyph = 0, colorIndex = 0;
    FT_OpaquePaint paint{};
    if (FT_HAS_COLOR(in.face) && (FT_Get_Color_Glyph_Layer(in.face, glyph, &layerGlyph, &colorIndex, &layers)
        || FT_Get_Color_Glyph_Paint(in.face, glyph, FT_COLOR_NO_ROOT_TRANSFORM, &paint))) {
        GlyphGeometry color; color.requiresColorPaint = true; return color;
    }
    if (!FT_IS_SCALABLE(in.face)) return {};
    const auto error = FT_Load_Glyph(in.face, glyph, FT_LOAD_NO_HINTING | FT_LOAD_NO_BITMAP | FT_LOAD_NO_SVG);
    if (error && FT_HAS_COLOR(in.face)) return {};
    check(error);
    const auto slot = in.face->glyph;
    if (slot->format != FT_GLYPH_FORMAT_OUTLINE || (FT_HAS_COLOR(in.face) && slot->outline.n_points == 0)) return {};
    const auto& m = slot->metrics;
    GlyphGeometry out;
    out.advance = float(slot->linearHoriAdvance) / 65536;
    out.leftBearing = float(m.horiBearingX) / 64;
    out.rightBearing = float(m.horiAdvance - m.horiBearingX - m.width) / 64;
    if (in.bold) check(FT_Outline_Embolden(&slot->outline, FT_Pos(in.face->units_per_EM) * 64 / 24));
    Builder builder;
    const FT_Outline_Funcs callbacks{Builder::move, Builder::line, Builder::conic, Builder::cubic, 0, 0};
    check(FT_Outline_Decompose(&slot->outline, &callbacks, &builder));
    if (builder.contour) builder.path.close();
    auto path = builder.path.detach();
    const auto scale = in.size / in.face->units_per_EM;
    // In y-down coordinates Skia's synthetic italic is skewX=-0.25.
    if (in.italic) path = path.makeTransform(SkMatrix::Skew(-.25f, 0));
    out.path = path.makeTransform(SkMatrix::Scale(scale, scale));
    return out;
}
}
