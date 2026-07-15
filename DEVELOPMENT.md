# Development Guide

This document contains maintainer-facing information for ComfyUI Missing Models Fetcher. User installation and usage belong in [README.md](README.md).

## Project layout

- `missing_models_fetcher/routes.py`: backend HTTP routes.
- `missing_models_fetcher/scanner.py`: workflow and model metadata scanning.
- `missing_models_fetcher/folders.py`: ComfyUI model-folder resolution and path safety.
- `missing_models_fetcher/downloader.py`: provider resolution, queueing, resumable downloads, verification, and proxy handling.
- `missing_models_fetcher/config.py`: private user configuration and credential masking.
- `web/js/missing_models_fetcher.js`: ComfyUI frontend extension.
- `web/js/mmf_state.mjs`: frontend state helpers.
- `tests/`: backend smoke and frontend state tests.
- `scripts/`: validation, live-network, live-download, and lifecycle checks.

## Development installation

Clone the repository or link it into the target ComfyUI `custom_nodes` directory, then restart ComfyUI. Runtime code must use ComfyUI APIs and registered folders; do not add machine-specific paths to the extension.

The only explicit runtime dependency is:

```text
certifi>=2024.7.4
```

Do not disable TLS verification as a workaround for certificate problems.

## Validation

Run the complete local validation suite:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\validate.ps1 `
  -ComfyUIRoot "<ComfyUI directory>" `
  -Python "<ComfyUI Python>" `
  -Node "<Node.js executable>"
```

The suite covers Python syntax, frontend JavaScript syntax, frontend state tests, backend smoke tests, and lifecycle-script syntax.

Registry metadata and source validation:

```powershell
python scripts\validate_release.py --allow-placeholders
comfy node validate
```

The non-placeholder release check is intentionally stricter:

```powershell
python scripts\validate_release.py
```

## Live network regression

Run small metadata and Range probes against the fixed Hugging Face, ModelScope, and Civitai samples:

```powershell
& "<ComfyUI Python>" scripts\live_network_regression.py
```

Add `--test-active-proxy` only when the saved proxy profile should be tested. The script restores the original proxy mode before exit.

## Live download queue E2E

The three-provider live E2E performs real downloads in an isolated subdirectory of a registered model folder:

```powershell
& "<ComfyUI Python>" scripts\live_download_e2e.py `
  --base-url http://127.0.0.1:8188 `
  --restart-bat "<ComfyUI restart script>"
```

It validates:

- origin-to-CDN redirects;
- HTTP `206 Content-Range` support;
- final file size and SHA-256;
- real pause and resume through `.part`;
- queue recovery after restarting ComfyUI;
- restoration of concurrency, bandwidth, and proxy settings;
- removal of test files and only the test-created queue records.

The script refuses to run when the plugin proxy is enabled or when user tasks are active or paused.

## Manager lifecycle E2E

Use a disposable Comfy CLI environment:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manager_lifecycle_e2e.ps1 `
  -ComfyCli "<Disposable venv>\Scripts\comfy.exe" `
  -ComfyUISource "<ComfyUI directory>"
```

The test copies a minimal temporary ComfyUI workspace, serves a temporary Git repository, and verifies install, disable, enable, update, and uninstall. It also proves that uninstall does not delete plugin-private user data. Temporary repositories, servers, and workspaces are removed at exit.

This test environment deliberately does not install PyTorch and must not modify the real ComfyUI Python, CUDA, or NVIDIA driver installation.

## Packaging

Build the Registry package with Comfy CLI:

```powershell
comfy node pack
```

`.comfyignore` excludes local instructions, tests, scripts, CI configuration, caches, logs, temporary downloads, and build artifacts. Audit every generated archive before release. It must not contain credentials, private configuration, queue state, `.part` files, local paths used as defaults, or downloaded models.

## Continuous integration

`.github/workflows/validate.yml` runs on pushes and pull requests. It installs Comfy CLI, validates metadata and source, checks Python and JavaScript syntax, and proves that the Registry package can be built.

`.github/workflows/publish.yml` is manual-only. Its publish job depends on a live E2E job running on a dedicated Windows self-hosted runner with these labels:

- `self-hosted`
- `Windows`
- `X64`
- `comfyui-mmf-e2e`

The runner must have a healthy ComfyUI backend, the plugin proxy disabled, and the loaded plugin path resolving to the current Actions checkout.

## Release checklist

1. Keep the Registry package ID `missing-models-fetcher` stable.
2. Update `pyproject.toml` with the real long-lived `Repository` and `PublisherId`.
3. Run the complete validation, live network regression, live download E2E, and Manager lifecycle E2E.
4. Rebuild and audit `node.zip` from the final commit.
5. Confirm the GitHub Validate workflow passes.
6. Configure the `REGISTRY_ACCESS_TOKEN` repository secret.
7. Run the manual `Publish to ComfyUI Registry` workflow.
8. Install the published package into a clean ComfyUI environment and repeat install, disable, enable, update, and uninstall checks.

Until `PublisherId` is replaced, `scripts/validate_release.py` must continue to block formal publishing.

## Security boundaries

- Never commit API keys, tokens, cookies, proxy passwords, local queue state, or downloaded models.
- Never persist signed CDN URLs or sensitive authentication query parameters.
- Send provider credentials only to the matching trusted provider host.
- Strip authentication headers, cookies, and proxy authorization on cross-host redirects.
- Keep destination validation bound to `folder_paths.folder_names_and_paths`.
- Preserve `.part` files on pause and cancel so downloads remain resumable.
- Automatic scanning may open the confirmation panel but must never start a download.
