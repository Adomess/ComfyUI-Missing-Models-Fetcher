from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import folder_paths


SECRET_KEYS = ("hf_api_key", "civitai_api_key", "modelscope_api_token")
DOWNLOAD_CONCURRENCY_MIN = 1
DOWNLOAD_CONCURRENCY_MAX = 32
DEFAULT_DOWNLOAD_CONCURRENCY = 1
DEFAULT_PROVIDER_CONCURRENCY = 1
PROVIDER_CONCURRENCY_MIN = 1
PROVIDER_CONCURRENCY_MAX = 32
DEFAULT_BANDWIDTH_LIMIT_MBPS = 0.0


def _proxy_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("代理地址必须是有效的 HTTP 或 HTTPS URL")
    if parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含查询参数或片段")
    return raw


def _proxy_url_masked(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parsed.username is not None:
        auth = f"{parsed.username}:****@" if parsed.password is not None else f"{parsed.username}@"
    netloc = f"{auth}{host}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _proxy_mode(value: Any, legacy_enabled: bool = False) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"off", "system", "custom"}:
        return mode
    return "custom" if legacy_enabled else "off"


def _proxy_profile(value: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("代理配置格式无效")
    existing = existing or {}
    scheme = str(value.get("scheme") or existing.get("scheme") or "http").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("代理协议仅支持 HTTP、HTTPS、SOCKS5 或 SOCKS5H")
    host = str(value.get("host") or existing.get("host") or "").strip()
    if not host or any(char in host for char in "/?#@"):
        raise ValueError("代理主机无效")
    try:
        port = int(value.get("port") or existing.get("port"))
    except (TypeError, ValueError):
        raise ValueError("代理端口无效") from None
    if not 1 <= port <= 65535:
        raise ValueError("代理端口必须在 1 到 65535 之间")
    password = (
        existing.get("password", "")
        if value.get("keep_password") or "password" not in value
        else value.get("password", "")
    )
    return {
        "id": str(value.get("id") or existing.get("id") or uuid.uuid4().hex),
        "name": str(value.get("name") or existing.get("name") or "").strip(),
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": str(value.get("username") or "").strip(),
        "password": str(password or ""),
    }


def _private_config_dir() -> str:
    if hasattr(folder_paths, "get_system_user_directory"):
        return folder_paths.get_system_user_directory("missing_models_fetcher")
    return os.path.join(folder_paths.get_user_directory(), "__missing_models_fetcher")


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    if value.startswith("hf_") and len(value) > 10:
        return f"{value[:3]}****{value[-4:]}"
    return f"{value[:4]}****{value[-4:]}"


def _download_concurrency(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DOWNLOAD_CONCURRENCY
    return max(DOWNLOAD_CONCURRENCY_MIN, min(DOWNLOAD_CONCURRENCY_MAX, parsed))


def _provider_concurrency(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PROVIDER_CONCURRENCY
    return max(PROVIDER_CONCURRENCY_MIN, min(PROVIDER_CONCURRENCY_MAX, parsed))


def _bandwidth_limit_mbps(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BANDWIDTH_LIMIT_MBPS
    return max(0.0, min(100000.0, parsed))


@dataclass
class ConfigSnapshot:
    hf_api_key_masked: str
    civitai_api_key_masked: str
    modelscope_api_token_masked: str
    has_hf_api_key: bool
    has_civitai_api_key: bool
    has_modelscope_api_token: bool
    download_concurrency: int
    provider_concurrency: int
    bandwidth_limit_mbps: float
    proxy_enabled: bool
    proxy_mode: str
    proxy_url_masked: str
    has_proxy_url: bool
    proxy_scheme: str
    proxy_host: str
    proxy_port: int | None
    proxy_username: str
    has_proxy_password: bool
    proxy_profiles: list[dict[str, Any]]
    active_proxy_id: str
    storage_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hf_api_key_masked": self.hf_api_key_masked,
            "civitai_api_key_masked": self.civitai_api_key_masked,
            "modelscope_api_token_masked": self.modelscope_api_token_masked,
            "has_hf_api_key": self.has_hf_api_key,
            "has_civitai_api_key": self.has_civitai_api_key,
            "has_modelscope_api_token": self.has_modelscope_api_token,
            "download_concurrency": self.download_concurrency,
            "provider_concurrency": self.provider_concurrency,
            "bandwidth_limit_mbps": self.bandwidth_limit_mbps,
            "proxy_enabled": self.proxy_enabled,
            "proxy_mode": self.proxy_mode,
            "proxy_url_masked": self.proxy_url_masked,
            "has_proxy_url": self.has_proxy_url,
            "proxy_scheme": self.proxy_scheme,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
            "proxy_username": self.proxy_username,
            "has_proxy_password": self.has_proxy_password,
            "proxy_profiles": self.proxy_profiles,
            "active_proxy_id": self.active_proxy_id,
            "storage_dir": self.storage_dir,
        }


class SecretsStore:
    def __init__(self) -> None:
        self.config_dir = _private_config_dir()
        self.path = os.path.join(self.config_dir, "secrets.json")

    def load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

        result: dict[str, Any] = {}
        for key in SECRET_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value:
                result[key] = value
        result["download_concurrency"] = _download_concurrency(
            raw.get("download_concurrency")
        )
        result["provider_concurrency"] = _provider_concurrency(raw.get("provider_concurrency"))
        result["bandwidth_limit_mbps"] = _bandwidth_limit_mbps(raw.get("bandwidth_limit_mbps"))
        result["proxy_mode"] = _proxy_mode(raw.get("proxy_mode"), bool(raw.get("proxy_enabled")))
        result["proxy_enabled"] = result["proxy_mode"] == "custom"
        result["proxy_url"] = _proxy_url(raw.get("proxy_url"))
        profiles = []
        for item in raw.get("proxy_profiles", []):
            try:
                profiles.append(_proxy_profile(item))
            except ValueError:
                continue
        result["proxy_profiles"] = profiles
        result["active_proxy_id"] = str(raw.get("active_proxy_id") or "")
        return result

    def save(self, values: dict[str, Any]) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        clean = {
            key: value
            for key, value in values.items()
            if key in SECRET_KEYS and isinstance(value, str) and value
        }
        clean["download_concurrency"] = _download_concurrency(
            values.get("download_concurrency")
        )
        clean["provider_concurrency"] = _provider_concurrency(values.get("provider_concurrency"))
        clean["bandwidth_limit_mbps"] = _bandwidth_limit_mbps(values.get("bandwidth_limit_mbps"))
        clean["proxy_mode"] = _proxy_mode(values.get("proxy_mode"), bool(values.get("proxy_enabled")))
        clean["proxy_enabled"] = clean["proxy_mode"] == "custom"
        clean["proxy_url"] = _proxy_url(values.get("proxy_url"))
        clean["proxy_profiles"] = list(values.get("proxy_profiles") or [])
        clean["active_proxy_id"] = str(values.get("active_proxy_id") or "")

        fd, temp_path = tempfile.mkstemp(
            prefix="secrets-",
            suffix=".json",
            dir=self.config_dir,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(clean, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def update(self, data: dict[str, Any]) -> ConfigSnapshot:
        values = self.load()
        for key in SECRET_KEYS:
            if key in data:
                raw_value = data.get(key)
                if raw_value is None or str(raw_value).strip() == "":
                    values.pop(key, None)
                else:
                    values[key] = str(raw_value).strip()
        if "download_concurrency" in data:
            values["download_concurrency"] = _download_concurrency(
                data.get("download_concurrency")
            )
        if "provider_concurrency" in data:
            values["provider_concurrency"] = _provider_concurrency(data.get("provider_concurrency"))
        if "bandwidth_limit_mbps" in data:
            values["bandwidth_limit_mbps"] = _bandwidth_limit_mbps(data.get("bandwidth_limit_mbps"))
        if "proxy_enabled" in data:
            values["proxy_enabled"] = bool(data.get("proxy_enabled"))
            if "proxy_mode" not in data:
                values["proxy_mode"] = "custom" if values["proxy_enabled"] else "off"
        if "proxy_mode" in data:
            values["proxy_mode"] = _proxy_mode(data.get("proxy_mode"))
        if "proxy_url" in data:
            values["proxy_url"] = _proxy_url(data.get("proxy_url"))
        if "proxy_profiles" in data:
            existing_profiles = {item.get("id"): item for item in values.get("proxy_profiles", []) if isinstance(item, dict)}
            values["proxy_profiles"] = [
                _proxy_profile(item, existing_profiles.get(item.get("id")))
                for item in data.get("proxy_profiles", [])
                if isinstance(item, dict)
            ]
        if "active_proxy_id" in data:
            values["active_proxy_id"] = str(data.get("active_proxy_id") or "")
        self.save(values)
        return self.snapshot(values)

    def clear(self, provider: str | None = None) -> ConfigSnapshot:
        values = self.load()
        if provider == "hf":
            values.pop("hf_api_key", None)
        elif provider == "civitai":
            values.pop("civitai_api_key", None)
        elif provider == "modelscope":
            values.pop("modelscope_api_token", None)
        else:
            for key in SECRET_KEYS:
                values.pop(key, None)
        self.save(values)
        return self.snapshot(values)

    def snapshot(self, values: dict[str, Any] | None = None) -> ConfigSnapshot:
        values = values if values is not None else self.load()
        hf_key = values.get("hf_api_key", "")
        civitai_key = values.get("civitai_api_key", "")
        modelscope_token = values.get("modelscope_api_token", "")
        proxy_url = _proxy_url(values.get("proxy_url"))
        parsed_proxy = urlparse(proxy_url) if proxy_url else None
        proxy_mode = _proxy_mode(values.get("proxy_mode"), bool(values.get("proxy_enabled")))
        raw_profiles = list(values.get("proxy_profiles") or [])
        public_profiles = [
            {
                "id": item["id"], "name": item.get("name", ""), "scheme": item["scheme"],
                "host": item["host"], "port": item["port"], "username": item.get("username", ""),
                "has_password": bool(item.get("password")),
            }
            for item in raw_profiles
        ]
        return ConfigSnapshot(
            hf_api_key_masked=_mask_secret(hf_key),
            civitai_api_key_masked=_mask_secret(civitai_key),
            modelscope_api_token_masked=_mask_secret(modelscope_token),
            has_hf_api_key=bool(hf_key),
            has_civitai_api_key=bool(civitai_key),
            has_modelscope_api_token=bool(modelscope_token),
            download_concurrency=_download_concurrency(
                values.get("download_concurrency")
            ),
            provider_concurrency=_provider_concurrency(values.get("provider_concurrency")),
            bandwidth_limit_mbps=_bandwidth_limit_mbps(values.get("bandwidth_limit_mbps")),
            proxy_enabled=proxy_mode == "custom" and bool(proxy_url),
            proxy_mode=proxy_mode,
            proxy_url_masked=_proxy_url_masked(proxy_url),
            has_proxy_url=bool(proxy_url),
            proxy_scheme=parsed_proxy.scheme if parsed_proxy else "http",
            proxy_host=parsed_proxy.hostname or "" if parsed_proxy else "",
            proxy_port=parsed_proxy.port if parsed_proxy else None,
            proxy_username=parsed_proxy.username or "" if parsed_proxy else "",
            has_proxy_password=bool(parsed_proxy and parsed_proxy.password is not None),
            proxy_profiles=public_profiles,
            active_proxy_id=str(values.get("active_proxy_id") or ""),
            storage_dir=self.config_dir,
        )
