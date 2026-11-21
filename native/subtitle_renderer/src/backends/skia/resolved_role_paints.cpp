#include "resolved_role_paints.h"
#include "image_fill_cache.h"
#include "scene_input.h"
#include "../../model/render_scene.h"
#include "../../model/text_line.h"
#include "../../model/text_style.h"
#include "../render_backend.h"
#include "../text_semantics.h"
#include "core/resolved_scene.h"
#include <algorithm>

namespace krok::subtitle::native {
void splitLatinOutlineStyles(RenderScene& scene) {
    // Match the GPU style projection: distinct script stroke widths also
    // partition role gradient domains and phase masks for glow.
    std::vector<int> roleVariants(scene.charStyles.size(), -1);
    for (std::size_t lineIndex = 0; lineIndex < scene.lines.size(); ++lineIndex) {
        int lineVariant = -1;
        const auto& lineStyle = scene.lineStyles[lineIndex];
        for (auto& cell : scene.lines[lineIndex].chars) {
            if (cell.vectorGlyph || cell.bitmapGuide || !isLatinText(cell.text)) continue;
            const int role = cell.styleIndex;
            const auto& style = role < 0 ? lineStyle : scene.charStyles[role];
            if (style.latinStrokeWidth == style.strokeWidth) continue;
            auto& variant = role < 0 ? lineVariant : roleVariants[role];
            if (variant < 0) {
                auto latin = style;
                latin.strokeWidth = latin.latinStrokeWidth;
                variant = int(scene.charStyles.size());
                scene.charStyles.push_back(std::move(latin));
            }
            cell.styleIndex = variant;
        }
    }
}

const SkRect& RolePaintBounds::forRole(int role) const {
    const auto it = roles.find(role);
    return it == roles.end() ? line : it->second.ink;
}

RolePaintBounds resolveRoleBodyPaints(::subtitle::skia::ResolvedLine& line, const TextLine& source, const TextStyle& base,
                           std::span<const TextStyle> styles, SkiaImageFillCache& images, float scale) {
    const auto appearance = [&](int index) -> const TextStyle& { return index < 0 ? base : styles[index]; };
    RolePaintBounds bounds;
    for (std::size_t index = 0; index < line.cells.size(); ++index) {
        if (line.cells[index].path.isEmpty()) continue;
        const int role = source.chars[index].styleIndex;
        const auto& look = appearance(role);
        for (const auto* paint : {&look.beforeStrokePaint, &look.afterStrokePaint,
                                  &look.beforeStroke2Paint, &look.afterStroke2Paint})
            if (paint->mode != "solid") throw BackendError("Skia 角色正文画笔暂不支持复杂描边");
        const auto ink = line.cells[index].path.computeTightBounds();
        bounds.roles[role].ink.join(ink);
        bounds.line.join(ink);
        bounds.outlinePad = std::max(bounds.outlinePad,
            ::subtitle::skia::outlineVisualPad(look.strokeWidth, look.stroke2Width, scale));
    }
    for (auto& [role, domain] : bounds.roles) {
        const auto& look = appearance(role);
        if (look.beforeFillPaint.mode == "solid" && look.afterFillPaint.mode == "solid") continue;
        domain.paintIndex = int(line.roleBodyPaints.size());
        ::subtitle::skia::BodyPaint prepared;
        const PaintStyle* states[]{&look.beforeFillPaint, &look.afterFillPaint};
        for (int phase = 0; phase < 2; ++phase) {
            const auto& paint = *states[phase];
            if (paint.mode == "image") {
                prepared.images[phase] = images.fill(paint, scale);
            } else if (paint.mode != "solid") {
                // Horizontal gradients span the role's glyphs. Vertical
                // gradients and discrete bands span the entire line and pen.
                const auto& ink = paint.mode == "gradient_horizontal" ? domain.ink : bounds.line;
                prepared.gradients[phase] = ::subtitle::skia::lineGradient(paint, ink, bounds.outlinePad);
            }
        }
        line.roleBodyPaints.push_back(std::move(prepared));
    }
    for (std::size_t index = 0; index < line.cells.size(); ++index)
        if (!line.cells[index].path.isEmpty())
            line.cells[index].bodyPaintIndex = bounds.roles.at(source.chars[index].styleIndex).paintIndex;
    return bounds;
}

void resolveRoleGlows(::subtitle::skia::ResolvedLine& line, const TextLine& source, const TextStyle& base,
                      std::span<const TextStyle> styles, const RolePaintBounds* gradientBounds) {
    std::map<int, int> groups;
    // The existing GPU composites phase layers in first-use role order.
    for (std::size_t index = 0; index < line.cells.size(); ++index) {
        const int role = source.chars[index].styleIndex;
        auto [it, inserted] = groups.try_emplace(role, -1);
        if (inserted) {
            const auto& style = role < 0 ? base : styles[role];
            const auto glow = skiaStaticGlow(style);
            if (glow.before.radius > 0 || glow.after.radius > 0) {
                ::subtitle::skia::GlowGradients gradients;
                if (gradientBounds && !gradientBounds->line.isEmpty()) {
                    const auto shader = [&](const PaintStyle& paint) {
                        const auto& ink = paint.mode == "gradient_horizontal" ? gradientBounds->forRole(role) : gradientBounds->line;
                        return ::subtitle::skia::lineGradient(paint, ink, gradientBounds->outlinePad);
                    };
                    if (glow.before.radius > 0) gradients[0] = shader(style.beforeDecorPaint);
                    if (glow.after.radius > 0) gradients[1] = shader(style.afterDecorPaint);
                }
                it->second = int(line.roleGlows.size());
                line.roleGlows.push_back({glow, {}, std::move(gradients)});
            }
        }
        if (it->second >= 0 && !line.cells[index].path.isEmpty())
            line.roleGlows[it->second].cells.push_back(index);
    }
}
}
