from __future__ import annotations

from typing import Literal

from krok_helper.network import GH_PROXY_PREFIXES

REPO_OWNER = "karaoke-studio"
REPO_NAME = "karaoke-studio"

SourceId = Literal["github", "gh-proxy"]
SOURCE_IDS: tuple[SourceId, ...] = ("github", "gh-proxy")
SOURCE_LABELS: dict[SourceId, str] = {
    "github": "GitHub Release（官方）",
    "gh-proxy": "GitHub Proxy（gh-proxy 多节点）",
}
DEFAULT_ORDER: list[SourceId] = list(SOURCE_IDS)


def normalize_order(order: list[str] | tuple[str, ...]) -> list[SourceId]:
    seen: list[SourceId] = []
    for value in order:
        if value in SOURCE_IDS and value not in seen:
            seen.append(value)  # type: ignore[arg-type]
    for value in DEFAULT_ORDER:
        if value not in seen:
            seen.append(value)
    return seen


def build_download_url(source: SourceId, tag: str, asset_name: str) -> str:
    path = f"{REPO_OWNER}/{REPO_NAME}/releases/download/{tag}/{asset_name}"
    if source == "github":
        return f"https://github.com/{path}"
    if source == "gh-proxy":
        return f"{GH_PROXY_PREFIXES[0]}/https://github.com/{path}"
    raise ValueError(f"未知的更新源 id: {source!r}")


def build_release_urls(order: list[str] | tuple[str, ...], tag: str, asset_name: str) -> list[tuple[SourceId, str]]:
    """按用户排序构造下载 URL 列表，元素为 ``(source_id, url)``。

    ``gh-proxy`` 展开为每个节点一条候选（保持节点顺序），同一源 id 出现
    多次；调用方按列表顺序接力即可。旧设置里遗留的 ``ghproxy`` /
    ``ghproxy-net`` 源 id 会被 :func:`normalize_order` 过滤掉，自然收敛
    到现行清单。
    """
    path = f"{REPO_OWNER}/{REPO_NAME}/releases/download/{tag}/{asset_name}"
    out: list[tuple[SourceId, str]] = []
    for source in normalize_order(order):
        if source == "github":
            out.append((source, f"https://github.com/{path}"))
        elif source == "gh-proxy":
            for prefix in GH_PROXY_PREFIXES:
                out.append((source, f"{prefix}/https://github.com/{path}"))
    return out


def _build_api_urls_for_path(order: list[str] | tuple[str, ...], api_path: str) -> list[tuple[SourceId, str]]:
    urls: list[tuple[SourceId, str]] = []
    for source in normalize_order(order):
        if source == "github":
            urls.append((source, f"https://api.github.com/{api_path}"))
        elif source == "gh-proxy":
            for prefix in GH_PROXY_PREFIXES:
                urls.append((source, f"{prefix}/https://api.github.com/{api_path}"))
    return urls


def build_api_urls(order: list[str] | tuple[str, ...]) -> list[tuple[SourceId, str]]:
    return _build_api_urls_for_path(order, f"repos/{REPO_OWNER}/{REPO_NAME}/releases/latest")


def build_api_list_urls(order: list[str] | tuple[str, ...], per_page: int = 100) -> list[tuple[SourceId, str]]:
    """releases 列表 API（用于跨版本 changelog 聚合）。

    GitHub 默认每页 30 条，跨很多版本升级时不够，固定拉 ``per_page=100``。
    """
    return _build_api_urls_for_path(order, f"repos/{REPO_OWNER}/{REPO_NAME}/releases?per_page={per_page}")
