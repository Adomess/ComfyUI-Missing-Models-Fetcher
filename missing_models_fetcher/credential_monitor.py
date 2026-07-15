from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import SecretsStore
from .downloader import DownloadManager, PROVIDER_NAMES


PROVIDER_SECRET_KEYS = {
    "hf": "hf_api_key",
    "modelscope": "modelscope_api_token",
    "civitai": "civitai_api_key",
}
DEFAULT_VALIDATION_INTERVAL_SECONDS = 30 * 60


class CredentialValidationMonitor:
    def __init__(
        self,
        secrets: SecretsStore,
        downloads: DownloadManager,
        interval_seconds: int = DEFAULT_VALIDATION_INTERVAL_SECONDS,
    ) -> None:
        self.secrets = secrets
        self.downloads = downloads
        self.interval_seconds = max(60, int(interval_seconds))
        self.lock = threading.RLock()
        self.validation_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.checking = False
        self.checked_at: float | None = None
        self.providers = {
            provider: self._unconfigured_result(provider)
            for provider in PROVIDER_SECRET_KEYS
        }

    @staticmethod
    def _unconfigured_result(provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "name": PROVIDER_NAMES[provider],
            "configured": False,
            "status": "unconfigured",
            "message": "未配置",
            "account": "",
            "checked_at": None,
        }

    def start(self) -> None:
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return
            self.worker = threading.Thread(
                target=self._worker_loop,
                name="MissingModelsFetcherCredentialMonitor",
                daemon=True,
            )
            self.worker.start()

    def invalidate(self, provider: str | None = None) -> None:
        with self.lock:
            targets = [provider] if provider in PROVIDER_SECRET_KEYS else list(PROVIDER_SECRET_KEYS)
            for target in targets:
                self.providers[target] = {
                    **self._unconfigured_result(target),
                    "status": "pending",
                    "message": "等待验证",
                }

    def validate_all(self) -> dict[str, Any]:
        if not self.validation_lock.acquire(blocking=False):
            with self.validation_lock:
                pass
            return self.snapshot()
        with self.lock:
            self.checking = True
        try:
            stored = self.secrets.load()
            checked_at = time.time()
            results: dict[str, dict[str, Any]] = {}
            for provider, secret_key in PROVIDER_SECRET_KEYS.items():
                if not str(stored.get(secret_key) or "").strip():
                    results[provider] = self._unconfigured_result(provider)
                    continue
                results[provider] = self._validate_provider_result(provider, checked_at)
            with self.lock:
                self.providers = results
                self.checked_at = checked_at
        finally:
            with self.lock:
                self.checking = False
            self.validation_lock.release()
        return self.snapshot()

    def validate_provider(self, provider: str) -> dict[str, Any]:
        if provider not in PROVIDER_SECRET_KEYS:
            raise ValueError("未知的 API Key 类型")
        with self.validation_lock:
            checked_at = time.time()
            stored = self.secrets.load()
            if not str(stored.get(PROVIDER_SECRET_KEYS[provider]) or "").strip():
                result = self._unconfigured_result(provider)
            else:
                result = self._validate_provider_result(provider, checked_at)
            with self.lock:
                self.providers[provider] = result
                self.checked_at = checked_at
        return dict(result)

    def _validate_provider_result(self, provider: str, checked_at: float) -> dict[str, Any]:
        try:
            result = self.downloads.test_api_key(provider)
            account = str(result.get("account") or "").strip()
            message = "验证通过"
            if account:
                message += f"，账号：{account}"
            return {
                "provider": provider,
                "name": PROVIDER_NAMES[provider],
                "configured": True,
                "status": "valid",
                "message": message,
                "account": account,
                "checked_at": checked_at,
            }
        except Exception as exc:
            message = self.downloads.friendly_key_test_error(provider, exc)
            return {
                "provider": provider,
                "name": PROVIDER_NAMES[provider],
                "configured": True,
                "status": self._error_status(message),
                "message": message,
                "account": "",
                "checked_at": checked_at,
            }

    @staticmethod
    def _error_status(message: str) -> str:
        lowered = message.lower()
        if any(marker in lowered for marker in ("http 401", "http 403", "无效", "已过期", "权限不足")):
            return "invalid"
        return "warning"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            providers = {
                provider: dict(result)
                for provider, result in self.providers.items()
            }
            problems = [
                result
                for result in providers.values()
                if result["configured"] and result["status"] in {"invalid", "warning"}
            ]
            return {
                "checking": self.checking,
                "checked_at": self.checked_at,
                "interval_seconds": self.interval_seconds,
                "has_problem": bool(problems),
                "providers": providers,
            }

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.validate_all()
            except Exception:
                logging.exception("[Missing Models Fetcher] Credential validation monitor failed")
            self.stop_event.wait(self.interval_seconds)
