from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_MARKERS = ("your-name", "your-publisher", "example.com", "replace-me")
PACKAGE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.-]+)?$")


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 ComfyUI Registry 发布前配置。")
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="允许 Repository 和 PublisherId 使用占位符，仅用于普通 CI。",
    )
    args = parser.parse_args()

    errors: list[str] = []
    pyproject_path = ROOT / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"无法读取 pyproject.toml: {exc}", file=sys.stderr)
        return 1

    project = data.get("project") or {}
    comfy = (data.get("tool") or {}).get("comfy") or {}
    project_urls = project.get("urls") or {}

    package_id = str(project.get("name") or "").strip()
    version = str(project.get("version") or "").strip()
    repository = str(project_urls.get("Repository") or "").strip()
    publisher_id = str(comfy.get("PublisherId") or "").strip()
    display_name = str(comfy.get("DisplayName") or "").strip()

    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        fail("project.name 必须是稳定的小写 Registry ID。", errors)
    if not VERSION_PATTERN.fullmatch(version):
        fail("project.version 必须使用明确的语义化版本号。", errors)
    if not display_name:
        fail("tool.comfy.DisplayName 不能为空。", errors)

    parsed_repository = urlparse(repository)
    if parsed_repository.scheme != "https" or parsed_repository.netloc.lower() != "github.com":
        fail("project.urls.Repository 必须是 HTTPS GitHub 仓库地址。", errors)

    placeholders = f"{repository} {publisher_id}".lower()
    if not args.allow_placeholders and any(marker in placeholders for marker in PLACEHOLDER_MARKERS):
        fail("Repository 或 PublisherId 仍是占位符，禁止发布。", errors)
    if not publisher_id:
        fail("tool.comfy.PublisherId 不能为空。", errors)

    for relative_path in ("README.md", "LICENSE", "__init__.py", ".comfyignore", "requirements.txt"):
        if not (ROOT / relative_path).is_file():
            fail(f"缺少发布文件: {relative_path}", errors)

    comfyignore = (ROOT / ".comfyignore").read_text(encoding="utf-8")
    for required_pattern in (
        ".git/",
        "tests/",
        "scripts/",
        ".ruff_cache/",
        "*.part",
        "*.log",
        "*.zip",
    ):
        if required_pattern not in comfyignore:
            fail(f".comfyignore 缺少规则: {required_pattern}", errors)

    if errors:
        for error in errors:
            print(f"[失败] {error}", file=sys.stderr)
        return 1

    mode = "普通 CI" if args.allow_placeholders else "正式发布"
    print(f"发布前检查通过（{mode}）：{package_id} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
