#pragma once
#include "text/shaped_cell.h"
#include <QtCore/QJsonObject>
#include <map>

namespace krok::subtitle::native::scene {
using FontResourceTable = std::map<QString, std::shared_ptr<const ::subtitle::skia::text::FontResource>>;
FontResourceTable loadSkiaFontResources(const QJsonObject&);
std::vector<std::shared_ptr<const ::subtitle::skia::text::FontInstance>> skiaRunFonts(const QJsonObject&, const FontResourceTable&);
QJsonObject skiaCellMetricsJson(const ::subtitle::skia::text::ShapedCell&);
}
