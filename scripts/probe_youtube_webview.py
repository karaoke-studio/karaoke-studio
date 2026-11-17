"""Interactive prototype: python scripts/probe_youtube_webview.py [--mode firefox]."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube 内嵌网页登录验证原型")
    parser.add_argument("--mode", choices=("default", "firefox"), default="default")
    parser.add_argument("--smoke", action="store_true", help="只验证窗口和 Cookie 管道，不联网、不登录")
    args = parser.parse_args()

    from PyQt6.QtCore import Qt, QUrl, QThread, QTimer, pyqtSignal
    from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
    # Import WebEngine before QApplication; keep this experiment out of host startup.
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtNetwork import QNetworkProxy, QNetworkCookie
    from krok_helper.network import load_current_app_settings, proxy_url_for_app_settings
    from krok_helper.video_download.cookie_manager import CookieManager
    from krok_helper.video_download.youtube_web_login import WebViewCookieSnapshot, verify_and_import

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    settings = load_current_app_settings()
    manager = CookieManager(app_settings=settings)
    proxy_url = proxy_url_for_app_settings(settings)
    if proxy_url:
        url = QUrl(proxy_url)
        if url.scheme() not in {"http", "socks5", "socks5h"}:
            print("当前代理协议不适用于此原型，请在客户端设置 HTTP 或 SOCKS5 代理。")
            return 2
        proxy_type = QNetworkProxy.ProxyType.HttpProxy if url.scheme() == "http" else QNetworkProxy.ProxyType.Socks5Proxy
        QNetworkProxy.setApplicationProxy(QNetworkProxy(
            proxy_type, url.host(), url.port(8080 if url.scheme() == "http" else 1080),
            url.userName(), url.password(),
        ))
    else:
        QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))

    class VerifyWorker(QThread):
        result = pyqtSignal(bool, str)

        def __init__(self, jar, parent):
            super().__init__(parent)
            self.jar = jar

        def run(self):
            try:
                verify_and_import(self.jar, manager, proxy=proxy_url)
            except Exception:
                self.result.emit(False, "验证失败，原有 Cookie 未替换。请确认网页已登录、代理可用；也可关闭窗口后尝试另一模式。")
            else:
                self.result.emit(True, "账号验证通过，Cookie 已导入客户端。请重新解析视频验证下载；无需下载稍后观看列表。")

    class LoginDialog(QDialog):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"YouTube 登录原型 · {'Firefox 兼容' if args.mode == 'firefox' else '默认'}模式")
            self.resize(1050, 800)
            self.cookies = WebViewCookieSnapshot()
            self.worker = None
            layout = QVBoxLayout(self)
            hint = QLabel("请在网页中手动登录并完成验证，再点击「验证并导入」。每次启动使用全新会话。")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.address = QLabel()
            self.address.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(self.address)
            # Off-the-record profile: no password/cache/session persistence on disk.
            self.profile = QWebEngineProfile(self)
            if args.mode == "firefox":
                platform = "Macintosh; Intel Mac OS X 10.15" if sys.platform == "darwin" else (
                    "Windows NT 10.0; Win64; x64" if sys.platform == "win32" else "X11; Linux x86_64")
                self.profile.setHttpUserAgent(f"Mozilla/5.0 ({platform}; rv:140.0) Gecko/20100101 Firefox/140.0")
            self.view = QWebEngineView(self)
            self.page = QWebEnginePage(self.profile, self.view)
            self.view.setPage(self.page)
            self.view.urlChanged.connect(lambda u: self.address.setText(f"当前站点：{u.scheme()}://{u.host()}"))
            self.view.loadFinished.connect(self.loaded)
            store = self.profile.cookieStore()
            store.cookieAdded.connect(self.cookie_added)
            store.cookieRemoved.connect(self.cookies.removed)
            layout.addWidget(self.view, 1)
            self.status = QLabel("等待网页登录。")
            self.status.setWordWrap(True)
            self.status.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(self.status)
            row = QHBoxLayout()
            back = QPushButton("返回")
            back.clicked.connect(self.view.back)
            reload_button = QPushButton("重新加载")
            reload_button.clicked.connect(self.view.reload)
            self.verify_button = QPushButton("验证并导入到客户端")
            self.verify_button.clicked.connect(self.verify)
            close = QPushButton("关闭")
            close.clicked.connect(self.close)
            for button in (back, reload_button, self.verify_button, close):
                row.addWidget(button)
            layout.addLayout(row)
            if args.smoke:
                self.view.setHtml("<h1>本地登录窗口检查</h1>", QUrl("https://www.youtube.com/"))
                cookie = QNetworkCookie(b"probe", b"local-only")
                cookie.setDomain(".youtube.com")
                cookie.setPath("/")
                store.setCookie(cookie)
                # Exercise delayed reads after native signal arguments expire,
                # including repeated cookie updates while the window is idle.
                self.smoke_updates = 0
                self.smoke_timer = QTimer(self)
                self.smoke_timer.timeout.connect(self.smoke_update)
                self.smoke_timer.start(100)
                QTimer.singleShot(15000, self.smoke_verify)
                QTimer.singleShot(25000, lambda: app.exit(2))
            else:
                self.view.load(QUrl("https://www.youtube.com/"))

        def cookie_added(self, cookie):
            self.cookies.added(cookie)

        def smoke_update(self):
            self.smoke_updates += 1
            cookie = QNetworkCookie(b"rotating", str(self.smoke_updates).encode("ascii"))
            cookie.setDomain(".youtube.com")
            cookie.setPath("/")
            self.profile.cookieStore().setCookie(cookie)

        def smoke_verify(self):
            self.smoke_timer.stop()
            jar = self.cookies.to_jar()
            assert {c.name for c in jar} == {"probe", "rotating"}
            assert self.smoke_updates >= 50
            # Actually click the production callback, including its QThread.
            # Synthetic guest cookies must be rejected without network access.
            self.verify_button.click()
            self.worker.result.connect(self.smoke_result)
            self.worker.finished.connect(self.close)

        def smoke_result(self, ok, message):
            assert not ok
            print("PASS: 15 秒 Cookie 更新、延迟点击验证、工作线程失败处理（未联网）")

        def loaded(self, ok):
            if not ok:
                self.status.setText("网页加载失败，请检查客户端代理设置后重启原型。")

        def verify(self):
            if self.worker is not None and self.worker.isRunning():
                return
            jar = self.cookies.to_jar()
            self.verify_button.setEnabled(False)
            self.status.setText("正在验证账号，仅请求稍后观看列表信息，不下载视频…")
            self.worker = VerifyWorker(jar, self)
            self.worker.result.connect(lambda ok, message: self.status.setText(message))
            self.worker.finished.connect(lambda: self.verify_button.setEnabled(True))
            self.worker.start()

        def closeEvent(self, event):
            if self.worker is not None and self.worker.isRunning():
                self.status.setText("正在验证，请等待结果后关闭。")
                event.ignore()
                return
            super().closeEvent(event)

    dialog = LoginDialog()
    dialog.show()
    result = app.exec()
    # Chromium pages must be destroyed before their profile.
    from PyQt6 import sip
    sip.delete(dialog.view)
    sip.delete(dialog.profile)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
