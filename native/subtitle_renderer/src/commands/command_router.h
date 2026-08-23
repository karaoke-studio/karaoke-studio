#pragma once

#include <QtCore/QJsonObject>

#include <memory>
#include <optional>

namespace krok::subtitle::native::commands {

struct CommandDispatchResult {
    std::optional<QJsonObject> response;
    bool shutdownRequested = false;
};

class CommandRouter {
public:
    CommandRouter();
    ~CommandRouter();

    CommandRouter(const CommandRouter &) = delete;
    CommandRouter &operator=(const CommandRouter &) = delete;

    CommandDispatchResult dispatch(const QJsonObject &request);
    void shutdown();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace krok::subtitle::native::commands
