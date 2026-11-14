#pragma once
#include "../model/text_style.h"
#include "../protocol/render_config.h"

namespace krok::subtitle::native::scene {
int alignmentIndexForLane(int lane, int alignmentCount, int pageLineCount, const QString& verticalPosition);
void applyLineLayout(TextStyle& target, const protocol::ResolvedLineLayout& layout,
                    int lane, bool centerOverride, int pageLineCount, double scale);
}
