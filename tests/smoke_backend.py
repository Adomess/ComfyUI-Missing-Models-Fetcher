from __future__ import annotations

import json
import os
import sys
import hashlib
import io
import select
import socket
import socketserver
import struct
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request


COMFYUI_ROOT = os.environ.get("COMFYUI_ROOT")
if not COMFYUI_ROOT:
    raise RuntimeError("Set COMFYUI_ROOT to your ComfyUI root directory before running this smoke test.")
if COMFYUI_ROOT not in sys.path:
    sys.path.insert(0, COMFYUI_ROOT)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import folder_paths  # noqa: E402
from missing_models_fetcher.config import SecretsStore  # noqa: E402
from missing_models_fetcher.credential_monitor import CredentialValidationMonitor  # noqa: E402
from missing_models_fetcher.downloader import (  # noqa: E402
    DownloadManager,
    DownloadTask,
    HashMismatchError,
    InsufficientDiskSpaceError,
    _SafeRedirectHandler,
    _configure_proxy,
    _open_download_url,
    _parse_hf_file_url,
    _parse_modelscope_file_url,
    _strip_sensitive_query,
    detect_provider,
    normalize_download_url,
)
from missing_models_fetcher.folders import FolderRegistry  # noqa: E402
from missing_models_fetcher.scanner import WorkflowScanner  # noqa: E402


PAYLOAD = (b"0123456789abcdef" * 65536) * 4
FIXED_PROVIDER_SAMPLES = {
    "hf": {
        "url": "https://huggingface.co/optimum-intel-internal-testing/tiny-random-bert/resolve/main/model.safetensors",
        "size": 520212,
        "sha256": "965f02b6a7e5520fc12f710e4e3b6132f697f1c8f648819553c5ade86752d2de",
    },
    "modelscope": {
        "url": "https://modelscope.cn/models/CrabInHoney/urlbert-tiny-base-v4/resolve/master/model.safetensors",
        "size": 14912996,
        "sha256": "1593cb4109cc7a6c44955214c97f75169a86217372300c2653fab9b6bd25ecab",
    },
    "civitai": {
        "url": "https://civitai.com/models/872135/nistyle-manga-sketch-and-detail?modelVersionId=976226",
        "size": 19314680,
        "sha256": "8f867bf357f788f9f8a56d21f71c76b82bf74749677aaaacc72661b948ab1b5c",
    },
}


class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        start = 0
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            if start >= len(PAYLOAD):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(PAYLOAD)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PAYLOAD) - start))
        self.end_headers()
        for index in range(start, len(PAYLOAD), 65536):
            try:
                self.wfile.write(PAYLOAD[index : index + 65536])
                self.wfile.flush()
            except ConnectionError:
                return
            time.sleep(0.005)


class ObservedRangeHandler(RangeHandler):
    observation_lock = threading.Lock()
    active_total = 0
    peak_total = 0
    active_by_host: dict[str, int] = {}
    peak_by_host: dict[str, int] = {}
    request_order: list[tuple[str, str]] = []
    concurrent_started = threading.Event()
    chunk_delay = 0.02

    @classmethod
    def reset(cls):
        with cls.observation_lock:
            cls.active_total = 0
            cls.peak_total = 0
            cls.active_by_host = {}
            cls.peak_by_host = {}
            cls.request_order = []
            cls.concurrent_started = threading.Event()

    def do_GET(self):
        host = str(self.headers.get("Host") or "").split(":", 1)[0]
        with self.observation_lock:
            type(self).active_total += 1
            type(self).peak_total = max(type(self).peak_total, type(self).active_total)
            type(self).active_by_host[host] = type(self).active_by_host.get(host, 0) + 1
            type(self).peak_by_host[host] = max(
                type(self).peak_by_host.get(host, 0),
                type(self).active_by_host[host],
            )
            type(self).request_order.append((host, self.path))
            if type(self).active_total >= 2:
                type(self).concurrent_started.set()
        try:
            start = 0
            range_header = self.headers.get("Range")
            if range_header and range_header.startswith("bytes="):
                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                if start >= len(PAYLOAD):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(PAYLOAD)}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(PAYLOAD) - start))
            self.end_headers()
            for index in range(start, len(PAYLOAD), 65536):
                try:
                    self.wfile.write(PAYLOAD[index:index + 65536])
                    self.wfile.flush()
                except (ConnectionError, OSError):
                    return
                if index + 65536 < len(PAYLOAD):
                    time.sleep(self.chunk_delay)
        finally:
            with self.observation_lock:
                type(self).active_total -= 1
                type(self).active_by_host[host] -= 1


class Socks5RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address):
        super().__init__(server_address, Socks5RelayHandler)
        self.address_types = []


class Socks5RelayHandler(socketserver.BaseRequestHandler):
    def _read_exact(self, size):
        chunks = []
        remaining = size
        while remaining:
            chunk = self.request.recv(remaining)
            if not chunk:
                raise ConnectionError("SOCKS client disconnected")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def handle(self):
        version, method_count = self._read_exact(2)
        assert version == 5
        self._read_exact(method_count)
        self.request.sendall(b"\x05\x00")

        version, command, _reserved, address_type = self._read_exact(4)
        assert version == 5 and command == 1
        if address_type == 1:
            host = socket.inet_ntoa(self._read_exact(4))
        elif address_type == 3:
            host = self._read_exact(self._read_exact(1)[0]).decode("idna")
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._read_exact(16))
        else:
            raise ValueError(f"Unsupported SOCKS address type: {address_type}")
        port = struct.unpack("!H", self._read_exact(2))[0]
        self.server.address_types.append(address_type)

        relay_host = "127.0.0.1" if host in {"localhost", "::1"} else host
        with socket.create_connection((relay_host, port), timeout=5) as upstream:
            bind_host, bind_port = upstream.getsockname()[:2]
            bind_bytes = socket.inet_aton(bind_host if ":" not in bind_host else "127.0.0.1")
            self.request.sendall(b"\x05\x00\x00\x01" + bind_bytes + struct.pack("!H", bind_port))
            sockets = [self.request, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 5)
                if not readable:
                    return
                for current in readable:
                    try:
                        data = current.recv(65536)
                    except (ConnectionError, OSError):
                        return
                    if not data:
                        return
                    try:
                        (upstream if current is self.request else self.request).sendall(data)
                    except (ConnectionError, OSError):
                        return


def assert_scan_and_url_normalization():
    folders = FolderRegistry()
    scanner = WorkflowScanner(folders)
    folder_paths.folder_names_and_paths.setdefault("diffusion_models", ([tempfile.gettempdir()], {".safetensors"}))
    folder_paths.folder_names_and_paths.setdefault("vae", ([tempfile.gettempdir()], {".safetensors"}))
    workflow = {
        "models": [
            {
                "name": "model.safetensors",
                "url": "https://huggingface.co/org/repo/blob/main/model.safetensors",
                "directory": "unet",
            }
        ]
    }
    models = scanner.scan(workflow)
    assert normalize_download_url(workflow["models"][0]["url"]) == (
        "https://huggingface.co/org/repo/resolve/main/model.safetensors"
    )
    assert workflow["models"][0]["url"] == scanner.scan(workflow)[0]["sources"][0]["url"]
    assert models[0]["directory"] == "diffusion_models"

    runtime_workflow = {
        "__missing_models_fetcher_runtime": {
            "nodes": [
                {
                    "type": "UNETLoader",
                    "widgets": [{"name": "unet_name", "value": "runtime-unet.safetensors"}],
                },
                {
                    "type": "VAELoader",
                    "widgets": [{"name": "vae_name", "value": "runtime-vae.safetensors"}],
                },
            ]
        }
    }
    runtime_models = scanner.scan(runtime_workflow)
    runtime_by_name = {model["name"]: model for model in runtime_models}
    assert runtime_by_name["runtime-unet.safetensors"]["directory"] == "diffusion_models"
    assert runtime_by_name["runtime-vae.safetensors"]["directory"] == "vae"

    metadata_workflow = {
        "missing_models": [
            {
                "filename": "metadata-unet.safetensors",
                "downloadUrl": "https://huggingface.co/org/repo/resolve/main/metadata-unet.safetensors",
                "folderName": "unet",
            }
        ]
    }
    metadata_models = scanner.scan(metadata_workflow)
    assert metadata_models[0]["name"] == "metadata-unet.safetensors"
    assert metadata_models[0]["directory"] == "diffusion_models"

    duplicate_workflow = {
        "models": [
            {
                "filename": "deduplicated-unet.safetensors",
                "downloadUrl": "https://huggingface.co/org/repo/resolve/main/deduplicated-unet.safetensors",
                "folderName": "unet",
            }
        ],
        "__missing_models_fetcher_runtime": {
            "nodes": [
                {
                    "type": "UNETLoader",
                    "widgets": [{"name": "unet_name", "value": "deduplicated-unet.safetensors"}],
                }
            ]
        },
    }
    duplicate_models = scanner.scan(duplicate_workflow)
    assert len(duplicate_models) == 1
    assert duplicate_models[0]["url"].endswith("/deduplicated-unet.safetensors")

    folder_paths.folder_names_and_paths.setdefault(
        "latent_upscale_models",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    combo_duplicate_workflow = {
        "missing_models": [
            {
                "filename": "spatial-upscaler.safetensors",
                "downloadUrl": (
                    "https://huggingface.co/org/repo/resolve/main/"
                    "spatial-upscaler.safetensors"
                ),
                "directory": "latent_upscale_models",
                "nodeType": "LatentUpscaleModelLoader",
            },
            {
                "filename": "spatial-upscaler.safetensors",
                "type": "combo",
                "nodeType": "runtime-node-id",
            },
        ],
        "__missing_models_fetcher_runtime": {
            "nodes": [
                {
                    "type": "runtime-node-id",
                    "widgets": [
                        {
                            "name": "model",
                            "type": "combo",
                            "value": "spatial-upscaler.safetensors",
                        }
                    ],
                }
            ]
        },
    }
    combo_duplicate_models = scanner.scan(combo_duplicate_workflow)
    assert len(combo_duplicate_models) == 1
    assert combo_duplicate_models[0]["directory"] == "latent_upscale_models"
    assert not combo_duplicate_models[0]["needs_directory"]
    assert combo_duplicate_models[0]["url"].endswith("/spatial-upscaler.safetensors")

    combo_only_models = scanner.scan(
        {
            "__missing_models_fetcher_runtime": {
                "nodes": [
                    {
                        "type": "unknown-loader",
                        "widgets": [
                            {
                                "name": "model",
                                "type": "combo",
                                "value": "unknown-combo-model.safetensors",
                            }
                        ],
                    }
                ]
            }
        }
    )
    assert combo_only_models[0]["directory"] == ""
    assert combo_only_models[0]["needs_directory"]

    folder_paths.folder_names_and_paths.setdefault(
        "mmf_same_name_a",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    folder_paths.folder_names_and_paths.setdefault(
        "mmf_same_name_b",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    valid_same_name_models = scanner.scan(
        {
            "missing_models": [
                {
                    "filename": "shared-name.safetensors",
                    "directory": "mmf_same_name_a",
                },
                {
                    "filename": "shared-name.safetensors",
                    "directory": "mmf_same_name_b",
                },
            ],
            "__missing_models_fetcher_runtime": {
                "nodes": [
                    {
                        "type": "unknown-loader",
                        "widgets": [
                            {
                                "name": "model",
                                "type": "combo",
                                "value": "shared-name.safetensors",
                            }
                        ],
                    }
                ]
            },
        }
    )
    assert len(valid_same_name_models) == 2
    assert {model["directory"] for model in valid_same_name_models} == {
        "mmf_same_name_a",
        "mmf_same_name_b",
    }

    sensitive_workflow = {
        "missing_models": [
            {
                "filename": "private-model.safetensors",
                "downloadUrl": (
                    "https://civitai.com/api/download/models/123"
                    "?type=Model&token=workflow-secret"
                ),
                "folderName": "unet",
            }
        ]
    }
    sensitive_models = scanner.scan(sensitive_workflow)
    assert sensitive_models[0]["url"] == (
        "https://civitai.com/api/download/models/123?type=Model"
    )


def assert_first_writable_folder_is_default():
    with tempfile.TemporaryDirectory() as temp_dir:
        first_path = os.path.join(temp_dir, "read-only")
        second_path = os.path.join(temp_dir, "writable")
        folder_paths.folder_names_and_paths["mmf_folder_defaults"] = (
            [first_path, second_path],
            {".safetensors"},
        )

        def fake_is_writable(path):
            return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(second_path))

        with patch("missing_models_fetcher.folders._is_writable_path", side_effect=fake_is_writable):
            options = FolderRegistry().folder_options("mmf_folder_defaults")

        assert not options[0]["default"]
        assert options[1]["default"]
        assert options[1]["writable"]


def assert_refresh_directory_invalidates_comfy_cache():
    cache_key = "mmf_refresh_models"
    with tempfile.TemporaryDirectory() as temp_dir:
        model_path = os.path.join(temp_dir, "new-model.safetensors")
        with open(model_path, "wb") as handle:
            handle.write(b"test")

        folder_paths.folder_names_and_paths[cache_key] = ([temp_dir], {".safetensors"})
        folder_paths.filename_list_cache[cache_key] = (["stale-model.safetensors"], {}, 0.0)
        folder_paths.cache_helper.cache[cache_key] = (["strong-stale.safetensors"], {}, 0.0)
        previous_active = folder_paths.cache_helper.active
        folder_paths.cache_helper.active = True
        try:
            FolderRegistry().refresh_directory(cache_key)
            refreshed = folder_paths.get_filename_list(cache_key)
            assert "new-model.safetensors" in refreshed
            assert "stale-model.safetensors" not in refreshed
            assert "strong-stale.safetensors" not in refreshed
        finally:
            folder_paths.cache_helper.active = previous_active
            folder_paths.cache_helper.cache.pop(cache_key, None)
            folder_paths.filename_list_cache.pop(cache_key, None)
            folder_paths.folder_names_and_paths.pop(cache_key, None)


class StaticSecrets:
    def __init__(self, values):
        self.values = values

    def load(self):
        return self.values


def assert_civitai_request_context_masks_token():
    manager = DownloadManager(StaticSecrets({"civitai_api_key": "civitai-secret"}), FolderRegistry())
    request_url, headers = manager._request_context("https://civitai.com/api/download/models/123?type=Model")
    assert "Authorization" in headers
    assert "token=civitai-secret" in request_url
    assert _strip_sensitive_query(request_url) == "https://civitai.com/api/download/models/123?type=Model"
    assert normalize_download_url(
        "https://civitai.com/api/download/models/123?type=Model&token=pasted-secret"
    ) == "https://civitai.com/api/download/models/123?type=Model"
    mirror_page = (
        "https://civitai.red/models/872135/nistyle-manga-sketch-and-detail"
        "?modelVersionId=976226"
    )
    assert detect_provider(mirror_page) == "civitai"
    assert normalize_download_url(mirror_page) == (
        "https://civitai.com/api/download/models/976226"
    )
    mirror_request_url, mirror_headers = manager._request_context(mirror_page)
    assert mirror_request_url == mirror_page
    assert "Authorization" not in mirror_headers
    assert "token=" not in mirror_request_url


def assert_civitai_version_metadata_uses_official_file_data():
    folder_paths.folder_names_and_paths.setdefault(
        "loras",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    manager = DownloadManager(
        StaticSecrets({"civitai_api_key": "civitai-secret"}),
        FolderRegistry(),
    )
    expected_sha = hashlib.sha256(b"civitai-model").hexdigest()
    seen_requests = []

    def fake_open_url(request, timeout):
        seen_requests.append((request, timeout))
        return JsonResponse(
            {
                "id": 976226,
                "model": {"name": "test model", "type": "LORA"},
                "files": [
                    {
                        "name": "test2-000013.safetensors",
                        "sizeKB": 18861.9921875,
                        "type": "Model",
                        "primary": True,
                        "hashes": {"SHA256": expected_sha.upper()},
                    }
                ],
            }
        )

    with patch("missing_models_fetcher.downloader._open_url", fake_open_url):
        metadata = manager._civitai_file_metadata("976226")

    assert metadata["filename"] == "test2-000013.safetensors"
    assert metadata["size"] == 19314680
    assert metadata["sha256"] == expected_sha
    assert metadata["hash_source"] == "civitai_file_sha256"
    assert metadata["directory"] == "loras"
    assert metadata["display_name"] == "test model"
    request = seen_requests[0][0]
    assert request.full_url.endswith("/api/v1/model-versions/976226")
    assert request.get_header("Authorization") == "Bearer civitai-secret"
    assert "token=" not in request.full_url


def assert_civitai_model_page_expands_versions_and_reports_invalid_ids():
    folder_paths.folder_names_and_paths.setdefault(
        "loras",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._civitai_model_metadata = lambda model_id: {
        "id": int(model_id),
        "name": "墨心 MoXin",
        "type": "LORA",
        "modelVersions": [
            {
                "id": 14856,
                "name": "墨心 MoXin 1.0",
                "files": [
                    {
                        "name": "MoXinV1.safetensors",
                        "sizeKB": 100,
                        "type": "Model",
                        "primary": True,
                        "hashes": {"SHA256": "a" * 64},
                    }
                ],
            },
            {
                "id": 16075,
                "name": "疏可走马 1.0",
                "files": [
                    {
                        "name": "shukezouma.safetensors",
                        "sizeKB": 200,
                        "type": "Model",
                        "primary": True,
                        "hashes": {"SHA256": "b" * 64},
                    }
                ],
            },
        ],
    }
    prepared = manager.prepare_manual_items(
        [{"url": "https://civitai.red/models/12597/moxin"}]
    )
    assert [item["name"] for item in prepared] == [
        "MoXinV1.safetensors",
        "shukezouma.safetensors",
    ]
    assert all(item["display_name"] == "墨心 MoXin" for item in prepared)
    assert len({item["group_id"] for item in prepared}) == 1
    assert all(item["group_label"] == "墨心 MoXin" for item in prepared)
    assert all(item["group_version_count"] == 2 for item in prepared)
    assert all(
        item["group_source_url"] == "https://civitai.red/models/12597/moxin"
        for item in prepared
    )
    assert [item["version_name"] for item in prepared] == [
        "墨心 MoXin 1.0",
        "疏可走马 1.0",
    ]
    assert all(item["directory"] == "loras" for item in prepared)
    assert [item["url"] for item in prepared] == [
        "https://civitai.com/api/download/models/14856",
        "https://civitai.com/api/download/models/16075",
    ]
    assert all(item["sources"][0]["verification"] == "hash_verified" for item in prepared)

    manager._civitai_model_metadata = lambda _model_id: (_ for _ in ()).throw(
        ValueError("Civitai 模型 ID 125917 不存在，请检查页面链接")
    )
    invalid = manager.prepare_manual_items(
        [{"url": "https://civitai.red/models/125917/moxin"}]
    )[0]
    assert invalid["display_name"] == "moxin"
    assert "125917 不存在" in invalid["error"]
    assert invalid["needs_directory"]


def assert_provider_credentials_stay_on_trusted_hosts():
    manager = DownloadManager(
        StaticSecrets(
            {
                "hf_api_key": "hf-secret",
                "civitai_api_key": "civitai-secret",
                "modelscope_api_token": "modelscope-secret",
            }
        ),
        FolderRegistry(),
    )
    for url in (
        "https://evilhuggingface.co/org/repo/blob/main/model.safetensors",
        "https://evilcivitai.com/api/download/models/123",
        "https://evilmodelscope.cn/api/v1/models/org/repo/repo",
        "https://cdn-lfs-cn-1.modelscope.cn/object?auth_key=signed",
    ):
        request_url, headers = manager._request_context(url)
        assert "Authorization" not in headers
        assert "token=" not in request_url

    evil_hf_url = "https://evilhuggingface.co/org/repo/blob/main/model.safetensors"
    assert normalize_download_url(evil_hf_url) == evil_hf_url
    modelscope_url = (
        "https://modelscope.cn/api/v1/models/org/repo/repo"
        "?Revision=master&FilePath=model.safetensors"
    )
    request_url, headers = manager._request_context(modelscope_url)
    assert request_url == modelscope_url
    assert headers["Authorization"] == "Bearer modelscope-secret"
    assert headers["Cookie"] == "m_session_id=modelscope-secret"
    assert detect_provider(modelscope_url) == "modelscope"
    assert _strip_sensitive_query(
        "https://cdn-lfs-cn-1.modelscope.cn/object?filename=model.safetensors"
        "&auth_key=secret&X-Amz-Signature=secret&X-Oss-Signature=secret"
    ) == "https://cdn-lfs-cn-1.modelscope.cn/object?filename=model.safetensors"

    for invalid_url in ("file:///tmp/model.safetensors", "https://user:password@example.com/model.safetensors"):
        try:
            normalize_download_url(invalid_url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe URL was accepted: {invalid_url}")


def assert_cross_host_redirect_strips_credentials():
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://huggingface.co/org/repo/resolve/main/model.safetensors",
        headers={
            "Authorization": "Bearer hf-secret",
            "Cookie": "session=private",
            "Proxy-Authorization": "Basic proxy-secret",
            "User-Agent": "test-agent",
        },
    )
    redirected = _SafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://cdn.example.net/model.safetensors?signature=public-download",
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("Cookie") is None
    assert redirected.get_header("Proxy-authorization") is None
    assert redirected.get_header("User-agent") == "test-agent"


def assert_hf_xet_403_refreshes_presigned_url_once():
    origin_url = "https://huggingface.co/org/repo/resolve/main/model.safetensors"
    xet_url = "https://cas-bridge.xethub.hf.co/xet-bridge-us/object?X-Amz-Signature=stale"
    seen_requests = []

    def fake_open_url(request, timeout):
        seen_requests.append((request, timeout))
        if len(seen_requests) == 1:
            raise HTTPError(xet_url, 403, "Forbidden", {}, io.BytesIO(b"AccessDenied"))
        return JsonResponse({"ok": True})

    headers = {
        "Authorization": "Bearer hf-secret",
        "Range": "bytes=123-",
        "User-Agent": "test-agent",
    }
    with patch("missing_models_fetcher.downloader._open_url", side_effect=fake_open_url):
        response = _open_download_url(origin_url, headers, timeout=17)
        response.close()

    assert len(seen_requests) == 2
    refreshed, timeout = seen_requests[1]
    assert timeout == 17
    assert refreshed.full_url.startswith(f"{origin_url}?mmf_xet_retry=")
    assert refreshed.get_header("Cache-control") == "no-cache"
    assert refreshed.get_header("Pragma") == "no-cache"
    assert refreshed.get_header("Range") == "bytes=123-"
    assert refreshed.get_header("Authorization") == "Bearer hf-secret"


def assert_non_xet_hf_403_is_not_retried_and_error_is_accurate():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    origin_url = "https://huggingface.co/org/private/resolve/main/model.safetensors"
    permission_error = HTTPError(origin_url, 403, "Forbidden", {}, io.BytesIO())
    with patch("missing_models_fetcher.downloader._open_url", side_effect=permission_error) as opened:
        try:
            _open_download_url(origin_url, {"User-Agent": "test-agent"})
        except HTTPError as exc:
            message = manager._friendly_error(exc)
        else:
            raise AssertionError("Expected Hugging Face permission error")
    assert opened.call_count == 1
    assert "API Key" in message

    xet_error = HTTPError(
        "https://cas-bridge.xethub.hf.co/xet-bridge-us/object",
        403,
        "Forbidden",
        {},
        io.BytesIO(),
    )
    xet_message = manager._friendly_error(xet_error)
    assert "Xet CDN" in xet_message
    assert "API Key" not in xet_message


class JsonResponse(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers = {}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def getcode(self):
        return self.status


def assert_hf_file_metadata_uses_lfs_oid_not_xet_hash():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    lfs_sha = hashlib.sha256(b"lfs-file-content").hexdigest()
    xet_hash = hashlib.sha256(b"xet-storage-object").hexdigest()
    seen_requests = []

    def fake_open_url(request, timeout):
        seen_requests.append((request, timeout))
        return JsonResponse(
            [
                {
                    "type": "file",
                    "oid": "1" * 40,
                    "size": 456,
                    "lfs": {"oid": lfs_sha, "size": 456, "pointerSize": 130},
                    "xetHash": xet_hash,
                    "path": "folder/model.safetensors",
                }
            ]
        )

    with patch("missing_models_fetcher.downloader._open_url", fake_open_url):
        metadata = manager._hf_file_metadata(
            {
                "repository": "org/repo",
                "revision": "main",
                "file_path": "folder/model.safetensors",
            }
        )

    assert metadata["sha256"] == lfs_sha
    assert metadata["sha256"] != xet_hash
    assert metadata["hash_source"] == "hf_lfs_oid"
    assert metadata["size"] == 456
    request = seen_requests[0][0]
    assert request.full_url.endswith("/api/models/org/repo/paths-info/main")
    assert json.loads(request.data.decode("utf-8"))["paths"] == ["folder/model.safetensors"]


def assert_hf_api_metadata_survives_download_probe_failure():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    lfs_sha = hashlib.sha256(b"api-metadata").hexdigest()
    with (
        patch.object(
            manager,
            "_hf_file_metadata",
            return_value={
                "size": 987,
                "sha256": lfs_sha,
                "hash_source": "hf_lfs_oid",
            },
        ),
        patch(
            "missing_models_fetcher.downloader._open_url",
            side_effect=OSError("simulated download probe failure"),
        ),
    ):
        metadata = manager.remote_metadata(
            "https://huggingface.co/org/repo/resolve/main/folder/model.safetensors"
        )
    assert metadata["size"] == 987
    assert metadata["sha256"] == lfs_sha
    assert metadata["hash_source"] == "hf_lfs_oid"


def assert_api_key_testing_does_not_expose_secrets():
    seen_requests = []

    def fake_open_url(request, timeout):
        seen_requests.append((request, timeout))
        if "huggingface.co" in request.full_url:
            return JsonResponse({"name": "test-user"})
        if "modelscope.cn" in request.full_url:
            return JsonResponse({"data": {"username": "modelscope-user"}})
        return JsonResponse({"items": []})

    secrets = StaticSecrets(
        {
            "hf_api_key": "hf_saved-secret",
            "civitai_api_key": "civitai-saved-secret",
            "modelscope_api_token": "modelscope-saved-secret",
        }
    )
    manager = DownloadManager(secrets, FolderRegistry())
    with patch("missing_models_fetcher.downloader._open_url", fake_open_url):
        hf_result = manager.test_api_key("hf")
        civitai_result = manager.test_api_key("civitai", "civitai-one-time-secret")
        modelscope_result = manager.test_api_key("modelscope")

    assert hf_result["valid"]
    assert hf_result["account"] == "test-user"
    assert hf_result["using_saved_key"]
    assert civitai_result["valid"]
    assert not civitai_result["using_saved_key"]
    assert modelscope_result["valid"]
    assert modelscope_result["account"] == "modelscope-user"
    assert "secret" not in json.dumps([hf_result, civitai_result, modelscope_result])

    hf_request = seen_requests[0][0]
    civitai_request = seen_requests[1][0]
    modelscope_request = seen_requests[2][0]
    assert hf_request.get_header("Authorization") == "Bearer hf_saved-secret"
    assert civitai_request.get_header("Authorization") == "Bearer civitai-one-time-secret"
    assert "civitai-one-time-secret" not in civitai_request.full_url
    assert modelscope_request.get_header("Authorization") == "Bearer modelscope-saved-secret"


def assert_modelscope_config_and_source_resolution():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = SecretsStore()
        store.config_dir = temp_dir
        store.path = os.path.join(temp_dir, "secrets.json")
        snapshot = store.update({"modelscope_api_token": "ms-test-secret-1234"})
        data = snapshot.to_dict()
        assert data["has_modelscope_api_token"]
        assert data["modelscope_api_token_masked"] != "ms-test-secret-1234"
        assert "ms-test-secret-1234" not in json.dumps(data)
        assert data["download_concurrency"] == 1
        assert data["provider_concurrency"] == 1
        assert data["bandwidth_limit_mbps"] == 0
        updated = store.update({
            "download_concurrency": 3,
            "provider_concurrency": 2,
            "bandwidth_limit_mbps": 50,
        })
        assert updated.download_concurrency == 3
        assert updated.provider_concurrency == 2
        assert updated.bandwidth_limit_mbps == 50
        assert store.load()["download_concurrency"] == 3
        assert store.load()["provider_concurrency"] == 2
        assert store.load()["bandwidth_limit_mbps"] == 50
        proxy = store.update({
            "proxy_enabled": True,
            "proxy_url": "http://proxy-user:proxy-secret@127.0.0.1:7890",
        })
        proxy_data = proxy.to_dict()
        assert proxy.proxy_enabled
        assert proxy.has_proxy_url
        assert proxy_data["proxy_url_masked"] == "http://proxy-user:****@127.0.0.1:7890"
        assert "proxy-secret" not in json.dumps(proxy_data)
        assert store.load()["proxy_url"].endswith("@127.0.0.1:7890")
        profiles_snapshot = store.update({
            "proxy_mode": "custom",
            "proxy_profiles": [{
                "name": "本地 SOCKS",
                "scheme": "socks5h",
                "host": "127.0.0.1",
                "port": 1080,
                "username": "proxy-user",
                "password": "profile-secret",
            }],
        })
        profile_id = profiles_snapshot.proxy_profiles[0]["id"]
        profiles_snapshot = store.update({"active_proxy_id": profile_id})
        assert profiles_snapshot.proxy_mode == "custom"
        assert profiles_snapshot.active_proxy_id == profile_id
        assert profiles_snapshot.proxy_profiles[0]["host"] == "127.0.0.1"
        assert profiles_snapshot.proxy_profiles[0]["has_password"]
        assert "profile-secret" not in json.dumps(profiles_snapshot.to_dict())
        persisted_profile = store.load()["proxy_profiles"][0]
        assert persisted_profile["password"] == "profile-secret"
        store.update({"proxy_profiles": profiles_snapshot.proxy_profiles})
        assert store.load()["proxy_profiles"][0]["password"] == "profile-secret"
        _configure_proxy("off", "")
        try:
            store.update({"proxy_url": "socks5://127.0.0.1:1080"})
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported proxy scheme must be rejected")
        assert not store.clear("modelscope").has_modelscope_api_token
        assert store.snapshot().download_concurrency == 3
        custom = store.update({
            "download_concurrency": 17,
            "provider_concurrency": 13,
            "bandwidth_limit_mbps": 750.5,
        })
        assert custom.download_concurrency == 17
        assert custom.provider_concurrency == 13
        assert custom.bandwidth_limit_mbps == 750.5
        reloaded_custom = store.snapshot(store.load())
        assert reloaded_custom.download_concurrency == 17
        assert reloaded_custom.provider_concurrency == 13
        assert reloaded_custom.bandwidth_limit_mbps == 750.5
        clamped = store.update({
            "download_concurrency": 99,
            "provider_concurrency": 99,
            "bandwidth_limit_mbps": 200000,
        })
        assert clamped.download_concurrency == 32
        assert clamped.provider_concurrency == 32
        assert clamped.bandwidth_limit_mbps == 100000


    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._find_civitai_source_by_hash = lambda _sha256: None
    expected_sha = hashlib.sha256(b"verified-model").hexdigest()
    original_repository = "org/repo"
    cross_repository = "mirror/repo-copy"

    def fake_list_files(repository, revision):
        if repository == original_repository and revision == "master":
            return [
                {
                    "Path": "folder/model.safetensors",
                    "Size": 123,
                    "Sha256": "0" * 64,
                }
            ]
        if repository == cross_repository and revision == "master":
            return [
                {
                    "Path": "weights/model.safetensors",
                    "Size": 456,
                    "Sha256": expected_sha,
                }
            ]
        return []

    manager._list_modelscope_files = fake_list_files
    manager._search_modelscope_models = lambda _query: [{"id": cross_repository}]
    manager.remote_metadata = lambda _url: {"size": 456, "sha256": expected_sha}
    resolved = manager.resolve_sources(
        [
            {
                "id": "model-1",
                "name": "model.safetensors",
                "url": "https://huggingface.co/org/repo/resolve/main/folder/model.safetensors",
                "hash": expected_sha,
                "hash_type": "sha256",
            }
        ]
    )[0]
    modelscope_sources = [
        source for source in resolved["sources"] if source["provider"] == "modelscope"
    ]
    assert len(modelscope_sources) == 1
    assert modelscope_sources[0]["repository"] == cross_repository
    assert modelscope_sources[0]["verification"] == "hash_verified"
    assert modelscope_sources[0]["file_path"] == "weights/model.safetensors"

    manager._list_modelscope_files = lambda repository, revision: (
        [
            {
                "Path": "folder/model.safetensors",
                "Size": 789,
                "Sha256": expected_sha,
            }
        ]
        if repository == original_repository and revision == "master"
        else []
    )
    manager.remote_metadata = lambda _url: {
        "size": 789,
        "sha256": "f" * 64,
        "hash_source": "hf_lfs_oid",
    }
    same_path = manager.resolve_sources(
        [
            {
                "id": "model-2",
                "name": "model.safetensors",
                "url": "https://huggingface.co/org/repo/resolve/main/folder/model.safetensors",
            }
        ]
    )[0]
    same_path_source = next(
        source for source in same_path["sources"] if source["provider"] == "modelscope"
    )
    assert same_path_source["verification"] == "hash_conflict"
    assert same_path_source["warning_level"] == "warning"
    assert same_path_source["selectable"]
    assert "SHA-256 不一致" in same_path_source["warning"]
    assert same_path_source["url"] == (
        "https://modelscope.cn/api/v1/models/org/repo/repo"
        "?Revision=master&FilePath=folder%2Fmodel.safetensors"
    )

    manager._search_modelscope_models = lambda _query: []
    blocked = manager.resolve_sources(
        [
            {
                "id": "model-3",
                "name": "model.safetensors",
                "url": "https://huggingface.co/org/repo/resolve/main/folder/model.safetensors",
                "hash": "a" * 64,
                "hash_type": "sha256",
            }
        ]
    )[0]
    blocked_modelscope = next(
        source for source in blocked["sources"] if source["provider"] == "modelscope"
    )
    assert not blocked_modelscope["selectable"]
    assert blocked_modelscope["warning_level"] == "error"
    assert blocked_modelscope["verification"] == "hash_mismatch"
    assert "禁止下载" in blocked_modelscope["blocked_reason"]

    legacy = DownloadTask.from_dict(
        {
            "id": "legacy-task",
            "name": "model.safetensors",
            "url": "https://modelscope.cn/api/v1/models/org/repo/repo?Revision=master&FilePath=model.safetensors",
            "normalized_url": "https://modelscope.cn/api/v1/models/org/repo/repo?Revision=master&FilePath=model.safetensors",
            "directory": "checkpoints",
            "destination_root": temp_dir,
            "target_path": os.path.join(temp_dir, "model.safetensors"),
            "part_path": os.path.join(temp_dir, "model.safetensors.part"),
        }
    )
    assert legacy.provider == "modelscope"


def assert_socks5_and_socks5h_proxy_dns_modes():
    target = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    proxy = Socks5RelayServer(("127.0.0.1", 0))
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    target_thread.start()
    proxy_thread.start()
    try:
        target_url = f"http://localhost:{target.server_port}/model.safetensors"
        for scheme, expected_address_type in (("socks5", "local"), ("socks5h", 3)):
            proxy.address_types.clear()
            _configure_proxy(
                "custom",
                "",
                {
                    "scheme": scheme,
                    "host": "127.0.0.1",
                    "port": proxy.server_address[1],
                },
            )
            from missing_models_fetcher.downloader import _open_url

            with _open_url(
                Request(target_url, headers={"Range": "bytes=0-31"}),
                timeout=5,
            ) as response:
                assert response.read(32) == PAYLOAD[:32]
            if expected_address_type == "local":
                assert proxy.address_types[0] in {1, 4}
            else:
                assert proxy.address_types == [expected_address_type]
    finally:
        _configure_proxy("off", "")
        proxy.shutdown()
        target.shutdown()
        proxy.server_close()
        target.server_close()


def assert_friendly_network_error_categories():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    assert "DNS 解析失败" in manager._friendly_error(
        URLError(socket.gaierror(11001, "getaddrinfo failed"))
    )
    assert "请求超时" in manager._friendly_error(URLError(TimeoutError("timed out")))
    _configure_proxy(
        "custom",
        "http://127.0.0.1:9",
        {"scheme": "http", "host": "127.0.0.1", "port": 9},
    )
    try:
        assert "代理不可用" in manager._friendly_error(
            URLError(ConnectionRefusedError("actively refused"))
        )
    finally:
        _configure_proxy("off", "")
    not_found = HTTPError(
        "https://huggingface.co/org/repo/resolve/main/missing.safetensors",
        404,
        "Not Found",
        {},
        io.BytesIO(),
    )
    assert "下载地址不存在" in manager._friendly_error(not_found)
    proxy_auth = HTTPError(
        "https://huggingface.co/",
        407,
        "Proxy Authentication Required",
        {},
        io.BytesIO(),
    )
    assert "代理认证失败" in manager._friendly_error(proxy_auth)


def assert_fixed_small_provider_regression_samples():
    hf_sample = FIXED_PROVIDER_SAMPLES["hf"]
    hf_reference = _parse_hf_file_url(hf_sample["url"])
    assert hf_reference == {
        "repository": "optimum-intel-internal-testing/tiny-random-bert",
        "revision": "main",
        "file_path": "model.safetensors",
    }

    modelscope_sample = FIXED_PROVIDER_SAMPLES["modelscope"]
    modelscope_reference = _parse_modelscope_file_url(modelscope_sample["url"])
    assert modelscope_reference == {
        "repository": "CrabInHoney/urlbert-tiny-base-v4",
        "revision": "master",
        "file_path": "model.safetensors",
    }

    civitai_sample = FIXED_PROVIDER_SAMPLES["civitai"]
    assert normalize_download_url(civitai_sample["url"]) == (
        "https://civitai.com/api/download/models/976226"
    )

    folder_paths.folder_names_and_paths.setdefault(
        "text_encoders",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._list_modelscope_files = lambda repository, revision: (
        [
            {
                "Path": modelscope_reference["file_path"],
                "Name": "model.safetensors",
                "Size": modelscope_sample["size"],
                "Sha256": modelscope_sample["sha256"],
            }
        ]
        if repository == modelscope_reference["repository"]
        and revision == modelscope_reference["revision"]
        else []
    )
    manager._infer_safetensors_directory_from_sources = lambda _sources: "text_encoders"
    resolved = manager.resolve_source_provider(
        {
            "id": "fixed-modelscope-sample",
            "name": "model.safetensors",
            "url": modelscope_sample["url"],
            "manual_entry": True,
        },
        "modelscope",
    )
    source = resolved["sources"][0]
    assert source["repository"] == modelscope_reference["repository"]
    assert source["revision"] == modelscope_reference["revision"]
    assert source["file_path"] == modelscope_reference["file_path"]
    assert source["size"] == modelscope_sample["size"]
    assert resolved["size"] == modelscope_sample["size"]
    assert source["sha256"] == modelscope_sample["sha256"]
    assert source["hash_source"] == "modelscope_file_sha256"
    assert resolved["directory"] == "text_encoders"
    assert resolved["directory_inference"] == "safetensors_header"

    manager._civitai_file_metadata = lambda version_id: {
        "filename": "test2-000013.safetensors",
        "size": civitai_sample["size"],
        "sha256": civitai_sample["sha256"],
        "hash_source": "civitai_file_sha256",
        "directory": "loras",
    }
    manager._infer_safetensors_directory_from_sources = lambda _sources: None
    civitai_resolved = manager.resolve_source_provider(
        {
            "id": "fixed-civitai-sample",
            "name": "",
            "url": civitai_sample["url"],
            "manual_entry": True,
        },
        "civitai",
    )
    civitai_source = civitai_resolved["sources"][0]
    assert civitai_resolved["name"] == "test2-000013.safetensors"
    assert civitai_resolved["size"] == civitai_sample["size"]
    assert civitai_source["sha256"] == civitai_sample["sha256"]
    assert civitai_resolved["directory"] == "loras"
    assert civitai_resolved["directory_inference"] == "source_metadata"

    for sample in FIXED_PROVIDER_SAMPLES.values():
        assert sample["size"] > 0
        assert len(sample["sha256"]) == 64


def assert_three_provider_hash_resolution():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    expected_sha = hashlib.sha256(b"three-provider-model").hexdigest()
    manager._civitai_file_metadata = lambda _version_id: {
        "filename": "model_on_civitai.safetensors",
        "size": 987,
        "sha256": expected_sha,
        "hash_source": "civitai_file_sha256",
        "directory": "checkpoints",
        "display_name": "Example Model",
        "version_name": "4B",
        "version_id": "123",
    }
    manager._find_hf_source_by_hash = lambda terms, name, sha256, size: (
        manager._source_dict(
            "hf",
            "https://huggingface.co/org/repo/resolve/main/model.safetensors",
            {
                "repository": "org/repo",
                "revision": "main",
                "file_path": "model.safetensors",
            },
            size=size,
            sha256=sha256,
            hash_source="hf_lfs_oid",
            verification="hash_verified",
        )
    )
    manager._find_modelscope_source_by_hash = lambda terms, name, sha256, size: (
        manager._source_dict(
            "modelscope",
            (
                "https://modelscope.cn/api/v1/models/org/repo/repo"
                "?Revision=master&FilePath=model.safetensors"
            ),
            {
                "repository": "org/repo",
                "revision": "master",
                "file_path": "model.safetensors",
            },
            size=size,
            sha256=sha256,
            hash_source="modelscope_file_sha256",
            verification="hash_verified",
        )
    )
    resolved = manager.resolve_sources(
        [
            {
                "id": "three-provider",
                "name": "model_on_civitai.safetensors",
                "display_name": "Example Model",
                "version_name": "4B",
                "url": "https://civitai.com/api/download/models/123",
            }
        ]
    )[0]
    providers = {source["provider"] for source in resolved["sources"]}
    assert providers == {"civitai", "hf", "modelscope"}
    assert all(
        source["sha256"] == expected_sha
        and source["verification"] == "hash_verified"
        and source["selectable"]
        for source in resolved["sources"]
    )


def assert_single_provider_progressive_resolution():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    expected_sha = hashlib.sha256(b"progressive-provider-model").hexdigest()
    manager._civitai_file_metadata = lambda _version_id: {
        "filename": "progressive.safetensors",
        "size": 321,
        "sha256": expected_sha,
        "hash_source": "civitai_file_sha256",
        "version_id": "456",
    }
    manager._find_hf_source_by_hash = lambda terms, name, sha256, size: (
        manager._source_dict(
            "hf",
            "https://huggingface.co/org/repo/resolve/main/progressive.safetensors",
            {
                "repository": "org/repo",
                "revision": "main",
                "file_path": "progressive.safetensors",
            },
            size=size,
            sha256=sha256,
            hash_source="hf_lfs_oid",
            verification="hash_verified",
        )
    )
    manager._find_modelscope_source_by_hash = lambda terms, name, sha256, size: (
        manager._source_dict(
            "modelscope",
            (
                "https://modelscope.cn/api/v1/models/org/repo/repo"
                "?Revision=master&FilePath=progressive.safetensors"
            ),
            {
                "repository": "org/repo",
                "revision": "master",
                "file_path": "progressive.safetensors",
            },
            size=size,
            sha256=sha256,
            hash_source="modelscope_file_sha256",
            verification="hash_verified",
        )
    )
    item = {
        "id": "progressive-provider",
        "name": "progressive.safetensors",
        "url": "https://civitai.com/api/download/models/456",
    }

    for provider in ("civitai", "hf", "modelscope"):
        result = manager.resolve_source_provider(item, provider)
        assert result["provider"] == provider
        assert not result["error"]
        assert {source["provider"] for source in result["sources"]} == {provider}
        assert result["sources"][0]["sha256"] == expected_sha

    manager._resolve_source_item_safe = lambda _item: (_ for _ in ()).throw(
        AssertionError("progressive manual parsing must not resolve all providers")
    )
    prepared = manager.prepare_manual_items(
        [{"url": "https://civitai.com/api/download/models/456"}],
        resolve_sources=False,
    )
    assert len(prepared) == 1
    assert {source["provider"] for source in prepared[0]["sources"]} == {"civitai"}


def assert_source_cache_coalesces_concurrent_requests_and_expires():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._source_cache_ttl = 0.05
    calls = 0
    calls_lock = threading.Lock()
    results = []

    def loader():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.08)
        return {"items": [{"value": calls}]}

    def read_cached():
        results.append(manager._cached_source_value("test", "same-key", loader))

    threads = [threading.Thread(target=read_cached) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert len(results) == 2
    results[0]["items"][0]["value"] = 999
    cached_again = manager._cached_source_value("test", "same-key", loader)
    assert cached_again["items"][0]["value"] == 1
    assert calls == 1

    time.sleep(0.06)
    refreshed = manager._cached_source_value("test", "same-key", loader)
    assert calls == 2
    assert refreshed["items"][0]["value"] == 2


def assert_source_resolution_jobs_are_cancelable_and_report_diagnostics():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())

    def slow_resolver(_item, _provider):
        while True:
            manager._check_source_resolution_deadline()
            manager._resolution_metric("repositories_checked")
            time.sleep(0.01)

    manager._resolve_source_provider = slow_resolver
    result_holder = {}

    def resolve():
        result_holder["result"] = manager.resolve_source_provider(
            {"id": "cancel-me", "name": "cancel-me.safetensors"},
            "hf",
            "cancel-job",
        )

    thread = threading.Thread(target=resolve)
    thread.start()
    time.sleep(0.03)
    assert manager.cancel_resolution_jobs(["cancel-job"]) == 1
    thread.join(timeout=2)
    assert not thread.is_alive()
    result = result_holder["result"]
    assert "已取消" in result["error"]
    assert result["diagnostics"]["canceled"] is True
    assert result["diagnostics"]["repositories_checked"] > 0
    assert result["diagnostics"]["elapsed_ms"] >= 0


def assert_restart_discards_only_the_failed_tasks_partial_file():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=temp_dir)
        target = os.path.join(temp_dir, "model.safetensors")
        unrelated = os.path.join(temp_dir, "other.safetensors.part")
        task = DownloadTask(
            id="restart-task",
            name="model.safetensors",
            url="https://example.com/model.safetensors",
            normalized_url="https://example.com/model.safetensors",
            directory="checkpoints",
            destination_root=temp_dir,
            target_path=target,
            part_path=f"{target}.part",
            status="failed",
            downloaded=7,
            error="Hash 校验失败",
            restart_required=True,
        )
        with open(task.part_path, "wb") as handle:
            handle.write(b"corrupt")
        with open(unrelated, "wb") as handle:
            handle.write(b"keep")
        manager.tasks[task.id] = task
        manager._ensure_worker_locked = lambda: None
        restarted = manager.restart(task.id)
        assert restarted["status"] == "queued"
        assert restarted["downloaded"] == 0
        assert restarted["restart_required"] is False
        assert not os.path.exists(task.part_path)
        assert os.path.exists(unrelated)


def assert_safetensors_header_range_infers_directory_without_full_download():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    header = json.dumps({
        "__metadata__": {"format": "pt"},
        "transformer_blocks.0.attn.weight": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]},
    }).encode("utf-8")
    payload = len(header).to_bytes(8, "little") + header + b"MODEL-DATA-MUST-NOT-BE-READ"
    reads = []

    class FakeResponse:
        status = 206
        def __init__(self, data):
            self.data = data
            self.offset = 0
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def getcode(self):
            return self.status
        def read(self, size=-1):
            reads.append(size)
            if size < 0:
                size = len(self.data) - self.offset
            result = self.data[self.offset:self.offset + size]
            self.offset += len(result)
            return result

    def fake_open(request, timeout=0):
        requested_range = request.headers.get("Range") or request.headers.get("range")
        if requested_range == "bytes=0-7":
            return FakeResponse(payload[:8])
        return FakeResponse(payload[8:8 + len(header)])

    with patch("missing_models_fetcher.downloader._open_url", side_effect=fake_open):
        inferred = manager._infer_safetensors_directory("https://example.com/model.safetensors")
    assert inferred == "diffusion_models"
    assert reads == [8, len(header)]
    assert sum(reads) < len(payload)


def assert_safetensors_directory_inference_is_conservative():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())

    class FakeResponse:
        status = 206

        def __init__(self, data):
            self.data = data
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

        def read(self, size=-1):
            if size < 0:
                size = len(self.data) - self.offset
            result = self.data[self.offset:self.offset + size]
            self.offset += len(result)
            return result

    def infer(keys):
        header = json.dumps(
            {
                "__metadata__": {"format": "pt"},
                **{
                    key: {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]}
                    for key in keys
                },
            }
        ).encode("utf-8")
        prefix = len(header).to_bytes(8, "little")

        def fake_open(request, timeout=0):
            requested_range = request.headers.get("Range") or request.headers.get("range")
            return FakeResponse(prefix if requested_range == "bytes=0-7" else header)

        with patch("missing_models_fetcher.downloader._open_url", side_effect=fake_open):
            return manager._infer_safetensors_directory(
                "https://example.com/model.safetensors"
            )

    assert infer([
        "bert.embeddings.word_embeddings.weight",
        "bert.encoder.layer.0.attention.self.query.weight",
    ]) == "text_encoders"
    assert infer([
        "generic.encoder.layer.0.weight",
    ]) is None
    assert infer([
        "encoder.down_blocks.0.resnets.0.conv1.weight",
        "decoder.up_blocks.0.resnets.0.conv1.weight",
    ]) == "vae"


def assert_trusted_source_index_is_persistent_sanitized_and_reused():
    sha256 = hashlib.sha256(b"indexed-model").hexdigest()
    with tempfile.TemporaryDirectory() as state_dir:
        manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        manager._record_trusted_sources([{
            "provider": "hf",
            "url": "https://huggingface.co/org/repo/resolve/main/model.safetensors?token=secret",
            "repository": "org/repo",
            "revision": "main",
            "file_path": "model.safetensors",
            "size": 123,
            "sha256": sha256,
            "hash_source": "hf_lfs_oid",
            "verification": "hash_verified",
            "selectable": True,
        }])
        with open(manager.source_index_path, "r", encoding="utf-8") as handle:
            persisted = handle.read()
        assert "secret" not in persisted
        restored = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        restored._resolve_hf_sources = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("trusted index should avoid network resolution")
        )
        result = restored.resolve_source_provider({
            "id": "indexed",
            "name": "model.safetensors",
            "hash": sha256,
            "hash_type": "sha256",
        }, "hf")
        assert not result["error"]
        assert result["sources"][0]["sha256"] == sha256
        assert "?" not in result["sources"][0]["url"]


def assert_provider_concurrency_and_global_bandwidth_limits():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager.provider_concurrency = 1
    first = DownloadTask(
        id="hf-active", name="a.safetensors", url="https://huggingface.co/a", normalized_url="https://huggingface.co/a",
        directory="checkpoints", destination_root=tempfile.gettempdir(), target_path=os.path.join(tempfile.gettempdir(), "a.safetensors"),
        part_path=os.path.join(tempfile.gettempdir(), "a.safetensors.part"), provider="hf", status="queued",
    )
    second = DownloadTask(
        id="hf-wait", name="b.safetensors", url="https://huggingface.co/b", normalized_url="https://huggingface.co/b",
        directory="checkpoints", destination_root=tempfile.gettempdir(), target_path=os.path.join(tempfile.gettempdir(), "b.safetensors"),
        part_path=os.path.join(tempfile.gettempdir(), "b.safetensors.part"), provider="hf", status="queued",
    )
    third = DownloadTask(
        id="ms-ready", name="c.safetensors", url="https://modelscope.cn/c", normalized_url="https://modelscope.cn/c",
        directory="checkpoints", destination_root=tempfile.gettempdir(), target_path=os.path.join(tempfile.gettempdir(), "c.safetensors"),
        part_path=os.path.join(tempfile.gettempdir(), "c.safetensors.part"), provider="modelscope", status="queued",
    )
    manager.tasks = {task.id: task for task in (first, second, third)}
    manager.queue = [second.id, third.id]
    manager._active_provider_counts = {"hf": 1}
    with patch.object(manager, "_save_state_locked"):
        selected = manager._next_task()
    assert selected.id == third.id
    assert manager.queue == [second.id]
    first.status = "queued"
    manager.queue = [first.id, second.id]
    with patch.object(manager, "_save_state_locked"):
        manager.set_task_priority(second.id, 1)
    assert manager.queue == [second.id, first.id]
    assert manager.tasks[second.id].priority == 1
    manager.bandwidth_limit_mbps = 100
    started = time.monotonic()
    manager._throttle_download(1024 * 1024)
    manager._throttle_download(1024 * 1024)
    assert time.monotonic() - started >= 0.009


def assert_disk_preflight_and_hash_verification_state():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    reserve = manager._disk_reserve_bytes(100)
    manager._filesystem_capacity = lambda _path: ("test-filesystem", 100 + reserve - 1)
    try:
        manager._check_batch_disk_space(
            [
                {
                    "name": "large-model.safetensors",
                    "target_path": os.path.join(tempfile.gettempdir(), "large-model.safetensors"),
                    "size": 100,
                }
            ]
        )
        raise AssertionError("disk preflight should reject insufficient free space")
    except InsufficientDiskSpaceError as exc:
        assert "磁盘空间不足" in str(exc)
        assert "large-model.safetensors" in str(exc)

    with tempfile.TemporaryDirectory() as temp_dir:
        model_path = os.path.join(temp_dir, "verified.safetensors.part")
        with open(model_path, "wb") as handle:
            handle.write(b"verify-me")
        expected = hashlib.sha256(b"verify-me").hexdigest()
        task = DownloadTask(
            id="verify-state",
            name="verified.safetensors",
            url="https://example.com/verified.safetensors",
            normalized_url="https://example.com/verified.safetensors",
            directory="checkpoints",
            destination_root=temp_dir,
            target_path=os.path.join(temp_dir, "verified.safetensors"),
            part_path=model_path,
            hash=expected,
            hash_type="sha256",
        )
        manager._verify_hash(task, model_path)
        assert task.status == "verifying"
        assert task.verified_hash
        assert task.verification_progress == 100.0

        task.hash = "0" * 64
        task.verified_hash = False
        try:
            manager._verify_hash(task, model_path)
            raise AssertionError("hash mismatch must fail")
        except HashMismatchError:
            pass


def assert_source_resolution_deadline_is_enforced():
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._source_resolution_local.deadline = time.monotonic() - 1
    try:
        manager._check_source_resolution_deadline()
        raise AssertionError("expired source resolution deadline must fail")
    except TimeoutError as exc:
        assert "单站点解析超过" in str(exc)
    finally:
        del manager._source_resolution_local.deadline


def assert_manual_item_preparation_uses_source_path_directory():
    folder_paths.folder_names_and_paths.setdefault(
        "loras",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    folder_paths.folder_names_and_paths.setdefault(
        "vae",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    folder_paths.folder_names_and_paths.setdefault(
        "diffusion_models",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    folder_paths.folder_names_and_paths.setdefault(
        "text_encoders",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._resolve_source_item_safe = lambda item: {
        "id": item["id"],
        "name": item["name"],
        "sources": [
            {
                "provider": "hf",
                "url": item["url"],
                "repository": "org/repo",
                "revision": "main",
                "file_path": "split_files/loras/manual-lora.safetensors",
                "size": 123456,
                "sha256": "",
                "hash_source": "",
                "verification": "original",
                "warning": "",
                "warning_level": "",
                "selectable": True,
                "blocked_reason": "",
            }
        ],
        "error": "",
    }
    prepared = manager.prepare_manual_items(
        [
            {
                "name": "manual-lora.safetensors",
                "url": (
                    "https://huggingface.co/org/repo/resolve/main/"
                    "split_files/loras/manual-lora.safetensors"
                ),
            }
        ]
    )[0]
    assert prepared["directory"] == "loras"
    assert prepared["directory_inference"] == "source_path"
    assert prepared["destinationOptions"]
    assert prepared["size"] == 123456
    assert not prepared["needs_directory"]
    assert not prepared["needs_url"]

    assert manager._infer_manual_directory("", "ae.safetensors", [])[0] == "vae"
    assert manager._infer_manual_directory(
        "",
        "diffusion_pytorch_model.safetensors",
        [{"file_path": "transformer/diffusion_pytorch_model.safetensors"}],
    )[0] == "diffusion_models"
    assert manager._infer_manual_directory(
        "",
        "model.safetensors",
        [{"file_path": "text_encoder_2/model.safetensors"}],
    )[0] == "text_encoders"

    manager._resolve_source_item_safe = lambda item: {
        "id": item["id"],
        "name": item["name"],
        "sources": [],
        "error": "",
    }
    name_only = manager.prepare_manual_items([{"name": "unknown-model.safetensors"}])[0]
    assert name_only["needs_url"]
    assert name_only["needs_directory"]


def assert_hf_repository_link_expands_model_files():
    folder_paths.folder_names_and_paths.setdefault(
        "loras",
        ([tempfile.gettempdir()], {".safetensors", ".gguf"}),
    )
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._list_hf_model_files = lambda repository, revision: [
        "weights/example-lora.safetensors",
        "weights/example-lora-q4.gguf",
    ]

    def resolve_item(item):
        reference = _parse_hf_file_url(item["url"])
        return {
            "id": item["id"],
            "name": item["name"],
            "sources": [
                {
                    "provider": "hf",
                    "url": item["url"],
                    "repository": reference["repository"],
                    "revision": reference["revision"],
                    "file_path": reference["file_path"],
                    "size": 123,
                    "sha256": "",
                    "hash_source": "",
                    "verification": "original",
                    "warning": "",
                    "warning_level": "",
                    "selectable": True,
                    "blocked_reason": "",
                }
            ],
            "error": "",
        }

    manager._resolve_source_item_safe = resolve_item
    prepared = manager.prepare_manual_items(
        [{"url": "https://huggingface.co/org/example-lora"}]
    )
    assert [item["name"] for item in prepared] == [
        "example-lora.safetensors",
        "example-lora-q4.gguf",
    ]
    assert all("/resolve/main/weights/" in item["url"] for item in prepared)
    assert all(item["directory"] == "loras" for item in prepared)
    assert all(item["size"] == 123 for item in prepared)


def assert_modelscope_repository_link_expands_model_files():
    folder_paths.folder_names_and_paths.setdefault(
        "vae",
        ([tempfile.gettempdir()], {".safetensors"}),
    )
    manager = DownloadManager(StaticSecrets({}), FolderRegistry())
    manager._list_modelscope_model_files = lambda repository, revision: [
        "vae/ae.safetensors",
    ]

    def resolve_item(item):
        reference = _parse_modelscope_file_url(item["url"])
        return {
            "id": item["id"],
            "name": item["name"],
            "sources": [
                {
                    "provider": "modelscope",
                    "url": item["url"],
                    "repository": reference["repository"],
                    "revision": reference["revision"],
                    "file_path": reference["file_path"],
                    "size": 456,
                    "sha256": "",
                    "hash_source": "",
                    "verification": "original",
                    "warning": "",
                    "warning_level": "",
                    "selectable": True,
                    "blocked_reason": "",
                }
            ],
            "error": "",
        }

    manager._resolve_source_item_safe = resolve_item
    prepared = manager.prepare_manual_items(
        [{"url": "https://modelscope.cn/models/org/example-model"}]
    )
    assert len(prepared) == 1
    assert prepared[0]["name"] == "ae.safetensors"
    assert prepared[0]["directory"] == "vae"
    assert prepared[0]["directory_inference"] == "source_path"
    assert prepared[0]["size"] == 456
    assert "FilePath=vae%2Fae.safetensors" in prepared[0]["url"]


def assert_credential_validation_monitor_reports_problems_without_secrets():
    class ValidationDownloads:
        @staticmethod
        def test_api_key(provider):
            if provider == "civitai":
                raise RuntimeError("Civitai API Key 无效、已过期或权限不足，HTTP 401")
            return {"valid": True, "account": f"{provider}-user"}

        @staticmethod
        def friendly_key_test_error(_provider, exc):
            return str(exc)

    secrets = StaticSecrets(
        {
            "hf_api_key": "hf-monitor-secret",
            "civitai_api_key": "civitai-monitor-secret",
        }
    )
    monitor = CredentialValidationMonitor(secrets, ValidationDownloads(), interval_seconds=60)
    snapshot = monitor.validate_all()
    assert snapshot["has_problem"]
    assert snapshot["providers"]["hf"]["status"] == "valid"
    assert snapshot["providers"]["modelscope"]["status"] == "unconfigured"
    assert snapshot["providers"]["civitai"]["status"] == "invalid"
    hf_result = monitor.validate_provider("hf")
    assert hf_result["status"] == "valid"
    assert monitor.snapshot()["providers"]["hf"]["status"] == "valid"
    civitai_result = monitor.validate_provider("civitai")
    assert civitai_result["status"] == "invalid"
    assert monitor.snapshot()["providers"]["civitai"]["status"] == "invalid"
    serialized = json.dumps(snapshot)
    assert "hf-monitor-secret" not in serialized
    assert "civitai-monitor-secret" not in serialized


def assert_state_save_retries_transient_windows_lock():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        original_replace = os.replace
        failed_attempts = 0

        def flaky_replace(source, destination):
            nonlocal failed_attempts
            if destination == manager.state_path and failed_attempts < 2:
                failed_attempts += 1
                raise PermissionError("simulated transient file lock")
            return original_replace(source, destination)

        with patch("missing_models_fetcher.downloader.os.replace", flaky_replace):
            with manager.lock:
                manager._save_state_locked(force=True)

        assert failed_attempts == 2
        assert os.path.isfile(manager.state_path)


def assert_sensitive_urls_are_not_persisted():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        state_dir = os.path.join(temp_dir, "state")
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        folder_paths.folder_names_and_paths["mmf_sensitive_models"] = ([model_dir], {".safetensors"})

        manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        item = {
            "name": "sensitive.safetensors",
            "url": "https://civitai.com/api/download/models/123?type=Model&token=pasted-secret",
            "directory": "mmf_sensitive_models",
            "destination_path": model_dir,
        }
        with patch.object(manager, "_ensure_worker_locked"):
            created = manager.enqueue([item])[0]

        assert created["url"] == "https://civitai.com/api/download/models/123?type=Model"
        assert created["normalized_url"] == "https://civitai.com/api/download/models/123?type=Model"
        with open(manager.state_path, "r", encoding="utf-8") as handle:
            state_text = handle.read()
        assert "pasted-secret" not in state_text

        migrated_task = {
            "id": "sensitive-migration",
            "name": "migrated.safetensors",
            "url": "https://civitai.com/api/download/models/456?token=old-secret",
            "normalized_url": "https://civitai.com/api/download/models/456?token=old-secret",
            "directory": "mmf_sensitive_models",
            "destination_root": model_dir,
            "target_path": os.path.join(model_dir, "migrated.safetensors"),
            "part_path": os.path.join(model_dir, "migrated.safetensors.part"),
            "status": "failed",
        }
        with open(os.path.join(state_dir, "downloads.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "tasks": [migrated_task]}, handle)

        migrated_manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        migrated = migrated_manager.tasks["sensitive-migration"]
        assert migrated.url == "https://civitai.com/api/download/models/456"
        with open(migrated_manager.state_path, "r", encoding="utf-8") as handle:
            migrated_state = handle.read()
        assert "old-secret" not in migrated_state


def assert_batch_enqueue_is_atomic():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        state_dir = os.path.join(temp_dir, "state")
        os.makedirs(model_dir, exist_ok=True)
        folder_paths.folder_names_and_paths["mmf_atomic_models"] = ([model_dir], {".safetensors"})
        manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        items = [
            {
                "name": "valid.safetensors",
                "url": "https://example.com/valid.safetensors",
                "directory": "mmf_atomic_models",
                "destination_path": model_dir,
            },
            {
                "name": "invalid.safetensors",
                "url": "https://example.com/invalid.safetensors",
                "directory": "unknown-model-directory",
            },
        ]

        try:
            manager.enqueue(items)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid batch unexpectedly succeeded")

        assert manager.tasks == {}
        assert manager.queue == []


def assert_source_hash_and_blocked_state_are_enforced_on_enqueue():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        state_dir = os.path.join(temp_dir, "state")
        os.makedirs(model_dir, exist_ok=True)
        folder_paths.folder_names_and_paths["mmf_source_hash_models"] = (
            [model_dir],
            {".safetensors"},
        )
        manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        source_sha = hashlib.sha256(b"selected-source").hexdigest()
        item = {
            "name": "source-hash.safetensors",
            "url": "https://modelscope.cn/api/v1/models/org/repo/repo"
            "?Revision=master&FilePath=source-hash.safetensors",
            "directory": "mmf_source_hash_models",
            "destination_path": model_dir,
            "source_sha256": source_sha,
            "hash_source": "modelscope_file_sha256",
            "selectable": True,
        }
        with patch.object(manager, "_ensure_worker_locked"):
            created = manager.enqueue([item])[0]
        assert created["hash"] == source_sha
        assert created["hash_type"] == "sha256"

        blocked_item = {
            **item,
            "name": "blocked.safetensors",
            "selectable": False,
            "blocked_reason": "工作流 SHA-256 不一致，禁止下载。",
        }
        try:
            manager.enqueue([blocked_item])
        except ValueError as exc:
            assert "禁止下载" in str(exc)
        else:
            raise AssertionError("Blocked source unexpectedly entered the queue")

        mismatch_item = {
            **item,
            "name": "mismatch.safetensors",
            "workflow_hash": "b" * 64,
            "workflow_hash_type": "sha256",
        }
        try:
            manager.enqueue([mismatch_item])
        except ValueError as exc:
            assert "工作流要求不一致" in str(exc)
        else:
            raise AssertionError("Hash-conflicting source unexpectedly entered the queue")


def assert_persisted_paths_are_revalidated():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        state_dir = os.path.join(temp_dir, "state")
        outside_dir = os.path.join(temp_dir, "outside")
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        os.makedirs(outside_dir, exist_ok=True)
        folder_paths.folder_names_and_paths["mmf_state_paths"] = ([model_dir], {".safetensors"})

        valid_task = DownloadTask(
            id="canonicalized-task",
            name="canonicalized.safetensors",
            url="https://example.com/canonicalized.safetensors",
            normalized_url="https://example.com/canonicalized.safetensors",
            directory="mmf_state_paths",
            destination_root=model_dir,
            target_path=os.path.join(outside_dir, "canonicalized.safetensors"),
            part_path=os.path.join(outside_dir, "canonicalized.safetensors.part"),
            status="paused",
        )
        invalid_task = DownloadTask(
            id="invalid-root-task",
            name="invalid-root.safetensors",
            url="https://example.com/invalid-root.safetensors",
            normalized_url="https://example.com/invalid-root.safetensors",
            directory="mmf_state_paths",
            destination_root=outside_dir,
            target_path=os.path.join(outside_dir, "invalid-root.safetensors"),
            part_path=os.path.join(outside_dir, "invalid-root.safetensors.part"),
            status="queued",
        )
        with open(os.path.join(state_dir, "downloads.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "tasks": [valid_task.to_dict(), invalid_task.to_dict()]}, handle)

        with patch("missing_models_fetcher.downloader.logging.warning") as warning:
            manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
        restored = manager.tasks["canonicalized-task"]
        assert restored.target_path == os.path.join(model_dir, "canonicalized.safetensors")
        assert restored.part_path == f"{restored.target_path}.part"
        assert "invalid-root-task" not in manager.tasks
        assert manager.queue == []
        warning.assert_called_once()


def assert_pause_and_cancel_win_over_network_errors():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        for requested_flag, expected_status in (
            ("pause_requested", "paused"),
            ("cancel_requested", "canceled"),
        ):
            task = DownloadTask(
                id=f"{expected_status}-task",
                name=f"{expected_status}.safetensors",
                url="https://example.com/model.safetensors",
                normalized_url="https://example.com/model.safetensors",
                directory="unused",
                destination_root=temp_dir,
                target_path=os.path.join(temp_dir, f"{expected_status}.safetensors"),
                part_path=os.path.join(temp_dir, f"{expected_status}.safetensors.part"),
                status="downloading",
            )
            setattr(task, requested_flag, True)
            manager.tasks[task.id] = task
            with patch.object(manager, "_download_once", side_effect=OSError("simulated network failure")):
                manager._run_task(task)
            assert task.status == expected_status


def assert_resume_reuses_existing_active_target():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        target_path = os.path.join(temp_dir, "same-target.safetensors")
        old_task = DownloadTask(
            id="old-canceled-task",
            name="same-target.safetensors",
            url="https://example.com/old.safetensors",
            normalized_url="https://example.com/old.safetensors",
            directory="unused",
            destination_root=temp_dir,
            target_path=target_path,
            part_path=f"{target_path}.part",
            status="canceled",
        )
        active_task = DownloadTask(
            id="active-task",
            name="same-target.safetensors",
            url="https://example.com/new.safetensors",
            normalized_url="https://example.com/new.safetensors",
            directory="unused",
            destination_root=temp_dir,
            target_path=target_path,
            part_path=f"{target_path}.part",
            status="queued",
        )
        manager.tasks = {
            old_task.id: old_task,
            active_task.id: active_task,
        }
        manager.queue = [active_task.id]

        resumed = manager.resume(old_task.id)

        assert resumed["id"] == active_task.id
        assert old_task.status == "canceled"
        assert manager.queue == [active_task.id]


def assert_clear_can_target_finished_task_ids():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        for task_id in ("keep-completed", "remove-completed"):
            target_path = os.path.join(temp_dir, f"{task_id}.safetensors")
            manager.tasks[task_id] = DownloadTask(
                id=task_id,
                name=f"{task_id}.safetensors",
                url=f"https://example.com/{task_id}.safetensors",
                normalized_url=f"https://example.com/{task_id}.safetensors",
                directory="unused",
                destination_root=temp_dir,
                target_path=target_path,
                part_path=f"{target_path}.part",
                status="completed",
            )

        cleared = manager.clear(
            {"completed"},
            {"remove-completed"},
        )

        remaining_ids = {task["id"] for task in cleared["tasks"]}
        assert remaining_ids == {"keep-completed"}


def assert_clear_never_removes_unfinished_tasks():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        for index, status in enumerate(("queued", "downloading", "verifying", "paused")):
            task_id = f"unfinished-{status}"
            target_path = os.path.join(temp_dir, f"{task_id}.safetensors")
            manager.tasks[task_id] = DownloadTask(
                id=task_id,
                name=f"{task_id}.safetensors",
                url=f"https://example.com/{task_id}.safetensors",
                normalized_url=f"https://example.com/{task_id}.safetensors",
                directory="unused",
                destination_root=temp_dir,
                target_path=target_path,
                part_path=f"{target_path}.part",
                status=status,
                display_order=index,
            )
        manager.queue.append("unfinished-queued")

        cleared = manager.clear(
            {"queued", "downloading", "verifying", "paused"},
            set(manager.tasks),
        )

        assert {task["id"] for task in cleared["tasks"]} == set(manager.tasks)


def assert_clear_removes_terminal_records_without_deleting_files():
    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = os.path.join(temp_dir, "state")
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=state_dir,
        )
        terminal_ids = set()
        for index, status in enumerate(("completed", "failed", "canceled")):
            task_id = f"terminal-{status}"
            target_path = os.path.join(temp_dir, f"{task_id}.safetensors")
            part_path = f"{target_path}.part"
            with open(target_path, "wb") as handle:
                handle.write(f"model-{status}".encode("utf-8"))
            with open(part_path, "wb") as handle:
                handle.write(f"partial-{status}".encode("utf-8"))
            manager.tasks[task_id] = DownloadTask(
                id=task_id,
                name=os.path.basename(target_path),
                url=f"https://example.com/{task_id}.safetensors",
                normalized_url=f"https://example.com/{task_id}.safetensors",
                directory="unused",
                destination_root=temp_dir,
                target_path=target_path,
                part_path=part_path,
                status=status,
                display_order=index,
            )
            terminal_ids.add(task_id)
        manager._save_state_locked(force=True)

        cleared = manager.clear({"completed", "failed", "canceled"}, terminal_ids)
        assert not ({task["id"] for task in cleared["tasks"]} & terminal_ids)
        for task_id in terminal_ids:
            target_path = os.path.join(temp_dir, f"{task_id}.safetensors")
            assert os.path.isfile(target_path)
            assert os.path.isfile(f"{target_path}.part")

        reloaded = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=state_dir,
        )
        assert not ({task["id"] for task in reloaded.snapshot()["tasks"]} & terminal_ids)


def assert_clear_blocks_worker_owned_terminal_task():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        task_id = "worker-owned-failed"
        target_path = os.path.join(temp_dir, f"{task_id}.safetensors")
        manager.tasks[task_id] = DownloadTask(
            id=task_id,
            name=os.path.basename(target_path),
            url=f"https://example.com/{task_id}.safetensors",
            normalized_url=f"https://example.com/{task_id}.safetensors",
            directory="unused",
            destination_root=temp_dir,
            target_path=target_path,
            part_path=f"{target_path}.part",
            status="failed",
        )
        manager._active_task_ids.add(task_id)
        assert {task["id"] for task in manager.clear({"failed"}, {task_id})["tasks"]} == {task_id}
        manager._active_task_ids.clear()
        assert manager.clear({"failed"}, {task_id})["tasks"] == []


def assert_snapshot_order_is_stable_across_pause_resume():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        for index, task_id in enumerate(("first", "second", "third")):
            target_path = os.path.join(temp_dir, f"{task_id}.safetensors")
            manager.tasks[task_id] = DownloadTask(
                id=task_id,
                name=f"{task_id}.safetensors",
                url=f"https://example.com/{task_id}.safetensors",
                normalized_url=f"https://example.com/{task_id}.safetensors",
                directory="unused",
                destination_root=temp_dir,
                target_path=target_path,
                part_path=f"{target_path}.part",
                status="paused" if task_id == "second" else "completed",
                display_order=index,
            )

        before = [task["id"] for task in manager.snapshot()["tasks"]]
        manager.tasks["second"].status = "queued"
        manager.queue.append("second")
        after = [task["id"] for task in manager.snapshot()["tasks"]]

        assert before == after == ["first", "second", "third"]


def assert_worker_exit_race_restarts_for_new_queue_item():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        empty_queue_observed = threading.Event()
        allow_original_worker_exit = threading.Event()
        replacement_processed = threading.Event()
        original_next_task = manager._next_task

        def controlled_next_task():
            if threading.current_thread().name == "mmf-race-original":
                empty_queue_observed.set()
                if not allow_original_worker_exit.wait(timeout=5):
                    raise AssertionError("Timed out waiting to release original worker")
                return None
            return original_next_task()

        def fake_run_task(task):
            with manager.lock:
                task.status = "completed"
            replacement_processed.set()

        task = DownloadTask(
            id="worker-race-task",
            name="worker-race.safetensors",
            url="https://example.com/worker-race.safetensors",
            normalized_url="https://example.com/worker-race.safetensors",
            directory="unused",
            destination_root=temp_dir,
            target_path=os.path.join(temp_dir, "worker-race.safetensors"),
            part_path=os.path.join(temp_dir, "worker-race.safetensors.part"),
            status="queued",
        )

        with (
            patch.object(manager, "_next_task", side_effect=controlled_next_task),
            patch.object(manager, "_run_task", side_effect=fake_run_task),
        ):
            original_worker = threading.Thread(
                target=manager._worker_loop,
                name="mmf-race-original",
                daemon=True,
            )
            manager.worker = original_worker
            manager.workers = [original_worker]
            original_worker.start()
            assert empty_queue_observed.wait(timeout=5)

            with manager.lock:
                manager.tasks[task.id] = task
                manager.queue.append(task.id)
                manager._ensure_worker_locked()
                assert manager.worker is original_worker

            allow_original_worker_exit.set()
            original_worker.join(timeout=5)
            assert replacement_processed.wait(timeout=5)
            assert task.status == "completed"


def assert_configurable_parallel_download_workers():
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = DownloadManager(
            StaticSecrets({"download_concurrency": 2, "provider_concurrency": 2}),
            FolderRegistry(),
            state_dir=os.path.join(temp_dir, "state"),
        )
        release = threading.Event()
        both_started = threading.Event()
        started: set[str] = set()
        started_lock = threading.Lock()

        def fake_run_task(task):
            with started_lock:
                started.add(task.id)
                if len(started) == 2:
                    both_started.set()
            if not release.wait(timeout=5):
                raise AssertionError("Timed out waiting to release parallel workers")
            with manager.lock:
                task.status = "completed"

        tasks = [
            DownloadTask(
                id=f"parallel-{index}",
                name=f"parallel-{index}.safetensors",
                url=f"https://example.com/parallel-{index}.safetensors",
                normalized_url=f"https://example.com/parallel-{index}.safetensors",
                directory="unused",
                destination_root=temp_dir,
                target_path=os.path.join(temp_dir, f"parallel-{index}.safetensors"),
                part_path=os.path.join(temp_dir, f"parallel-{index}.safetensors.part"),
                status="queued",
            )
            for index in range(2)
        ]
        with patch.object(manager, "_run_task", side_effect=fake_run_task):
            with manager.lock:
                manager.tasks = {task.id: task for task in tasks}
                manager.queue = [task.id for task in tasks]
                manager._ensure_worker_locked()
            assert both_started.wait(timeout=5)
            snapshot = manager.snapshot()
            assert snapshot["download_concurrency"] == 2
            assert snapshot["provider_concurrency"] == 2
            assert snapshot["active_workers"] == 2
            release.set()
            for worker in list(manager.workers):
                worker.join(timeout=5)
        assert all(task.status == "completed" for task in tasks)


def assert_real_small_download_queue_limits_priority_and_bulk_controls():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        os.makedirs(model_dir, exist_ok=True)
        directory = "mmf_observed_queue_models"
        folder_paths.folder_names_and_paths[directory] = ([model_dir], {".safetensors"})
        ObservedRangeHandler.reset()
        ObservedRangeHandler.chunk_delay = 0.02
        server = ThreadingHTTPServer(("0.0.0.0", 0), ObservedRangeHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            port = server.server_port
            manager = DownloadManager(
                StaticSecrets({"download_concurrency": 3, "provider_concurrency": 1}),
                FolderRegistry(),
                state_dir=os.path.join(temp_dir, "state"),
            )
            items = [
                {
                    "name": "normal.safetensors",
                    "url": f"http://127.0.0.1:{port}/normal.safetensors",
                    "directory": directory,
                    "destination_path": model_dir,
                },
                {
                    "name": "high.safetensors",
                    "url": f"http://127.0.0.1:{port}/high.safetensors",
                    "directory": directory,
                    "destination_path": model_dir,
                },
                {
                    "name": "other-host.safetensors",
                    "url": f"http://127.0.0.2:{port}/other-host.safetensors",
                    "directory": directory,
                    "destination_path": model_dir,
                },
            ]
            with patch.object(manager, "_ensure_worker_locked"):
                tasks = manager.enqueue(items)
            task_by_name = {task["name"]: task["id"] for task in tasks}
            manager.set_task_priority(task_by_name["high.safetensors"], 1)
            scheduler_peak_by_provider: dict[str, int] = {}
            original_next_task = manager._next_task

            def observed_next_task():
                task = original_next_task()
                with manager.lock:
                    for provider_key, count in manager._active_provider_counts.items():
                        scheduler_peak_by_provider[provider_key] = max(
                            scheduler_peak_by_provider.get(provider_key, 0),
                            count,
                        )
                return task

            manager._next_task = observed_next_task
            with manager.lock:
                manager._ensure_worker_locked()

            assert ObservedRangeHandler.concurrent_started.wait(timeout=10)
            paused_snapshot = manager.bulk_control("pause")
            assert paused_snapshot["tasks"]
            deadline = time.time() + 10
            while time.time() < deadline:
                statuses = {task.status for task in manager.tasks.values()}
                if statuses <= {"paused"}:
                    break
                if "failed" in statuses:
                    raise AssertionError([task.error for task in manager.tasks.values()])
                time.sleep(0.03)
            assert {task.status for task in manager.tasks.values()} == {"paused"}
            assert any(os.path.exists(task.part_path) for task in manager.tasks.values())

            deadline = time.time() + 5
            while time.time() < deadline:
                with ObservedRangeHandler.observation_lock:
                    if ObservedRangeHandler.active_total == 0:
                        break
                time.sleep(0.02)
            with ObservedRangeHandler.observation_lock:
                assert ObservedRangeHandler.active_total == 0
                assert ObservedRangeHandler.peak_total == 2
                first_127_request = next(
                    path for host, path in ObservedRangeHandler.request_order if host == "127.0.0.1"
                )
                assert first_127_request.startswith("/high.safetensors")
            ObservedRangeHandler.reset()

            manager.bulk_control("resume")
            deadline = time.time() + 30
            while time.time() < deadline:
                statuses = {task.status for task in manager.tasks.values()}
                if statuses == {"completed"}:
                    break
                if "failed" in statuses:
                    raise AssertionError([task.error for task in manager.tasks.values()])
                time.sleep(0.05)
            assert {task.status for task in manager.tasks.values()} == {"completed"}
            assert ObservedRangeHandler.peak_total == 2, {
                "peak_total": ObservedRangeHandler.peak_total,
                "peak_by_host": ObservedRangeHandler.peak_by_host,
                "request_order": ObservedRangeHandler.request_order,
            }
            assert scheduler_peak_by_provider
            assert all(peak == 1 for peak in scheduler_peak_by_provider.values())
            first_127_request = next(path for host, path in ObservedRangeHandler.request_order if host == "127.0.0.1")
            assert first_127_request.startswith("/high.safetensors")
            for task in manager.tasks.values():
                with open(task.target_path, "rb") as handle:
                    assert handle.read() == PAYLOAD

            ObservedRangeHandler.chunk_delay = 0
            limited_manager = DownloadManager(
                StaticSecrets({
                    "download_concurrency": 1,
                    "provider_concurrency": 1,
                    "bandwidth_limit_mbps": 8,
                }),
                FolderRegistry(),
                state_dir=os.path.join(temp_dir, "limited-state"),
            )
            limited_item = {
                "name": "limited.safetensors",
                "url": f"http://127.0.0.3:{port}/limited.safetensors",
                "directory": directory,
                "destination_path": model_dir,
            }
            started = time.monotonic()
            limited_id = limited_manager.enqueue([limited_item])[0]["id"]
            deadline = time.time() + 20
            while time.time() < deadline:
                limited_task = limited_manager.tasks[limited_id]
                if limited_task.status == "completed":
                    break
                if limited_task.status == "failed":
                    raise AssertionError(limited_task.error)
                time.sleep(0.03)
            elapsed = time.monotonic() - started
            assert limited_manager.tasks[limited_id].status == "completed"
            expected_seconds = len(PAYLOAD) / (8 * 1024 * 1024)
            assert elapsed >= expected_seconds * 0.85
        finally:
            server.shutdown()
            folder_paths.folder_names_and_paths.pop(directory, None)


def assert_resumable_download():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        os.makedirs(model_dir, exist_ok=True)
        folder_paths.folder_names_and_paths["mmf_test_models"] = ([model_dir], {".safetensors"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=os.path.join(temp_dir, "state"))
            url = f"http://127.0.0.1:{server.server_port}/model.safetensors"
            metadata = manager.remote_metadata(url)
            assert metadata["size"] == len(PAYLOAD)
            expected_hash = hashlib.sha256(PAYLOAD).hexdigest()
            item = {
                "name": "model.safetensors",
                "url": url,
                "directory": "mmf_test_models",
                "destination_path": model_dir,
                "hash": expected_hash,
                "hash_type": "sha256",
            }
            task_id = manager.enqueue([item])[0]["id"]
            duplicate_id = manager.enqueue([item])[0]["id"]
            assert duplicate_id == task_id
            assert len(manager.tasks) == 1

            paused = False
            deadline = time.time() + 20
            while time.time() < deadline:
                task = manager.tasks[task_id]
                if task.status == "downloading" and task.downloaded > 0 and not paused:
                    manager.pause(task_id)
                    paused = True
                if paused and task.status == "paused":
                    assert os.path.exists(task.part_path)
                    manager.resume(task_id)
                if task.status == "completed":
                    break
                if task.status == "failed":
                    raise AssertionError(task.error)
                time.sleep(0.05)

            task = manager.tasks[task_id]
            assert paused
            assert task.status == "completed"
            assert task.verified_hash
            with open(task.target_path, "rb") as handle:
                assert handle.read() == PAYLOAD

            complete_part_item = {
                "name": "complete-part.safetensors",
                "url": f"http://127.0.0.1:{server.server_port}/complete-part.safetensors",
                "directory": "mmf_test_models",
                "destination_path": model_dir,
                "hash": expected_hash,
                "hash_type": "sha256",
            }
            complete_part_path = os.path.join(model_dir, "complete-part.safetensors.part")
            with open(complete_part_path, "wb") as handle:
                handle.write(PAYLOAD)
            complete_part_id = manager.enqueue([complete_part_item])[0]["id"]
            deadline = time.time() + 20
            while time.time() < deadline:
                complete_part_task = manager.tasks[complete_part_id]
                if complete_part_task.status == "completed":
                    break
                if complete_part_task.status == "failed":
                    raise AssertionError(complete_part_task.error)
                time.sleep(0.05)
            assert manager.tasks[complete_part_id].status == "completed"
            assert not os.path.exists(complete_part_path)
        finally:
            server.shutdown()


def assert_persistent_queue_restores_downloading_task():
    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "models")
        state_dir = os.path.join(temp_dir, "state")
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        folder_paths.folder_names_and_paths["mmf_restore_models"] = ([model_dir], {".safetensors"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/restore.safetensors"
            target_path = os.path.join(model_dir, "restore.safetensors")
            part_path = f"{target_path}.part"
            part_size = len(PAYLOAD) // 3
            with open(part_path, "wb") as handle:
                handle.write(PAYLOAD[:part_size])

            task = DownloadTask(
                id="restore-task",
                name="restore.safetensors",
                url=url,
                normalized_url=url,
                directory="mmf_restore_models",
                destination_root=model_dir,
                target_path=target_path,
                part_path=part_path,
                hash=hashlib.sha256(PAYLOAD).hexdigest(),
                hash_type="sha256",
                status="downloading",
                downloaded=part_size,
                total=len(PAYLOAD),
            )
            with open(os.path.join(state_dir, "downloads.json"), "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "tasks": [task.to_dict()]}, handle)

            manager = DownloadManager(StaticSecrets({}), FolderRegistry(), state_dir=state_dir)
            deadline = time.time() + 20
            while time.time() < deadline:
                restored = manager.tasks["restore-task"]
                if restored.status == "completed":
                    break
                if restored.status == "failed":
                    raise AssertionError(restored.error)
                time.sleep(0.05)

            restored = manager.tasks["restore-task"]
            assert restored.status == "completed"
            assert restored.verified_hash
            assert not os.path.exists(part_path)
            with open(target_path, "rb") as handle:
                assert handle.read() == PAYLOAD

            cleared = manager.clear()
            assert cleared["tasks"] == []
            with open(os.path.join(state_dir, "downloads.json"), "r", encoding="utf-8") as handle:
                persisted = json.load(handle)
            assert persisted["tasks"] == []
        finally:
            server.shutdown()


if __name__ == "__main__":
    assert_scan_and_url_normalization()
    assert_first_writable_folder_is_default()
    assert_refresh_directory_invalidates_comfy_cache()
    assert_civitai_request_context_masks_token()
    assert_civitai_version_metadata_uses_official_file_data()
    assert_civitai_model_page_expands_versions_and_reports_invalid_ids()
    assert_provider_credentials_stay_on_trusted_hosts()
    assert_cross_host_redirect_strips_credentials()
    assert_hf_xet_403_refreshes_presigned_url_once()
    assert_non_xet_hf_403_is_not_retried_and_error_is_accurate()
    assert_hf_file_metadata_uses_lfs_oid_not_xet_hash()
    assert_hf_api_metadata_survives_download_probe_failure()
    assert_api_key_testing_does_not_expose_secrets()
    assert_modelscope_config_and_source_resolution()
    assert_socks5_and_socks5h_proxy_dns_modes()
    assert_friendly_network_error_categories()
    assert_fixed_small_provider_regression_samples()
    assert_three_provider_hash_resolution()
    assert_single_provider_progressive_resolution()
    assert_source_cache_coalesces_concurrent_requests_and_expires()
    assert_source_resolution_jobs_are_cancelable_and_report_diagnostics()
    assert_restart_discards_only_the_failed_tasks_partial_file()
    assert_safetensors_header_range_infers_directory_without_full_download()
    assert_safetensors_directory_inference_is_conservative()
    assert_trusted_source_index_is_persistent_sanitized_and_reused()
    assert_provider_concurrency_and_global_bandwidth_limits()
    assert_disk_preflight_and_hash_verification_state()
    assert_source_resolution_deadline_is_enforced()
    assert_manual_item_preparation_uses_source_path_directory()
    assert_hf_repository_link_expands_model_files()
    assert_modelscope_repository_link_expands_model_files()
    assert_credential_validation_monitor_reports_problems_without_secrets()
    assert_state_save_retries_transient_windows_lock()
    assert_sensitive_urls_are_not_persisted()
    assert_batch_enqueue_is_atomic()
    assert_source_hash_and_blocked_state_are_enforced_on_enqueue()
    assert_persisted_paths_are_revalidated()
    assert_pause_and_cancel_win_over_network_errors()
    assert_resume_reuses_existing_active_target()
    assert_clear_can_target_finished_task_ids()
    assert_clear_never_removes_unfinished_tasks()
    assert_clear_removes_terminal_records_without_deleting_files()
    assert_clear_blocks_worker_owned_terminal_task()
    assert_snapshot_order_is_stable_across_pause_resume()
    assert_worker_exit_race_restarts_for_new_queue_item()
    assert_configurable_parallel_download_workers()
    assert_real_small_download_queue_limits_priority_and_bulk_controls()
    assert_resumable_download()
    assert_persistent_queue_restores_downloading_task()
    print("smoke backend ok")
