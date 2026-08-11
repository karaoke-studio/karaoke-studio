#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <optional>

namespace krok::subtitle::native::protocol {

inline constexpr int kRenderIrSchema = 1;

QJsonObject response(bool ok, const QString &event);
QJsonObject parseErrorResponse(const QString &message);
std::optional<QJsonObject> parseRequestLine(
    const QString &line,
    QJsonObject *errorResponse
);
void writeJson(const QJsonObject &object);

}  // namespace krok::subtitle::native::protocol
