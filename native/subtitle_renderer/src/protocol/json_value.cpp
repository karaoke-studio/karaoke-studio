#include "json_value.h"

namespace krok::subtitle::native::protocol {

QString stringValue(
    const QJsonObject &object,
    const QString &key,
    const QString &fallback
) {
    const auto value = object.value(key);
    return value.isString() ? value.toString() : fallback;
}

int intValue(const QJsonObject &object, const QString &key, int fallback) {
    const auto value = object.value(key);
    return value.isDouble() ? value.toInt() : fallback;
}

std::vector<int> parseIntArray(const QJsonArray &items) {
    std::vector<int> out;
    out.reserve(static_cast<std::size_t>(items.size()));
    for (const auto &item : items) {
        if (item.isDouble()) {
            out.push_back(item.toInt());
        }
    }
    return out;
}

}  // namespace krok::subtitle::native::protocol
