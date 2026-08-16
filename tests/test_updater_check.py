"""Phase 1 检查链路强化的单元测试。

覆盖：302 跳转兜底、403 限流文案、跨版本 changelog 聚合、列表 API URL、
4 段版本号比较边界。
"""

from __future__ import annotations

import krok_helper.updater.worker as worker
from krok_helper.updater.http_client import HttpResult, SourceTrialRunner
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import build_api_list_urls
from krok_helper.updater.worker import (
    LatestRelease,
    _build_check_error,
    _version_key,
    fetch_latest_release_via_redirect,
    fetch_releases_since,
    is_newer_version,
    probe_github_connectivity,
)


def _release(version: str, body: str = "", tag: str | None = None) -> LatestRelease:
    return LatestRelease(
        tag=tag or f"v{version}",
        version=version,
        name=f"v{version}",
        body=body,
        html_url="",
        prerelease=False,
        published_at="2026-07-11T00:00:00Z",
        assets=[],
    )


# ── 版本比较（KS 4 段语义，不采用 SUG 3 段实现的回归护栏） ──────────────


def test_version_key_four_segments() -> None:
    assert is_newer_version("3.1.7.4", "3.1.7")
    assert is_newer_version("3.2.0", "3.1.7.4")
    assert is_newer_version("3.1.7.10", "3.1.7.9")
    assert not is_newer_version("3.1.7", "3.1.7.4")
    assert _version_key("v3.1.7.4") == (3, 1, 7, 4)


# ── 403 限流文案 ─────────────────────────────────────────────────────


def test_check_error_distinguishes_rate_limit() -> None:
    attempts = [
        ("github", "https://api.github.com/x", "HTTP 403"),
        ("ghproxy", "https://ghfast.top/x", "网络错误: timeout"),
    ]
    assert "频率超限" in _build_check_error(attempts)

    attempts_plain = [("github", "https://api.github.com/x", "网络错误: timeout")]
    assert _build_check_error(attempts_plain) == "无法访问任何更新源（请检查网络/代理）"


def test_connectivity_probe_treats_api_403_as_reachable(monkeypatch) -> None:
    monkeypatch.setattr(
        worker.http_client,
        "get_redirect_location",
        lambda *args, **kwargs: HttpResult(ok=True, status=302, body="https://github.com/x/y/releases/tag/v1"),
    )
    monkeypatch.setattr(
        worker.http_client,
        "get_text",
        lambda *args, **kwargs: HttpResult(ok=False, status=403, error="HTTP 403"),
    )

    ok, message = probe_github_connectivity("manual", "http://127.0.0.1:7890")

    assert ok
    assert "API" in message and "403" in message
    assert "自动改用网页端" in message


def test_connectivity_probe_requires_github_web_reachability(monkeypatch) -> None:
    monkeypatch.setattr(
        worker.http_client,
        "get_redirect_location",
        lambda *args, **kwargs: HttpResult(ok=False, error="网络错误: timeout"),
    )
    monkeypatch.setattr(
        worker.http_client,
        "get_text",
        lambda *args, **kwargs: HttpResult(ok=True, status=200, body="Keep it logically awesome"),
    )

    ok, message = probe_github_connectivity("off")

    assert not ok
    assert "网络错误: timeout" in message


# ── 302 跳转兜底 ─────────────────────────────────────────────────────


def test_redirect_fallback_parses_tag(monkeypatch) -> None:
    monkeypatch.setattr(
        worker.http_client,
        "get_redirect_location",
        lambda url, **kw: HttpResult(
            ok=True,
            status=302,
            body="https://github.com/karaoke-studio/karaoke-studio/releases/tag/v3.9.9",
        ),
    )
    release, attempts = fetch_latest_release_via_redirect(UpdaterSettings())
    assert release is not None
    assert release.tag == "v3.9.9"
    assert release.version == "3.9.9"
    assert release.assets == []
    assert attempts[-1][2] == ""


def test_redirect_fallback_handles_bad_location(monkeypatch) -> None:
    monkeypatch.setattr(
        worker.http_client,
        "get_redirect_location",
        lambda url, **kw: HttpResult(ok=True, status=302, body="https://github.com/login"),
    )
    release, attempts = fetch_latest_release_via_redirect(UpdaterSettings())
    assert release is None
    assert "无法从跳转地址解析 tag" in attempts[-1][2]


def test_do_check_uses_redirect_fallback(monkeypatch) -> None:
    settings = UpdaterSettings()
    monkeypatch.setattr(worker, "fetch_latest_release", lambda s: (None, [("github", "u", "HTTP 403")]))
    monkeypatch.setattr(
        worker,
        "fetch_latest_release_via_redirect",
        lambda s: (_release("99.0.0", tag="v99.0.0"), [("github", "u2", "")]),
    )
    runnable = worker._CheckRunnable(settings, manual=True)
    result = runnable._do_check()
    assert result.ok
    assert result.has_update
    assert result.primary_asset_name  # 按命名约定合成
    assert result.download_candidates  # 下载候选按 tag 合成，Updater 仍可接力
    assert result.all_releases == []  # 兜底模式不做聚合
    assert result.primary_sha256 == ""  # 跳转兜底拿不到资产清单，无 digest


def test_do_check_reports_rate_limit_when_all_fail(monkeypatch) -> None:
    settings = UpdaterSettings()
    monkeypatch.setattr(worker, "fetch_latest_release", lambda s: (None, [("github", "u", "HTTP 403")]))
    monkeypatch.setattr(worker, "fetch_latest_release_via_redirect", lambda s: (None, [("github", "u2", "网络错误: x")]))
    result = worker._CheckRunnable(settings, manual=True)._do_check()
    assert not result.ok
    assert "频率超限" in result.error


# ── 跨版本 changelog 聚合 ────────────────────────────────────────────


def test_fetch_releases_since_filters_and_sorts(monkeypatch) -> None:
    payload = [
        {"tag_name": "v3.1.6", "body": "old", "assets": []},
        {"tag_name": "v3.1.7.4", "body": "newest", "assets": []},
        {"tag_name": "v3.1.7", "body": "mid", "assets": []},
        {"tag_name": "v3.2.0-beta", "body": "pre", "prerelease": True, "assets": []},
    ]
    monkeypatch.setattr(
        worker.http_client,
        "get_json",
        lambda url, **kw: HttpResult(ok=True, status=200, body=payload),
    )
    releases, attempts = fetch_releases_since("3.1.6", UpdaterSettings())
    assert [r.version for r in releases] == ["3.1.7.4", "3.1.7"]
    assert attempts[-1][2] == ""


def test_fetch_releases_since_relays_to_next_source(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get_json(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            return HttpResult(ok=False, error="HTTP 403")
        return HttpResult(ok=True, status=200, body=[{"tag_name": "v9.0.0", "assets": []}])

    monkeypatch.setattr(worker.http_client, "get_json", fake_get_json)
    releases, _ = fetch_releases_since("3.0.0", UpdaterSettings())
    assert len(calls) == 2
    assert [r.version for r in releases] == ["9.0.0"]


def test_do_check_aggregates_changelogs(monkeypatch) -> None:
    settings = UpdaterSettings()
    latest = LatestRelease(
        tag="v9.0.0",
        version="9.0.0",
        name="v9.0.0",
        body="latest body",
        html_url="",
        prerelease=False,
        published_at="",
        assets=[worker.ReleaseAsset(worker.current_asset_name(), 1, "https://example.invalid/a.zip")],
    )
    monkeypatch.setattr(worker, "fetch_latest_release", lambda s: (latest, [("github", "u", "")]))
    monkeypatch.setattr(
        worker,
        "fetch_releases_since",
        lambda cur, s: ([latest, _release("8.9.0", body="mid body")], []),
    )
    result = worker._CheckRunnable(settings, manual=True)._do_check()
    assert result.ok and result.has_update
    assert [r.version for r in result.all_releases] == ["9.0.0", "8.9.0"]


# ── asset digest → --sha256 接线 ──────────────────────────────────────


def test_parse_release_extracts_sha256_digest() -> None:
    payload = {
        "tag_name": "v9.0.0",
        "assets": [
            {
                "name": "KaraokeStudio-windows.zip",
                "size": 1,
                "browser_download_url": "u",
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": "KaraokeStudio-macos.zip",
                "size": 1,
                "browser_download_url": "u2",
                "digest": "sha512:" + "b" * 128,
            },
            {
                "name": "KaraokeStudio-windows.zip.sha256",
                "size": 1,
                "browser_download_url": "u3",
                "digest": "sha256:too-short",
            },
        ],
    }
    release = worker._parse_release(payload)
    by_name = {a.name: a for a in release.assets}
    assert by_name["KaraokeStudio-windows.zip"].digest == "a" * 64
    assert by_name["KaraokeStudio-macos.zip"].digest == ""  # 非 sha256 算法不收
    assert by_name["KaraokeStudio-windows.zip.sha256"].digest == ""  # 畸形摘要不收


def test_do_check_carries_primary_sha256_for_updater(monkeypatch) -> None:
    settings = UpdaterSettings()
    latest = LatestRelease(
        tag="v9.0.0",
        version="9.0.0",
        name="v9.0.0",
        body="latest body",
        html_url="",
        prerelease=False,
        published_at="",
        assets=[
            worker.ReleaseAsset(
                worker.current_asset_name(), 1, "https://example.invalid/a.zip", digest="c" * 64
            )
        ],
    )
    monkeypatch.setattr(worker, "fetch_latest_release", lambda s: (latest, [("github", "u", "")]))
    monkeypatch.setattr(worker, "fetch_releases_since", lambda cur, s: ([latest], []))
    result = worker._CheckRunnable(settings, manual=True)._do_check()
    assert result.ok and result.has_update
    assert result.primary_sha256 == "c" * 64


# ── 列表 API URL ─────────────────────────────────────────────────────


def test_build_api_list_urls_covers_all_sources() -> None:
    urls = build_api_list_urls(["github", "ghproxy"])
    assert urls[0][1] == "https://api.github.com/repos/karaoke-studio/karaoke-studio/releases?per_page=100"
    assert all("releases?per_page=100" in u for _s, u in urls)
    assert len(urls) == 4  # normalize_order 兜底补齐


# ── SourceTrialRunner ────────────────────────────────────────────────


def test_source_trial_runner_stops_at_first_success() -> None:
    runner = SourceTrialRunner([("a", "u1"), ("b", "u2"), ("c", "u3")])
    results = {"u1": HttpResult(ok=False, error="x"), "u2": HttpResult(ok=True, status=200)}
    hit = runner.run(lambda url: results.get(url, HttpResult(ok=False, error="y")))
    assert hit is not None and hit.ok
    assert [a.source_id for a in runner.attempts] == ["a", "b"]
    assert "FAIL" in runner.summary() and "OK" in runner.summary()
