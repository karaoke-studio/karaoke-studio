#pragma once
#include "frame_commands.h"
#include "../../runtime/owner_backend_session.h"
#include <memory>
namespace krok::subtitle::native::commands {
class SkiaCommandRouter {
public:
    ~SkiaCommandRouter() { close(); }
    bool dispatch(const QJsonObject& request);
    void close();
private:
    QJsonObject select(const QJsonObject& request);
    QJsonObject info(RenderBackend& backend) const;
    void synchronous(const QJsonObject& request);
    std::shared_ptr<runtime::GenerationCancellation> cancellation_ = std::make_shared<runtime::GenerationCancellation>();
    std::shared_ptr<SkiaFrames> frames_ = std::make_shared<SkiaFrames>();
    std::unique_ptr<runtime::OwnerBackendSession> session_;
    QString requestedTarget_, fallbackReason_;
    bool configured_ = false;
};
}
