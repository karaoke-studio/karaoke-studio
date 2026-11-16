#pragma once
#include "core/resolved_scene.h"
#include "text/font_instance.h"
#include <QtCore/QJsonObject>
#include <QtCore/QString>
#include <memory>
#include <string>
#include <vector>

namespace krok::subtitle::native {
struct ResolvedGlyphContext {
    using Fonts = std::vector<std::shared_ptr<const ::subtitle::skia::text::FontInstance>>;
    std::vector<Fonts> fonts;
    std::vector<std::string> languages;
    std::vector<bool> mixedFallback;
    double dpr = 1;
    ::subtitle::skia::ResolvedCell cell(const QJsonObject&, const QString& text, int startMs, int endMs,
                                     std::uint32_t before, std::uint32_t after) const;
};
}
