#include "checksum.h"

#include <algorithm>

namespace krok::subtitle::native::runtime {

std::uint64_t imageChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    const qsizetype step = std::max<qsizetype>(1, size / 4096);
    for (qsizetype i = 0; i < size; i += step) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::uint64_t imageFullChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    for (qsizetype i = 0; i < size; ++i) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::uint64_t bytesChecksum(const std::uint8_t *data, std::size_t size) {
    std::uint64_t hash = 1469598103934665603ull;
    for (std::size_t index = 0; index < size; ++index) {
        hash ^= static_cast<std::uint64_t>(data[index]);
        hash *= 1099511628211ull;
    }
    return hash;
}

}  // namespace krok::subtitle::native::runtime
