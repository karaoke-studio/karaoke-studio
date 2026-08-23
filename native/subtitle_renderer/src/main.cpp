#include <QtCore/QByteArray>
#include <QtCore/QJsonObject>
#include <QtCore/QIODevice>
#include <QtCore/QTextStream>
#include <QtWidgets/QApplication>

#include "commands/command_router.h"
#include "protocol/json_protocol.h"

namespace {

using krok::subtitle::native::protocol::kRenderIrSchema;
using krok::subtitle::native::protocol::parseRequestLine;
using krok::subtitle::native::protocol::response;
using krok::subtitle::native::protocol::writeJson;
using krok::subtitle::native::commands::CommandRouter;

}  // namespace

int main(int argc, char **argv) {
#if !defined(Q_OS_WIN)
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
#endif
    QApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kRenderIrSchema);
    ready.insert(QStringLiteral("gpu_protocol"), 1);
    ready.insert(QStringLiteral("native_preview_protocol"), 1);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    CommandRouter router;
    QTextStream input(stdin, QIODevice::ReadOnly);
    while (!input.atEnd()) {
        const QString line = input.readLine().trimmed();
        if (line.isEmpty()) {
            continue;
        }

        QJsonObject parseError;
        const auto request = parseRequestLine(line, &parseError);
        if (!request.has_value()) {
            writeJson(parseError);
            continue;
        }

        const auto result = router.dispatch(*request);
        if (result.response.has_value()) {
            writeJson(*result.response);
        }
        if (result.shutdownRequested) {
            return 0;
        }
    }

    router.shutdown();
    return 0;
}
