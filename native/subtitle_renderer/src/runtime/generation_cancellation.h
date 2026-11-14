#pragma once
#include <functional>
#include <mutex>
#include <unordered_set>

namespace krok::subtitle::native::runtime {
// Shared cancellation/publication authority. The publisher must be short and nonblocking.
class GenerationCancellation {
public:
    bool cancelled(int generation) const;
    void cancel(int generation);
    void clear(int generation);
    void shutdown() noexcept;
    bool publishIfActive(int generation, const std::function<void()>& publish);
private:
    mutable std::recursive_mutex mutex_;
    std::unordered_set<int> cancelled_;
    bool shutdown_ = false;
};
}
