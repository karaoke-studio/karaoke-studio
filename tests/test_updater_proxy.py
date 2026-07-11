"""工作台共用代理解析与更新器代理接线测试。"""

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


def test_off_mode_removes_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://leak:1")
    monkeypatch.setenv("https_proxy", "http://leak:1")
    app = AppSettings(updater={"proxy": {"mode": "off", "manual_url": ""}})
    env = network.subprocess_env_for_app_settings(app)
    assert all(key not in env for key in network.PROXY_ENV_KEYS)
