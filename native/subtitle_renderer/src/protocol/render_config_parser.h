#pragma once

#include "render_config.h"

#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <optional>

namespace krok::subtitle::native::protocol {

std::optional<RenderConfig> parseRenderConfig(
    const QJsonObject &ir,
    QString *error
);
ResolvedStyle resolvedStyleFromTitle(
    const ResolvedStyle &base,
    const QJsonObject &title
);
QString resolvedStyleKey(int singerId, const QString &roleLabel);
const ResolvedStyle &resolvedStyleForLine(
    const RenderConfig &cfg,
    const TimingLine &line
);
const ResolvedStyle &resolvedStyleForCharacter(
    const RenderConfig &cfg,
    const TimingLine &line,
    const TimingChar &ch
);

}  // namespace krok::subtitle::native::protocol

