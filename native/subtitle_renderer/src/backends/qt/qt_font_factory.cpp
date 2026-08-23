#include "qt_font_factory.h"

#include <QtCore/QtGlobal>
#include <QtGui/QFontDatabase>

#include <algorithm>
#include <mutex>

namespace krok::subtitle::native::legacy_qt {

using protocol::ResolvedStyle;

QFont buildLineFont(const ResolvedStyle &style) {
    QFont font(style.fontFamily);
    font.setPixelSize(style.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    return font;
}

bool isEmojiText(const QString &text) {
    for (const uint scalar : text.toUcs4()) {
        if ((scalar >= 0x1F000U && scalar <= 0x1FAFFU)
            || (scalar >= 0x2600U && scalar <= 0x27BFU)) {
            return true;
        }
    }
    return false;
}

QFont buildEmojiFont(const ResolvedStyle &style) {
    static std::once_flag emojiFontRegistration;
    std::call_once(emojiFontRegistration, []() {
        const QString windowsRoot = qEnvironmentVariable("WINDIR", QStringLiteral("C:/Windows"));
        QFontDatabase::addApplicationFont(
            windowsRoot + QStringLiteral("/Fonts/seguisym.ttf")
        );
    });
    QFont font(QStringLiteral("Segoe UI Symbol"));
    font.setPixelSize(style.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    font.setItalic(style.italic);
    return font;
}

QFont buildRubyFont(const ResolvedStyle &style) {
    QFont font(style.fontFamily);
    font.setPixelSize(style.rubyFontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(style.fontWeight, 1, 999)));
    return font;
}

}  // namespace krok::subtitle::native::legacy_qt
