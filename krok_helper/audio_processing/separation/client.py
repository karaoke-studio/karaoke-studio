"""HTTP client for the pinned pymss v2 server protocol."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class PyMSSAPIError(RuntimeError):
    status_code: int
    code: str
    message: str
    parameter: str = ""

    def __str__(self) -> str:
        return self.message or self.code or f"HTTP {self.status_code}"


def normalize_server_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("PyMSS 服务地址必须是有效的 http:// 或 https:// 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("PyMSS 服务地址不能包含账号、查询参数或片段。")
    return text


class PyMSSClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (5.0, 30.0),
    ) -> None:
        self.base_url = normalize_server_url(base_url)
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        host = (urlparse(self.base_url).hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"}:
            # A local managed service must never be routed through a proxy.
            self.session.trust_env = False

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _raise_api_error(self, response: requests.Response) -> None:
        if response.ok:
            return
        code = "http_error"
        message = f"PyMSS 服务返回 HTTP {response.status_code}。"
        parameter = ""
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
                parameter = str(error.get("param") or "")
        except (ValueError, json.JSONDecodeError):
            pass
        raise PyMSSAPIError(response.status_code, code, message, parameter)

    def _json(self, method: str, path: str, *, payload=None, timeout=None) -> dict:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(json_body=payload is not None),
            json=payload,
            timeout=timeout or self.timeout,
        )
        self._raise_api_error(response)
        data = response.json()
        if not isinstance(data, dict):
            raise PyMSSAPIError(response.status_code, "invalid_response", "PyMSS 返回了无效 JSON。")
        return data

    def health(self) -> dict:
        return self._json("GET", "/health")

    def server_info(self) -> dict:
        return self._json("GET", "/v1/server/info")

    def openapi(self) -> dict:
        return self._json("GET", "/openapi.json")

    def loaded_models(self) -> list[dict]:
        data = self._json("GET", "/v1/models").get("data", [])
        return data if isinstance(data, list) else []

    def catalog_model(self, model: str, *, source: str = "modelscope") -> dict:
        response = self.session.get(
            f"{self.base_url}/v1/catalog/models/{model}",
            headers=self._headers(),
            params={"source": source},
            timeout=self.timeout,
        )
        self._raise_api_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise PyMSSAPIError(response.status_code, "invalid_response", "模型目录响应无效。")
        return payload

    def download_model(
        self,
        model: str,
        *,
        source: str = "modelscope",
        force: bool = False,
        verify: bool = True,
        timeout_seconds: float = 30.0,
    ) -> dict:
        return self._json(
            "POST",
            "/v1/models/download",
            payload={
                "model": model,
                "source": source,
                "endpoint": None,
                "force": bool(force),
                "verify": bool(verify),
                "timeout_seconds": float(timeout_seconds),
            },
            timeout=(10.0, max(60.0, timeout_seconds * 4)),
        )

    def load_model(
        self,
        model: str,
        *,
        source: str = "modelscope",
        inference_params: dict | None = None,
    ) -> dict:
        return self._json(
            "POST",
            "/v1/models/load",
            payload={
                "model": model,
                "source": source,
                "endpoint": None,
                "inference_params": dict(inference_params or {}),
            },
            timeout=(10.0, 900.0),
        )

    def separate_pcm(
        self,
        pcm_path: str | Path,
        output_zip: str | Path,
        *,
        model: str,
        sample_rate: int,
        channels: int = 2,
        stems: Iterable[str] = (),
        output_audio_format: str = "wav",
        cancelled=None,
    ) -> None:
        source = Path(pcm_path)
        destination = Path(output_zip)
        params = {
            "model": model,
            "format": "pcm_f32le",
            "sample_rate": int(sample_rate),
            "channels": int(channels),
            "stems": ",".join(str(stem) for stem in stems if str(stem).strip()),
            "response_format": "zip",
            "output_audio_format": output_audio_format,
        }
        with source.open("rb") as body, self.session.post(
            f"{self.base_url}/v1/audio/separations",
            headers={**self._headers(), "Content-Type": "application/octet-stream"},
            params=params,
            data=body,
            stream=True,
            timeout=(15.0, 3600.0),
        ) as response:
            self._raise_api_error(response)
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_suffix(destination.suffix + ".part")
            try:
                with partial.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("音频分离已取消。")
                        if chunk:
                            stream.write(chunk)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)

    def wait_until_healthy(self, timeout_seconds: float = 30.0, *, cancelled=None) -> dict:
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("等待 PyMSS 服务启动已取消。")
            try:
                health = self.health()
                if health.get("status") == "ok":
                    return health
            except (requests.RequestException, PyMSSAPIError, ValueError) as exc:
                last_error = exc
            time.sleep(0.2)
        detail = f"：{last_error}" if last_error else ""
        raise TimeoutError(f"等待 PyMSS 服务启动超时{detail}")

    def capability_checks(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []
        try:
            health = self.health()
            ok = health.get("status") == "ok"
            checks.append(("健康检查", ok, "/health 正常" if ok else "状态不是 ok"))
        except Exception as exc:
            return [("健康检查", False, str(exc))]
        try:
            openapi = self.openapi()
            version = str(openapi.get("info", {}).get("version", "未知"))
            paths = openapi.get("paths", {})
            required = {
                "/v1/models/load",
                "/v1/models/download",
                "/v1/audio/separations",
            }
            missing = sorted(required - set(paths if isinstance(paths, dict) else {}))
            checks.append(("Server API 版本", version == "1", f"API {version}"))
            checks.append(
                (
                    "模型管理与分离端点",
                    not missing,
                    "端点完整" if not missing else "缺少 " + "、".join(missing),
                )
            )
        except Exception as exc:
            checks.extend(
                [("Server API 版本", False, str(exc)), ("模型管理与分离端点", False, str(exc))]
            )
        try:
            info = self.server_info()
            checks.append(("服务能力信息", info.get("object") == "server.info", "协议兼容"))
        except Exception as exc:
            checks.append(("服务能力信息", False, str(exc)))
        return checks


__all__ = ["PyMSSAPIError", "PyMSSClient", "normalize_server_url"]
