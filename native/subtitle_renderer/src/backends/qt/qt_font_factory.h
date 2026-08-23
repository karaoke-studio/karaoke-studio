#pragma once

#include "../../protocol/render_config.h"

#include <QtCore/QString>
#include <QtGui/QFont>

namespace krok::subtitle::native::legacy_qt {

QFont buildLineFont(const protocol::ResolvedStyle &style);
bool isEmojiText(const QString &text);
QFont buildEmojiFont(const protocol::ResolvedStyle &style);
QFont buildRubyFont(const protocol::ResolvedStyle &style);

}  // namespace krok::subtitle::native::legacy_qt
