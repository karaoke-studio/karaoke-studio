#pragma once

#include "qt_render_types.h"

#include <QtCore/QString>

#include <vector>

namespace krok::subtitle::native::legacy_qt {

QString lineText(const protocol::TimingLine &line);
bool lineHasRoleLabels(const protocol::TimingLine &line);
int lineStartMs(const protocol::TimingLine &line);
int lineEndMs(const protocol::TimingLine &line);
std::vector<DisplayLineRef> visibleDisplayLines(
    const protocol::RenderConfig &cfg,
    int tMs
);

}  // namespace krok::subtitle::native::legacy_qt

