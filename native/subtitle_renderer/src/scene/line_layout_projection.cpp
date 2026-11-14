#include "line_layout_projection.h"
#include <algorithm>
#include <cmath>
#include <QtCore/QFileInfo>

namespace krok::subtitle::native::scene {
using protocol::ResolvedStyle;
using protocol::ResolvedLineLayout;
using protocol::PaintFillSpec;

// N3 CalcHorizontalAlignment: Top/Middle count forward from the page's first
// line, Bottom counts backward from its last one.  The two agree on a full
// page and diverge on a short one, where Bottom takes the tail of the list --
// a 2-line page under [left, center, right] is "center + right".
int alignmentIndexForLane(
    int lane,
    int alignmentCount,
    int pageLineCount,
    const QString &verticalPosition
) {
    if (alignmentCount <= 0) {
        return 0;
    }
    int index = std::max(lane, 0);
    if (verticalPosition == QStringLiteral("bottom")
        && pageLineCount > 0
        && pageLineCount < alignmentCount) {
        index = std::max(alignmentCount - pageLineCount + index, 0);
    }
    return std::clamp(index, 0, alignmentCount - 1);
}

void applyLineLayout(
    krok::subtitle::native::TextStyle &target,
    const ResolvedLineLayout &layout,
    int lane,
    bool centerOverride,
    int pageLineCount,
    double scale
) {
    if (!layout.present) {
        return;
    }
    target.bottomMargin = static_cast<float>(layout.lineYMarginPx * scale);
    target.lineGap = static_cast<float>(layout.lineGapPx * scale);
    target.dualLineLayout = layout.dualLineLayout;
    target.laneCount = layout.dualLineLayout
        ? std::max(static_cast<int>(layout.lineAlignments.size()), 1)
        : 1;
    target.verticalPosition = layout.lineYPosition.toStdString();
    target.smartHorizontal = layout.lineHorizontalLayout == QStringLiteral("asymmetric")
        ? layout.smartHorizontal.toStdString()
        : "none";
    target.letterSpacing = static_cast<float>(layout.letterSpacingPx * scale);
    target.spaceWidthPercent = layout.spaceWidthPercent;
    target.allowBiting = layout.allowBiting;
    target.rubyInterval = static_cast<float>(layout.rubyIntervalPx * scale);
    target.rubyAlignment = layout.rubyAlignment.toStdString();
    target.rubyGap = static_cast<float>(layout.rubyGapPx * scale);
    const int offsetIndex = alignmentIndexForLane(
        lane, static_cast<int>(layout.lineAlignments.size()),
        layout.dualLineLayout && layout.lineHorizontalLayout == QStringLiteral("asymmetric")
            ? pageLineCount : 0,
        layout.lineYPosition
    );
    target.laneOffsetX = offsetIndex < static_cast<int>(layout.lineOffsetsX.size())
        ? static_cast<float>(layout.lineOffsetsX[offsetIndex] * scale) : 0.0f;
    target.layoutOffsetX = 0.0f;
    target.layoutOffsetY = 0.0f;

    if (centerOverride || layout.lineHorizontalLayout == QStringLiteral("center")) {
        target.alignment = "center";
    } else if (layout.lineHorizontalLayout == QStringLiteral("per_row")) {
        const bool secondRow = lane == 1;
        target.alignment = (
            secondRow ? layout.row2Align : layout.row1Align
        ).toStdString();
        target.horizontalMargin = 0.0f;
        target.layoutOffsetX = static_cast<float>(
            (secondRow ? layout.row2OffsetX : layout.row1OffsetX) * scale
        );
    } else if (!layout.lineAlignments.empty()) {
        const int alignmentIndex = alignmentIndexForLane(
            lane,
            static_cast<int>(layout.lineAlignments.size()),
            pageLineCount,
            layout.lineYPosition
        );
        target.alignment = layout.lineAlignments[
            static_cast<std::size_t>(alignmentIndex)
        ].toStdString();
        target.horizontalMargin = static_cast<float>(
            layout.horizontalMarginPx * scale
        );
    }

    if (layout.lineHorizontalLayout == QStringLiteral("per_row")) {
        if (lane == 0) {
            target.layoutOffsetY = static_cast<float>(layout.row1OffsetY * scale);
        } else if (lane == 1) {
            target.layoutOffsetY = static_cast<float>(layout.row2OffsetY * scale);
        }
    }
}
}  // namespace krok::subtitle::native::scene
