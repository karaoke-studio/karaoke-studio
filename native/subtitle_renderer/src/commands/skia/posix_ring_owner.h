#pragma once
#include <QtCore/QString>
#include <map>
#include <mutex>

namespace krok::subtitle::native::commands {
// Only server-issued, exclusively created POSIX names belong to this owner.
class PosixRingOwner {
public:
    ~PosixRingOwner();
    QString reserve(const QString& requested);
    void create(const QString& key, int bytes);
    void complete(const QString& key);
    bool release(const QString& key, bool force = false);
private:
    std::mutex mutex_;
    struct Allocation { bool created = false, complete = false; };
    std::map<QString, Allocation> keys_;
};
}
