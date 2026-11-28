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
void splitLatinOutlineStyles(RenderScene& scene, bool primary, bool secondary) {
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
            const bool first = primary && style.latinStrokeWidth != style.strokeWidth;
            const bool second = secondary && style.latinStroke2Width != style.stroke2Width;
            if (!first && !second) continue;
            auto& variant = role < 0 ? lineVariant : roleVariants[role];
            if (variant < 0) {
                auto latin = style;
                if (first) latin.strokeWidth = latin.latinStrokeWidth;
                if (second) latin.stroke2Width = latin.latinStroke2Width;
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

std::optional<int> RolePaintBounds::leftmostRole() const {
    const auto first = std::ranges::min_element(roles, {}, [](const auto& item) { return item.second.ink.left(); });
    return first == roles.end() ? std::nullopt : std::optional(first->first);
}

RolePaintBounds collectRolePaintBounds(const ::subtitle::skia::ResolvedLine& line, const TextLine& source,
                                      const TextStyle& base, std::span<const TextStyle> styles, float scale,
                                      bool includeBitmaps) {
    RolePaintBounds bounds;
    for (std::size_t index = 0; index < line.cells.size(); ++index) {
        const auto& cell = line.cells[index];
        const int role = source.chars[index].styleIndex;
        if (cell.path.isEmpty()) {
            // Bitmap guides participate in the role's horizontal paint span.
            if (includeBitmaps && cell.bitmap) bounds.roles[role].ink.join(cell.bitmap->box);
            continue;
        }
        const auto& look = role < 0 ? base : styles[role];
        const auto ink = cell.path.computeTightBounds();
        bounds.roles[role].ink.join(ink);
        bounds.line.join(ink);
        bounds.outlinePad = std::max(bounds.outlinePad,
            ::subtitle::skia::outlineVisualPad(look.strokeWidth, look.stroke2Width, scale));
    }
    return bounds;
}

void resolveRoleBodyPaints(::subtitle::skia::ResolvedLine& line, const TextLine& source, const TextStyle& base,
                         std::span<const TextStyle> styles, RolePaintBounds& bounds, SkiaImageFillCache& images, float scale,
                         bool outlineGradients, bool outlinePaints) {
    for (auto& [role, domain] : bounds.roles) {
        const auto& look = role < 0 ? base : styles[role];
        const auto needsPaint = [](const PaintStyle& before, const PaintStyle& after) {
            return before.mode != "solid" || after.mode != "solid";
        };
        const bool outline = look.strokeWidth > 0 && needsPaint(look.beforeStrokePaint, look.afterStrokePaint);
        const bool outer = look.stroke2Width > 0 && needsPaint(look.beforeStroke2Paint, look.afterStroke2Paint);
        if (!needsPaint(look.beforeFillPaint, look.afterFillPaint) && !outline && !outer) continue;
        domain.paintIndex = int(line.roleBodyPaints.size());
        ::subtitle::skia::BodyPaint prepared;
        const auto prepare = [&](const PaintStyle& paint, ::subtitle::skia::ImageFill& image, bool stroke) {
            if (paint.mode == "solid") return sk_sp<SkShader>{};
            const bool verticalGradient = paint.mode == "gradient_vertical" || paint.mode == "split_vertical";
            if (paint.mode == "image") {
                image = images.fill(paint, scale);
                return sk_sp<SkShader>{};
            }
            const auto& ink = paint.mode == "gradient_horizontal" ? domain.ink : bounds.line;
            return ::subtitle::skia::lineGradient(paint, ink, bounds.outlinePad);
        };
        prepared.gradients = {prepare(look.beforeFillPaint, prepared.images.body[0], false),
                              prepare(look.afterFillPaint, prepared.images.body[1], false)};
        if (outline)
            prepared.outlineGradients = {prepare(look.beforeStrokePaint, prepared.images.outline[0], true),
                                         prepare(look.afterStrokePaint, prepared.images.outline[1], true)};
        if (outer)
            prepared.outerGradients = {prepare(look.beforeStroke2Paint, prepared.images.outer[0], true),
                                       prepare(look.afterStroke2Paint, prepared.images.outer[1], true)};
        line.roleBodyPaints.push_back(std::move(prepared));
    }
    for (std::size_t index = 0; index < line.cells.size(); ++index)
        if (!line.cells[index].path.isEmpty())
            line.cells[index].bodyPaintIndex = bounds.roles.at(source.chars[index].styleIndex).paintIndex;
}

bool usesImageGlow(const TextStyle& style) {
    const auto glow = skiaStaticGlow(style);
    return (glow.before.radius > 0 && style.beforeGlowPaint.mode == "image")
        || (glow.after.radius > 0 && style.afterGlowPaint.mode == "image");
}

void resolveRoleGlows(::subtitle::skia::ResolvedLine& line, const TextLine& source, const TextStyle& base,
                      std::span<const TextStyle> styles, const RolePaintBounds* gradientBounds, bool bitmapGradients,
                      SkiaImageFillCache& images, float layoutScale) {
    auto horizontalBitmapSpan = SkRect::MakeEmpty();
    if (bitmapGradients)
        for (const auto& cell : line.cells)
            if (cell.bitmap) horizontalBitmapSpan.join(cell.bitmap->box);
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
                const bool hasTextBounds = gradientBounds && !gradientBounds->line.isEmpty();
                const bool hasBitmapFallback = !hasTextBounds && bitmapGradients && !horizontalBitmapSpan.isEmpty();
                if (hasTextBounds || hasBitmapFallback) {
                    const auto shader = [&](const PaintStyle& paint) {
                        auto ink = hasTextBounds
                            ? (paint.mode == "gradient_horizontal" ? gradientBounds->forRole(role) : gradientBounds->line)
                            : horizontalBitmapSpan;
                        if (hasTextBounds && role < 0 && paint.mode == "gradient_horizontal" && !horizontalBitmapSpan.isEmpty()) {
                            ink.fLeft = std::min(ink.fLeft, horizontalBitmapSpan.left());
                            ink.fRight = std::max(ink.fRight, horizontalBitmapSpan.right());
                        }
                        return ::subtitle::skia::lineGradient(paint, ink, hasTextBounds ? gradientBounds->outlinePad : 0.f);
                    };
                    if (glow.before.radius > 0) gradients[0] = shader(style.beforeGlowPaint);
                    if (glow.after.radius > 0) gradients[1] = shader(style.afterGlowPaint);
                }
                ::subtitle::skia::GlowImages glowImages;
                if (glow.before.radius > 0 && style.beforeGlowPaint.mode == "image")
                    glowImages[0] = images.fill(style.beforeGlowPaint, layoutScale);
                if (glow.after.radius > 0 && style.afterGlowPaint.mode == "image")
                    glowImages[1] = images.fill(style.afterGlowPaint, layoutScale);
                it->second = int(line.roleGlows.size());
                line.roleGlows.push_back({glow, {}, std::move(gradients), std::move(glowImages)});
            }
        }
        if (it->second < 0) continue;
        auto& group = line.roleGlows[it->second];
        auto& cell = line.cells[index];
        if (!cell.path.isEmpty()) group.cells.push_back(index);
        if (bitmapGradients && cell.bitmap && cell.bitmap->decorate)
            cell.bitmap->glowGradients = group.gradients;
    }
}

void resolveRoleShadows(::subtitle::skia::ResolvedLine& line, const TextLine& source, std::span<const TextStyle> styles,
                       const RolePaintBounds* bounds, SkiaImageFillCache& images, float layoutScale) {
    std::map<int, ::subtitle::skia::StaticShadow> roles;
    line.cellShadows.reserve(source.chars.size());
    for (const auto& cell : source.chars) {
        const int role = cell.styleIndex;
        if (role < 0) {
            line.cellShadows.push_back(line.shadow);
            continue;
        }
        auto [entry, inserted] = roles.try_emplace(role);
        auto& shadow = entry->second;
        if (inserted) {
            const auto& look = styles[role];
            shadow = skiaStaticShadow(look);
            if (bounds) {
                const auto prepare = [&](auto& state, const PaintStyle& paint) {
                    if (!state.enabled || paint.mode == "solid") return;
                    if (paint.mode == "image") {
                        state.image = images.fill(paint, layoutScale);
                        state.color = SkColorSetARGB(paint.color.alpha, paint.color.red, paint.color.green, paint.color.blue);
                    } else if (hasVisibleGradientStops(paint)) {
                        const auto& ink = paint.mode == "gradient_horizontal" ? bounds->forRole(role) : bounds->line;
                        if (!ink.isEmpty())
                            state.shader = ::subtitle::skia::lineGradient(paint, ink, bounds->outlinePad);
                    }
                };
                prepare(shadow.before, look.beforeDecorPaint);
                prepare(shadow.after, look.afterDecorPaint);
            }
        }
        line.cellShadows.push_back(shadow);
    }
    if (std::ranges::none_of(line.cellShadows, &::subtitle::skia::StaticShadow::visible)) {
        // No cell casts a shadow: the line's own shadow must not come back as a fallback.
        line.cellShadows.clear();
        line.shadow.before.enabled = line.shadow.after.enabled = false;
    }
}
}
