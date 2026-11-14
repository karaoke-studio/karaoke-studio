// Private Qt APIs are restricted to this test helper, built only with native tests enabled.
#include <QtGui/QGuiApplication>
#include <QtCore/QThread>
#include <QtGui/private/qhighdpiscaling_p.h>
#include <QtGui/private/qwindow_p.h>
#include <cmath>

extern "C" Q_DECL_EXPORT bool krokSetPreviewTestScreenScale(double factor) {
    auto* app = QCoreApplication::instance();
    if (!app || QThread::currentThread() != app->thread() || !std::isfinite(factor) || factor <= 0)
        return false;
    auto* screen = QGuiApplication::primaryScreen();
    if (!screen) return false;
    QHighDpiScaling::setScreenFactor(screen, factor);
    // The platform plugin normally refreshes each window's cached DPR after an OS screen
    // notification. This test changes only Qt's process-local factor, so perform that step
    // explicitly; updateDevicePixelRatio sends the genuine DevicePixelRatioChange event.
    for (auto* window : QGuiApplication::allWindows())
        QWindowPrivate::get(window)->updateDevicePixelRatio();
    return true;
}
