"""工作台共用代理解析与更新器代理接线测试。"""

import ssl
import urllib.request

import certifi
import pytest

from krok_helper import network
from krok_helper.network import ProxyInfo, _parse_proxy_server, parse_manual_proxy, resolve_proxy
from krok_helper.settings import AppSettings


def test_windows_proxy_server_formats():
    assert _parse_proxy_server("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert _parse_proxy_server("http=10.0.0.1:80;https=10.0.0.1:443") == "http://10.0.0.1:443"
    assert _parse_proxy_server("http=10.0.0.1:80") == "http://10.0.0.1:80"
    assert _parse_proxy_server("socks=10.0.0.1:1080") == ""


def test_manual_proxy_validation_and_normalization():
    assert parse_manual_proxy("127.0.0.1:7890") == ProxyInfo("http://127.0.0.1:7890", "manual")
    assert parse_manual_proxy("socks5://u:p@127.0.0.1:1080") == ProxyInfo(
        "socks5://u:p@127.0.0.1:1080", "manual"
    )
    assert parse_manual_proxy("") is None
    assert parse_manual_proxy("host-without-port") is None


def test_resolve_proxy_modes(monkeypatch):
    monkeypatch.setattr(network, "read_system_proxy", lambda: ProxyInfo("http://system:1", "system"))
    monkeypatch.setattr(network, "detect_proxy_auto", lambda: ProxyInfo("http://auto:2", "scan"))
    assert resolve_proxy("off") == (None, None)
    assert resolve_proxy("system")[0] == ProxyInfo("http://system:1", "system")
    assert resolve_proxy("auto")[0] == ProxyInfo("http://auto:2", "scan")
    assert resolve_proxy("manual", "127.0.0.1:7890")[1] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_auto_detection_prefers_system_proxy(monkeypatch):
    monkeypatch.setattr(network, "read_system_proxy", lambda: ProxyInfo("http://system:1", "system"))
    monkeypatch.setattr(network, "scan_local_proxy_ports", lambda ports: (_ for _ in ()).throw(AssertionError()))
    assert network.detect_proxy_auto() == ProxyInfo("http://system:1", "system")


def test_proxy_cli_args_use_updater_settings(monkeypatch):
    monkeypatch.setattr(network, "read_system_proxy", lambda: None)
    app = AppSettings(updater={"proxy": {"mode": "manual", "manual_url": "127.0.0.1:7890"}})
    assert network.proxy_cli_args_for_app_settings(app) == ["--proxy", "http://127.0.0.1:7890"]


def test_requests_session_for_current_settings_applies_workbench_proxy(monkeypatch):
    monkeypatch.setattr(network, "read_system_proxy", lambda: None)
    app = AppSettings(updater={"proxy": {"mode": "manual", "manual_url": "127.0.0.1:7890"}})
    monkeypatch.setattr(network, "load_current_app_settings", lambda: app)

    session, proxies = network.requests_session_for_current_settings()

    assert proxies == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    assert session.trust_env is False


def test_off_mode_removes_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://leak:1")
    monkeypatch.setenv("https_proxy", "http://leak:1")
    app = AppSettings(updater={"proxy": {"mode": "off", "manual_url": ""}})
    env = network.subprocess_env_for_app_settings(app)
    assert all(key not in env for key in network.PROXY_ENV_KEYS)


def test_ssl_context_stacks_certifi_onto_system_store():
    system_only = ssl.create_default_context()
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    stacked = network.build_ssl_context()

    system_count = system_only.cert_store_stats()["x509"]
    certifi_count = certifi_only.cert_store_stats()["x509"]
    stacked_count = stacked.cert_store_stats()["x509"]

    # 两份信任锚互不包含，叠加后必须严格多于任意一边，否则就是把系统存储覆盖掉了——
    # 那会挂掉企业根、杀软 HTTPS 扫描根这类只存在于系统存储里的中间人环境。
    assert stacked_count > system_count
    assert stacked_count > certifi_count


def test_ssl_context_falls_back_to_system_store_without_certifi(monkeypatch):
    def explode(name, *args, **kwargs):
        if name == "certifi":
            raise ModuleNotFoundError("No module named 'certifi'")
        return original_import(name, *args, **kwargs)

    original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    monkeypatch.setattr("builtins.__import__", explode)

    # certifi 没被打进包时只退回系统存储，不能抛异常把整条请求链炸掉。
    fallback_count = network.build_ssl_context().cert_store_stats()["x509"]
    assert fallback_count == ssl.create_default_context().cert_store_stats()["x509"]


def test_urllib_opener_uses_stacked_ssl_context(monkeypatch):
    monkeypatch.setattr(network, "read_system_proxy", lambda: None)
    monkeypatch.setattr(network, "_shared_ssl_context", None)

    app = AppSettings(updater={"proxy": {"mode": "manual", "manual_url": "127.0.0.1:7890"}})
    opener = network.build_urllib_opener_for_app_settings(app)

    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(https) == 1, "默认 HTTPSHandler 必须被带 context 的那个顶掉，不能两个并存"
    assert https[0]._context is network.shared_ssl_context()
    assert proxies and proxies[0].proxies["https"] == "http://127.0.0.1:7890"


def test_urllib_opener_keeps_caller_handlers(monkeypatch):
    monkeypatch.setattr(network, "read_system_proxy", lambda: None)
    app = AppSettings(updater={"proxy": {"mode": "off", "manual_url": ""}})

    jar_handler = urllib.request.HTTPCookieProcessor()
    opener = network.build_urllib_opener_for_app_settings(app, jar_handler)

    assert jar_handler in opener.handlers
    assert any(isinstance(h, urllib.request.HTTPSHandler) for h in opener.handlers)


@pytest.mark.parametrize("host", ["utaten.com", "lrclib.net"])
def test_stacked_context_verifies_lyrics_providers(host):
    pytest.importorskip("socket")
    import socket

    context = network.build_ssl_context()
    try:
        with socket.create_connection((host, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                assert tls.getpeercert()
    except (socket.gaierror, OSError) as exc:
        if isinstance(exc, ssl.SSLCertVerificationError):
            raise
        pytest.skip(f"{host} 不可达：{exc}")
