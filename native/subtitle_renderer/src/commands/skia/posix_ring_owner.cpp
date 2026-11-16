#include "posix_ring_owner.h"
#include "../../backends/render_backend.h"
#include <QtCore/QNativeIpcKey>
#include <QtCore/QUuid>
#include <QtCore/QFile>
#ifdef Q_OS_UNIX
#include <sys/mman.h>
#include <semaphore.h>
#include <fcntl.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#endif

namespace krok::subtitle::native::commands {
PosixRingOwner::~PosixRingOwner() {
    while (!keys_.empty()) release(keys_.begin()->first, true);
}
QString PosixRingOwner::reserve(const QString& requested) {
#ifdef Q_OS_UNIX
    (void)requested;
    // 28 characters, below macOS's short POSIX name limit. The client hint
    // cannot select an existing object's name or transfer its ownership.
    const auto name = QStringLiteral("/ks-") + QUuid::createUuid().toString(QUuid::Id128).left(24);
    const auto key = QNativeIpcKey(name, QNativeIpcKey::Type::PosixRealtime).toString();
    std::lock_guard lock(mutex_);
    keys_.emplace(key, Allocation{});
    return key;
#else
    return requested;
#endif
}
void PosixRingOwner::create(const QString& key, int bytes) {
#ifdef Q_OS_UNIX
    std::lock_guard lock(mutex_);
    auto entry = keys_.find(key);
    if (entry == keys_.end()) throw BackendError("Skia 共享帧缺少有效所有者");
    const auto name = QFile::encodeName(QNativeIpcKey::fromString(key).nativeKey());
    int fd;
    do { fd = ::shm_open(name.constData(), O_RDWR | O_CREAT | O_EXCL, 0600); } while (fd < 0 && errno == EINTR);
    if (fd < 0) throw BackendError(std::string("Skia 共享帧创建失败：") + std::strerror(errno));
    entry->second.created = true;
    int result;
    do { result = ::ftruncate(fd, bytes); } while (result < 0 && errno == EINTR);
    const int error = errno;
    ::close(fd);
    if (result < 0) throw BackendError(std::string("Skia 共享帧容量分配失败：") + std::strerror(error));
    // Qt 6.11 detaches without shm_unlink on Linux/macOS. The transport ring
    // attaches to this exclusive allocation; release/exit removes its name.
#else
    (void)key; (void)bytes;
#endif
}
void PosixRingOwner::complete(const QString& key) {
    std::lock_guard lock(mutex_);
    if (const auto entry = keys_.find(key); entry != keys_.end()) entry->second.complete = true;
}
bool PosixRingOwner::release(const QString& key, bool force) {
    std::lock_guard lock(mutex_);
    const auto entry = keys_.find(key);
    if (entry == keys_.end() || (!force && !entry->second.complete)) return false;
#ifdef Q_OS_UNIX
    if (entry->second.created) {
        const auto name = QFile::encodeName(QNativeIpcKey::fromString(key).nativeKey());
        ::shm_unlink(name.constData());
        ::sem_unlink(name.constData());
    }
#endif
    keys_.erase(entry);
    return true;
}
}
