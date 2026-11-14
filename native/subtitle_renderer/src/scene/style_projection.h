#pragma once
#include "../model/text_style.h"
#include "../protocol/render_config.h"

namespace krok::subtitle::native::scene {
void applyResolvedStyle(TextStyle& target, const protocol::ResolvedStyle& source, double scale);
bool latinStrokesDiffer(const protocol::ResolvedStyle& style);
}
