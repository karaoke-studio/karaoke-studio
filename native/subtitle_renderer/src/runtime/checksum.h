#pragma once

#include <QtGui/QImage>

#include <cstddef>
#include <cstdint>

namespace krok::subtitle::native::runtime {

std::uint64_t imageChecksum(const QImage &image);
std::uint64_t imageFullChecksum(const QImage &image);
std::uint64_t bytesChecksum(const std::uint8_t *data, std::size_t size);

}  // namespace krok::subtitle::native::runtime
