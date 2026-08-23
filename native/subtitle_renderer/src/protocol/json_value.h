#pragma once

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <vector>

namespace krok::subtitle::native::protocol {

QString stringValue(
    const QJsonObject &object,
    const QString &key,
    const QString &fallback = {}
);
int intValue(const QJsonObject &object, const QString &key, int fallback = 0);
std::vector<int> parseIntArray(const QJsonArray &items);

}  // namespace krok::subtitle::native::protocol
