#include "commands/skia/command_router.h"
#include "protocol/json_protocol.h"
#include <QtCore/QCoreApplication>
#include <QtCore/QJsonArray>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    using namespace krok::subtitle::native;
    auto ready = protocol::response(true, "ready");
    ready.insert("backend", "skia");
    ready.insert("schema", 3);
    ready.insert("selection_version", 1);
    ready.insert("profiles", QJsonArray{"p04_ascii_solid_wipe", "p05_resolved_horizontal", "p05_single_line_placement"});
    QJsonArray targets{"raster"};
#ifdef KROK_SKIA_SIDECAR_GPU
    targets.append("gpu");
#endif
    ready.insert("execution_targets", targets);
    ready.insert("pixel_formats", QJsonArray{"rgba8888", "bgra8888_premultiplied"});
    ready.insert("text_metrics_schema", 1);
    ready.insert("resolved_layout_schema", 2);
    ready.insert("fingerprint", KROK_SKIA_FINGERPRINT);
    protocol::writeJson(ready);
    commands::SkiaCommandRouter router;
    std::string line;
    while (std::getline(std::cin, line)) {
        QJsonObject error;
        const auto request = protocol::parseRequestLine(QString::fromUtf8(line), &error);
        if (!request) protocol::writeJson(error);
        else if (!router.dispatch(*request)) break;
    }
    router.close();
}
