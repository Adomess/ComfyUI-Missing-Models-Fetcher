from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
    urlopen,
)


SAMPLES = {
    "hf": {
        "queue_url": "https://huggingface.co/optimum-intel-internal-testing/tiny-random-bert/resolve/main/model.safetensors",
        "probe_url": "https://huggingface.co/optimum-intel-internal-testing/tiny-random-bert/resolve/main/model.safetensors",
        "size": 520212,
        "sha256": "965f02b6a7e5520fc12f710e4e3b6132f697f1c8f648819553c5ade86752d2de",
    },
    "modelscope": {
        "queue_url": "https://modelscope.cn/models/CrabInHoney/urlbert-tiny-base-v4/resolve/master/model.safetensors",
        "probe_url": "https://modelscope.cn/models/CrabInHoney/urlbert-tiny-base-v4/resolve/master/model.safetensors",
        "size": 14912996,
        "sha256": "1593cb4109cc7a6c44955214c97f75169a86217372300c2653fab9b6bd25ecab",
    },
    "civitai": {
        "queue_url": "https://civitai.com/models/872135/nistyle-manga-sketch-and-detail?modelVersionId=976226",
        "probe_url": "https://civitai.com/api/download/models/976226",
        "size": 19314680,
        "sha256": "8f867bf357f788f9f8a56d21f71c76b82bf74749677aaaacc72661b948ab1b5c",
    },
}

TERMINAL_STATUSES = {"completed", "canceled", "failed"}


def request_json(url: str, payload: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError(str(result.get("error") if isinstance(result, dict) else result))
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def queue_tasks(api_root: str, timeout: int) -> list[dict[str, Any]]:
    queue = request_json(f"{api_root}/downloads", None, timeout).get("queue") or {}
    tasks = queue.get("tasks") or []
    return [task for task in tasks if isinstance(task, dict)]


def task_by_id(api_root: str, task_id: str, timeout: int) -> dict[str, Any] | None:
    return next((task for task in queue_tasks(api_root, timeout) if task.get("id") == task_id), None)


def wait_for_task(
    api_root: str,
    task_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: int,
    description: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last = task_by_id(api_root, task_id, min(timeout, 10))
        except Exception:
            time.sleep(0.2)
            continue
        if last and predicate(last):
            return last
        if last and last.get("status") == "failed":
            raise RuntimeError(f"{description}失败: {last.get('error')}")
        time.sleep(0.2)
    raise TimeoutError(f"等待{description}超时，最后状态: {last}")


def add_query(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(query)))


class RecordingRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        self.hops: list[dict[str, Any]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = (urlparse(req.full_url).hostname or "").lower()
        new_host = (urlparse(newurl).hostname or "").lower()
        self.hops.append({"status": code, "from_host": old_host, "to_host": new_host})
        if old_host != new_host:
            for store in (redirected.headers, redirected.unredirected_hdrs):
                for header in list(store):
                    if header.lower() in {"authorization", "cookie", "proxy-authorization"}:
                        redirected.remove_header(header)
        return redirected


def load_private_credentials(storage_dir: str) -> dict[str, str]:
    path = Path(storage_dir) / "secrets.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        key: str(raw.get(key) or "")
        for key in ("hf_api_key", "modelscope_api_token", "civitai_api_key")
    }


def probe_range(provider: str, storage_dir: str, timeout: int) -> dict[str, Any]:
    sample = SAMPLES[provider]
    credentials = load_private_credentials(storage_dir)
    url = str(sample["probe_url"])
    headers = {"User-Agent": "ComfyUI-Missing-Models-Fetcher-E2E/1.0", "Range": "bytes=0-0"}
    if provider == "hf" and credentials.get("hf_api_key"):
        headers["Authorization"] = f"Bearer {credentials['hf_api_key']}"
    elif provider == "modelscope" and credentials.get("modelscope_api_token"):
        token = credentials["modelscope_api_token"]
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"m_session_id={token}"
    elif provider == "civitai" and credentials.get("civitai_api_key"):
        token = credentials["civitai_api_key"]
        headers["Authorization"] = f"Bearer {token}"
        url = add_query(url, "token", token)

    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    last_error: Exception | None = None
    for attempt in range(3):
        redirect_handler = RecordingRedirectHandler()
        opener = build_opener(ProxyHandler({}), redirect_handler, HTTPSHandler(context=context))
        try:
            with opener.open(Request(url, headers=headers), timeout=timeout) as response:
                response.read(1)
                status = int(getattr(response, "status", response.getcode()))
                final_host = (urlparse(response.geturl()).hostname or "").lower()
                content_range = str(response.headers.get("Content-Range") or "")
                accept_ranges = str(response.headers.get("Accept-Ranges") or "")
            break
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    else:
        raise RuntimeError(f"{provider} Range 探针失败: {last_error}")
    if status != 206 or not content_range.lower().startswith("bytes 0-0/"):
        raise RuntimeError(
            f"{provider} 未返回可续传的 206 Content-Range: status={status}, content-range={content_range!r}"
        )
    if not redirect_handler.hops:
        raise RuntimeError(f"{provider} 固定样本未观察到 CDN 跳转")
    return {
        "status": status,
        "content_range": content_range,
        "accept_ranges": accept_ranges,
        "redirects": redirect_handler.hops,
        "final_host": final_host,
    }


def find_destination_root(api_root: str, timeout: int, preferred_fragment: str) -> str:
    folders = request_json(f"{api_root}/folders", None, timeout).get("folders") or []
    checkpoint = next(
        (item for item in folders if isinstance(item, dict) and item.get("directory") == "checkpoints"),
        None,
    )
    if not checkpoint:
        raise RuntimeError("ComfyUI 未注册 checkpoints 目录")
    paths = [
        item
        for item in checkpoint.get("paths") or []
        if isinstance(item, dict) and item.get("writable")
    ]
    preferred = next(
        (item for item in paths if preferred_fragment.lower() in str(item.get("path") or "").lower()),
        None,
    )
    selected = preferred or (paths[0] if paths else None)
    if not selected:
        raise RuntimeError("checkpoints 没有可写的注册路径")
    return str(selected["path"])


def enqueue_sample(
    api_root: str,
    provider: str,
    relative_name: str,
    destination_root: str,
    timeout: int,
) -> dict[str, Any]:
    sample = SAMPLES[provider]
    payload = {
        "items": [
            {
                "name": relative_name,
                "url": sample["queue_url"],
                "directory": "checkpoints",
                "destination_path": destination_root,
                "size": sample["size"],
                "hash": sample["sha256"],
                "hash_type": "sha256",
            }
        ]
    }
    result = request_json(f"{api_root}/downloads", payload, timeout)
    tasks = result.get("tasks") or []
    if len(tasks) != 1:
        raise RuntimeError(f"{provider} 入队未返回唯一任务")
    return tasks[0]


def verify_completed(task: dict[str, Any], provider: str) -> dict[str, Any]:
    sample = SAMPLES[provider]
    path = Path(str(task["target_path"]))
    if task.get("status") != "completed" or not path.is_file():
        raise RuntimeError(f"{provider} 下载未完成")
    actual_size = path.stat().st_size
    actual_sha256 = sha256_file(path)
    if actual_size != sample["size"]:
        raise RuntimeError(f"{provider} 文件大小错误: {actual_size} != {sample['size']}")
    if actual_sha256 != sample["sha256"]:
        raise RuntimeError(f"{provider} SHA-256 错误: {actual_sha256}")
    if not task.get("verified_hash"):
        raise RuntimeError(f"{provider} 队列未标记 verified_hash")
    return {
        "size": actual_size,
        "sha256": actual_sha256,
        "verified_hash": True,
        "target_path": str(path),
    }


def update_runtime_limits(api_root: str, timeout: int, bandwidth: float) -> None:
    request_json(
        f"{api_root}/config",
        {
            "download_concurrency": 1,
            "provider_concurrency": 1,
            "bandwidth_limit_mbps": bandwidth,
        },
        timeout,
    )


def cleanup(
    api_root: str,
    task_ids: list[str],
    run_root: Path | None,
    timeout: int,
) -> None:
    for task_id in task_ids:
        try:
            task = task_by_id(api_root, task_id, timeout)
            if task and task.get("status") not in TERMINAL_STATUSES:
                request_json(f"{api_root}/downloads/{task_id}/cancel", {}, timeout)
                wait_for_task(
                    api_root,
                    task_id,
                    lambda value: value.get("status") in TERMINAL_STATUSES,
                    min(timeout, 30),
                    "清理测试任务",
                )
        except Exception:
            pass
    if run_root and run_root.is_dir() and run_root.name.startswith("run-"):
        shutil.rmtree(run_root)
        try:
            run_root.parent.rmdir()
        except OSError:
            pass
    if task_ids:
        try:
            request_json(f"{api_root}/downloads/clear", {"task_ids": task_ids}, timeout)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated live download-queue E2E checks for Hugging Face, ModelScope, and Civitai."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8188")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--restart-bat", default=r"C:\ComfyUI\restart_comfyui.bat")
    parser.add_argument(
        "--preferred-root-fragment",
        default=r"C:\ComfyUI\ComfyUI\models\checkpoints",
    )
    args = parser.parse_args()

    api_root = f"{args.base_url.rstrip('/')}/missing-models-fetcher"
    config = request_json(f"{api_root}/config", None, args.timeout).get("config") or {}
    original_limits = {
        "download_concurrency": config.get("download_concurrency"),
        "provider_concurrency": config.get("provider_concurrency"),
        "bandwidth_limit_mbps": config.get("bandwidth_limit_mbps"),
    }
    original_proxy_mode = str(config.get("proxy_mode") or "off")
    if original_proxy_mode != "off" or config.get("proxy_enabled"):
        raise RuntimeError("该测试要求插件代理保持停用；当前配置不是 off，脚本不会修改代理")

    baseline_tasks = queue_tasks(api_root, args.timeout)
    active_baseline = [
        task for task in baseline_tasks if task.get("status") in {"queued", "downloading", "verifying", "paused"}
    ]
    if active_baseline:
        raise RuntimeError("存在用户活动或暂停任务；为避免干扰，拒绝运行真实 E2E")

    destination_root = find_destination_root(
        api_root,
        args.timeout,
        args.preferred_root_fragment,
    )
    run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    relative_root = f"__mmf_live_e2e__/{run_id}"
    run_root = (Path(destination_root) / "__mmf_live_e2e__" / run_id).resolve()
    destination_resolved = Path(destination_root).resolve()
    if destination_resolved not in run_root.parents:
        raise RuntimeError("隔离测试目录越界")

    report: dict[str, Any] = {
        "ok": False,
        "network": {"proxy_mode": original_proxy_mode},
        "destination_root": destination_root,
        "run_root": str(run_root),
        "providers": {},
        "pause_resume": None,
        "restart_resume": None,
        "cleanup": None,
        "config_restored": False,
    }
    task_ids: list[str] = []

    try:
        for provider in SAMPLES:
            print(f"[e2e] probing {provider} redirect and Range support", file=sys.stderr, flush=True)
            report["providers"][provider] = {"range_probe": probe_range(provider, config["storage_dir"], args.timeout)}

        print("[e2e] testing Hugging Face queue download", file=sys.stderr, flush=True)
        update_runtime_limits(api_root, args.timeout, bandwidth=4.0)
        hf_task = enqueue_sample(
            api_root,
            "hf",
            f"{relative_root}/hf/model.safetensors",
            destination_root,
            args.timeout,
        )
        task_ids.append(str(hf_task["id"]))
        hf_completed = wait_for_task(
            api_root,
            str(hf_task["id"]),
            lambda task: task.get("status") == "completed",
            args.timeout,
            "Hugging Face 下载完成",
        )
        report["providers"]["hf"]["download"] = verify_completed(hf_completed, "hf")

        print("[e2e] testing ModelScope restart recovery", file=sys.stderr, flush=True)
        update_runtime_limits(api_root, args.timeout, bandwidth=4.0)
        modelscope_task = enqueue_sample(
            api_root,
            "modelscope",
            f"{relative_root}/modelscope/model.safetensors",
            destination_root,
            args.timeout,
        )
        task_ids.append(str(modelscope_task["id"]))
        modelscope_partial = wait_for_task(
            api_root,
            str(modelscope_task["id"]),
            lambda task: task.get("status") == "downloading" and int(task.get("downloaded") or 0) >= 1048576,
            args.timeout,
            "ModelScope 生成重启断点",
        )
        restart_part_size = Path(str(modelscope_partial["part_path"])).stat().st_size
        subprocess.run(
            ["cmd.exe", "/d", "/c", str(Path(args.restart_bat))],
            check=True,
            timeout=45,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            try:
                request_json(f"{api_root}/health", None, 5)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise TimeoutError("ComfyUI 重启后健康接口未恢复")
        modelscope_recovered = wait_for_task(
            api_root,
            str(modelscope_task["id"]),
            lambda task: task.get("status") in {"downloading", "verifying", "completed"}
            and int(task.get("downloaded") or 0) >= restart_part_size,
            args.timeout,
            "ModelScope 重启恢复",
        )
        modelscope_completed = wait_for_task(
            api_root,
            str(modelscope_task["id"]),
            lambda task: task.get("status") == "completed",
            args.timeout,
            "ModelScope 下载完成",
        )
        report["restart_resume"] = {
            "provider": "modelscope",
            "part_size_before_restart": restart_part_size,
            "downloaded_after_restart": int(modelscope_recovered.get("downloaded") or 0),
            "health_recovered": True,
        }
        report["providers"]["modelscope"]["download"] = verify_completed(
            modelscope_completed,
            "modelscope",
        )

        print("[e2e] testing Civitai pause and resume", file=sys.stderr, flush=True)
        civitai_task = enqueue_sample(
            api_root,
            "civitai",
            f"{relative_root}/civitai/model.safetensors",
            destination_root,
            args.timeout,
        )
        task_ids.append(str(civitai_task["id"]))
        civitai_partial = wait_for_task(
            api_root,
            str(civitai_task["id"]),
            lambda task: task.get("status") == "downloading" and int(task.get("downloaded") or 0) >= 1048576,
            args.timeout,
            "Civitai 生成暂停断点",
        )
        request_json(f"{api_root}/downloads/{civitai_task['id']}/pause", {}, args.timeout)
        civitai_paused = wait_for_task(
            api_root,
            str(civitai_task["id"]),
            lambda task: task.get("status") == "paused",
            args.timeout,
            "Civitai 暂停",
        )
        paused_size = Path(str(civitai_paused["part_path"])).stat().st_size
        if not 0 < paused_size < int(SAMPLES["civitai"]["size"]):
            raise RuntimeError(f"Civitai .part 大小无效: {paused_size}")
        request_json(f"{api_root}/downloads/{civitai_task['id']}/resume", {}, args.timeout)
        civitai_resumed = wait_for_task(
            api_root,
            str(civitai_task["id"]),
            lambda task: task.get("status") == "downloading" and int(task.get("downloaded") or 0) >= paused_size,
            args.timeout,
            "Civitai Range 续传",
        )
        civitai_completed = wait_for_task(
            api_root,
            str(civitai_task["id"]),
            lambda task: task.get("status") == "completed",
            args.timeout,
            "Civitai 下载完成",
        )
        report["pause_resume"] = {
            "provider": "civitai",
            "part_size": paused_size,
            "resumed_downloaded": int(civitai_resumed.get("downloaded") or 0),
            "pre_pause_observed": int(civitai_partial.get("downloaded") or 0),
        }
        report["providers"]["civitai"]["download"] = verify_completed(civitai_completed, "civitai")
        report["ok"] = True
    finally:
        try:
            request_json(f"{api_root}/config", original_limits, args.timeout)
            restored = request_json(f"{api_root}/config", None, args.timeout).get("config") or {}
            report["config_restored"] = all(
                restored.get(key) == value for key, value in original_limits.items()
            ) and str(restored.get("proxy_mode") or "off") == original_proxy_mode
        finally:
            cleanup(api_root, task_ids, run_root, args.timeout)
            remaining_ids = {task.get("id") for task in queue_tasks(api_root, args.timeout)}
            report["cleanup"] = {
                "run_root_removed": not run_root.exists(),
                "test_tasks_removed": not any(task_id in remaining_ids for task_id in task_ids),
                "baseline_task_count": len(baseline_tasks),
                "final_task_count": len(remaining_ids),
            }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    success = bool(report["ok"] and report["config_restored"])
    success = success and bool(
        report["cleanup"]["run_root_removed"]
        and report["cleanup"]["test_tasks_removed"]
        and report["cleanup"]["baseline_task_count"] == report["cleanup"]["final_task_count"]
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
