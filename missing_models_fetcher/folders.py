from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import folder_paths


DIRECTORY_ALIASES = {
    "checkpoint": "checkpoints",
    "ckpt": "checkpoints",
    "clip": "text_encoders",
    "control_net": "controlnet",
    "controlnet": "controlnet",
    "diffusion_model": "diffusion_models",
    "diffusion_models": "diffusion_models",
    "lora": "loras",
    "loras": "loras",
    "unet": "diffusion_models",
    "upscale_model": "upscale_models",
    "vae": "vae",
}


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.realpath(path)))


def _is_writable_path(path: str) -> bool:
    if os.path.isdir(path):
        return os.access(path, os.W_OK)
    parent = os.path.dirname(path) or os.getcwd()
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def _safe_relative_filename(filename: str) -> str:
    normalized = os.path.normpath(filename.replace("\\", "/"))
    if normalized in ("", ".", os.curdir):
        raise ValueError("模型文件名不能为空")
    if os.path.isabs(normalized):
        raise ValueError("模型文件名不能是绝对路径")
    parts = normalized.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("模型文件名包含不安全的路径片段")
    return normalized


@dataclass
class FolderOption:
    path: str
    exists: bool
    writable: bool
    default: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "writable": self.writable,
            "default": self.default,
        }


class FolderRegistry:
    def normalize_directory(self, directory: str | None) -> str | None:
        if not directory:
            return None
        raw = str(directory).strip()
        if raw in folder_paths.folder_names_and_paths:
            return raw
        mapped = DIRECTORY_ALIASES.get(raw.lower())
        if mapped in folder_paths.folder_names_and_paths:
            return mapped
        return None

    def all_folders(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for name in sorted(folder_paths.folder_names_and_paths.keys()):
            paths, extensions = folder_paths.folder_names_and_paths[name]
            options = [option.to_dict() for option in self._folder_options(paths)]
            output.append(
                {
                    "directory": name,
                    "paths": options,
                    "extensions": sorted(list(extensions)),
                }
            )
        return output

    def folder_options(self, directory: str | None) -> list[dict[str, Any]]:
        normalized = self.normalize_directory(directory)
        if normalized is None:
            return []
        paths = folder_paths.get_folder_paths(normalized)
        return [option.to_dict() for option in self._folder_options(paths)]

    def is_installed(self, directory: str | None, filename: str | None) -> bool:
        normalized = self.normalize_directory(directory)
        if normalized is None or not filename:
            return False
        try:
            safe_name = _safe_relative_filename(filename)
        except ValueError:
            return False
        return folder_paths.get_full_path(normalized, safe_name) is not None

    def resolve_destination(
        self,
        directory: str,
        filename: str,
        selected_root: str | None = None,
    ) -> tuple[str, str, str]:
        normalized = self.normalize_directory(directory)
        if normalized is None:
            raise ValueError(f"未知模型目录: {directory}")

        safe_name = _safe_relative_filename(filename)
        registered_paths = folder_paths.get_folder_paths(normalized)
        if not registered_paths:
            raise ValueError(f"模型目录没有注册保存路径: {normalized}")

        root = selected_root or self._first_writable_path(registered_paths) or registered_paths[0]
        root_norm = _norm_path(root)
        registered_norms = {_norm_path(path) for path in registered_paths}
        if root_norm not in registered_norms:
            raise ValueError("目标路径不属于 ComfyUI 注册的模型目录")

        target_path = os.path.abspath(os.path.join(root, safe_name))
        if os.path.commonpath([root_norm, _norm_path(target_path)]) != root_norm:
            raise ValueError("目标文件路径越界")
        return normalized, root, target_path

    def _first_writable_path(self, paths: list[str]) -> str | None:
        for path in paths:
            if _is_writable_path(path):
                return path
        return None

    def _folder_options(self, paths: list[str]) -> list[FolderOption]:
        default_path = self._first_writable_path(paths)
        if default_path is None and paths:
            default_path = paths[0]
        default_norm = _norm_path(default_path) if default_path else None
        return [
            self._folder_option(path, default_norm is not None and _norm_path(path) == default_norm)
            for path in paths
        ]

    def _folder_option(self, path: str, is_default: bool) -> FolderOption:
        return FolderOption(
            path=path,
            exists=os.path.isdir(path),
            writable=_is_writable_path(path),
            default=is_default,
        )

    def refresh_directory(self, directory: str) -> None:
        normalized = self.normalize_directory(directory)
        if normalized is None:
            return
        try:
            filename_cache = getattr(folder_paths, "filename_list_cache", None)
            if isinstance(filename_cache, dict):
                filename_cache.pop(normalized, None)

            cache_helper = getattr(folder_paths, "cache_helper", None)
            helper_cache = getattr(cache_helper, "cache", None)
            if isinstance(helper_cache, dict):
                helper_cache.pop(normalized, None)

            folder_paths.get_filename_list(normalized)
        except Exception:
            pass
