#include "generation_cancellation.h"

namespace krok::subtitle::native::runtime {
bool GenerationCancellation::cancelled(int generation) const {
    std::lock_guard lock(mutex_);
    return shutdown_ || cancelled_.contains(generation);
}
void GenerationCancellation::cancel(int generation) {
    std::lock_guard lock(mutex_);
    cancelled_.insert(generation);
}
void GenerationCancellation::clear(int generation) {
    std::lock_guard lock(mutex_);
    cancelled_.erase(generation);
}
void GenerationCancellation::shutdown() noexcept {
    std::lock_guard lock(mutex_);
    shutdown_ = true;
}
bool GenerationCancellation::publishIfActive(int generation, const std::function<void()>& publish) {
    std::lock_guard lock(mutex_);
    if (shutdown_ || cancelled_.contains(generation)) return false;
    publish();
    return true;
}
}
