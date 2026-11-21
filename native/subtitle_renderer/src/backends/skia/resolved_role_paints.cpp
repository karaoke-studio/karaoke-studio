#include "resolved_role_paints.h"
#include "image_fill_cache.h"
#include "../../model/text_line.h"
#include "../../model/text_style.h"
#include "../render_backend.h"
#include "core/resolved_scene.h"
#include <algorithm>
#include <map>

namespace krok::subtitle::native {
void resolveRoleBodyPaints(::subtitle::skia::ResolvedLine& line, const TextLine& source, const TextStyle& base,
                           std::span<const TextStyle> styles, SkiaImageFillCache& images, float scale) {
    struct Domain {
        SkRect ink = SkRect::MakeEmpty();
        int paintIndex = -1;
    };
    const auto appearance = [&](int index) -> const TextStyle& { return index < 0 ? base : styles[index]; };
    std::map<int, Domain> domains;
    SkRect lineInk = SkRect::MakeEmpty();
    float visualPad = 0;
    for (std::size_t index = 0; index < line.cells.size(); ++index) {
        if (line.cells[index].path.isEmpty()) continue;
        const int role = source.chars[index].styleIndex;
        const auto& look = appearance(role);
        for (const auto* paint : {&look.beforeStrokePaint, &look.afterStrokePaint,
                                  &look.beforeStroke2Paint, &look.afterStroke2Paint})
            if (paint->mode != "solid") throw BackendError("Skia 角色正文画笔暂不支持复杂描边");
        const auto ink = line.cells[index].path.computeTightBounds();
        domains[role].ink.join(ink);
        lineInk.join(ink);
        visualPad = std::max(visualPad, ::subtitle::skia::outlineVisualPad(look.strokeWidth, look.stroke2Width, scale));
    }
    for (auto& [role, domain] : domains) {
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
                const auto& ink = paint.mode == "gradient_horizontal" ? domain.ink : lineInk;
                prepared.gradients[phase] = ::subtitle::skia::lineGradient(paint, ink, visualPad);
            }
        }
        line.roleBodyPaints.push_back(std::move(prepared));
    }
    for (std::size_t index = 0; index < line.cells.size(); ++index)
        if (!line.cells[index].path.isEmpty())
            line.cells[index].bodyPaintIndex = domains.at(source.chars[index].styleIndex).paintIndex;
}
}
