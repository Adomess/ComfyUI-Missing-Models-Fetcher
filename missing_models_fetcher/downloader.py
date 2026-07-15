from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .config import (
    DEFAULT_BANDWIDTH_LIMIT_MBPS,
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DEFAULT_PROVIDER_CONCURRENCY,
    DOWNLOAD_CONCURRENCY_MAX,
    SecretsStore,
)
from .folders import FolderRegistry
from .scanner import MODEL_EXTENSIONS, _filename_from_url
from .urls import strip_sensitive_query as _strip_sensitive_query


CHUNK_SIZE = 1024 * 1024
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 2.0
STATE_SAVE_INTERVAL_SECONDS = 1.0
STATE_REPLACE_RETRIES = 4
STATE_REPLACE_RETRY_DELAY_SECONDS = 0.05
DISK_RESERVE_MIN_BYTES = 16 * 1024 * 1024
DISK_RESERVE_MAX_BYTES = 256 * 1024 * 1024
SOURCE_RESOLUTION_TIMEOUT_SECONDS = 75.0
SAFETENSORS_HEADER_MAX_BYTES = 4 * 1024 * 1024
USER_AGENT = "ComfyUI-Missing-Models-Fetcher/0.1"
RESUME_ON_START_STATUSES = {"queued", "downloading", "verifying"}
KNOWN_STATUSES = {"queued", "downloading", "verifying", "paused", "completed", "canceled", "failed"}
PROVIDER_TEST_URLS = {
    "hf": "https://huggingface.co/api/whoami-v2",
    "civitai": "https://civitai.com/api/v1/models?limit=1&favorites=true",
    "modelscope": "https://modelscope.cn/openapi/v1/users/me",
}
PROVIDER_NAMES = {
    "hf": "Hugging Face",
    "civitai": "Civitai",
    "modelscope": "魔搭 ModelScope",
    "manual": "手动链接",
}
CIVITAI_PAGE_HOSTS = {"civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red"}
SAME_REPO_WARNING = (
    "仅确认魔搭中存在相同仓库 ID 和文件路径，未取得 SHA-256，"
    "无法验证内容完全一致，请确认来源后再下载。"
)
METADATA_MATCH_WARNING = (
    "Hugging Face 与魔搭的仓库 ID、文件路径和文件大小一致，"
    "但未取得可比对的 SHA-256，请确认来源后再下载。"
)
HASH_CONFLICT_WARNING = (
    "Hugging Face 与魔搭的仓库 ID、文件路径和文件大小一致，"
    "但 SHA-256 不一致，请确认来源后再下载。"
)
WEAK_FILENAME_WARNING = (
    "仅在同一仓库中找到文件名相同的候选，文件路径或文件大小不一致，"
    "可能不是同一个模型文件，请谨慎确认。"
)


def _host_matches(hostname: str | None, expected: str) -> bool:
    return bool(hostname) and (hostname == expected or hostname.endswith(f".{expected}"))


def _normalize_sha256(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else ""


def _parse_http_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("下载链接必须是有效的 HTTP 或 HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("下载链接不能包含用户名或密码")
    return parsed


def detect_provider(url: str) -> str:
    try:
        host = _parse_http_url(url).hostname.lower()
    except (AttributeError, ValueError):
        return "manual"
    if _host_matches(host, "huggingface.co"):
        return "hf"
    if host in CIVITAI_PAGE_HOSTS or _host_matches(host, "civitai.com"):
        return "civitai"
    if _host_matches(host, "modelscope.cn"):
        return "modelscope"
    return "manual"


def _is_modelscope_api_host(hostname: str | None) -> bool:
    return hostname in {"modelscope.cn", "www.modelscope.cn"}


def _civitai_model_version_id(url: str) -> str:
    parsed = _parse_http_url(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if hostname not in CIVITAI_PAGE_HOSTS and not _host_matches(hostname, "civitai.com"):
        return ""
    query = parse_qs(parsed.query)
    version_id = str(query.get("modelVersionId", [""])[0]).strip()
    if version_id.isdigit():
        return version_id
    match = re.fullmatch(r"/api/download/models/(\d+)/?", parsed.path)
    return match.group(1) if match else ""


def _parse_civitai_model_page(url: str) -> dict[str, str] | None:
    parsed = _parse_http_url(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if hostname not in CIVITAI_PAGE_HOSTS and not _host_matches(hostname, "civitai.com"):
        return None
    match = re.fullmatch(r"/models/(\d+)(?:/([^/?#]+))?/?", parsed.path)
    if not match:
        return None
    return {
        "model_id": match.group(1),
        "slug": unquote(match.group(2) or ""),
        "version_id": _civitai_model_version_id(url),
    }


def _parse_hf_file_url(url: str) -> dict[str, str] | None:
    parsed = _parse_http_url(url)
    if not _host_matches(parsed.hostname.lower(), "huggingface.co"):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 5:
        return None
    marker_index = next(
        (index for index, part in enumerate(parts) if part in {"blob", "resolve"}),
        -1,
    )
    if marker_index != 2 or len(parts) <= marker_index + 2:
        return None
    return {
        "repository": "/".join(parts[:2]),
        "revision": parts[marker_index + 1],
        "file_path": "/".join(parts[marker_index + 2 :]),
    }


def _parse_hf_repository_url(url: str) -> dict[str, str] | None:
    parsed = _parse_http_url(url)
    if not _host_matches(parsed.hostname.lower(), "huggingface.co"):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) == 2:
        return {"repository": "/".join(parts), "revision": "main"}
    if len(parts) == 4 and parts[2] == "tree":
        return {
            "repository": "/".join(parts[:2]),
            "revision": parts[3],
        }
    return None


def _parse_modelscope_file_url(url: str) -> dict[str, str] | None:
    parsed = _parse_http_url(url)
    if not _host_matches(parsed.hostname.lower(), "modelscope.cn"):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if len(parts) >= 6 and parts[0] == "models" and parts[3] == "resolve":
        return {
            "repository": "/".join(parts[1:3]),
            "revision": parts[4],
            "file_path": "/".join(parts[5:]),
        }
    if len(parts) == 6 and parts[:3] == ["api", "v1", "models"] and parts[5] == "repo":
        file_path = query.get("FilePath", query.get("file_path", [""]))[0]
        if file_path:
            return {
                "repository": "/".join(parts[3:5]),
                "revision": query.get("Revision", query.get("revision", ["master"]))[0] or "master",
                "file_path": unquote(file_path),
            }
    if len(parts) >= 7 and parts[0] == "models" and parts[3:5] == ["file", "view"]:
        return {
            "repository": "/".join(parts[1:3]),
            "revision": parts[5],
            "file_path": "/".join(parts[6:]),
        }
    return None


def _parse_modelscope_repository_url(url: str) -> dict[str, str] | None:
    parsed = _parse_http_url(url)
    if not _host_matches(parsed.hostname.lower(), "modelscope.cn"):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] != "models":
        return None
    if len(parts) >= 4 and parts[3] == "resolve":
        return None
    if len(parts) >= 5 and parts[3:5] == ["file", "view"]:
        return None
    if len(parts) > 4 or (len(parts) == 4 and parts[3] not in {"summary", "files"}):
        return None
    query = parse_qs(parsed.query)
    return {
        "repository": "/".join(parts[1:3]),
        "revision": query.get("Revision", query.get("revision", ["master"]))[0] or "master",
    }


def _build_hf_file_url(repository: str, revision: str, file_path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in file_path.split("/"))
    return f"https://huggingface.co/{repository}/resolve/{quote(revision, safe='')}/{encoded_path}"


def _build_modelscope_file_url(repository: str, revision: str, file_path: str) -> str:
    query = urlencode({"Revision": revision, "FilePath": file_path})
    return f"https://modelscope.cn/api/v1/models/{repository}/repo?{query}"


def _canonical_model_name(value: str) -> str:
    stem = os.path.splitext(os.path.basename(str(value or "")))[0].lower()
    return re.sub(r"[^a-z0-9]+", "", stem)


def _header_sha256(headers: dict[str, str]) -> str:
    for key in ("x-linked-etag", "etag"):
        value = str(headers.get(key) or "").strip().strip('"')
        if value.lower().startswith("sha256:"):
            value = value.split(":", 1)[1]
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return value.lower()
    return ""


def _build_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    return context


SSL_CONTEXT = _build_ssl_context()


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None

        old_host = urlparse(req.full_url).hostname
        new_host = urlparse(newurl).hostname
        if old_host != new_host:
            sensitive_headers = {"authorization", "cookie", "proxy-authorization"}
            for header_store in (redirected.headers, redirected.unredirected_hdrs):
                for header in list(header_store):
                    if header.lower() in sensitive_headers:
                        redirected.remove_header(header)
        return redirected


URL_OPENER = build_opener(
    _SafeRedirectHandler(),
    HTTPSHandler(context=SSL_CONTEXT),
)
DIRECT_URL_OPENER = build_opener(
    ProxyHandler({}),
    _SafeRedirectHandler(),
    HTTPSHandler(context=SSL_CONTEXT),
)
_PROXY_LOCK = threading.RLock()
_PROXY_OPENER = None
_PROXY_MODE = "off"


def _configure_proxy(mode: str, proxy_url: str, profile: dict[str, Any] | None = None) -> None:
    global _PROXY_MODE, _PROXY_OPENER
    with _PROXY_LOCK:
        _PROXY_MODE = mode if mode in {"off", "system", "custom"} else "off"
        _PROXY_OPENER = None
        if _PROXY_MODE == "custom" and profile and profile.get("scheme") in {"socks5", "socks5h"}:
            try:
                import socks
                from sockshandler import SocksiPyHandler
            except ImportError as exc:
                raise RuntimeError("SOCKS5 代理需要安装 PySocks") from exc
            _PROXY_OPENER = build_opener(
                SocksiPyHandler(
                    socks.SOCKS5,
                    profile["host"],
                    int(profile["port"]),
                    rdns=profile.get("scheme") == "socks5h",
                    username=profile.get("username") or None,
                    password=profile.get("password") or None,
                ),
                _SafeRedirectHandler(),
            )
        elif _PROXY_MODE == "custom" and proxy_url:
            _PROXY_OPENER = build_opener(
                ProxyHandler({"http": proxy_url, "https": proxy_url}),
                _SafeRedirectHandler(),
                HTTPSHandler(context=SSL_CONTEXT),
            )


class InsufficientDiskSpaceError(RuntimeError):
    pass


class HashMismatchError(ValueError):
    pass


class ResolutionCancelledError(RuntimeError):
    pass


def _open_url(request: Request, timeout: int):
    hostname = (urlparse(request.full_url).hostname or "").lower()
    with _PROXY_LOCK:
        if _PROXY_MODE == "system":
            opener = URL_OPENER
        elif _PROXY_MODE == "custom":
            opener = _PROXY_OPENER or DIRECT_URL_OPENER
        else:
            opener = DIRECT_URL_OPENER
    return opener.open(request, timeout=timeout)


def _is_hf_xet_cdn_url(url: str) -> bool:
    hostname = (urlparse(str(url or "")).hostname or "").lower()
    return _host_matches(hostname, "xethub.hf.co")


def _open_download_url(request_url: str, headers: dict[str, str], timeout: int = 60):
    """Open a download URL and refresh a rejected Hugging Face Xet signature once."""
    request = Request(request_url, headers=headers)
    try:
        return _open_url(request, timeout=timeout)
    except HTTPError as exc:
        origin_host = (urlparse(request_url).hostname or "").lower()
        failed_url = exc.geturl() if hasattr(exc, "geturl") else ""
        if (
            exc.code != 403
            or not _host_matches(origin_host, "huggingface.co")
            or not _is_hf_xet_cdn_url(failed_url)
        ):
            raise

        # The Hub can return a cached or otherwise rejected presigned Xet URL.
        # Never persist that URL: request a fresh redirect from the stable Hub URL.
        refresh_url = _append_query_param(request_url, "mmf_xet_retry", str(time.time_ns()))
        refresh_headers = dict(headers)
        refresh_headers["Cache-Control"] = "no-cache"
        refresh_headers["Pragma"] = "no-cache"
        return _open_url(Request(refresh_url, headers=refresh_headers), timeout=timeout)


@dataclass
class DownloadTask:
    id: str
    name: str
    url: str
    normalized_url: str
    directory: str
    destination_root: str
    target_path: str
    part_path: str
    provider: str = "manual"
    hash: str = ""
    hash_type: str = ""
    verified_hash: bool = False
    status: str = "queued"
    downloaded: int = 0
    total: int | None = None
    speed: float = 0.0
    eta: float | None = None
    verification_progress: float | None = None
    verification_speed: float = 0.0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    retries: int = 0
    pause_requested: bool = False
    cancel_requested: bool = False
    restart_required: bool = False
    priority: int = 0
    display_order: int = 0

    def to_dict(self) -> dict[str, Any]:
        progress = None
        if self.total:
            progress = min(100.0, self.downloaded / self.total * 100)
        return {
            "id": self.id,
            "name": self.name,
            "url": _strip_sensitive_query(self.url),
            "normalized_url": _strip_sensitive_query(self.normalized_url),
            "directory": self.directory,
            "destination_root": self.destination_root,
            "target_path": self.target_path,
            "part_path": self.part_path,
            "provider": self.provider,
            "hash": self.hash,
            "hash_type": self.hash_type,
            "verified_hash": self.verified_hash,
            "status": self.status,
            "downloaded": self.downloaded,
            "total": self.total,
            "progress": progress,
            "speed": self.speed,
            "eta": self.eta,
            "verification_progress": self.verification_progress,
            "verification_speed": self.verification_speed,
            "error": self.error,
            "retries": self.retries,
            "pause_requested": self.pause_requested,
            "cancel_requested": self.cancel_requested,
            "restart_required": self.restart_required,
            "priority": self.priority,
            "display_order": self.display_order,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadTask":
        required = ("id", "name", "url", "normalized_url", "directory", "destination_root", "target_path", "part_path")
        if any(not isinstance(data.get(key), str) or not data.get(key) for key in required):
            raise ValueError("下载任务状态文件缺少必要字段")
        status = data.get("status") if data.get("status") in KNOWN_STATUSES else "failed"
        return cls(
            id=data["id"],
            name=data["name"],
            url=_strip_sensitive_query(data["url"]),
            normalized_url=_strip_sensitive_query(data["normalized_url"]),
            directory=data["directory"],
            destination_root=data["destination_root"],
            target_path=data["target_path"],
            part_path=data["part_path"],
            provider=str(data.get("provider") or detect_provider(data["normalized_url"])),
            hash=str(data.get("hash") or ""),
            hash_type=str(data.get("hash_type") or ""),
            verified_hash=bool(data.get("verified_hash")),
            status=status,
            downloaded=int(data.get("downloaded") or 0),
            total=int(data["total"]) if data.get("total") is not None else None,
            speed=0.0,
            eta=None,
            verification_progress=(
                float(data["verification_progress"])
                if data.get("verification_progress") is not None
                else None
            ),
            verification_speed=0.0,
            error=str(data.get("error") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            retries=int(data.get("retries") or 0),
            restart_required=bool(data.get("restart_required")),
            priority=max(-1, min(1, int(data.get("priority") or 0))),
            display_order=max(0, int(data.get("display_order") or 0)),
        )


def normalize_download_url(url: str) -> str:
    cleaned = _strip_sensitive_query(url.strip())
    parsed = _parse_http_url(cleaned)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if _host_matches(hostname, "huggingface.co") and "/blob/" in parsed.path:
        path = parsed.path.replace("/blob/", "/resolve/", 1)
        return urlunparse(parsed._replace(path=path))

    if hostname in CIVITAI_PAGE_HOSTS or _host_matches(hostname, "civitai.com"):
        model_version_id = _civitai_model_version_id(cleaned)
        is_official_download = (
            _host_matches(hostname, "civitai.com")
            and re.fullmatch(r"/api/download/models/\d+/?", parsed.path) is not None
        )
        if model_version_id and not is_official_download:
            return f"https://civitai.com/api/download/models/{quote(model_version_id)}"

    return cleaned


def _content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", value)
    if not match or match.group(1) == "*":
        return None
    return int(match.group(1))


def _unsatisfied_range_total(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"bytes\s+\*/(\d+)", value)
    return int(match.group(1)) if match else None


def _header_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _header_map(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if any(existing_key.lower() == key.lower() for existing_key, _ in params):
        return url
    params.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(params)))


def _infer_hash_type(hash_value: str, hash_type: str = "") -> str:
    normalized_type = hash_type.strip().lower().replace("-", "")
    if normalized_type in {"sha256", "sha1", "md5"}:
        return normalized_type
    normalized_hash = hash_value.strip().lower()
    if len(normalized_hash) == 64:
        return "sha256"
    if len(normalized_hash) == 40:
        return "sha1"
    if len(normalized_hash) == 32:
        return "md5"
    return normalized_type


class DownloadManager:
    def __init__(self, secrets: SecretsStore, folders: FolderRegistry, state_dir: str | None = None) -> None:
        self.secrets = secrets
        self.folders = folders
        self.lock = threading.RLock()
        self.tasks: dict[str, DownloadTask] = {}
        self.queue: list[str] = []
        self.worker: threading.Thread | None = None
        self.workers: list[threading.Thread] = []
        self.max_concurrent_downloads = max(
            1,
            min(
                DOWNLOAD_CONCURRENCY_MAX,
                int(
                    self.secrets.load().get(
                        "download_concurrency",
                        DEFAULT_DOWNLOAD_CONCURRENCY,
                    )
                ),
            ),
        )
        runtime_config = self.secrets.load()
        self._apply_proxy_config(runtime_config)
        self.provider_concurrency = max(1, min(32, int(runtime_config.get("provider_concurrency", DEFAULT_PROVIDER_CONCURRENCY))))
        self.bandwidth_limit_mbps = max(0.0, float(runtime_config.get("bandwidth_limit_mbps", DEFAULT_BANDWIDTH_LIMIT_MBPS)))
        self._active_provider_counts: dict[str, int] = {}
        self._active_task_ids: set[str] = set()
        self._bandwidth_lock = threading.Lock()
        self._bandwidth_next_at = time.monotonic()
        self._source_cache_lock = threading.RLock()
        self._source_cache_ttl = 300.0
        self._source_cache: dict[tuple[str, Any], tuple[float, Any]] = {}
        self._source_inflight: dict[tuple[str, Any], threading.Event] = {}
        self._source_resolution_local = threading.local()
        self._resolution_jobs_lock = threading.RLock()
        self._resolution_jobs: dict[str, threading.Event] = {}
        self.state_dir = state_dir or getattr(
            secrets,
            "config_dir",
            os.path.join(tempfile.gettempdir(), "comfyui_missing_models_fetcher"),
        )
        self.state_path = os.path.join(self.state_dir, "downloads.json")
        self.source_index_path = os.path.join(self.state_dir, "source_index.json")
        self._source_index: dict[str, dict[str, dict[str, Any]]] = self._load_source_index()
        self._last_state_save = 0.0
        with self.lock:
            self._load_state_locked()
            if self.tasks:
                self._save_state_locked(force=True)
            if self.queue:
                self._ensure_worker_locked()

    def _cached_source_value(
        self,
        namespace: str,
        key: Any,
        loader: Callable[[], Any],
    ) -> Any:
        cache_key = (namespace, key)
        now = time.monotonic()
        with self._source_cache_lock:
            if len(self._source_cache) > 256:
                expired_keys = [
                    cached_key
                    for cached_key, (expires_at, _) in self._source_cache.items()
                    if expires_at <= now
                ]
                for expired_key in expired_keys:
                    self._source_cache.pop(expired_key, None)
            cached = self._source_cache.get(cache_key)
            if cached and cached[0] > now:
                self._resolution_metric("cache_hits")
                return copy.deepcopy(cached[1])
            if cached:
                self._source_cache.pop(cache_key, None)
            inflight = self._source_inflight.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                self._source_inflight[cache_key] = inflight
                owner = True
                self._resolution_metric("cache_misses")
            else:
                owner = False
                self._resolution_metric("coalesced_requests")

        if not owner:
            if not inflight.wait(timeout=45):
                raise TimeoutError("等待相同下载源请求超时")
            with self._source_cache_lock:
                cached = self._source_cache.get(cache_key)
                if cached and cached[0] > time.monotonic():
                    return copy.deepcopy(cached[1])
            raise RuntimeError("相同下载源请求未返回可用结果")

        try:
            value = loader()
        except Exception:
            with self._source_cache_lock:
                self._source_inflight.pop(cache_key, None)
                inflight.set()
            raise

        with self._source_cache_lock:
            self._source_cache[cache_key] = (
                time.monotonic() + self._source_cache_ttl,
                copy.deepcopy(value),
            )
            self._source_inflight.pop(cache_key, None)
            inflight.set()
        return copy.deepcopy(value)

    def _resolution_metric(self, name: str, amount: int = 1) -> None:
        metrics = getattr(self._source_resolution_local, "metrics", None)
        if isinstance(metrics, dict):
            metrics[name] = int(metrics.get(name, 0)) + amount

    def _load_source_index(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            with open(self.source_index_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, dict):
            return {}
        clean: dict[str, dict[str, dict[str, Any]]] = {}
        for sha256, providers in raw_entries.items():
            normalized_sha = _normalize_sha256(sha256)
            if not normalized_sha or not isinstance(providers, dict):
                continue
            for provider, source in providers.items():
                if provider not in {"hf", "modelscope", "civitai"} or not isinstance(source, dict):
                    continue
                url = _strip_sensitive_query(str(source.get("url") or ""))
                if not url:
                    continue
                clean.setdefault(normalized_sha, {})[provider] = {
                    "provider": provider,
                    "url": url,
                    "repository": str(source.get("repository") or ""),
                    "revision": str(source.get("revision") or ""),
                    "file_path": str(source.get("file_path") or ""),
                    "size": source.get("size"),
                    "sha256": normalized_sha,
                    "hash_source": str(source.get("hash_source") or "trusted_index"),
                    "updated_at": float(source.get("updated_at") or 0),
                }
        return clean

    def _save_source_index(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="source-index-", suffix=".json", dir=self.state_dir, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "entries": self._source_index}, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.source_index_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _record_trusted_sources(self, sources: list[dict[str, Any]]) -> None:
        changed = False
        with self._source_cache_lock:
            for source in sources:
                sha256 = _normalize_sha256(source.get("sha256"))
                provider = str(source.get("provider") or "")
                if not sha256 or provider not in {"hf", "modelscope", "civitai"}:
                    continue
                if source.get("verification") != "hash_verified" or source.get("selectable") is False:
                    continue
                self._source_index.setdefault(sha256, {})[provider] = {
                    "provider": provider,
                    "url": _strip_sensitive_query(str(source.get("url") or "")),
                    "repository": str(source.get("repository") or ""),
                    "revision": str(source.get("revision") or ""),
                    "file_path": str(source.get("file_path") or ""),
                    "size": source.get("size"),
                    "sha256": sha256,
                    "hash_source": str(source.get("hash_source") or "trusted_index"),
                    "updated_at": time.time(),
                }
                changed = True
            if changed:
                if len(self._source_index) > 2048:
                    ordered_hashes = sorted(
                        self._source_index,
                        key=lambda digest: max(
                            (float(entry.get("updated_at") or 0) for entry in self._source_index[digest].values()),
                            default=0,
                        ),
                    )
                    for digest in ordered_hashes[: len(self._source_index) - 2048]:
                        self._source_index.pop(digest, None)
                try:
                    self._save_source_index()
                except OSError as exc:
                    logging.warning("[Missing Models Fetcher] 无法保存可信来源索引: %s", exc)

    def _indexed_source(self, item: dict[str, Any], provider: str) -> dict[str, Any] | None:
        sha256 = self._expected_sha256(item)
        if not sha256:
            for source in item.get("sources") or []:
                if isinstance(source, dict) and source.get("verification") == "hash_verified":
                    sha256 = _normalize_sha256(source.get("sha256"))
                    if sha256:
                        break
        with self._source_cache_lock:
            indexed = copy.deepcopy(self._source_index.get(sha256, {}).get(provider)) if sha256 else None
        if not indexed:
            return None
        indexed.update({
            "verification": "hash_verified",
            "warning": "",
            "warning_level": "",
            "selectable": True,
            "blocked_reason": "",
            "hash_source": indexed.get("hash_source") or "trusted_index",
        })
        self._resolution_metric("cache_hits")
        return indexed

    @staticmethod
    def _disk_reserve_bytes(required: int) -> int:
        return min(
            DISK_RESERVE_MAX_BYTES,
            max(DISK_RESERVE_MIN_BYTES, required // 20),
        )

    @staticmethod
    def _format_byte_count(value: int) -> str:
        size = float(max(0, value))
        units = ("B", "KB", "MB", "GB", "TB")
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        return f"{size:.2f} {units[index]}" if index else f"{int(size)} B"

    @staticmethod
    def _filesystem_capacity(path: str) -> tuple[str, int] | None:
        probe = os.path.abspath(os.path.dirname(path) or path)
        while not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if not parent or parent == probe:
                return None
            probe = parent
        try:
            free = int(shutil.disk_usage(probe).free)
            try:
                key = f"device:{os.stat(probe).st_dev}"
            except OSError:
                drive = os.path.splitdrive(probe)[0]
                key = f"path:{os.path.normcase(drive or probe)}"
            return key, free
        except OSError:
            return None

    def _check_batch_disk_space(self, items: list[dict[str, Any]]) -> None:
        required_by_filesystem: dict[str, int] = {}
        free_by_filesystem: dict[str, int] = {}
        names_by_filesystem: dict[str, list[str]] = {}
        seen_targets: set[str] = set()
        for item in items:
            target_path = str(item["target_path"])
            normalized_target = os.path.normcase(os.path.abspath(target_path))
            if normalized_target in seen_targets:
                continue
            seen_targets.add(normalized_target)
            if self._find_existing_task_locked(target_path) is not None:
                continue
            if os.path.isfile(target_path):
                continue
            expected_size = item.get("size")
            if expected_size in {None, ""}:
                continue
            part_path = f"{target_path}.part"
            part_size = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            required = max(int(expected_size) - part_size, 0)
            if required <= 0:
                continue
            capacity = self._filesystem_capacity(target_path)
            if capacity is None:
                continue
            filesystem, free = capacity
            required_by_filesystem[filesystem] = required_by_filesystem.get(filesystem, 0) + required
            free_by_filesystem[filesystem] = min(free_by_filesystem.get(filesystem, free), free)
            names_by_filesystem.setdefault(filesystem, []).append(str(item["name"]))

        for filesystem, required in required_by_filesystem.items():
            reserve = self._disk_reserve_bytes(required)
            free = free_by_filesystem[filesystem]
            if free < required + reserve:
                names = "、".join(names_by_filesystem[filesystem][:3])
                raise InsufficientDiskSpaceError(
                    f"保存磁盘空间不足，无法加入 {names}。"
                    f"还需写入 {self._format_byte_count(required)}，"
                    f"并预留 {self._format_byte_count(reserve)}；"
                    f"当前可用 {self._format_byte_count(free)}。"
                )

    def _check_task_disk_space(
        self,
        task: DownloadTask,
        total: int | None,
        existing_part_size: int,
    ) -> None:
        if total is None:
            return
        required = max(int(total) - max(0, existing_part_size), 0)
        if required <= 0:
            return
        capacity = self._filesystem_capacity(task.target_path)
        if capacity is None:
            return
        _, free = capacity
        reserve = self._disk_reserve_bytes(required)
        if free < required + reserve:
            raise InsufficientDiskSpaceError(
                f"{task.name} 的保存磁盘空间不足。"
                f"还需写入 {self._format_byte_count(required)}，"
                f"并预留 {self._format_byte_count(reserve)}；"
                f"当前可用 {self._format_byte_count(free)}。"
            )

    def enqueue(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        created: list[DownloadTask] = []
        with self.lock:
            resolved_items: list[dict[str, Any]] = []
            for item in items:
                blocked_reason = str(item.get("blocked_reason") or "").strip()
                if item.get("selectable") is False or blocked_reason:
                    raise ValueError(blocked_reason or "所选下载源已被安全校验阻止")
                url = str(item.get("url", "")).strip()
                if not url:
                    raise ValueError("下载链接不能为空")
                normalized_url = normalize_download_url(url)
                public_url = _strip_sensitive_query(url)
                provider = detect_provider(normalized_url)
                name = str(item.get("name") or _filename_from_url(normalized_url)).strip()
                if not name:
                    raise ValueError("无法从任务中确定模型文件名")
                workflow_hash = self._expected_sha256(
                    {
                        "hash": item.get("workflow_hash"),
                        "hash_type": item.get("workflow_hash_type"),
                    }
                )
                source_sha256 = _normalize_sha256(item.get("source_sha256"))
                if workflow_hash and source_sha256 != workflow_hash:
                    if source_sha256:
                        raise ValueError(
                            f"{name} 的来源 SHA-256 与工作流要求不一致，禁止下载"
                        )
                    raise ValueError(
                        f"{name} 的来源未提供可验证的 SHA-256，无法满足工作流完整性要求"
                    )
                trusted_source_hash = (
                    source_sha256
                    if str(item.get("hash_source") or "")
                    in {"hf_lfs_oid", "modelscope_file_sha256"}
                    else ""
                )
                validation_hash = (
                    workflow_hash
                    or trusted_source_hash
                    or str(item.get("hash") or "").strip()
                )
                validation_hash_type = (
                    "sha256"
                    if _normalize_sha256(validation_hash)
                    else str(item.get("hash_type") or item.get("hashType") or "").strip()
                )
                directory, destination_root, target_path = self.folders.resolve_destination(
                    str(item.get("directory", "")).strip(),
                    name,
                    item.get("destination_path") or item.get("destinationRoot"),
                )
                raw_size = item.get("size")
                try:
                    expected_size = int(raw_size) if raw_size not in {None, ""} else None
                except (TypeError, ValueError):
                    expected_size = None
                if expected_size is not None and expected_size <= 0:
                    expected_size = None
                resolved_items.append(
                    {
                        "name": name,
                        "url": public_url,
                        "normalized_url": normalized_url,
                        "provider": provider,
                        "directory": directory,
                        "destination_root": destination_root,
                        "target_path": target_path,
                        "hash": validation_hash,
                        "hash_type": validation_hash_type,
                        "size": expected_size,
                    }
                )

            self._check_batch_disk_space(resolved_items)
            for resolved in resolved_items:
                target_path = resolved["target_path"]
                existing = self._find_existing_task_locked(target_path)
                if existing is not None:
                    if existing.status == "paused":
                        existing.status = "queued"
                        existing.error = ""
                        existing.pause_requested = False
                        existing.cancel_requested = False
                        existing.updated_at = time.time()
                        if existing.id not in self.queue:
                            self.queue.append(existing.id)
                    created.append(existing)
                    continue
                task = DownloadTask(
                    id=uuid.uuid4().hex,
                    name=resolved["name"],
                    url=resolved["url"],
                    normalized_url=resolved["normalized_url"],
                    directory=resolved["directory"],
                    destination_root=resolved["destination_root"],
                    target_path=target_path,
                    part_path=f"{target_path}.part",
                    provider=resolved["provider"],
                    hash=resolved["hash"],
                    hash_type=resolved["hash_type"],
                    total=resolved["size"],
                    display_order=self._next_display_order_locked(),
                )
                self.tasks[task.id] = task
                self.queue.append(task.id)
                created.append(task)
            self._ensure_worker_locked()
            self._save_state_locked(force=True)
        return [task.to_dict() for task in created]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tasks = sorted(
                self.tasks.values(),
                key=lambda task: (task.display_order, task.created_at),
            )
            active_workers = sum(worker.is_alive() for worker in self.workers)
            active_tasks = [task for task in tasks if task.status in {"downloading", "verifying"}]
            total_speed = sum(
                task.verification_speed if task.status == "verifying" else task.speed
                for task in active_tasks
            )
            remaining = sum(
                max((task.total or task.downloaded) - task.downloaded, 0)
                for task in tasks
                if task.status in {"queued", "downloading"} and task.total is not None
            )
            return {
                "tasks": [task.to_dict() for task in tasks],
                "active": active_workers > 0,
                "active_workers": active_workers,
                "download_concurrency": self.max_concurrent_downloads,
                "provider_concurrency": self.provider_concurrency,
                "bandwidth_limit_mbps": self.bandwidth_limit_mbps,
                "summary": {
                    "total_speed": total_speed,
                    "eta": remaining / total_speed if total_speed > 0 else None,
                    "queued": sum(task.status == "queued" for task in tasks),
                    "active": len(active_tasks),
                    "paused": sum(task.status == "paused" for task in tasks),
                },
            }

    def set_concurrency(self, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = DEFAULT_DOWNLOAD_CONCURRENCY
        with self.lock:
            self.max_concurrent_downloads = max(
                1,
                min(DOWNLOAD_CONCURRENCY_MAX, parsed),
            )
            self._ensure_worker_locked()
            return self.max_concurrent_downloads

    def set_runtime_limits(self, provider_concurrency: Any, bandwidth_limit_mbps: Any) -> None:
        try:
            provider_limit = int(provider_concurrency)
        except (TypeError, ValueError):
            provider_limit = DEFAULT_PROVIDER_CONCURRENCY
        try:
            bandwidth = float(bandwidth_limit_mbps)
        except (TypeError, ValueError):
            bandwidth = DEFAULT_BANDWIDTH_LIMIT_MBPS
        with self.lock:
            self.provider_concurrency = max(1, min(32, provider_limit))
            self.bandwidth_limit_mbps = max(0.0, min(100000.0, bandwidth))
            with self._bandwidth_lock:
                self._bandwidth_next_at = time.monotonic()
            self._ensure_worker_locked()

    def reload_proxy_config(self) -> None:
        config = self.secrets.load()
        self._apply_proxy_config(config)

    def test_proxy_profile(self, profile_id: str) -> dict[str, Any]:
        config = self.secrets.load()
        profile = next(
            (item for item in config.get("proxy_profiles", []) if item.get("id") == profile_id),
            None,
        )
        if profile is None:
            raise ValueError("代理配置不存在")
        scheme = profile.get("scheme")
        if scheme in {"socks5", "socks5h"}:
            try:
                import socks
                from sockshandler import SocksiPyHandler
            except ImportError as exc:
                raise RuntimeError("SOCKS5 代理需要安装 PySocks") from exc
            opener = build_opener(
                SocksiPyHandler(
                    socks.SOCKS5, profile["host"], int(profile["port"]),
                    rdns=scheme == "socks5h", username=profile.get("username") or None,
                    password=profile.get("password") or None,
                ),
                _SafeRedirectHandler(),
            )
        else:
            username = quote(str(profile.get("username") or ""), safe="")
            password = quote(str(profile.get("password") or ""), safe="")
            auth = f"{username}:{password}@" if username else ""
            proxy_url = f"{scheme}://{auth}{profile['host']}:{profile['port']}"
            opener = build_opener(
                ProxyHandler({"http": proxy_url, "https": proxy_url}),
                _SafeRedirectHandler(), HTTPSHandler(context=SSL_CONTEXT),
            )
        started = time.monotonic()
        request = Request("https://huggingface.co/api/models?limit=1", headers={"User-Agent": USER_AGENT})
        with opener.open(request, timeout=12) as response:
            response.read(1)
        return {"available": True, "latency_ms": round((time.monotonic() - started) * 1000)}

    @staticmethod
    def _apply_proxy_config(config: dict[str, Any]) -> None:
        profiles = [item for item in config.get("proxy_profiles", []) if isinstance(item, dict)]
        active_id = str(config.get("active_proxy_id") or "")
        profile = next((item for item in profiles if item.get("id") == active_id), None)
        proxy_url = str(config.get("proxy_url") or "")
        if profile and profile.get("scheme") in {"http", "https"}:
            username = quote(str(profile.get("username") or ""), safe="")
            password = quote(str(profile.get("password") or ""), safe="")
            auth = f"{username}:{password}@" if username else ""
            proxy_url = f"{profile['scheme']}://{auth}{profile['host']}:{profile['port']}"
        _configure_proxy(
            str(config.get("proxy_mode") or "off"),
            proxy_url,
            profile,
        )

    def _throttle_download(self, byte_count: int) -> None:
        bytes_per_second = self.bandwidth_limit_mbps * 1024 * 1024
        if bytes_per_second <= 0 or byte_count <= 0:
            return
        duration = byte_count / bytes_per_second
        with self._bandwidth_lock:
            now = time.monotonic()
            start_at = max(now, self._bandwidth_next_at)
            finish_at = start_at + duration
            self._bandwidth_next_at = finish_at
            wait = finish_at - now
        if wait > 0:
            time.sleep(wait)

    def bulk_control(self, action: str) -> dict[str, Any]:
        if action not in {"pause", "resume"}:
            raise ValueError("不支持的批量任务操作")
        if action == "resume":
            with self.lock:
                for task in self.tasks.values():
                    if task.status != "paused":
                        continue
                    task.status = "queued"
                    task.error = ""
                    task.pause_requested = False
                    task.cancel_requested = False
                    task.updated_at = time.time()
                    if task.id not in self.queue:
                        self.queue.append(task.id)
                self._sort_queue_by_priority_locked()
                self._ensure_worker_locked()
                self._save_state_locked(force=True)
            return self.snapshot()
        with self.lock:
            task_ids = list(self.tasks)
        for task_id in task_ids:
            with self.lock:
                task = self.tasks.get(task_id)
                if task is None:
                    continue
                eligible = (
                    task.status in {"queued", "downloading", "verifying"}
                    if action == "pause"
                    else task.status == "paused"
                )
            if eligible:
                getattr(self, action)(task_id)
        return self.snapshot()

    def move_queued_task(self, task_id: str, direction: str) -> dict[str, Any]:
        if direction not in {"up", "down"}:
            raise ValueError("队列移动方向无效")
        with self.lock:
            if task_id not in self.queue:
                raise ValueError("只有排队中的任务可以调整顺序")
            index = self.queue.index(task_id)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(self.queue):
                other_id = self.queue[target]
                self.queue[index], self.queue[target] = other_id, task_id
                task = self.tasks.get(task_id)
                other = self.tasks.get(other_id)
                if task is not None and other is not None:
                    task.display_order, other.display_order = other.display_order, task.display_order
                self._save_state_locked(force=True)
            return self.snapshot()

    def _next_display_order_locked(self) -> int:
        return max((task.display_order for task in self.tasks.values()), default=-1) + 1

    def set_task_priority(self, task_id: str, priority: Any) -> dict[str, Any]:
        try:
            clean_priority = max(-1, min(1, int(priority)))
        except (TypeError, ValueError):
            raise ValueError("任务优先级无效") from None
        with self.lock:
            task = self._get_task(task_id)
            task.priority = clean_priority
            task.updated_at = time.time()
            self._sort_queue_by_priority_locked()
            self._save_state_locked(force=True)
            return self.snapshot()

    def _sort_queue_by_priority_locked(self) -> None:
        queue_positions = {queued_id: index for index, queued_id in enumerate(self.queue)}
        self.queue.sort(
            key=lambda queued_id: (
                -int(self.tasks[queued_id].priority) if queued_id in self.tasks else 0,
                queue_positions.get(queued_id, len(queue_positions)),
            )
        )

    def clear(
        self,
        statuses: set[str] | None = None,
        task_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        removable_statuses = {"completed", "failed", "canceled"}
        target_statuses = (statuses or removable_statuses) & removable_statuses
        with self.lock:
            active_ids = {
                task_id
                for task_id, task in self.tasks.items()
                if task.status in {"queued", "downloading", "verifying", "paused"}
                or task_id in self.queue
                or task_id in self._active_task_ids
            }
            removable_ids = [
                task_id
                for task_id, task in self.tasks.items()
                if task_id not in active_ids
                and task.status in target_statuses
                and (task_ids is None or task_id in task_ids)
            ]
            for task_id in removable_ids:
                self.tasks.pop(task_id, None)
            self._save_state_locked(force=True)
            return self.snapshot()

    def remote_metadata(self, url: str) -> dict[str, Any]:
        normalized_url = normalize_download_url(url)
        cache_key = _strip_sensitive_query(normalized_url)
        return self._cached_source_value(
            "remote-metadata",
            cache_key,
            lambda: self._remote_metadata_uncached(url),
        )

    def _remote_metadata_uncached(self, url: str) -> dict[str, Any]:
        normalized_url = normalize_download_url(url)
        request_url, headers = self._request_context(normalized_url)
        provider = detect_provider(normalized_url)
        hf_ref = _parse_hf_file_url(normalized_url) if provider == "hf" else None
        modelscope_ref = _parse_modelscope_file_url(normalized_url)
        civitai_metadata: dict[str, Any] = {}
        civitai_version_id = _civitai_model_version_id(normalized_url)
        if civitai_version_id:
            try:
                civitai_metadata = self._civitai_file_metadata(civitai_version_id)
            except Exception:
                civitai_metadata = {}
        filename = (
            os.path.basename(modelscope_ref["file_path"])
            if modelscope_ref
            else os.path.basename(hf_ref["file_path"])
            if hf_ref
            else str(civitai_metadata.get("filename") or "")
            if civitai_metadata
            else _filename_from_url(normalized_url)
        )
        response_headers: dict[str, str] = {}
        final_url = normalized_url
        hf_metadata: dict[str, Any] = {}
        if hf_ref:
            try:
                hf_metadata = self._hf_file_metadata(hf_ref)
            except Exception:
                hf_metadata = {}

        try:
            request = Request(request_url, headers=headers, method="HEAD")
            with _open_url(request, timeout=30) as response:
                response_headers = _header_map(response.headers)
                final_url = response.geturl()
        except HTTPError as exc:
            try:
                if exc.code not in {400, 403, 404, 405, 501}:
                    raise
                response_headers, final_url = self._range_probe(request_url, headers)
            except Exception:
                if not hf_metadata and not civitai_metadata:
                    raise
        except Exception:
            try:
                response_headers, final_url = self._range_probe(request_url, headers)
            except Exception:
                if not hf_metadata and not civitai_metadata:
                    raise

        size = _content_range_total(response_headers.get("content-range"))
        if size is None:
            size = _header_int(response_headers.get("content-length"))
        sha256 = _header_sha256(response_headers) if provider != "hf" else ""
        hash_source = "http_etag_sha256" if sha256 else ""
        if hf_metadata:
            if hf_metadata.get("size") is not None:
                size = hf_metadata["size"]
            sha256 = str(hf_metadata.get("sha256") or "")
            hash_source = str(hf_metadata.get("hash_source") or "")
        if civitai_metadata:
            if civitai_metadata.get("size") is not None:
                size = civitai_metadata["size"]
            sha256 = str(civitai_metadata.get("sha256") or "")
            hash_source = str(civitai_metadata.get("hash_source") or "")
        return {
            "url": _strip_sensitive_query(url),
            "normalized_url": _strip_sensitive_query(normalized_url),
            "final_url": _strip_sensitive_query(final_url),
            "filename": filename or _filename_from_url(final_url),
            "size": size,
            "accept_ranges": response_headers.get("accept-ranges", ""),
            "content_type": response_headers.get("content-type", ""),
            "sha256": sha256,
            "hash_source": hash_source,
            "directory": str(civitai_metadata.get("directory") or ""),
            "display_name": str(civitai_metadata.get("display_name") or ""),
            "version_name": str(civitai_metadata.get("version_name") or ""),
            "version_id": str(civitai_metadata.get("version_id") or ""),
        }

    def _civitai_file_metadata(self, version_id: str) -> dict[str, Any]:
        url = f"https://civitai.com/api/v1/model-versions/{quote(version_id, safe='')}"
        request_url, headers = self._request_context(url)
        request = Request(
            request_url,
            headers={**headers, "Accept": "application/json"},
            method="GET",
        )
        with _open_url(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Civitai 模型版本接口返回了无效数据")
        model = payload.get("model")
        model = model if isinstance(model, dict) else {}
        return self._civitai_metadata_from_version(payload, model)

    def _civitai_model_metadata(self, model_id: str) -> dict[str, Any]:
        url = f"https://civitai.com/api/v1/models/{quote(model_id, safe='')}"
        request_url, headers = self._request_context(url)
        request = Request(
            request_url,
            headers={**headers, "Accept": "application/json"},
            method="GET",
        )
        try:
            with _open_url(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError(f"Civitai 模型 ID {model_id} 不存在，请检查页面链接") from exc
            raise
        if not isinstance(payload, dict):
            raise ValueError("Civitai 模型接口返回了无效数据")
        return payload

    def _civitai_metadata_from_version(
        self,
        version: dict[str, Any],
        model: dict[str, Any],
    ) -> dict[str, Any]:
        files = version.get("files")
        files = files if isinstance(files, list) else []
        candidates = [item for item in files if isinstance(item, dict)]
        file_info = next((item for item in candidates if item.get("primary") is True), None)
        if file_info is None:
            file_info = next(
                (item for item in candidates if str(item.get("type") or "").lower() == "model"),
                candidates[0] if candidates else None,
            )
        if file_info is None:
            raise ValueError("Civitai 模型版本中没有可下载文件")
        hashes = file_info.get("hashes")
        hashes = hashes if isinstance(hashes, dict) else {}
        sha256 = _normalize_sha256(hashes.get("SHA256") or hashes.get("sha256"))
        size_kb = file_info.get("sizeKB")
        try:
            size = round(float(size_kb) * 1024) if size_kb not in {None, ""} else None
        except (TypeError, ValueError):
            size = None
        model_type = str(model.get("type") or "").strip().lower()
        directory_hint = {
            "lora": "loras",
            "locon": "loras",
            "lycoris": "loras",
            "checkpoint": "checkpoints",
            "vae": "vae",
            "controlnet": "controlnet",
            "upscaler": "upscale_models",
            "embedding": "embeddings",
        }.get(model_type, "")
        directory = self.folders.normalize_directory(directory_hint) or ""
        return {
            "filename": str(file_info.get("name") or "").strip(),
            "size": size,
            "sha256": sha256,
            "hash_source": "civitai_file_sha256" if sha256 else "",
            "directory": directory,
            "display_name": str(model.get("name") or "").strip(),
            "version_name": str(version.get("name") or "").strip(),
            "version_id": str(version.get("id") or "").strip(),
        }

    def test_api_key(self, provider: str, api_key: str | None = None) -> dict[str, Any]:
        if provider not in PROVIDER_TEST_URLS:
            raise ValueError("未知的 API Key 类型")

        stored = self.secrets.load()
        secret_name = {
            "hf": "hf_api_key",
            "civitai": "civitai_api_key",
            "modelscope": "modelscope_api_token",
        }[provider]
        key = (api_key or stored.get(secret_name) or "").strip()
        if not key:
            provider_name = PROVIDER_NAMES[provider]
            raise ValueError(f"尚未输入或保存 {provider_name} API Key")

        request = Request(
            PROVIDER_TEST_URLS[provider],
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": USER_AGENT,
            },
        )
        with _open_url(request, timeout=30) as response:
            account = ""
            if provider in {"hf", "modelscope"}:
                try:
                    payload = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict):
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                    account = str(
                        data.get("username")
                        or data.get("name")
                        or data.get("fullname")
                        or ""
                    ).strip()
            else:
                response.read(1)

        return {
            "provider": provider,
            "valid": True,
            "account": account,
            "using_saved_key": api_key is None or not api_key.strip(),
        }

    def resolve_sources(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean_items = [item for item in items if isinstance(item, dict)]
        if not clean_items:
            return []
        workers = min(4, len(clean_items))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mmf-sources") as executor:
            return list(executor.map(self._resolve_source_item_with_deadline, clean_items))

    def resolve_source_provider(
        self,
        item: dict[str, Any],
        target_provider: str,
        job_id: str = "",
    ) -> dict[str, Any]:
        provider = str(target_provider or "").strip().lower()
        if provider not in {"hf", "modelscope", "civitai"}:
            raise ValueError("不支持的下载站点")
        previous_deadline = getattr(self._source_resolution_local, "deadline", None)
        previous_cancel_event = getattr(self._source_resolution_local, "cancel_event", None)
        previous_metrics = getattr(self._source_resolution_local, "metrics", None)
        cancel_event = threading.Event()
        clean_job_id = str(job_id or "").strip()
        if clean_job_id:
            with self._resolution_jobs_lock:
                previous_job = self._resolution_jobs.get(clean_job_id)
                if previous_job is not None:
                    previous_job.set()
                self._resolution_jobs[clean_job_id] = cancel_event
        started_at = time.monotonic()
        metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "coalesced_requests": 0,
            "network_requests": 0,
            "repositories_checked": 0,
        }
        self._source_resolution_local.deadline = (
            time.monotonic() + SOURCE_RESOLUTION_TIMEOUT_SECONDS
        )
        self._source_resolution_local.cancel_event = cancel_event
        self._source_resolution_local.metrics = metrics
        try:
            result = self._resolve_source_provider_safe(item, provider)
            sources = result.get("sources") if isinstance(result, dict) else None
            source = sources[0] if isinstance(sources, list) and sources else {}
            result["diagnostics"] = {
                **metrics,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                "match_method": str(source.get("verification") or "not_found"),
                "canceled": cancel_event.is_set(),
            }
            return result
        finally:
            if clean_job_id:
                with self._resolution_jobs_lock:
                    if self._resolution_jobs.get(clean_job_id) is cancel_event:
                        self._resolution_jobs.pop(clean_job_id, None)
            if previous_deadline is None:
                try:
                    del self._source_resolution_local.deadline
                except AttributeError:
                    pass
            else:
                self._source_resolution_local.deadline = previous_deadline
            if previous_cancel_event is None:
                try:
                    del self._source_resolution_local.cancel_event
                except AttributeError:
                    pass
            else:
                self._source_resolution_local.cancel_event = previous_cancel_event
            if previous_metrics is None:
                try:
                    del self._source_resolution_local.metrics
                except AttributeError:
                    pass
            else:
                self._source_resolution_local.metrics = previous_metrics

    def cancel_resolution_jobs(self, job_ids: list[str]) -> int:
        canceled = 0
        with self._resolution_jobs_lock:
            for job_id in job_ids:
                event = self._resolution_jobs.get(str(job_id or "").strip())
                if event is not None and not event.is_set():
                    event.set()
                    canceled += 1
        return canceled

    def _check_source_resolution_deadline(self) -> None:
        cancel_event = getattr(self._source_resolution_local, "cancel_event", None)
        if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
            raise ResolutionCancelledError("下载源解析已取消")
        deadline = getattr(self._source_resolution_local, "deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                f"单站点解析超过 {int(SOURCE_RESOLUTION_TIMEOUT_SECONDS)} 秒，已停止继续搜索"
            )

    def _resolve_source_item_with_deadline(self, item: dict[str, Any]) -> dict[str, Any]:
        previous_deadline = getattr(self._source_resolution_local, "deadline", None)
        self._source_resolution_local.deadline = (
            time.monotonic() + SOURCE_RESOLUTION_TIMEOUT_SECONDS
        )
        try:
            return self._resolve_source_item_safe(item)
        finally:
            if previous_deadline is None:
                try:
                    del self._source_resolution_local.deadline
                except AttributeError:
                    pass
            else:
                self._source_resolution_local.deadline = previous_deadline

    def prepare_manual_items(
        self,
        items: list[dict[str, Any]],
        resolve_sources: bool = True,
    ) -> list[dict[str, Any]]:
        clean_items = [item for item in items if isinstance(item, dict)]
        if not clean_items:
            return []
        workers = min(4, len(clean_items))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mmf-manual") as executor:
            prepared_groups = list(
                executor.map(
                    lambda item: self._prepare_manual_item_safe(item, resolve_sources),
                    clean_items,
                )
            )
        return [prepared for group in prepared_groups for prepared in group]

    def _prepare_manual_item_safe(
        self,
        item: dict[str, Any],
        resolve_sources: bool = True,
    ) -> list[dict[str, Any]]:
        try:
            return self._prepare_manual_item(item, resolve_sources)
        except Exception as exc:
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            page = _parse_civitai_model_page(url) if url else None
            display_name = name or (page.get("slug", "") if page else "")
            return [
                {
                    "id": str(item.get("id") or uuid.uuid4().hex[:16]),
                    "name": name or _filename_from_url(url) or display_name or "未命名模型",
                    "display_name": display_name,
                    "url": _strip_sensitive_query(url) if url else "",
                    "sources": [],
                    "directory": "",
                    "directory_valid": False,
                    "needs_url": not bool(url),
                    "needs_directory": True,
                    "destinationOptions": [],
                    "size": None,
                    "error": self._friendly_error(exc),
                    "manual_entry": True,
                }
            ]

    def _prepare_manual_item(
        self,
        item: dict[str, Any],
        resolve_sources: bool = True,
    ) -> list[dict[str, Any]]:
        raw_url = str(item.get("url") or "").strip()
        civitai_page = _parse_civitai_model_page(raw_url) if raw_url else None
        if civitai_page and not civitai_page["version_id"]:
            payload = self._civitai_model_metadata(civitai_page["model_id"])
            versions = payload.get("modelVersions")
            versions = versions if isinstance(versions, list) else []
            versions = [version for version in versions if isinstance(version, dict)]
            if not versions:
                raise ValueError("Civitai 模型页面没有可下载版本")
            model = {
                "name": payload.get("name"),
                "type": payload.get("type"),
            }
            group_id = hashlib.sha256(
                f"civitai-model|{civitai_page['model_id']}|{raw_url}".encode("utf-8")
            ).hexdigest()[:16]
            expanded: list[dict[str, Any]] = []
            for version in versions:
                metadata = self._civitai_metadata_from_version(version, model)
                version_id = metadata.get("version_id")
                if not version_id:
                    continue
                file_item = dict(item)
                file_item["url"] = (
                    f"https://civitai.com/api/download/models/"
                    f"{quote(str(version_id), safe='')}"
                )
                file_item["_manual_metadata"] = metadata
                file_item["_manual_group"] = {
                    "id": group_id,
                    "label": str(payload.get("name") or "").strip(),
                    "source_url": _strip_sensitive_query(raw_url),
                    "version_count": len(versions),
                }
                expanded.append(self._prepare_manual_file_item(file_item, resolve_sources))
            if not expanded:
                raise ValueError("Civitai 模型页面没有可下载文件")
            return expanded

        normalized_url = normalize_download_url(raw_url) if raw_url else ""
        repository_ref = _parse_hf_repository_url(normalized_url) if normalized_url else None
        if repository_ref:
            file_paths = self._list_hf_model_files(
                repository_ref["repository"],
                repository_ref["revision"],
            )
            if not file_paths:
                raise ValueError("Hugging Face 仓库中没有找到支持的模型文件")
            explicit_name = str(item.get("name") or "").strip()
            expanded: list[dict[str, Any]] = []
            for file_path in file_paths:
                file_item = dict(item)
                file_item["name"] = (
                    explicit_name
                    if explicit_name and len(file_paths) == 1
                    else os.path.basename(file_path)
                )
                file_item["url"] = _build_hf_file_url(
                    repository_ref["repository"],
                    repository_ref["revision"],
                    file_path,
                )
                expanded.append(self._prepare_manual_file_item(file_item, resolve_sources))
            return expanded
        modelscope_repository_ref = (
            _parse_modelscope_repository_url(normalized_url) if normalized_url else None
        )
        if modelscope_repository_ref:
            file_paths = self._list_modelscope_model_files(
                modelscope_repository_ref["repository"],
                modelscope_repository_ref["revision"],
            )
            if not file_paths:
                raise ValueError("魔搭仓库中没有找到支持的模型文件")
            explicit_name = str(item.get("name") or "").strip()
            expanded: list[dict[str, Any]] = []
            for file_path in file_paths:
                file_item = dict(item)
                file_item["name"] = (
                    explicit_name
                    if explicit_name and len(file_paths) == 1
                    else os.path.basename(file_path)
                )
                file_item["url"] = _build_modelscope_file_url(
                    modelscope_repository_ref["repository"],
                    modelscope_repository_ref["revision"],
                    file_path,
                )
                expanded.append(self._prepare_manual_file_item(file_item, resolve_sources))
            return expanded
        return [self._prepare_manual_file_item(item, resolve_sources)]

    def _prepare_manual_file_item(
        self,
        item: dict[str, Any],
        resolve_sources: bool = True,
    ) -> dict[str, Any]:
        raw_url = str(item.get("url") or "").strip()
        normalized_url = normalize_download_url(raw_url) if raw_url else ""
        name = str(item.get("name") or "").strip()
        supplied_metadata = item.get("_manual_metadata")
        metadata = dict(supplied_metadata) if isinstance(supplied_metadata, dict) else {}
        supplied_group = item.get("_manual_group")
        group = dict(supplied_group) if isinstance(supplied_group, dict) else {}

        if normalized_url and not name:
            reference = _parse_hf_file_url(normalized_url) or _parse_modelscope_file_url(
                normalized_url
            )
            if reference:
                name = os.path.basename(reference["file_path"])
            else:
                name = _filename_from_url(normalized_url)
        if metadata and not name:
            name = str(metadata.get("filename") or "").strip()
        if normalized_url and not name:
            try:
                metadata = self.remote_metadata(normalized_url)
                name = str(metadata.get("filename") or "").strip()
            except Exception:
                metadata = {}
        if not name:
            name = "未命名模型"

        model_id = str(item.get("id") or "").strip() or hashlib.sha256(
            f"{name}|{normalized_url}".encode("utf-8")
        ).hexdigest()[:16]
        source_item = {
            "id": model_id,
            "name": name,
            "url": normalized_url,
            "display_name": str(metadata.get("display_name") or ""),
            "version_name": str(metadata.get("version_name") or ""),
            "sources": [
                {
                    "url": normalized_url,
                    "size": metadata.get("size"),
                    "sha256": metadata.get("sha256"),
                    "hash_source": metadata.get("hash_source"),
                }
            ]
            if normalized_url
            else [],
        }
        resolved = (
            self._resolve_source_item_with_deadline(source_item)
            if resolve_sources
            else {"sources": self._original_sources(source_item), "error": ""}
        )
        sources = resolved.get("sources") or []
        if metadata and normalized_url:
            self._merge_source_metadata(sources, detect_provider(normalized_url), metadata)

        directory, inference = self._infer_manual_directory(
            str(item.get("directory") or metadata.get("directory") or ""),
            name,
            sources,
        )
        size = next(
            (
                int(source["size"])
                for source in sources
                if source.get("verification") == "hash_verified"
                and source.get("size") not in {None, ""}
            ),
            None,
        )
        if size is None:
            sizes = {
                int(source["size"])
                for source in sources
                if source.get("selectable") is not False
                and source.get("size") not in {None, ""}
            }
            if len(sizes) == 1:
                size = sizes.pop()

        return {
            "id": model_id,
            "name": name,
            "url": _strip_sensitive_query(normalized_url) if normalized_url else "",
            "sources": sources,
            "directory": directory or "",
            "directory_valid": directory is not None,
            "needs_url": not bool(normalized_url),
            "needs_directory": directory is None,
            "destinationOptions": self.folders.folder_options(directory),
            "size": size,
            "error": str(resolved.get("error") or ""),
            "manual_entry": True,
            "directory_inference": inference,
            "display_name": str(metadata.get("display_name") or ""),
            "version_name": str(metadata.get("version_name") or ""),
            "original_filename": name,
            "group_id": str(group.get("id") or ""),
            "group_label": str(group.get("label") or ""),
            "group_source_url": str(group.get("source_url") or ""),
            "group_version_count": int(group.get("version_count") or 0),
        }

    def _list_hf_model_files(self, repository: str, revision: str) -> list[str]:
        encoded_repository = quote(repository, safe="/")
        encoded_revision = quote(revision, safe="")
        url = (
            f"https://huggingface.co/api/models/{encoded_repository}"
            f"?revision={encoded_revision}"
        )
        request_url, headers = self._request_context(url)
        request = Request(
            request_url,
            headers={**headers, "Accept": "application/json"},
            method="GET",
        )
        with _open_url(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        siblings = payload.get("siblings") if isinstance(payload, dict) else None
        if not isinstance(siblings, list):
            raise ValueError("Hugging Face 仓库文件列表返回了无效数据")
        candidates = {
            str(item.get("rfilename") or item.get("path") or "").strip()
            for item in siblings
            if isinstance(item, dict)
        }
        return sorted(
            path
            for path in candidates
            if path and os.path.splitext(path)[1].lower() in MODEL_EXTENSIONS
        )

    def _list_modelscope_model_files(
        self,
        repository: str,
        revision: str,
    ) -> list[str]:
        candidates = {
            str(
                self._modelscope_file_value(
                    item,
                    "Path",
                    "path",
                    "Name",
                    "name",
                )
                or ""
            ).strip()
            for item in self._list_modelscope_files(repository, revision)
        }
        return sorted(
            path
            for path in candidates
            if path and os.path.splitext(path)[1].lower() in MODEL_EXTENSIONS
        )

    def _infer_manual_directory(
        self,
        requested: str,
        name: str,
        sources: list[dict[str, Any]],
    ) -> tuple[str | None, str]:
        explicit = self.folders.normalize_directory(requested)
        if explicit:
            return explicit, "explicit"

        metadata_matches = {
            normalized
            for normalized in (
                self.folders.normalize_directory(str(source.get("directory") or ""))
                for source in sources
            )
            if normalized
        }
        if len(metadata_matches) == 1:
            return metadata_matches.pop(), "source_metadata"
        if len(metadata_matches) > 1:
            return None, ""

        path_matches: set[str] = set()
        component_hints = {
            "transformer": "diffusion_models",
            "transformers": "diffusion_models",
            "dit": "diffusion_models",
            "autoencoder": "vae",
            "autoencoder_kl": "vae",
            "first_stage_model": "vae",
            "image_encoder": "clip_vision",
            "vision_encoder": "clip_vision",
        }
        for source in sources:
            file_path = str(source.get("file_path") or "").replace("\\", "/")
            for segment in reversed(file_path.split("/")[:-1]):
                component = segment.strip().lower().replace("-", "_")
                if component.startswith("text_encoder") or component in {
                    "clip_l",
                    "clip_g",
                    "t5",
                    "t5xxl",
                    "umt5",
                }:
                    component = "text_encoders"
                normalized = self.folders.normalize_directory(
                    component_hints.get(component, component)
                )
                if normalized:
                    path_matches.add(normalized)
                    break
        if len(path_matches) == 1:
            return path_matches.pop(), "source_path"
        if len(path_matches) > 1:
            return None, ""

        lowered = os.path.basename(name).lower()
        stem = os.path.splitext(lowered)[0]
        heuristics = (
            ("lora", "loras"),
            ("controlnet", "controlnet"),
            ("clip_vision", "clip_vision"),
            ("vae", "vae"),
            ("text_encoder", "text_encoders"),
            ("t5", "text_encoders"),
        )
        for token, directory in heuristics:
            if token in lowered:
                normalized = self.folders.normalize_directory(directory)
                if normalized:
                    return normalized, "filename"
        if stem == "ae" or stem.startswith("ae_"):
            normalized = self.folders.normalize_directory("vae")
            if normalized:
                return normalized, "filename"
        if any(
            token in stem
            for token in ("unet", "transformer", "diffusion_model")
        ):
            normalized = self.folders.normalize_directory("diffusion_models")
            if normalized:
                return normalized, "filename"
        if "upscale" in lowered or "upscaler" in lowered:
            preferred = (
                "latent_upscale_models"
                if "latent" in lowered or "spatial" in lowered
                else "upscale_models"
            )
            normalized = self.folders.normalize_directory(preferred)
            if normalized:
                return normalized, "filename"
        header_directory = self._infer_safetensors_directory_from_sources(sources)
        if header_directory:
            return header_directory, "safetensors_header"
        return None, ""

    def _infer_safetensors_directory_from_sources(self, sources: list[dict[str, Any]]) -> str | None:
        candidates: set[str] = set()
        for source in sources:
            url = str(source.get("url") or "")
            file_path = str(source.get("file_path") or urlparse(url).path)
            if not url or not file_path.lower().endswith(".safetensors"):
                continue
            try:
                inferred = self._cached_source_value(
                    "safetensors-directory",
                    _strip_sensitive_query(url),
                    lambda source_url=url: self._infer_safetensors_directory(source_url),
                )
            except Exception:
                continue
            if inferred:
                candidates.add(inferred)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def _infer_safetensors_directory(self, url: str) -> str | None:
        request_url, headers = self._request_context(url)
        length_headers = dict(headers)
        length_headers["Range"] = "bytes=0-7"
        with _open_url(Request(request_url, headers=length_headers), timeout=30) as response:
            prefix = response.read(8)
        if len(prefix) != 8:
            return None
        header_length = int.from_bytes(prefix, "little", signed=False)
        if header_length <= 2 or header_length > SAFETENSORS_HEADER_MAX_BYTES:
            return None
        header_headers = dict(headers)
        header_headers["Range"] = f"bytes=8-{7 + header_length}"
        with _open_url(Request(request_url, headers=header_headers), timeout=30) as response:
            status = getattr(response, "status", response.getcode())
            if status == 206:
                raw_header = response.read(header_length)
            else:
                response.read(8)
                raw_header = response.read(header_length)
        if len(raw_header) != header_length:
            return None
        payload = json.loads(raw_header.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("__metadata__") if isinstance(payload.get("__metadata__"), dict) else {}
        keys = [str(key).lower() for key in payload if key != "__metadata__"]
        metadata_text = " ".join(f"{key}={value}" for key, value in metadata.items()).lower()
        matches: set[str] = set()
        if "lora" in metadata_text or any(any(token in key for token in ("lora_up", "lora_down", "lora_a", "lora_b")) for key in keys):
            matches.add("loras")
        has_diffusers_vae_encoder = any(
            key.startswith(("encoder.down_blocks.", "encoder.mid_block.", "encoder.conv_in."))
            for key in keys
        )
        has_diffusers_vae_decoder = any(
            key.startswith(("decoder.up_blocks.", "decoder.mid_block.", "decoder.conv_in."))
            for key in keys
        )
        has_explicit_vae_key = any(
            any(token in key for token in ("first_stage_model.", "quant_conv", "post_quant_conv"))
            for key in keys
        )
        if has_explicit_vae_key or (has_diffusers_vae_encoder and has_diffusers_vae_decoder):
            matches.add("vae")
        if any(any(token in key for token in ("controlnet", "input_hint_block", "zero_convs")) for key in keys):
            matches.add("controlnet")
        text_encoder_prefixes = (
            "bert.",
            "roberta.",
            "distilbert.",
            "albert.",
            "deberta.",
            "electra.",
            "xlm_roberta.",
        )
        if any(
            any(token in key for token in ("text_model.", "text_encoder.", "encoder.block."))
            or key.startswith(text_encoder_prefixes)
            for key in keys
        ):
            matches.add("text_encoders")
        if any(any(token in key for token in ("diffusion_model.", "double_blocks.", "transformer_blocks.")) for key in keys):
            matches.add("diffusion_models")
        normalized = {directory for directory in (self.folders.normalize_directory(value) for value in matches) if directory}
        return next(iter(normalized)) if len(normalized) == 1 else None

    def _resolve_source_item_safe(self, item: dict[str, Any]) -> dict[str, Any]:
        model_id = str(item.get("id") or "")
        name = str(item.get("name") or "").strip()
        try:
            sources = self._resolve_source_item(item)
            self._apply_source_quality(sources)
            self._record_trusted_sources(sources)
            return {"id": model_id, "name": name, "sources": sources, "error": ""}
        except Exception as exc:
            return {
                "id": model_id,
                "name": name,
                "sources": self._original_sources(item),
                "error": self._friendly_error(exc),
            }

    def _resolve_source_provider_safe(
        self,
        item: dict[str, Any],
        target_provider: str,
    ) -> dict[str, Any]:
        model_id = str(item.get("id") or "")
        name = str(item.get("name") or "").strip()
        try:
            sources = self._resolve_source_provider(item, target_provider)
            self._apply_source_quality(sources)
            self._record_trusted_sources(sources)
            resolved_name = name or next(
                (
                    os.path.basename(str(source.get("file_path") or ""))
                    for source in sources
                    if str(source.get("file_path") or "").strip()
                ),
                "",
            )
            selectable_sizes = {
                int(source["size"])
                for source in sources
                if source.get("selectable") is not False
                and source.get("size") not in {None, ""}
            }
            result = {
                "id": model_id,
                "name": resolved_name,
                "provider": target_provider,
                "sources": sources,
                "error": "",
                "size": selectable_sizes.pop() if len(selectable_sizes) == 1 else None,
            }
            if item.get("manual_entry"):
                directory, inference = self._infer_manual_directory(
                    str(item.get("directory") or ""),
                    resolved_name,
                    sources,
                )
                result.update(
                    {
                        "directory": directory or "",
                        "directory_valid": directory is not None,
                        "needs_directory": directory is None,
                        "destinationOptions": self.folders.folder_options(directory),
                        "directory_inference": inference,
                    }
                )
            return result
        except Exception as exc:
            return {
                "id": model_id,
                "name": name,
                "provider": target_provider,
                "sources": [],
                "error": self._friendly_error(exc),
            }

    def _resolve_source_provider(
        self,
        item: dict[str, Any],
        target_provider: str,
    ) -> list[dict[str, Any]]:
        indexed = self._indexed_source(item, target_provider)
        if indexed:
            return [indexed]
        sources = self._original_sources(item)
        original_url = str(item.get("url") or "").strip()
        if not original_url:
            original_url = next(
                (
                    str(source.get("url") or "").strip()
                    for source in sources
                    if source.get("url")
                ),
                "",
            )
        if not original_url:
            return []

        expected_sha256 = self._expected_sha256(item)
        original_provider = detect_provider(original_url)
        original_metadata: dict[str, Any] = {}
        original_reference: dict[str, str] | None = None

        if original_provider == "hf":
            original_reference = _parse_hf_file_url(original_url)
            if original_reference:
                try:
                    original_metadata = self.remote_metadata(original_url)
                    self._merge_source_metadata(sources, "hf", original_metadata)
                except Exception:
                    original_metadata = {}
        elif original_provider == "modelscope":
            original_reference = _parse_modelscope_file_url(original_url)
            if original_reference:
                file_info = self._find_modelscope_file(
                    original_reference["repository"],
                    original_reference["revision"],
                    original_reference["file_path"],
                )
                if file_info:
                    sha256 = _normalize_sha256(
                        self._modelscope_file_value(file_info, "Sha256", "sha256")
                    )
                    original_metadata = {
                        "size": self._modelscope_file_value(file_info, "Size", "size"),
                        "sha256": sha256,
                        "hash_source": "modelscope_file_sha256" if sha256 else "",
                    }
                    self._merge_source_metadata(sources, "modelscope", original_metadata)
        elif original_provider == "civitai":
            version_id = _civitai_model_version_id(original_url)
            if version_id:
                try:
                    original_metadata = self._civitai_file_metadata(version_id)
                    self._merge_source_metadata(sources, "civitai", original_metadata)
                except Exception:
                    original_metadata = {}

        if target_provider == "modelscope" and original_provider == "hf" and original_reference:
            source = self._resolve_modelscope_mirror(
                original_reference,
                expected_sha256,
                original_metadata,
            )
            if source:
                self._append_source(sources, source)
        elif target_provider == "hf" and original_provider == "modelscope" and original_reference:
            hf_url = _build_hf_file_url(
                original_reference["repository"],
                original_reference["revision"],
                original_reference["file_path"],
            )
            try:
                hf_metadata = self.remote_metadata(hf_url)
            except Exception:
                hf_metadata = None
            if hf_metadata:
                comparison = self._classify_same_path_source(
                    hf_metadata,
                    original_metadata,
                    forward_provider="hf",
                )
                self._append_source(
                    sources,
                    self._source_dict(
                        "hf",
                        hf_url,
                        original_reference,
                        size=hf_metadata.get("size"),
                        sha256=str(hf_metadata.get("sha256") or ""),
                        hash_source=str(hf_metadata.get("hash_source") or ""),
                        **comparison,
                    ),
                )

        target_sources = [
            source for source in sources if source.get("provider") == target_provider
        ]
        if not target_sources:
            reference_source = next(
                (
                    source
                    for source in sources
                    if source.get("provider") == original_provider
                    and _normalize_sha256(source.get("sha256"))
                ),
                None,
            )
            reference_sha256 = _normalize_sha256(
                reference_source.get("sha256") if reference_source else expected_sha256
            )
            if reference_sha256:
                reference_size = self._metadata_size(reference_source or original_metadata)
                search_terms = self._source_search_terms(item)
                source: dict[str, Any] | None = None
                if target_provider == "hf":
                    source = self._find_hf_source_by_hash(
                        search_terms,
                        str(item.get("name") or ""),
                        reference_sha256,
                        reference_size,
                    )
                elif target_provider == "modelscope":
                    source = self._find_modelscope_source_by_hash(
                        search_terms,
                        str(item.get("name") or ""),
                        reference_sha256,
                        reference_size,
                    )
                elif target_provider == "civitai":
                    source = self._find_civitai_source_by_hash(reference_sha256)
                if source:
                    self._append_source(sources, source)

        if expected_sha256:
            self._apply_required_hash_policy(sources, expected_sha256)
        return [
            source for source in sources if source.get("provider") == target_provider
        ]

    def _resolve_source_item(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        sources = self._original_sources(item)
        original_url = str(item.get("url") or "").strip()
        existing_sources = item.get("sources")
        if not original_url and isinstance(existing_sources, list):
            original_url = next(
                (
                    str(source.get("url") or "").strip()
                    for source in existing_sources
                    if isinstance(source, dict) and source.get("url")
                ),
                "",
            )
        if not original_url:
            return sources

        expected_sha256 = self._expected_sha256(item)
        provider = detect_provider(original_url)
        if provider == "hf":
            hf_ref = _parse_hf_file_url(original_url)
            if hf_ref:
                hf_metadata: dict[str, Any] = {}
                try:
                    hf_metadata = self.remote_metadata(original_url)
                    self._merge_source_metadata(sources, "hf", hf_metadata)
                except Exception:
                    pass
                modelscope_source = self._resolve_modelscope_mirror(
                    hf_ref,
                    expected_sha256,
                    hf_metadata,
                )
                if modelscope_source:
                    self._append_source(sources, modelscope_source)
        elif provider == "modelscope":
            modelscope_ref = _parse_modelscope_file_url(original_url)
            if modelscope_ref:
                modelscope_file = self._find_modelscope_file(
                    modelscope_ref["repository"],
                    modelscope_ref["revision"],
                    modelscope_ref["file_path"],
                )
                modelscope_metadata: dict[str, Any] = {}
                if modelscope_file:
                    modelscope_sha = _normalize_sha256(
                        self._modelscope_file_value(modelscope_file, "Sha256", "sha256")
                    )
                    modelscope_metadata = {
                        "size": self._modelscope_file_value(modelscope_file, "Size", "size"),
                        "sha256": modelscope_sha,
                        "hash_source": "modelscope_file_sha256" if modelscope_sha else "",
                    }
                    self._merge_source_metadata(
                        sources,
                        "modelscope",
                        modelscope_metadata,
                    )
                hf_url = _build_hf_file_url(
                    modelscope_ref["repository"],
                    modelscope_ref["revision"],
                    modelscope_ref["file_path"],
                )
                try:
                    hf_metadata = self.remote_metadata(hf_url)
                except Exception:
                    hf_metadata = None
                if hf_metadata:
                    comparison = self._classify_same_path_source(
                        hf_metadata,
                        modelscope_metadata,
                        forward_provider="hf",
                    )
                    self._append_source(
                        sources,
                        self._source_dict(
                            "hf",
                            hf_url,
                            modelscope_ref,
                            size=hf_metadata.get("size"),
                            sha256=str(hf_metadata.get("sha256") or ""),
                            hash_source=str(hf_metadata.get("hash_source") or ""),
                            **comparison,
                        ),
                    )
        elif provider == "civitai":
            version_id = _civitai_model_version_id(original_url)
            if version_id:
                try:
                    civitai_metadata = self._civitai_file_metadata(version_id)
                    self._merge_source_metadata(sources, "civitai", civitai_metadata)
                except Exception:
                    pass

        reference_source = next(
            (
                source
                for source in sources
                if source.get("provider") == provider
                and _normalize_sha256(source.get("sha256"))
            ),
            None,
        )
        if reference_source:
            self._append_cross_provider_hash_sources(
                item,
                sources,
                provider,
                reference_source,
            )
        if expected_sha256:
            self._apply_required_hash_policy(sources, expected_sha256)
        return sources

    def _append_cross_provider_hash_sources(
        self,
        item: dict[str, Any],
        sources: list[dict[str, Any]],
        original_provider: str,
        reference_source: dict[str, Any],
    ) -> None:
        sha256 = _normalize_sha256(reference_source.get("sha256"))
        if not sha256:
            return
        size = self._metadata_size(reference_source)
        existing = {str(source.get("provider") or "") for source in sources}
        search_terms = self._source_search_terms(item)

        if "hf" not in existing:
            try:
                source = self._find_hf_source_by_hash(
                    search_terms,
                    str(item.get("name") or ""),
                    sha256,
                    size,
                )
            except Exception:
                source = None
            if source:
                self._append_source(sources, source)

        if "modelscope" not in existing:
            try:
                source = self._find_modelscope_source_by_hash(
                    search_terms,
                    str(item.get("name") or ""),
                    sha256,
                    size,
                )
            except Exception:
                source = None
            if source:
                self._append_source(sources, source)

        if original_provider != "civitai" and "civitai" not in existing:
            try:
                source = self._find_civitai_source_by_hash(sha256)
            except Exception:
                source = None
            if source:
                self._append_source(sources, source)

    @staticmethod
    def _source_search_terms(item: dict[str, Any]) -> list[str]:
        name = str(item.get("name") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        version_name = str(item.get("version_name") or "").strip()
        values = [
            " ".join(value for value in (display_name, version_name) if value),
            display_name,
            os.path.splitext(os.path.basename(name))[0],
        ]
        return [value for value in dict.fromkeys(values) if len(value) >= 3]

    def _find_civitai_source_by_hash(self, sha256: str) -> dict[str, Any] | None:
        self._check_source_resolution_deadline()
        url = (
            "https://civitai.com/api/v1/model-versions/by-hash/"
            f"{quote(sha256, safe='')}"
        )
        request_url, headers = self._request_context(url)
        request = Request(
            request_url,
            headers={**headers, "Accept": "application/json"},
            method="GET",
        )
        try:
            with _open_url(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        model = model if isinstance(model, dict) else {}
        metadata = self._civitai_metadata_from_version(payload, model)
        if _normalize_sha256(metadata.get("sha256")) != sha256:
            return None
        version_id = str(metadata.get("version_id") or "").strip()
        if not version_id:
            return None
        source = self._source_dict(
            "civitai",
            f"https://civitai.com/api/download/models/{quote(version_id, safe='')}",
            None,
            size=metadata.get("size"),
            sha256=sha256,
            hash_source="civitai_file_sha256",
            verification="hash_verified",
        )
        source["file_path"] = str(metadata.get("filename") or "")
        return source

    def _find_hf_source_by_hash(
        self,
        search_terms: list[str],
        target_name: str,
        sha256: str,
        size: int | None,
    ) -> dict[str, Any] | None:
        target_canonical = _canonical_model_name(target_name)
        repositories: list[str] = []
        for term in search_terms:
            self._check_source_resolution_deadline()
            for model in self._search_hf_models(term):
                repository = str(model.get("id") or model.get("modelId") or "").strip()
                if repository and repository not in repositories:
                    repositories.append(repository)
                if len(repositories) >= 20:
                    break
            if len(repositories) >= 20:
                break
        for repository in repositories:
            self._check_source_resolution_deadline()
            self._resolution_metric("repositories_checked")
            for file_info in self._list_hf_files(repository, "main"):
                file_path = str(file_info.get("rfilename") or file_info.get("path") or "")
                if os.path.splitext(file_path)[1].lower() not in MODEL_EXTENSIONS:
                    continue
                lfs = file_info.get("lfs")
                lfs = lfs if isinstance(lfs, dict) else {}
                candidate_sha = _normalize_sha256(
                    lfs.get("sha256") or lfs.get("oid")
                )
                candidate_size = lfs.get("size", file_info.get("size"))
                if candidate_sha != sha256:
                    continue
                if (
                    target_canonical
                    and _canonical_model_name(file_path) != target_canonical
                    and size is not None
                    and self._metadata_size({"size": candidate_size}) != size
                ):
                    continue
                return self._source_dict(
                    "hf",
                    _build_hf_file_url(repository, "main", file_path),
                    {
                        "repository": repository,
                        "revision": "main",
                        "file_path": file_path,
                    },
                    size=candidate_size,
                    sha256=sha256,
                    hash_source="hf_lfs_oid",
                    verification="hash_verified",
                )
        return None

    def _find_modelscope_source_by_hash(
        self,
        search_terms: list[str],
        target_name: str,
        sha256: str,
        size: int | None,
    ) -> dict[str, Any] | None:
        target_canonical = _canonical_model_name(target_name)
        repositories: list[str] = []
        for term in search_terms:
            self._check_source_resolution_deadline()
            for model in self._search_modelscope_models(term):
                repository = str(
                    model.get("id")
                    or model.get("model_id")
                    or model.get("ModelId")
                    or ""
                ).strip()
                if repository and repository not in repositories:
                    repositories.append(repository)
                if len(repositories) >= 20:
                    break
            if len(repositories) >= 20:
                break
        for repository in repositories:
            self._check_source_resolution_deadline()
            self._resolution_metric("repositories_checked")
            for revision in ("master", "main"):
                for file_info in self._list_modelscope_files(repository, revision):
                    file_path = str(
                        self._modelscope_file_value(
                            file_info,
                            "Path",
                            "path",
                            "Name",
                            "name",
                        )
                        or ""
                    )
                    if os.path.splitext(file_path)[1].lower() not in MODEL_EXTENSIONS:
                        continue
                    candidate_sha = _normalize_sha256(
                        self._modelscope_file_value(file_info, "Sha256", "sha256")
                    )
                    candidate_size = self._modelscope_file_value(
                        file_info,
                        "Size",
                        "size",
                    )
                    if candidate_sha != sha256:
                        continue
                    if (
                        target_canonical
                        and _canonical_model_name(file_path) != target_canonical
                        and size is not None
                        and self._metadata_size({"size": candidate_size}) != size
                    ):
                        continue
                    return self._source_dict(
                        "modelscope",
                        _build_modelscope_file_url(
                            repository,
                            revision,
                            file_path,
                        ),
                        {
                            "repository": repository,
                            "revision": revision,
                            "file_path": file_path,
                        },
                        size=candidate_size,
                        sha256=sha256,
                        hash_source="modelscope_file_sha256",
                        verification="hash_verified",
                    )
        return None

    def _original_sources(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        raw_sources = item.get("sources")
        values = raw_sources if isinstance(raw_sources, list) else []
        url = str(item.get("url") or "").strip()
        if url:
            values = [*values, {"url": url}]
        sources: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            source_url = str(value.get("url") or "").strip()
            if not source_url:
                continue
            source_url = normalize_download_url(source_url)
            provider = str(value.get("provider") or detect_provider(source_url))
            reference = (
                _parse_hf_file_url(source_url)
                if provider == "hf"
                else _parse_modelscope_file_url(source_url)
                if provider == "modelscope"
                else None
            )
            source = self._source_dict(
                provider,
                source_url,
                reference,
                size=value.get("size"),
                sha256=str(value.get("sha256") or ""),
                hash_source=str(value.get("hash_source") or ""),
                verification=str(value.get("verification") or "original"),
                warning=str(value.get("warning") or ""),
                warning_level=str(value.get("warning_level") or ""),
                selectable=value.get("selectable") is not False,
                blocked_reason=str(value.get("blocked_reason") or ""),
            )
            self._append_source(sources, source)
        return sources

    @staticmethod
    def _metadata_size(metadata: dict[str, Any]) -> int | None:
        value = metadata.get("size")
        try:
            return int(value) if value not in {None, ""} else None
        except (TypeError, ValueError):
            return None

    def _classify_same_path_source(
        self,
        candidate_metadata: dict[str, Any],
        reference_metadata: dict[str, Any],
        *,
        forward_provider: str,
    ) -> dict[str, Any]:
        candidate_sha = _normalize_sha256(candidate_metadata.get("sha256"))
        reference_sha = _normalize_sha256(reference_metadata.get("sha256"))
        candidate_size = self._metadata_size(candidate_metadata)
        reference_size = self._metadata_size(reference_metadata)
        sizes_known = candidate_size is not None and reference_size is not None
        same_size = sizes_known and candidate_size == reference_size
        provider_name = PROVIDER_NAMES.get(forward_provider, forward_provider)

        if candidate_sha and reference_sha and candidate_sha == reference_sha:
            return {
                "verification": "hash_verified",
                "warning": "",
                "warning_level": "",
                "selectable": True,
                "blocked_reason": "",
            }
        if candidate_sha and reference_sha and candidate_sha != reference_sha:
            warning = (
                HASH_CONFLICT_WARNING
                if same_size
                else f"{provider_name} 与原始来源的 SHA-256 不一致，且文件大小也不一致，请谨慎确认。"
            )
            return {
                "verification": "hash_conflict",
                "warning": warning,
                "warning_level": "warning" if same_size else "error",
                "selectable": True,
                "blocked_reason": "",
            }
        if sizes_known:
            if same_size:
                return {
                    "verification": "metadata_matched",
                    "warning": METADATA_MATCH_WARNING,
                    "warning_level": "warning",
                    "selectable": True,
                    "blocked_reason": "",
                }
            return {
                "verification": "metadata_conflict",
                "warning": f"{provider_name} 与原始来源的文件大小不一致，请谨慎确认。",
                "warning_level": "error",
                "selectable": True,
                "blocked_reason": "",
            }
        return {
            "verification": "same_repo_path",
            "warning": (
                f"仅确认 {provider_name} 中存在相同仓库 ID 和文件路径，未取得 SHA-256，"
                "无法验证内容完全一致，请确认来源后再下载。"
            ),
            "warning_level": "warning",
            "selectable": True,
            "blocked_reason": "",
        }

    @staticmethod
    def _apply_required_hash_policy(
        sources: list[dict[str, Any]],
        expected_sha256: str,
    ) -> None:
        expected = _normalize_sha256(expected_sha256)
        if not expected:
            return
        for source in sources:
            actual = _normalize_sha256(source.get("sha256"))
            if actual == expected:
                source.update(
                    {
                        "verification": "hash_verified",
                        "warning": "",
                        "warning_level": "",
                        "selectable": True,
                        "blocked_reason": "",
                    }
                )
                continue
            if actual:
                reason = (
                    f"来源 SHA-256 {actual} 与工作流要求的 SHA-256 {expected} 不一致，"
                    "该来源已禁止下载。"
                )
            else:
                reason = (
                    f"工作流要求 SHA-256 {expected}，但该来源未提供可验证的 SHA-256，"
                    "该来源已禁止下载。"
                )
            source.update(
                {
                    "verification": "hash_mismatch" if actual else "hash_unavailable",
                    "warning": reason,
                    "warning_level": "error",
                    "selectable": False,
                    "blocked_reason": reason,
                }
            )

    def _resolve_modelscope_mirror(
        self,
        hf_ref: dict[str, str],
        expected_sha256: str,
        hf_metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        repository = hf_ref["repository"]
        file_path = hf_ref["file_path"]
        revisions = list(dict.fromkeys([hf_ref["revision"], "master", "main"]))
        blocked_exact: dict[str, Any] | None = None
        blocked_weak: dict[str, Any] | None = None
        weak_candidates: dict[str, tuple[str, dict[str, Any]]] = {}
        for revision in revisions:
            self._check_source_resolution_deadline()
            files = self._list_modelscope_files(repository, revision)
            file_info = next(
                (
                    item
                    for item in files
                    if str(
                        self._modelscope_file_value(item, "Path", "path", "Name", "name")
                        or ""
                    )
                    == file_path
                ),
                None,
            )
            if file_info:
                sha256 = _normalize_sha256(
                    self._modelscope_file_value(file_info, "Sha256", "sha256")
                )
                source = self._source_dict(
                    "modelscope",
                    _build_modelscope_file_url(repository, revision, file_path),
                    {"repository": repository, "revision": revision, "file_path": file_path},
                    size=self._modelscope_file_value(file_info, "Size", "size"),
                    sha256=sha256,
                    hash_source="modelscope_file_sha256" if sha256 else "",
                    **self._classify_same_path_source(
                        {
                            "size": self._modelscope_file_value(file_info, "Size", "size"),
                            "sha256": sha256,
                        },
                        hf_metadata,
                        forward_provider="modelscope",
                    ),
                )
                if not expected_sha256 or sha256 == expected_sha256:
                    return source
                if blocked_exact is None:
                    blocked_exact = source
            for candidate in files:
                candidate_path = str(
                    self._modelscope_file_value(candidate, "Path", "path", "Name", "name")
                    or ""
                )
                if (
                    candidate_path
                    and candidate_path != file_path
                    and os.path.basename(candidate_path) == os.path.basename(file_path)
                ):
                    weak_candidates.setdefault(candidate_path, (revision, candidate))

        if len(weak_candidates) == 1:
            candidate_path, (revision, file_info) = next(iter(weak_candidates.items()))
            candidate_sha = _normalize_sha256(
                self._modelscope_file_value(file_info, "Sha256", "sha256")
            )
            weak_source = self._source_dict(
                "modelscope",
                _build_modelscope_file_url(repository, revision, candidate_path),
                {
                    "repository": repository,
                    "revision": revision,
                    "file_path": candidate_path,
                },
                size=self._modelscope_file_value(file_info, "Size", "size"),
                sha256=candidate_sha,
                hash_source="modelscope_file_sha256" if candidate_sha else "",
                verification="filename_match",
                warning=WEAK_FILENAME_WARNING,
                warning_level="error",
                selectable=True,
            )
            if not expected_sha256 or candidate_sha == expected_sha256:
                return weak_source
            blocked_weak = weak_source

        comparison_sha256 = (
            _normalize_sha256(expected_sha256)
            or _normalize_sha256(hf_metadata.get("sha256"))
        )
        if not comparison_sha256:
            return blocked_exact or blocked_weak
        search_terms = list(
            dict.fromkeys(
                [
                    repository,
                    repository.split("/", 1)[-1],
                    os.path.splitext(os.path.basename(file_path))[0],
                ]
            )
        )
        seen_repositories = {repository}
        for search_term in search_terms:
            self._check_source_resolution_deadline()
            for candidate in self._search_modelscope_models(search_term):
                candidate_repository = str(
                    candidate.get("id")
                    or candidate.get("model_id")
                    or candidate.get("ModelId")
                    or ""
                ).strip()
                if not candidate_repository or candidate_repository in seen_repositories:
                    continue
                seen_repositories.add(candidate_repository)
                for revision in ("master", "main"):
                    for file_info in self._list_modelscope_files(candidate_repository, revision):
                        candidate_path = str(
                            self._modelscope_file_value(file_info, "Path", "path", "Name", "name") or ""
                        )
                        candidate_sha = _normalize_sha256(
                            self._modelscope_file_value(file_info, "Sha256", "sha256")
                        )
                        if (
                            os.path.basename(candidate_path) == os.path.basename(file_path)
                            and candidate_sha == comparison_sha256
                        ):
                            return self._source_dict(
                                "modelscope",
                                _build_modelscope_file_url(
                                    candidate_repository,
                                    revision,
                                    candidate_path,
                                ),
                                {
                                    "repository": candidate_repository,
                                    "revision": revision,
                                    "file_path": candidate_path,
                                },
                                size=self._modelscope_file_value(file_info, "Size", "size"),
                                sha256=candidate_sha,
                                hash_source="modelscope_file_sha256",
                                verification="hash_verified",
                            )
        return blocked_exact or blocked_weak

    def _find_modelscope_file(
        self,
        repository: str,
        revision: str,
        file_path: str,
    ) -> dict[str, Any] | None:
        for file_info in self._list_modelscope_files(repository, revision):
            candidate_path = str(
                self._modelscope_file_value(file_info, "Path", "path", "Name", "name") or ""
            )
            if candidate_path == file_path:
                return file_info
        return None

    def _hf_file_metadata(self, reference: dict[str, str]) -> dict[str, Any]:
        repository = quote(reference["repository"], safe="/")
        revision = quote(reference["revision"], safe="")
        url = f"https://huggingface.co/api/models/{repository}/paths-info/{revision}"
        request_url, headers = self._request_context(url)
        payload = json.dumps(
            {"paths": [reference["file_path"]], "expand": True}
        ).encode("utf-8")
        request = Request(
            request_url,
            data=payload,
            headers={
                **headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with _open_url(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, list):
                raise ValueError("Hugging Face 文件接口返回了无效数据")
            file_info = next(
                (
                    item
                    for item in data
                    if isinstance(item, dict)
                    and str(item.get("path") or "") == reference["file_path"]
                ),
                None,
            )
        except HTTPError as exc:
            if exc.code not in {301, 302, 307, 308}:
                raise
            file_info = next(
                (
                    item
                    for item in self._list_hf_files(
                        reference["repository"],
                        reference["revision"],
                    )
                    if str(item.get("rfilename") or item.get("path") or "")
                    == reference["file_path"]
                ),
                None,
            )
        if not file_info:
            return {}
        lfs = file_info.get("lfs")
        lfs = lfs if isinstance(lfs, dict) else {}
        sha256 = _normalize_sha256(lfs.get("oid") or lfs.get("sha256"))
        size = lfs.get("size")
        if size in {None, ""}:
            size = file_info.get("size")
        return {
            "size": int(size) if size not in {None, ""} else None,
            "sha256": sha256,
            "hash_source": "hf_lfs_oid" if sha256 else "",
        }

    def _list_modelscope_files(self, repository: str, revision: str) -> list[dict[str, Any]]:
        cache_key = (repository, revision)
        def load() -> list[dict[str, Any]]:
            query = urlencode({"Revision": revision, "Recursive": "True"})
            url = f"https://modelscope.cn/api/v1/models/{repository}/repo/files?{query}"
            try:
                payload = self._json_request(url)
            except HTTPError as exc:
                if exc.code == 404:
                    return []
                raise
            data = payload.get("Data", payload.get("data", payload))
            if isinstance(data, dict):
                data = data.get("Files", data.get("files", []))
            return (
                [item for item in data if isinstance(item, dict)]
                if isinstance(data, list)
                else []
            )

        return self._cached_source_value("modelscope-files", cache_key, load)

    def _search_modelscope_models(self, search: str) -> list[dict[str, Any]]:
        cache_key = search.strip().lower()
        def load() -> list[dict[str, Any]]:
            query = urlencode({"search": search, "page_number": 1, "page_size": 10})
            payload = self._json_request(f"https://modelscope.cn/openapi/v1/models?{query}")
            data = payload.get("data", payload.get("Data", payload))
            if isinstance(data, dict):
                data = data.get("models", data.get("Models", []))
            return (
                [item for item in data if isinstance(item, dict)]
                if isinstance(data, list)
                else []
            )

        return self._cached_source_value("modelscope-search", cache_key, load)

    def _search_hf_models(self, search: str) -> list[dict[str, Any]]:
        cache_key = search.strip().lower()
        def load() -> list[dict[str, Any]]:
            query = urlencode({"search": search, "limit": 10})
            url = f"https://huggingface.co/api/models?{query}"
            request_url, headers = self._request_context(url)
            request = Request(
                request_url,
                headers={**headers, "Accept": "application/json"},
                method="GET",
            )
            with _open_url(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return (
                [item for item in payload if isinstance(item, dict)]
                if isinstance(payload, list)
                else []
            )

        return self._cached_source_value("hf-search", cache_key, load)

    def _list_hf_files(
        self,
        repository: str,
        revision: str,
    ) -> list[dict[str, Any]]:
        cache_key = (repository, revision)
        def load() -> list[dict[str, Any]]:
            encoded_repository = quote(repository, safe="/")
            query = urlencode({"revision": revision, "blobs": "true"})
            url = f"https://huggingface.co/api/models/{encoded_repository}?{query}"
            request_url, headers = self._request_context(url)
            request = Request(
                request_url,
                headers={**headers, "Accept": "application/json"},
                method="GET",
            )
            try:
                with _open_url(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in {401, 403, 404}:
                    return []
                raise
            siblings = payload.get("siblings") if isinstance(payload, dict) else None
            return (
                [item for item in siblings if isinstance(item, dict)]
                if isinstance(siblings, list)
                else []
            )

        return self._cached_source_value("hf-files", cache_key, load)

    def _json_request(self, url: str) -> dict[str, Any]:
        request_url, headers = self._request_context(url)
        request = Request(
            request_url,
            headers={**headers, "Accept": "application/json"},
        )
        with _open_url(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("远程来源接口返回了无效数据")
        return payload

    @staticmethod
    def _modelscope_file_value(file_info: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in file_info:
                return file_info[key]
        return None

    @staticmethod
    def _expected_sha256(item: dict[str, Any]) -> str:
        value = _normalize_sha256(item.get("hash"))
        hash_type = str(item.get("hash_type") or item.get("hashType") or "").lower().replace("-", "")
        return value if value and hash_type in {"", "sha256"} else ""

    @staticmethod
    def _source_dict(
        provider: str,
        url: str,
        reference: dict[str, str] | None,
        *,
        size: Any = None,
        sha256: str = "",
        hash_source: str = "",
        verification: str = "original",
        warning: str = "",
        warning_level: str = "",
        selectable: bool = True,
        blocked_reason: str = "",
    ) -> dict[str, Any]:
        return {
            "provider": provider,
            "url": _strip_sensitive_query(url),
            "repository": reference.get("repository", "") if reference else "",
            "revision": reference.get("revision", "") if reference else "",
            "file_path": reference.get("file_path", "") if reference else "",
            "size": int(size) if size not in {None, ""} else None,
            "sha256": sha256,
            "hash_source": hash_source,
            "verification": verification,
            "warning": warning,
            "warning_level": warning_level,
            "selectable": selectable,
            "blocked_reason": blocked_reason,
        }

    @staticmethod
    def _apply_source_quality(sources: list[dict[str, Any]]) -> None:
        scores = {
            "hash_verified": (100, "高", "SHA-256 完全一致"),
            "metadata_matched": (75, "中", "仓库、完整路径和大小一致"),
            "same_repo_path": (65, "中", "仓库与完整路径一致，未完成内容校验"),
            "original": (50, "中", "原始工作流或用户提供的链接"),
            "hash_conflict": (40, "低", "来源之间的 SHA-256 不一致"),
            "filename_match": (25, "低", "仅文件名匹配"),
            "metadata_conflict": (20, "低", "来源 metadata 存在冲突"),
            "hash_mismatch": (0, "阻止", "不符合工作流要求的 SHA-256"),
            "hash_unavailable": (0, "阻止", "无法满足工作流 SHA-256 约束"),
        }
        for source in sources:
            verification = str(source.get("verification") or "original")
            score, level, reason = scores.get(verification, (35, "低", "匹配证据有限"))
            if source.get("selectable") is False:
                score, level = 0, "阻止"
            source["confidence_score"] = score
            source["confidence_level"] = level
            source["confidence_reasons"] = [reason]

    @staticmethod
    def _append_source(sources: list[dict[str, Any]], source: dict[str, Any]) -> None:
        key = (source.get("provider"), source.get("url"))
        if any((existing.get("provider"), existing.get("url")) == key for existing in sources):
            return
        sources.append(source)

    @staticmethod
    def _merge_source_metadata(
        sources: list[dict[str, Any]],
        provider: str,
        metadata: dict[str, Any],
    ) -> None:
        for source in sources:
            if source.get("provider") != provider:
                continue
            if metadata.get("size") is not None:
                source["size"] = metadata["size"]
            if metadata.get("sha256"):
                source["sha256"] = metadata["sha256"]
            if metadata.get("hash_source"):
                source["hash_source"] = metadata["hash_source"]
            if metadata.get("filename") and not source.get("file_path"):
                source["file_path"] = metadata["filename"]
            if metadata.get("directory"):
                source["directory"] = metadata["directory"]
            if metadata.get("sha256") and metadata.get("hash_source"):
                source["verification"] = "hash_verified"
            return

    def pause(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self._get_task(task_id)
            if task.status == "queued":
                task.status = "paused"
                self._remove_from_queue_locked(task_id)
            elif task.status in {"downloading", "verifying"}:
                task.pause_requested = True
            task.updated_at = time.time()
            self._save_state_locked(force=True)
            return task.to_dict()

    def resume(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self._get_task(task_id)
            if task.status in {"downloading", "verifying"} and task.pause_requested:
                task.pause_requested = False
                task.cancel_requested = False
                task.error = ""
            elif task.status in {"paused", "failed", "canceled"}:
                existing = self._find_existing_task_locked(
                    task.target_path,
                    exclude_task_id=task_id,
                )
                if existing is not None:
                    return existing.to_dict()
                task.status = "queued"
                task.error = ""
                task.pause_requested = False
                task.cancel_requested = False
                if task_id not in self.queue:
                    self.queue.append(task_id)
                if task_id not in self._active_task_ids:
                    self._ensure_worker_locked()
            task.updated_at = time.time()
            self._save_state_locked(force=True)
            return task.to_dict()

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self._get_task(task_id)
            if task.status == "queued":
                self._remove_from_queue_locked(task_id)
                task.status = "canceled"
            elif task.status in {"downloading", "verifying"}:
                task.cancel_requested = True
            elif task.status in {"paused", "failed"}:
                task.status = "canceled"
            task.updated_at = time.time()
            self._save_state_locked(force=True)
            return task.to_dict()

    def restart(self, task_id: str) -> dict[str, Any]:
        """Discard the exact task's partial data and enqueue a clean download."""
        with self.lock:
            task = self._get_task(task_id)
            if task.status in {"downloading", "verifying", "queued"}:
                raise ValueError("活动下载任务不能重新开始，请先暂停或取消")
            existing = self._find_existing_task_locked(
                task.target_path,
                exclude_task_id=task_id,
            )
            if existing is not None:
                return existing.to_dict()
            if os.path.isfile(task.part_path):
                os.remove(task.part_path)
            if task.restart_required and os.path.isfile(task.target_path):
                os.remove(task.target_path)
            task.status = "queued"
            task.downloaded = 0
            task.speed = 0.0
            task.eta = None
            task.verification_progress = None
            task.verification_speed = 0.0
            task.verified_hash = False
            task.error = ""
            task.retries = 0
            task.pause_requested = False
            task.cancel_requested = False
            task.restart_required = False
            task.updated_at = time.time()
            if task_id not in self.queue:
                self.queue.append(task_id)
            self._ensure_worker_locked()
            self._save_state_locked(force=True)
            return task.to_dict()

    def _get_task(self, task_id: str) -> DownloadTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        return task

    def _find_existing_task_locked(
        self,
        target_path: str,
        exclude_task_id: str | None = None,
    ) -> DownloadTask | None:
        normalized_target = os.path.normcase(os.path.abspath(target_path))
        for task in self.tasks.values():
            if task.id == exclude_task_id:
                continue
            if task.status not in {"queued", "downloading", "verifying", "paused"}:
                continue
            if os.path.normcase(os.path.abspath(task.target_path)) == normalized_target:
                return task
        return None

    def _remove_from_queue_locked(self, task_id: str) -> None:
        self.queue = [queued_id for queued_id in self.queue if queued_id != task_id]

    def _ensure_worker_locked(self) -> None:
        self.workers = [worker for worker in self.workers if worker.is_alive()]
        while self.queue and len(self.workers) < self.max_concurrent_downloads:
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"MissingModelsFetcherDownloader-{len(self.workers) + 1}",
                daemon=True,
            )
            self.workers.append(worker)
            worker.start()
        self.worker = self.workers[0] if self.workers else None

    def _worker_loop(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self.lock:
                    if (
                        current in self.workers
                        and self.workers.index(current) >= self.max_concurrent_downloads
                    ):
                        return
                task = self._next_task()
                if task is None:
                    return
                try:
                    self._run_task(task)
                finally:
                    with self.lock:
                        self._active_task_ids.discard(task.id)
                        provider_key = self._provider_concurrency_key(task)
                        count = self._active_provider_counts.get(provider_key, 0)
                        if count <= 1:
                            self._active_provider_counts.pop(provider_key, None)
                        else:
                            self._active_provider_counts[provider_key] = count - 1
        finally:
            with self.lock:
                self.workers = [
                    worker
                    for worker in self.workers
                    if worker is not current and worker.is_alive()
                ]
                self.worker = self.workers[0] if self.workers else None
                if self.queue:
                    self._ensure_worker_locked()

    def _next_task(self) -> DownloadTask | None:
        with self.lock:
            index = 0
            while index < len(self.queue):
                task_id = self.queue[index]
                task = self.tasks.get(task_id)
                if task is None or task.status != "queued":
                    self.queue.pop(index)
                    continue
                provider_key = self._provider_concurrency_key(task)
                if self._active_provider_counts.get(provider_key, 0) >= self.provider_concurrency:
                    index += 1
                    continue
                self.queue.pop(index)
                if task is not None:
                    task.status = "downloading"
                    task.pause_requested = False
                    task.cancel_requested = False
                    task.updated_at = time.time()
                    self._active_provider_counts[provider_key] = self._active_provider_counts.get(provider_key, 0) + 1
                    self._active_task_ids.add(task.id)
                    self._save_state_locked(force=True)
                    return task
            return None

    @staticmethod
    def _provider_concurrency_key(task: DownloadTask) -> str:
        if task.provider != "manual":
            return task.provider
        hostname = urlparse(task.normalized_url).hostname or "manual"
        return f"manual:{hostname.lower()}"

    def _run_task(self, task: DownloadTask) -> None:
        for attempt in range(MAX_RETRIES + 1):
            with self.lock:
                task.retries = attempt
                task.error = ""
                task.speed = 0.0
                task.eta = None
                task.verification_progress = None
                task.verification_speed = 0.0
                task.updated_at = time.time()
                self._save_state_locked(force=True)
            try:
                outcome = self._download_once(task)
                if outcome in {"completed", "paused", "canceled"}:
                    return
            except Exception as exc:
                terminal_error = isinstance(
                    exc,
                    (InsufficientDiskSpaceError, HashMismatchError),
                )
                with self.lock:
                    if task.cancel_requested:
                        task.status = "canceled"
                        task.error = ""
                        task.updated_at = time.time()
                        self._save_state_locked(force=True)
                        return
                    if task.pause_requested:
                        task.status = "paused"
                        task.error = ""
                        task.updated_at = time.time()
                        self._save_state_locked(force=True)
                        return
                    friendly_error = self._friendly_error(exc)
                    task.error = (
                        friendly_error
                        if terminal_error or attempt >= MAX_RETRIES
                        else f"网络连接暂时异常，正在自动重试（{attempt + 1}/{MAX_RETRIES}）：{friendly_error}"
                    )
                    task.restart_required = isinstance(exc, HashMismatchError)
                    task.status = (
                        "failed"
                        if terminal_error or attempt >= MAX_RETRIES
                        else "downloading"
                    )
                    task.updated_at = time.time()
                    self._save_state_locked(force=True)
                if terminal_error:
                    return
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    def _download_once(self, task: DownloadTask) -> str:
        os.makedirs(os.path.dirname(task.target_path), exist_ok=True)
        if os.path.isfile(task.target_path):
            self._verify_hash(task, task.target_path)
            with self.lock:
                task.downloaded = os.path.getsize(task.target_path)
                task.total = task.downloaded
                task.status = "completed"
                task.updated_at = time.time()
                self._save_state_locked(force=True)
            self.folders.refresh_directory(task.directory)
            return "completed"

        part_size = os.path.getsize(task.part_path) if os.path.exists(task.part_path) else 0
        request_url, headers = self._request_context(task.normalized_url)
        if part_size:
            headers["Range"] = f"bytes={part_size}-"

        started_at = time.time()
        last_update = started_at
        attempt_downloaded = 0

        try:
            response_context = _open_download_url(request_url, headers, timeout=60)
        except HTTPError as exc:
            response_headers = _header_map(exc.headers)
            total = _unsatisfied_range_total(response_headers.get("content-range"))
            if exc.code == 416 and part_size and total == part_size:
                with self.lock:
                    task.downloaded = part_size
                    task.total = total
                return self._finalize_part(task)
            raise

        with response_context as response:
            status = getattr(response, "status", response.getcode())
            if part_size and status == 200:
                part_size = 0
            mode = "ab" if part_size and status == 206 else "wb"
            response_headers = _header_map(response.headers)
            content_length = _header_int(response_headers.get("content-length"))
            total = _content_range_total(response_headers.get("content-range"))
            if total is None and content_length is not None:
                total = part_size + content_length

            self._check_task_disk_space(task, total, part_size)

            with self.lock:
                task.downloaded = part_size
                task.total = total
                task.updated_at = time.time()
                self._save_state_locked(force=True)

            with open(task.part_path, mode) as handle:
                while True:
                    with self.lock:
                        if task.cancel_requested:
                            task.status = "canceled"
                            task.updated_at = time.time()
                            self._save_state_locked(force=True)
                            return "canceled"
                        if task.pause_requested:
                            task.status = "paused"
                            task.updated_at = time.time()
                            self._save_state_locked(force=True)
                            return "paused"

                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self._throttle_download(len(chunk))
                    handle.write(chunk)

                    now = time.time()
                    attempt_downloaded += len(chunk)
                    with self.lock:
                        task.downloaded += len(chunk)
                        elapsed = max(now - started_at, 0.001)
                        task.speed = attempt_downloaded / elapsed
                        if task.total and task.speed > 0:
                            task.eta = max(task.total - task.downloaded, 0) / task.speed
                        if now - last_update > 0.25:
                            task.updated_at = now
                            last_update = now
                            self._save_state_locked()

        if task.total is not None and task.downloaded < task.total:
            raise IOError(f"连接提前中断，已下载 {task.downloaded} / {task.total} 字节")

        return self._finalize_part(task)

    def _finalize_part(self, task: DownloadTask) -> str:
        self._verify_hash(task, task.part_path)
        os.replace(task.part_path, task.target_path)
        with self.lock:
            task.downloaded = os.path.getsize(task.target_path)
            task.total = task.downloaded
            task.speed = 0.0
            task.eta = 0.0
            task.status = "completed"
            task.updated_at = time.time()
            self._save_state_locked(force=True)
        self.folders.refresh_directory(task.directory)
        return "completed"

    def _load_state_locked(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        items = raw.get("tasks") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return

        next_display_order = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                task = DownloadTask.from_dict(item)
            except (TypeError, ValueError):
                continue

            if "display_order" not in item:
                task.display_order = next_display_order
            next_display_order = max(next_display_order, task.display_order + 1)

            try:
                task.url = _strip_sensitive_query(task.url)
                task.normalized_url = normalize_download_url(task.normalized_url)
                directory, destination_root, target_path = self.folders.resolve_destination(
                    task.directory,
                    task.name,
                    task.destination_root,
                )
            except (TypeError, ValueError) as exc:
                logging.warning(
                    "[Missing Models Fetcher] Ignoring invalid persisted task %s: %s",
                    task.id,
                    exc,
                )
                continue

            task.directory = directory
            task.destination_root = destination_root
            task.target_path = target_path
            task.part_path = f"{target_path}.part"

            target_requires_verification = (
                os.path.isfile(task.target_path)
                and bool(task.hash.strip())
                and not task.verified_hash
            )
            if os.path.isfile(task.target_path) and not target_requires_verification:
                task.downloaded = os.path.getsize(task.target_path)
                task.total = task.downloaded
                task.status = "completed"
                task.speed = 0.0
                task.eta = 0.0
            elif target_requires_verification:
                task.downloaded = os.path.getsize(task.target_path)
                task.total = task.downloaded
                task.status = "queued"
            elif os.path.exists(task.part_path):
                task.downloaded = os.path.getsize(task.part_path)
            elif task.status == "downloading":
                task.status = "queued"
                task.downloaded = 0

            if task.status in RESUME_ON_START_STATUSES and not os.path.isfile(task.target_path):
                task.status = "queued"
                task.error = ""
                self.queue.append(task.id)
            elif task.status == "queued" and target_requires_verification:
                task.error = ""
                self.queue.append(task.id)

            if task.status == "queued":
                task.verification_progress = None
                task.verification_speed = 0.0

            task.pause_requested = False
            task.cancel_requested = False
            self.tasks[task.id] = task

    def _save_state_locked(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_state_save < STATE_SAVE_INTERVAL_SECONDS:
            return
        self._last_state_save = now
        os.makedirs(self.state_dir, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": now,
            "tasks": [
                task.to_dict()
                for task in sorted(self.tasks.values(), key=lambda item: (item.display_order, item.created_at))
            ],
        }
        fd, temp_path = tempfile.mkstemp(prefix="downloads-", suffix=".json", dir=self.state_dir, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            replace_error: OSError | None = None
            for attempt in range(STATE_REPLACE_RETRIES):
                try:
                    os.replace(temp_path, self.state_path)
                    replace_error = None
                    break
                except OSError as exc:
                    replace_error = exc
                    if attempt + 1 < STATE_REPLACE_RETRIES:
                        time.sleep(STATE_REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
            if replace_error is not None:
                logging.warning(
                    "[Missing Models Fetcher] Could not persist download queue state after %s attempts: %s",
                    STATE_REPLACE_RETRIES,
                    replace_error,
                )
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _range_probe(self, url: str, headers: dict[str, str]) -> tuple[dict[str, str], str]:
        range_headers = dict(headers)
        range_headers["Range"] = "bytes=0-0"
        request = Request(url, headers=range_headers)
        with _open_url(request, timeout=30) as response:
            return _header_map(response.headers), response.geturl()

    def _request_context(self, url: str) -> tuple[str, dict[str, str]]:
        self._check_source_resolution_deadline()
        self._resolution_metric("network_requests")
        headers = {"User-Agent": USER_AGENT}
        request_url = _strip_sensitive_query(url)
        parsed = _parse_http_url(request_url)
        host = parsed.hostname.lower() if parsed.hostname else ""
        secrets = self.secrets.load()
        if _host_matches(host, "huggingface.co") and secrets.get("hf_api_key"):
            headers["Authorization"] = f"Bearer {secrets['hf_api_key']}"
        elif _host_matches(host, "civitai.com") and secrets.get("civitai_api_key"):
            token = secrets["civitai_api_key"]
            headers["Authorization"] = f"Bearer {token}"
            if "/api/download/models/" in parsed.path:
                request_url = _append_query_param(request_url, "token", token)
        elif _is_modelscope_api_host(host) and secrets.get("modelscope_api_token"):
            token = secrets["modelscope_api_token"]
            headers["Authorization"] = f"Bearer {token}"
            headers["Cookie"] = f"m_session_id={token}"
        return request_url, headers

    def _verify_hash(self, task: DownloadTask, path: str) -> None:
        expected = task.hash.strip().lower()
        if not expected:
            return

        hash_name = _infer_hash_type(expected, task.hash_type)
        if hash_name not in {"sha256", "sha1", "md5"}:
            raise ValueError(f"不支持的 hash 类型: {task.hash_type or 'unknown'}")

        file_size = os.path.getsize(path)
        processed = 0
        started_at = time.time()
        last_update = started_at
        with self.lock:
            task.status = "verifying"
            task.speed = 0.0
            task.eta = None
            task.verification_progress = 0.0
            task.verification_speed = 0.0
            task.updated_at = started_at
            self._save_state_locked(force=True)

        digest = hashlib.new(hash_name)
        with open(path, "rb") as handle:
            while True:
                with self.lock:
                    if task.cancel_requested:
                        raise RuntimeError("Hash 校验已取消")
                    if task.pause_requested:
                        raise RuntimeError("Hash 校验已暂停")
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                processed += len(chunk)
                now = time.time()
                elapsed = max(now - started_at, 0.001)
                verification_speed = processed / elapsed
                if now - last_update > 0.25 or processed >= file_size:
                    with self.lock:
                        task.verification_progress = (
                            min(100.0, processed / file_size * 100)
                            if file_size
                            else 100.0
                        )
                        task.verification_speed = verification_speed
                        task.eta = (
                            max(file_size - processed, 0) / verification_speed
                            if verification_speed > 0
                            else None
                        )
                        task.updated_at = now
                        self._save_state_locked()
                    last_update = now
        actual = digest.hexdigest().lower()
        if actual != expected:
            raise HashMismatchError(f"Hash 校验失败: 期望 {expected}，实际 {actual}")
        with self.lock:
            task.hash_type = hash_name
            task.verified_hash = True
            task.verification_progress = 100.0
            task.verification_speed = 0.0
            task.eta = 0.0
            task.updated_at = time.time()
            self._save_state_locked(force=True)

    def _friendly_error(self, exc: Exception) -> str:
        if isinstance(exc, HTTPError):
            failed_url = exc.geturl() if hasattr(exc, "geturl") else ""
            if exc.code == 403 and _is_hf_xet_cdn_url(failed_url):
                return (
                    "Hugging Face 已返回下载地址，但 Xet CDN 拒绝访问，HTTP 403。"
                    "这通常是签名链接、区域网络或代理链路异常，请稍后重试或切换代理。"
                )
            if exc.code in {401, 403}:
                return (
                    f"认证失败或没有模型访问权限，HTTP {exc.code}。"
                    "请检查 API Key、账号授权和模型许可。"
                )
            if exc.code == 404:
                return "模型文件或下载地址不存在，HTTP 404。请重新解析下载来源。"
            if exc.code == 407:
                return "代理认证失败，HTTP 407。请检查代理用户名和密码。"
            if exc.code in {408, 504}:
                return f"请求超时，HTTP {exc.code}。请稍后重试或切换网络/代理。"
            if exc.code == 429:
                return "下载站点请求过于频繁，HTTP 429。请稍后重试。"
            if 500 <= exc.code <= 599:
                return f"下载站点暂时不可用，HTTP {exc.code}。请稍后重试。"
            return f"HTTP {exc.code}: {exc.reason}"
        if isinstance(exc, URLError):
            return self._friendly_network_error(exc.reason)
        return str(exc)

    @staticmethod
    def _friendly_network_error(reason: Any, provider_name: str = "") -> str:
        prefix = f"{provider_name} " if provider_name else ""
        text = str(reason or "未知网络错误")
        lower = text.lower()
        if isinstance(reason, socket.gaierror) or any(
            marker in lower
            for marker in ("getaddrinfo failed", "name or service not known", "nodename nor servname")
        ):
            return f"{prefix}DNS 解析失败：无法解析下载站点或代理主机。请检查网络、DNS 和代理地址。"
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in lower:
            return f"{prefix}请求超时。请稍后重试或切换网络/代理。"
        if isinstance(reason, ConnectionRefusedError) or any(
            marker in lower for marker in ("connection refused", "actively refused")
        ):
            if _PROXY_MODE == "custom":
                return f"{prefix}代理不可用或拒绝连接。请检查代理地址、端口和运行状态。"
            return f"{prefix}目标站点拒绝连接。请稍后重试或检查网络设置。"
        if isinstance(reason, ssl.SSLError) or any(
            marker in lower for marker in ("certificate verify failed", "ssl:", "tls")
        ):
            return f"{prefix}TLS 安全连接失败。请检查系统时间、证书链或代理的 HTTPS 支持。"
        if "proxy" in lower:
            return f"{prefix}代理连接失败：{text}"
        return f"{prefix}网络连接失败：{text}"

    def friendly_key_test_error(self, provider: str, exc: Exception) -> str:
        provider_name = PROVIDER_NAMES.get(provider, provider)
        if isinstance(exc, HTTPError) and exc.code in {401, 403}:
            return f"{provider_name} API Key 无效、已过期或权限不足，HTTP {exc.code}"
        if isinstance(exc, HTTPError):
            return f"{provider_name} 验证失败，HTTP {exc.code}: {exc.reason}"
        if isinstance(exc, URLError):
            return self._friendly_network_error(exc.reason, provider_name)
        return str(exc)
