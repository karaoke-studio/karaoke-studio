#pragma once
namespace krok::subtitle::native::runtime {
// Credit includes running work, matching the production Direct2D pool.
constexpr bool canAcceptWork(bool stopping, bool accepting, int outstanding, int capacity) noexcept {
    return !stopping && accepting && outstanding < capacity;
}
}
