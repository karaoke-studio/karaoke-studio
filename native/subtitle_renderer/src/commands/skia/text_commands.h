#pragma once
#include <QtCore/QJsonObject>
namespace krok::subtitle::native::commands {
// GPU-free request: may be called before select_backend and before scene configure.
QJsonObject measureSkiaText(const QJsonObject& request);
}
