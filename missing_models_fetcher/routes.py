from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from server import PromptServer

from . import API_PREFIX
from .config import SecretsStore
from .credential_monitor import CredentialValidationMonitor
from .downloader import DownloadManager
from .folders import FolderRegistry
from .scanner import WorkflowScanner


_REGISTERED = False


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def register_routes() -> None:
    global _REGISTERED
    if _REGISTERED:
        return

    folders = FolderRegistry()
    secrets = SecretsStore()
    scanner = WorkflowScanner(folders)
    downloads = DownloadManager(secrets, folders)
    credential_monitor = CredentialValidationMonitor(secrets, downloads)
    routes = PromptServer.instance.routes

    @routes.get(f"{API_PREFIX}/health")
    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "name": "ComfyUI Missing Models Fetcher"})

    @routes.get(f"{API_PREFIX}/config")
    async def get_config(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "config": secrets.snapshot().to_dict()})

    @routes.post(f"{API_PREFIX}/config")
    async def update_config(request: web.Request) -> web.Response:
        data = await _body(request)
        try:
            config = secrets.update(data)
        except ValueError as exc:
            return _error(str(exc))
        downloads.set_concurrency(config.download_concurrency)
        downloads.set_runtime_limits(config.provider_concurrency, config.bandwidth_limit_mbps)
        downloads.reload_proxy_config()
        credential_monitor.invalidate()
        return web.json_response({"ok": True, "config": config.to_dict()})

    @routes.post(f"{API_PREFIX}/config/clear")
    async def clear_config(request: web.Request) -> web.Response:
        data = await _body(request)
        provider = data.get("provider")
        if provider not in {None, "hf", "civitai", "modelscope"}:
            return _error("未知的密钥类型")
        config = secrets.clear(provider)
        downloads.set_concurrency(config.download_concurrency)
        downloads.set_runtime_limits(config.provider_concurrency, config.bandwidth_limit_mbps)
        downloads.reload_proxy_config()
        credential_monitor.invalidate(provider)
        return web.json_response({"ok": True, "config": config.to_dict()})

    @routes.post(f"{API_PREFIX}/config/test")
    async def test_config(request: web.Request) -> web.Response:
        data = await _body(request)
        provider = str(data.get("provider") or "").strip()
        if provider not in {"hf", "civitai", "modelscope"}:
            return _error("未知的 API Key 类型")
        api_key_value = data.get("api_key")
        api_key = str(api_key_value).strip() if isinstance(api_key_value, str) else None
        try:
            result = await asyncio.to_thread(downloads.test_api_key, provider, api_key)
        except Exception as exc:
            return _error(downloads.friendly_key_test_error(provider, exc))
        return web.json_response({"ok": True, "result": result})

    @routes.post(f"{API_PREFIX}/config/proxy/test")
    async def test_proxy(request: web.Request) -> web.Response:
        data = await _body(request)
        profile_id = str(data.get("profile_id") or "")
        try:
            result = await asyncio.to_thread(downloads.test_proxy_profile, profile_id)
        except Exception as exc:
            return _error(downloads._friendly_error(exc))
        return web.json_response({"ok": True, "result": result})

    @routes.get(f"{API_PREFIX}/config/validation")
    async def get_config_validation(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "validation": credential_monitor.snapshot()})

    @routes.post(f"{API_PREFIX}/config/validation")
    async def refresh_config_validation(request: web.Request) -> web.Response:
        validation = await asyncio.to_thread(credential_monitor.validate_all)
        return web.json_response({"ok": True, "validation": validation})

    @routes.post(f"{API_PREFIX}/config/validation/{{provider}}")
    async def refresh_provider_validation(request: web.Request) -> web.Response:
        provider = str(request.match_info["provider"] or "").strip().lower()
        if provider not in {"hf", "civitai", "modelscope"}:
            return _error("未知的 API Key 类型")
        result = await asyncio.to_thread(credential_monitor.validate_provider, provider)
        return web.json_response({
            "ok": True,
            "result": result,
            "validation": credential_monitor.snapshot(),
        })

    @routes.get(f"{API_PREFIX}/folders")
    async def list_folders(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "folders": folders.all_folders()})

    @routes.post(f"{API_PREFIX}/scan")
    async def scan_workflow(request: web.Request) -> web.Response:
        data = await _body(request)
        workflow = data.get("workflow")
        if workflow is None:
            return _error("请求中缺少 workflow")
        return web.json_response({"ok": True, "models": scanner.scan(workflow)})

    @routes.post(f"{API_PREFIX}/metadata")
    async def get_remote_metadata(request: web.Request) -> web.Response:
        data = await _body(request)
        url = str(data.get("url") or "").strip()
        if not url:
            return _error("请求中缺少 url")
        try:
            metadata = await asyncio.to_thread(downloads.remote_metadata, url)
        except Exception as exc:
            return _error(downloads._friendly_error(exc))
        return web.json_response({"ok": True, "metadata": metadata})

    @routes.post(f"{API_PREFIX}/sources/resolve")
    async def resolve_sources(request: web.Request) -> web.Response:
        data = await _body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return _error("没有需要解析下载源的模型")
        clean_items = [item for item in items if isinstance(item, dict)]
        if not clean_items:
            return _error("下载源解析参数无效")
        results = await asyncio.to_thread(downloads.resolve_sources, clean_items)
        return web.json_response({"ok": True, "models": results})

    @routes.post(f"{API_PREFIX}/sources/resolve/provider")
    async def resolve_source_provider(request: web.Request) -> web.Response:
        data = await _body(request)
        item = data.get("item")
        provider = str(data.get("provider") or "").strip().lower()
        job_id = str(data.get("job_id") or "").strip()
        if not isinstance(item, dict):
            return _error("下载源解析参数无效")
        if provider not in {"hf", "modelscope", "civitai"}:
            return _error("下载站点无效")
        result = await asyncio.to_thread(
            downloads.resolve_source_provider,
            item,
            provider,
            job_id,
        )
        return web.json_response({"ok": True, "model": result})

    @routes.post(f"{API_PREFIX}/sources/resolve/cancel")
    async def cancel_source_resolution(request: web.Request) -> web.Response:
        data = await _body(request)
        job_ids = data.get("job_ids")
        if not isinstance(job_ids, list):
            return _error("下载源解析任务参数无效")
        canceled = downloads.cancel_resolution_jobs(
            [str(job_id) for job_id in job_ids if str(job_id or "").strip()]
        )
        return web.json_response({"ok": True, "canceled": canceled})

    @routes.post(f"{API_PREFIX}/manual/parse")
    async def parse_manual_items(request: web.Request) -> web.Response:
        data = await _body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return _error("没有需要解析的模型链接或名称")
        clean_items = [item for item in items if isinstance(item, dict)]
        if not clean_items:
            return _error("手动解析参数无效")
        resolve_sources = data.get("resolve_sources") is not False
        results = await asyncio.to_thread(
            downloads.prepare_manual_items,
            clean_items,
            resolve_sources,
        )
        return web.json_response({"ok": True, "models": results})

    @routes.get(f"{API_PREFIX}/downloads")
    async def get_downloads(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "queue": downloads.snapshot()})

    @routes.post(f"{API_PREFIX}/downloads")
    async def create_downloads(request: web.Request) -> web.Response:
        data = await _body(request)
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return _error("没有可下载的模型任务")
        try:
            created = downloads.enqueue([item for item in items if isinstance(item, dict)])
        except Exception as exc:
            logging.exception("[Missing Models Fetcher] Failed to enqueue download")
            return _error(str(exc))
        return web.json_response({"ok": True, "tasks": created, "queue": downloads.snapshot()})

    @routes.post(f"{API_PREFIX}/downloads/clear")
    async def clear_downloads(request: web.Request) -> web.Response:
        data = await _body(request)
        statuses = data.get("statuses")
        clean_statuses = None
        if statuses is not None:
            if not isinstance(statuses, list):
                return _error("statuses 必须是数组")
            clean_statuses = {str(status) for status in statuses if isinstance(status, str)}
        task_ids = data.get("task_ids")
        clean_task_ids = None
        if task_ids is not None:
            if not isinstance(task_ids, list):
                return _error("task_ids 必须是数组")
            clean_task_ids = {str(task_id) for task_id in task_ids if isinstance(task_id, str)}
        return web.json_response(
            {
                "ok": True,
                "queue": downloads.clear(clean_statuses, clean_task_ids),
            }
        )

    @routes.post(f"{API_PREFIX}/downloads/bulk/{{action}}")
    async def bulk_download_control(request: web.Request) -> web.Response:
        try:
            queue = downloads.bulk_control(request.match_info["action"])
        except ValueError as exc:
            return _error(str(exc))
        return web.json_response({"ok": True, "queue": queue})

    @routes.post(f"{API_PREFIX}/downloads/{{task_id}}/move/{{direction}}")
    async def move_download(request: web.Request) -> web.Response:
        try:
            queue = downloads.move_queued_task(
                request.match_info["task_id"],
                request.match_info["direction"],
            )
        except (KeyError, ValueError) as exc:
            return _error(str(exc))
        return web.json_response({"ok": True, "queue": queue})

    @routes.post(f"{API_PREFIX}/downloads/{{task_id}}/priority")
    async def set_download_priority(request: web.Request) -> web.Response:
        data = await _body(request)
        try:
            queue = downloads.set_task_priority(request.match_info["task_id"], data.get("priority"))
        except KeyError as exc:
            return _error(str(exc), status=404)
        except ValueError as exc:
            return _error(str(exc))
        return web.json_response({"ok": True, "queue": queue})

    @routes.post(f"{API_PREFIX}/downloads/{{task_id}}/pause")
    async def pause_download(request: web.Request) -> web.Response:
        try:
            task = downloads.pause(request.match_info["task_id"])
        except KeyError as exc:
            return _error(str(exc), status=404)
        return web.json_response({"ok": True, "task": task, "queue": downloads.snapshot()})

    @routes.post(f"{API_PREFIX}/downloads/{{task_id}}/resume")
    async def resume_download(request: web.Request) -> web.Response:
        try:
            task = downloads.resume(request.match_info["task_id"])
        except KeyError as exc:
            return _error(str(exc), status=404)
        return web.json_response({"ok": True, "task": task, "queue": downloads.snapshot()})

    @routes.post(f"{API_PREFIX}/downloads/{{task_id}}/cancel")
    async def cancel_download(request: web.Request) -> web.Response:
        try:
            task = downloads.cancel(request.match_info["task_id"])
        except KeyError as exc:
            return _error(str(exc), status=404)
        return web.json_response({"ok": True, "task": task, "queue": downloads.snapshot()})

    @routes.post(f"{API_PREFIX}/downloads/{{task_id}}/restart")
    async def restart_download(request: web.Request) -> web.Response:
        try:
            task = downloads.restart(request.match_info["task_id"])
        except KeyError as exc:
            return _error(str(exc), status=404)
        except (OSError, ValueError) as exc:
            return _error(str(exc))
        return web.json_response({"ok": True, "task": task, "queue": downloads.snapshot()})

    credential_monitor.start()
    _REGISTERED = True
