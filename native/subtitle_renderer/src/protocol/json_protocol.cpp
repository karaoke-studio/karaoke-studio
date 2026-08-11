#include "json_protocol.h"

#include <QtCore/QJsonDocument>
#include <QtCore/QJsonParseError>

#include <iostream>
#include <mutex>

namespace krok::subtitle::native::protocol {

QJsonObject response(bool ok, const QString &event) {
    QJsonObject out;
    out.insert(QStringLiteral("ok"), ok);
    out.insert(QStringLiteral("event"), event);
    return out;
}

QJsonObject parseErrorResponse(const QString &message) {
    QJsonObject out = response(false, QStringLiteral("parse_error"));
    out.insert(QStringLiteral("error"), message);
    return out;
}

std::optional<QJsonObject> parseRequestLine(
    const QString &line,
    QJsonObject *errorResponse
) {
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(
        line.toUtf8(),
        &parseError
    );
    if (parseError.error == QJsonParseError::NoError && document.isObject()) {
        return document.object();
    }
    if (errorResponse != nullptr) {
        *errorResponse = parseErrorResponse(parseError.errorString());
    }
    return std::nullopt;
}

void writeJson(const QJsonObject &object) {
    static std::mutex mutex;
    std::lock_guard<std::mutex> lock(mutex);
    const QJsonDocument document(object);
    std::cout << document.toJson(QJsonDocument::Compact).constData()
              << std::endl;
}

}  // namespace krok::subtitle::native::protocol
