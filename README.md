# ComfyUI Missing Models Fetcher

中文 | [English](#english)

一个 ComfyUI 自定义扩展，用于扫描当前工作流中缺失的模型，并把模型下载到 ComfyUI 当前注册的正确模型目录中。下载使用 `.part` 文件和 HTTP Range，支持断点续传、暂停、继续和取消。

## 功能

- 扫描当前工作流里的模型 metadata，例如 `name`、`url`、`directory`、`hash`、`hash_type`。
- 以 ComfyUI 运行时的 `folder_paths.folder_names_and_paths` 为准，不写死本机路径。
- 支持 Hugging Face、魔搭 ModelScope 和 Civitai 凭据。
- 支持解析 Civitai 官方模型页及带 `modelVersionId` 的 `civitai.red` 页面，并安全转换为官方 Civitai 下载接口。
- 凭据的保存、验证和清除集中在 ComfyUI 设置页的 `MMFetcher` 分类中；三个服务各自独立成行，不占用下载确认弹窗空间。
- 凭据输入后会经过防抖自动验证；保存后的凭据也会在后端启动、定时任务和 Web 页面加载时复查。验证成功不代表已经接受某个 gated 模型的许可。
- 凭据异常时会在顶部“缺失模型”按钮后显示悬停警告，点击警告可直接打开 `MMFetcher` 设置页。
- 使用 `certifi` 提供跨平台 TLS 证书链，避免嵌入式 Python 的系统 CA 路径差异影响下载。
- API Key 保存在 ComfyUI 用户数据目录下的插件私有文件中，不保存到插件源码目录。
- `MMFetcher -> 网络代理` 支持停用、系统代理和自定义代理模式；可保存并切换多个 HTTP、HTTPS、SOCKS5 或 SOCKS5H 代理。来源解析、凭据验证、metadata 与下载使用同一代理设置。主机/IP、端口和用户名正常显示，密码不会返回前端。
- 扫描结果和下载队列会移除 URL 查询参数中的 `token`、`api_key` 等敏感字段。
- 只接受 HTTP / HTTPS 下载链接；认证信息只发送到对应服务域名，跨主机跳转到 CDN 时会移除认证头和 Cookie。
- Hugging Face 文件优先通过官方 API 读取 Git LFS `lfs.oid` 作为 SHA-256，不把 Xet 存储 hash 或 CDN ETag 当作文件 hash。
- Hugging Face 原始链接跳转到 Xet CDN 后若签名 URL 返回临时 `403`，会从稳定原始链接刷新一次签名并重试；CDN 签名 URL 不写入队列状态。
- 工作流提供 SHA-256 时严格阻止不匹配或无法验证的来源；未提供时综合仓库、路径、大小和可信来源 hash，并用黄/红警告区分风险。
- 工作流加载完成后自动扫描；发现缺失模型时打开确认面板，也可手动打开面板重新扫描。
- 发现缺失模型时，顶部“缺失模型”按钮切换为红色并显示白色提示图标；模型安装完成后自动恢复。
- 下载前显示确认列表，可选择模型保存路径，不会静默开始下载。
- “手动新增”标签页支持粘贴多行模型名称和链接，批量解析 Hugging Face、魔搭、Civitai 来源，并在可信时自动推断模型目录。
- 单一 Civitai 模型页解析出多个版本时，前端会将这些版本归入同一个模型模块，并保留逐版本选择与下载。
- 模型与下载站点采用渐进式解析；每个站点完成后立即更新，可单独重试失败或未命中的站点，旧解析请求不会覆盖新结果。
- 来源 metadata、仓库搜索和文件列表使用 5 分钟线程安全缓存，并合并同时到达的相同请求；单站点搜索达到 75 秒后停止继续扩展候选。切换工作流或关闭面板会同时取消浏览器请求和后端解析任务。
- SHA-256 已验证的来源会写入 ComfyUI 用户目录中的去敏 `source_index.json`，后续遇到相同 hash 时可直接复用；这不是源码内的硬编码模型表。
- 下载总并发和每站并发可从预设值选择，也可手动填写 `1`–`32`；全局共享限速可手动填写 `0`–`100000 MB/s`，其中 `0` 表示不限速。每个任务继续使用独立 `.part` 文件断点续传。
- 队列支持全部暂停/继续、排队任务上移/下移以及高/普通/低持久化优先级，并显示活动数、排队数、暂停数、总速度和总 ETA。
- 手动解析 `.safetensors` 时可通过受 4 MiB 上限保护的 HTTP Range 读取文件头，辅助识别 LoRA、VAE、ControlNet、文本编码器和扩散模型目录，不会为识别类型下载整个模型。
- 模型目录推断优先采用来源 metadata 和仓库完整路径；safetensors 结构只接受明确特征，证据不足或来源冲突时要求用户选择目录，不再用通用 `encoder` / `decoder` 名称猜测 VAE。模型卡片会显示目录依据，文件名推测和待确认状态使用黄色提示。
- 来源按钮的悬停提示包含验证等级、可信度评分、判断依据和解析诊断；hash 冲突仍按既有安全规则警告或阻止。
- 已知模型大小时会在批量入队和收到服务器真实大小后检查磁盘空间，并扣除已有 `.part` 大小。
- Hash 校验使用独立的“校验中”状态，显示进度、速度和预计时间；校验期间支持暂停、继续和取消。
- 下载队列状态保存在 ComfyUI 用户数据目录，后端重启后可以继续未完成任务。
- 已在排队、下载、校验或暂停的同一目标文件不会重复创建任务。
- 前端中文界面，并通过 ComfyUI 官方顶部快捷栏、命令 / 菜单 API 和设置页提供入口。

## 安装

开发阶段推荐使用目录联接，把本仓库接入 ComfyUI：

```powershell
cmd /c mklink /J "<ComfyUI目录>\custom_nodes\ComfyUI-Missing-Models-Fetcher" "<本仓库目录>"
```

然后重启 ComfyUI。

稳定发布版应通过 ComfyUI Manager / ComfyUI Registry 安装、卸载、启用 / 禁用和更新。发布到 Registry 前，请把 `pyproject.toml` 中的 `Repository` 和 `PublisherId` 改成真实值。

Registry 发布 ID 是 `missing-models-fetcher`，ComfyUI 内显示名是 `ComfyUI Missing Models Fetcher`。不要在发布后随意修改 `pyproject.toml` 的 `name`。

说明：本地目录联接适合开发验证，但 ComfyUI Manager 的 installed 列表通常只完整识别有 Git remote 的仓库，或由 Registry 安装后带 `.tracking` 信息的包。发布前如果只使用本地新仓库，这是预期状态。

GitHub 仓库中已经包含：

- `.github/workflows/validate.yml`：每次推送和 PR 执行 Registry 校验、语法检查和打包检查。
- `.github/workflows/publish.yml`：手动发布到 ComfyUI Registry。
- `scripts/validate_release.py`：正式发布时阻止占位 `Repository` / `PublisherId` 被上传。

正式发布前，在 GitHub 仓库 Secrets 中配置 `REGISTRY_ACCESS_TOKEN`，然后手动运行 `Publish to ComfyUI Registry` 工作流。Registry 发布成功后，再用 ComfyUI Manager 验证安装、卸载、启用 / 禁用和更新。

## 使用

1. 打开 ComfyUI。
2. 如果工作流包含缺失模型，扩展会自动打开确认面板；也可以点击菜单中的“缺失模型”。
3. 在设置页的 `MMFetcher` 分类中按需填写 Hugging Face API Key、魔搭 Access Token 或 Civitai API Key。
4. 每个服务可独立保存或清除凭据；输入后会自动验证，已保存凭据会定时复查。大陆网络访问不稳定时，可在 `MMFetcher -> 网络代理` 选择系统代理，或添加并选中 HTTP / HTTPS / SOCKS5 代理。
5. 面板会自动扫描当前工作流；也可以点击“扫描当前工作流”手动刷新。
6. 为每个模型明确选择魔搭、Hugging Face、Civitai 或“手动输入”，再检查 `directory` 和保存路径。
7. 点击“下载选中模型”。插件不会自动在不同来源之间切换。

队列工具栏中的“并行”表示所有网站合计的最大并行任务数，“每站”表示每个下载网站各自的并行上限。三个下拉框均提供“自定义…”：总并行和每站并行接受 `1`–`32` 的整数，全局限速接受 `0`–`100000 MB/s`，并会保存到 ComfyUI 用户配置中。并发过高可能触发下载网站限流，也会增加网络、磁盘和文件句柄压力。

也可以切换到“手动新增”标签页，粘贴多行模型链接并点击“解析”。同一 Civitai 模型的不同版本会归入同一模型模块，其他条目会生成独立卡片，可选择下载站点、模型目录和保存路径。

如果没有解析到可用来源，选择“手动输入”并粘贴 HTTP(S) 下载地址。如果缺少 `directory`，请手动填写 ComfyUI 模型目录键，例如 `diffusion_models`、`vae`、`loras`、`checkpoints`、`background_removal`。

“删除已结束任务”只清理已完成、失败和已取消的队列记录，不会删除已经下载好的模型文件或 `.part` 断点文件。排队中、下载中、校验中和已暂停任务不能清理。失败任务在清理前仍可使用“继续”或“重新下载”。

需要检查当前网络与代理配置时，可在 ComfyUI 后端运行期间执行：

```powershell
C:\ComfyUI\python_embeded\python.exe scripts\live_network_regression.py --test-active-proxy
```

该脚本只对三站点固定小样本执行 metadata、HEAD 或 Range 探测，不会把模型加入下载队列；测试结束后会恢复原代理模式。暂停、继续、重启恢复、SOCKS5/SOCKS5H DNS 模式和凭据隔离由后端 smoke 测试覆盖。

真实下载队列端到端回归使用同一组三站点固定小样本，并把文件隔离到 ComfyUI 已注册的本地 `checkpoints/__mmf_live_e2e__/run-*` 子目录：

```powershell
C:\ComfyUI\python_embeded\python.exe scripts\live_download_e2e.py
```

脚本要求插件代理当前为停用状态且没有用户活动/暂停任务。它会验证 CDN 跳转、HTTP `206 Content-Range`、最终文件大小和 SHA-256，并真实执行暂停/继续和 ComfyUI 中途重启后的 `.part` 恢复。测试只临时调整下载并发和限速，结束时会恢复原值、删除测试文件和仅属于本次运行的队列记录；它不会启用或修改代理配置。

Manager 生命周期可在一次性 ComfyUI 工作区中验证，不会改动真实安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\manager_lifecycle_e2e.ps1 `
  -ComfyCli C:\path\to\disposable-venv\Scripts\comfy.exe
```

该脚本从本机 ComfyUI 复制最小测试工作区，通过临时本地 Git 服务依次验证安装、禁用、启用、更新和卸载，并确认卸载不会删除插件私有用户数据。临时 CLI 环境不安装 PyTorch，所有临时仓库、服务和工作区在结束时删除。

Registry 发布工作流把真实三站点下载 E2E 作为前置门禁。运行发布工作流前，需要准备带有 `self-hosted`、`Windows`、`X64` 和 `comfyui-mmf-e2e` 标签的专用 Runner，并确保运行时插件 Junction 指向 Actions 当前 checkout、插件代理保持停用。

## 安全说明

- 不要把真实 API Key 写入 `config.example.json`、README 或任何仓库文件。
- 本扩展不会静默下载模型，下载前需要用户确认。
- 目标保存路径必须属于 ComfyUI 已注册的模型目录，扩展不会写入任意本机路径。

## 开发验证

```powershell
$env:COMFYUI_ROOT="<ComfyUI目录>"
python tests\smoke_backend.py
```

或运行完整验证：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate.ps1 -ComfyUIRoot "<ComfyUI目录>" -Python "<python>" -Node "<node>"
```

## English

A ComfyUI extension that scans the current workflow for missing model metadata and downloads the models into ComfyUI's registered model folders. Downloads use `.part` files and HTTP Range requests for resumable transfers, with pause, resume, and cancel controls.

### Features

- Scans workflow model metadata such as `name`, `url`, `directory`, `hash`, and `hash_type`.
- Uses ComfyUI runtime `folder_paths.folder_names_and_paths`; no hardcoded local model paths.
- Supports Hugging Face, ModelScope, and Civitai credentials.
- Resolves official Civitai model pages and `civitai.red` pages carrying `modelVersionId`, then safely canonicalizes them to the official Civitai download API.
- Groups multiple versions resolved from one Civitai model page into one model module while keeping per-version selection and download controls.
- Manages each provider credential independently from separate rows in the `MMFetcher` category in ComfyUI settings.
- Reads Hugging Face Git LFS `lfs.oid` as the file SHA-256 instead of treating Xet hashes or CDN ETags as file hashes.
- Strictly blocks sources that cannot satisfy a workflow-provided SHA-256; otherwise compares repository, path, size, and trusted provider hashes and surfaces warning severity.
- Debounces and automatically validates typed credentials, then revalidates saved credentials on backend startup, every 30 minutes, and on each Web page load.
- Credential failures appear as a hoverable warning beside the top-bar Missing Models button, and clicking it opens the `MMFetcher` settings category.
- Stores API keys outside the extension source tree, under ComfyUI user data.
- Supports disabled, system, and saved custom HTTP, HTTPS, SOCKS5, or SOCKS5H proxy modes for credential checks, source resolution, metadata probes, and downloads.
- Provides preset and custom queue limits: total concurrency and per-provider concurrency accept integers from `1` to `32`; the shared bandwidth cap accepts `0` to `100000 MB/s`, where `0` means unlimited.
- Accepts HTTP/HTTPS download URLs only and removes credentials on cross-host CDN redirects.
- If a Hugging Face origin redirects to an expired Xet CDN signed URL and returns a transient `403`, the downloader refreshes the signed URL once from the stable origin; signed CDN URLs are never persisted.
- Automatically scans after a workflow is configured and opens the confirmation panel when missing models are found.
- Turns the top-bar Missing Models button red and shows a white alert icon while the current workflow has missing models.
- Never starts downloads silently; the user must confirm the selected models.
- Supports multi-line manual model links in a dedicated Manual Add tab, source resolution, grouped Civitai versions, and conservative destination-folder inference.
- Resolves models and providers progressively, updates each provider as soon as it completes, supports per-provider retry, and prevents stale results from replacing a newer scan.
- Uses a five-minute thread-safe cache for provider metadata, searches, and file lists, including in-flight request coalescing and a 75-second provider search deadline.
- Infers destination folders from provider metadata and full repository paths first, and only accepts unambiguous safetensors architecture signals; uncertain or conflicting results remain user-selectable instead of being guessed.
- Configurable total and per-provider concurrency from `1` to `32`, defaulting to `1`, with resumable transfers per task.
- Performs disk-space preflight checks when size is known and accounts for existing `.part` bytes.
- Exposes a dedicated verifying state with hash progress, speed, ETA, pause, resume, and cancel controls.
- Download queue state is stored under ComfyUI user data, so unfinished tasks can resume after backend restart.
- Duplicate queued, downloading, verifying, or paused tasks targeting the same model file are reused instead of being queued twice.
- Chinese-first UI with top action bar, command/menu, and settings entries.

### Install

For local development on Windows:

```powershell
cmd /c mklink /J "<ComfyUI directory>\custom_nodes\ComfyUI-Missing-Models-Fetcher" "<this repository>"
```

Restart ComfyUI after installation.

For stable distribution, publish through ComfyUI Manager / ComfyUI Registry. Before publishing, update `Repository` and `PublisherId` in `pyproject.toml`.

The Registry package id is `missing-models-fetcher`, while the ComfyUI display name is `ComfyUI Missing Models Fetcher`. Do not rename the `pyproject.toml` package id after publishing.

Note: the local junction workflow is for development. ComfyUI Manager's installed list normally fully recognizes Git-backed repositories with a remote, or Registry-installed packages with `.tracking` metadata.

### Development Check

```powershell
$env:COMFYUI_ROOT="<ComfyUI directory>"
python tests\smoke_backend.py
```

Or run the full validation script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate.ps1 -ComfyUIRoot "<ComfyUI directory>" -Python "<python>" -Node "<node>"
```
