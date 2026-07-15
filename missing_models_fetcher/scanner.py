from __future__ import annotations

import hashlib
import os
import re
from typing import Any
from urllib.parse import unquote, urlparse

from .folders import FolderRegistry
from .urls import strip_sensitive_query


NAME_KEYS = ("filename", "file_name", "fileName", "model_name", "modelName", "model", "name")
URL_KEYS = (
    "download_url",
    "downloadUrl",
    "model_url",
    "modelUrl",
    "file_url",
    "fileUrl",
    "source_url",
    "sourceUrl",
    "url",
    "source",
    "link",
    "homepage",
)
DIRECTORY_KEYS = ("directory", "folder", "folder_name", "folderName", "model_type", "modelType")
HASH_KEYS = ("hash", "sha256", "sha1", "md5", "hash_value", "hashValue", "checksum")
WIDGET_VALUE_KEYS = ("value", "selected", "file", "filename", "model")
WIDGET_NAME_KEYS = ("widgetName", "widget_name", "name", "label")
MODEL_EXTENSIONS = {
    ".bin",
    ".ckpt",
    ".gguf",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
}

WIDGET_DIRECTORY_HINTS = {
    "ckpt_name": "checkpoints",
    "checkpoint": "checkpoints",
    "clip_name": "text_encoders",
    "clip_l": "text_encoders",
    "clip_g": "text_encoders",
    "t5xxl": "text_encoders",
    "control_net_name": "controlnet",
    "controlnet_name": "controlnet",
    "diffusion_model_name": "diffusion_models",
    "lora_name": "loras",
    "unet_name": "diffusion_models",
    "upscale_model_name": "upscale_models",
    "vae_name": "vae",
    "clip_vision_name": "clip_vision",
    "style_model_name": "style_models",
    "gligen_name": "gligen",
    "background_removal_model": "background_removal",
}

NODE_TYPE_DIRECTORY_HINTS = (
    ("checkpointloader", "checkpoints"),
    ("unetloader", "diffusion_models"),
    ("diffusionmodelloader", "diffusion_models"),
    ("dualcliploader", "text_encoders"),
    ("triplecliploader", "text_encoders"),
    ("cliploader", "text_encoders"),
    ("clipvisionloader", "clip_vision"),
    ("vaeloader", "vae"),
    ("loraloader", "loras"),
    ("controlnetloader", "controlnet"),
    ("upscalemodelloader", "upscale_models"),
    ("stylemodelloader", "style_models"),
    ("gligenloader", "gligen"),
    ("backgroundremoval", "background_removal"),
)


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _filename_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    name = os.path.basename(unquote(parsed.path))
    return name if _has_model_extension(name) else ""


def _has_model_extension(value: str) -> bool:
    return os.path.splitext(value)[1].lower() in MODEL_EXTENSIONS


def _filename_from_value(value: str) -> str:
    if not value:
        return ""
    cleaned = value.strip()
    if _has_model_extension(cleaned):
        return cleaned
    return _filename_from_url(cleaned)


def _provider_from_url(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname == "huggingface.co" or hostname.endswith(".huggingface.co"):
        return "hf"
    if (
        hostname in {"civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red"}
        or hostname.endswith(".civitai.com")
    ):
        return "civitai"
    if hostname == "modelscope.cn" or hostname.endswith(".modelscope.cn"):
        return "modelscope"
    return "manual"


def _compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", value.lower())


def _looks_like_model(data: dict[str, Any]) -> bool:
    name = _first_string(data, NAME_KEYS)
    url = _first_string(data, URL_KEYS)
    if not name and url:
        name = _filename_from_url(url)
    if not name:
        return False

    if _has_model_extension(name):
        return True
    return any(key in data for key in ("directory", "url", "hash", "hash_type", "download_url", "downloadUrl"))


class WorkflowScanner:
    def __init__(self, folders: FolderRegistry) -> None:
        self.folders = folders

    def scan(self, workflow: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        candidate_by_key: dict[str, dict[str, Any]] = {}

        def resolve_directory(raw_directory: str, widget_name: str = "", node_type: str = "") -> tuple[str | None, str]:
            normalized = self.folders.normalize_directory(raw_directory)
            if normalized is not None:
                return normalized, raw_directory

            widget_key = _compact_key(widget_name)
            if widget_key:
                for key, directory in WIDGET_DIRECTORY_HINTS.items():
                    if key in widget_key:
                        normalized = self.folders.normalize_directory(directory)
                        if normalized is not None:
                            return normalized, directory

            node_key = _compact_key(node_type)
            if node_key:
                for key, directory in NODE_TYPE_DIRECTORY_HINTS:
                    if key in node_key:
                        normalized = self.folders.normalize_directory(directory)
                        if normalized is not None:
                            return normalized, directory

            return None, raw_directory

        def add_candidate(raw: dict[str, Any], context: dict[str, Any], *, widget_value: str = "") -> None:
            url = strip_sensitive_query(_first_string(raw, URL_KEYS))
            name = _first_string(raw, NAME_KEYS)
            if widget_value:
                name = _filename_from_value(widget_value) or name
            if not name and url:
                name = _filename_from_url(url)
            if not name:
                return

            raw_directory = _first_string(raw, DIRECTORY_KEYS)
            if not raw_directory and not widget_value:
                raw_type = _first_string(raw, ("type",))
                if self.folders.normalize_directory(raw_type) is not None:
                    raw_directory = raw_type
            widget_name = _first_string(raw, WIDGET_NAME_KEYS) or context.get("widgetName", "")
            node_type = _first_string(raw, ("nodeType", "node_type")) or context.get("nodeType", "")
            directory, directory_hint = resolve_directory(raw_directory, widget_name, node_type)

            directory_name = directory or directory_hint
            key = "|".join([directory_name.lower(), name.replace("\\", "/").lower()])
            existing = candidate_by_key.get(key)
            if existing is not None:
                if not existing["url"] and url:
                    existing["url"] = url
                    existing["needs_url"] = False
                if url and all(source.get("url") != url for source in existing["sources"]):
                    existing["sources"].append(
                        {
                            "provider": _provider_from_url(url),
                            "url": url,
                            "verification": "original",
                        }
                    )
                hash_value = _first_string(raw, HASH_KEYS)
                hash_type = _first_string(raw, ("hash_type", "hashType"))
                if not existing["hash"] and hash_value:
                    existing["hash"] = hash_value
                    existing["hash_type"] = hash_type
                if not existing["nodeType"] and node_type:
                    existing["nodeType"] = node_type
                if not existing["widgetName"] and widget_name:
                    existing["widgetName"] = widget_name
                return

            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            hash_value = _first_string(raw, HASH_KEYS)
            hash_type = _first_string(raw, ("hash_type", "hashType"))
            installed = self.folders.is_installed(directory, name)
            candidate = {
                "id": digest,
                "name": name,
                "url": url,
                "sources": (
                    [
                        {
                            "provider": _provider_from_url(url),
                            "url": url,
                            "verification": "original",
                        }
                    ]
                    if url
                    else []
                ),
                "directory": directory_name,
                "directory_valid": directory is not None,
                "nodeType": node_type,
                "widgetName": widget_name,
                "hash": hash_value,
                "hash_type": hash_type,
                "installed": installed,
                "needs_url": not bool(url),
                "needs_directory": directory is None,
                "destinationOptions": self.folders.folder_options(directory),
            }
            candidate_by_key[key] = candidate
            candidates.append(candidate)

        def maybe_add_widget_candidate(raw: dict[str, Any], context: dict[str, Any]) -> None:
            widget_name = _first_string(raw, WIDGET_NAME_KEYS)
            value = _first_string(raw, WIDGET_VALUE_KEYS)
            filename = _filename_from_value(value)
            if not filename:
                return
            widget_context = dict(context)
            if widget_name:
                widget_context["widgetName"] = widget_name
            add_candidate(raw, widget_context, widget_value=filename)

        def walk(value: Any, context: dict[str, Any]) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item, context)
                return

            if not isinstance(value, dict):
                return

            node_context = dict(context)
            node_type = value.get("type") or value.get("class_type") or value.get("nodeType") or value.get("node_type")
            if isinstance(node_type, str):
                node_context["nodeType"] = node_type

            maybe_add_widget_candidate(value, node_context)

            models = value.get("models") or value.get("missing_models") or value.get("missingModels")
            if isinstance(models, list):
                for model in models:
                    if isinstance(model, dict) and _looks_like_model(model):
                        add_candidate(model, node_context)
                    else:
                        walk(model, node_context)
            elif isinstance(models, dict):
                walk(models, node_context)

            widgets = value.get("widgets") or value.get("widget_values") or value.get("widgetValues")
            if isinstance(widgets, list):
                for widget in widgets:
                    if isinstance(widget, dict):
                        maybe_add_widget_candidate(widget, node_context)
                    else:
                        walk(widget, node_context)

            properties = value.get("properties")
            if isinstance(properties, dict):
                prop_models = properties.get("models") or properties.get("missing_models") or properties.get("missingModels")
                if isinstance(prop_models, list):
                    for model in prop_models:
                        if isinstance(model, dict) and _looks_like_model(model):
                            add_candidate(model, node_context)

            if _looks_like_model(value):
                add_candidate(value, node_context)

            for child_key, child_value in value.items():
                if child_key in {"models", "missing_models", "missingModels", "properties", "widgets", "widget_values", "widgetValues"}:
                    continue
                walk(child_value, node_context)

        def merge_candidate(target: dict[str, Any], source: dict[str, Any]) -> None:
            if not target["url"] and source["url"]:
                target["url"] = source["url"]
                target["needs_url"] = False
            for source_item in source["sources"]:
                if all(
                    existing.get("url") != source_item.get("url")
                    for existing in target["sources"]
                ):
                    target["sources"].append(source_item)
            if not target["hash"] and source["hash"]:
                target["hash"] = source["hash"]
                target["hash_type"] = source["hash_type"]
            if not target["nodeType"] and source["nodeType"]:
                target["nodeType"] = source["nodeType"]
            if not target["widgetName"] and source["widgetName"]:
                target["widgetName"] = source["widgetName"]

        walk(workflow, {})

        candidates_by_name: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            name_key = candidate["name"].replace("\\", "/").lower()
            candidates_by_name.setdefault(name_key, []).append(candidate)

        reconciled: list[dict[str, Any]] = []
        for same_name in candidates_by_name.values():
            valid = [candidate for candidate in same_name if candidate["directory_valid"]]
            weak = [candidate for candidate in same_name if not candidate["directory_valid"]]
            if valid:
                if len(valid) == 1:
                    for weak_candidate in weak:
                        merge_candidate(valid[0], weak_candidate)
                reconciled.extend(valid)
            else:
                reconciled.extend(weak)

        return sorted(
            reconciled,
            key=lambda item: (
                item.get("installed", False),
                item.get("directory", ""),
                item["name"],
            ),
        )
