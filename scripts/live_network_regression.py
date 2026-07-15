from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.request import Request, urlopen


PROVIDER_SAMPLES = {
    "hf": "https://huggingface.co/optimum-intel-internal-testing/tiny-random-bert/resolve/main/model.safetensors",
    "modelscope": "https://modelscope.cn/models/CrabInHoney/urlbert-tiny-base-v4/resolve/master/model.safetensors",
    "civitai": "https://civitai.com/api/download/models/976226",
}


def request_json(url: str, payload: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def run_provider_matrix(api_root: str, timeout: int, label: str) -> list[dict[str, Any]]:
    results = []
    for provider, url in PROVIDER_SAMPLES.items():
        separator = "&" if "?" in url else "?"
        probe_url = f"{url}{separator}mmf_network_probe={label}-{time.time_ns()}"
        started = time.monotonic()
        try:
            response = request_json(f"{api_root}/metadata", {"url": probe_url}, timeout)
            metadata = response.get("metadata") or {}
            digest = str(metadata.get("sha256") or "")
            results.append(
                {
                    "provider": provider,
                    "ok": bool(response.get("ok")),
                    "size": metadata.get("size"),
                    "sha256_prefix": f"{digest[:12]}..." if digest else None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "provider": provider,
                    "ok": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "error": str(exc),
                }
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run small live network probes through MMFetcher.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8188")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--test-active-proxy", action="store_true")
    args = parser.parse_args()

    api_root = f"{args.base_url.rstrip('/')}/missing-models-fetcher"
    config = request_json(f"{api_root}/config", None, args.timeout).get("config") or {}
    original_mode = str(config.get("proxy_mode") or "off")
    active_proxy_id = str(config.get("active_proxy_id") or "")
    report: dict[str, Any] = {
        "direct": run_provider_matrix(api_root, args.timeout, "direct"),
        "proxy": None,
        "restored_proxy_mode": original_mode,
    }

    if args.test_active_proxy:
        if not active_proxy_id:
            report["proxy"] = {"skipped": True, "reason": "没有已选中的自定义代理"}
        else:
            profile = next(
                (item for item in config.get("proxy_profiles", []) if item.get("id") == active_proxy_id),
                {},
            )
            try:
                request_json(
                    f"{api_root}/config",
                    {"proxy_mode": "custom", "active_proxy_id": active_proxy_id},
                    args.timeout,
                )
                report["proxy"] = {
                    "scheme": profile.get("scheme"),
                    "results": run_provider_matrix(api_root, args.timeout, "proxy"),
                }
            finally:
                request_json(
                    f"{api_root}/config",
                    {"proxy_mode": original_mode, "active_proxy_id": active_proxy_id},
                    args.timeout,
                )
                restored = request_json(f"{api_root}/config", None, args.timeout).get("config") or {}
                report["restored_proxy_mode"] = restored.get("proxy_mode")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    result_groups = [report["direct"]]
    if isinstance(report.get("proxy"), dict) and isinstance(report["proxy"].get("results"), list):
        result_groups.append(report["proxy"]["results"])
    success = all(item.get("ok") for group in result_groups for item in group)
    success = success and report["restored_proxy_mode"] == original_mode
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
