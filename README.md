# ComfyUI Missing Models Fetcher

中文 | [English](#english)

一个用于发现并下载 ComfyUI 工作流缺失模型的扩展。

加载工作流后，扩展会识别缺失模型、查找可用来源并显示确认列表。只有在你确认模型来源、保存目录和文件名后，下载才会开始。

> 当前为预发布版本，尚未发布到 ComfyUI Registry。请使用下方的手动安装方式。

## 主要功能

- 自动扫描当前工作流中的缺失模型，也支持手动粘贴模型链接。
- 支持 Hugging Face、魔搭 ModelScope 和 Civitai。
- 下载前显示来源、模型类型、文件大小、Hash 和保存位置。
- 动态读取 ComfyUI 已注册的模型目录，不依赖固定安装路径。
- 支持下载队列、暂停、继续、取消、优先级和并发控制。
- 使用 `.part` 文件和 HTTP Range 断点续传，重启 ComfyUI 后可继续未完成任务。
- 支持 SHA-256 校验、磁盘空间检查和来源风险提示。
- 支持停用、系统代理及 HTTP、HTTPS、SOCKS5、SOCKS5H 自定义代理。
- API Key 和下载状态保存在 ComfyUI 用户数据目录，不写入插件源码。
- 中文优先界面，通过顶部“缺失模型”按钮、扩展菜单和设置页进入。

## 安装

在 ComfyUI 的 `custom_nodes` 目录中克隆仓库：

```bash
git clone https://github.com/Adomess/ComfyUI-Missing-Models-Fetcher.git
```

安装依赖：

```bash
python -m pip install -r ComfyUI-Missing-Models-Fetcher/requirements.txt
```

然后重启 ComfyUI。

如果使用便携版 ComfyUI，请把上面的 `python` 替换为该便携版自带的 Python。

## 使用

1. 打开或加载一个 ComfyUI 工作流。
2. 如果发现缺失模型，扩展会打开确认面板；也可以点击顶部“缺失模型”按钮手动扫描。
3. 检查每个模型的下载来源、模型目录和保存路径。
4. 选中需要的模型，然后点击“下载选中模型”。
5. 在队列区域查看进度，或暂停、继续、取消任务。

扩展不会自动开始下载，也不会在下载失败后静默切换到其他来源。

### 手动新增

在“手动新增”标签页中，可以粘贴一个或多个模型名称、模型页面或直接下载链接。扩展会尝试解析来源和模型信息；无法可靠判断模型目录时，需要手动选择保存目录。

### API Key

在 ComfyUI 设置页的 `MMFetcher` 分类中，可以分别配置：

- Hugging Face API Key
- ModelScope Access Token
- Civitai API Key

公开模型通常不要求凭据；私有、gated 或需要登录的模型可能需要相应账号权限。凭据验证成功不代表账号已经接受模型许可。

### 网络代理

在 `MMFetcher -> 网络代理` 中可以选择：

- 停用：插件直接连接下载站点。
- 系统代理：使用当前系统代理设置。
- 自定义代理：保存并切换 HTTP、HTTPS、SOCKS5 或 SOCKS5H 代理。

代理设置同时用于凭据验证、来源解析、文件信息读取和下载。

## 下载与队列

- “并行”控制全部站点合计的最大活动任务数。
- “每站”控制单个下载站点的最大活动任务数。
- 全局限速设为 `0` 时表示不限速。
- 暂停和取消会保留 `.part` 文件，以便之后继续下载。
- “删除已结束任务”只清理队列记录，不会删除已下载模型。
- 同一目标文件已经排队、下载或暂停时，不会重复创建任务。

## 安全与隐私

- 下载前必须由用户确认。
- 目标文件只能写入 ComfyUI 已注册的模型目录。
- API Key、代理密码和下载状态保存在 ComfyUI 用户数据目录。
- 返回前端和写入队列的 URL 会移除常见认证查询参数。
- 认证信息只发送给对应服务域名，跨主机 CDN 跳转会移除认证头和 Cookie。
- TLS 校验保持启用，并使用 `certifi` 提供可移植 CA 证书链。

## 常见问题

### 为什么找到了模型，但不能直接下载？

模型可能缺少下载地址、目录信息或必要权限。请检查来源提示，并在需要时配置 API Key 或手动选择模型目录。

### 为什么 Hugging Face 或 Civitai 返回 401/403？

通常表示凭据无效、账号未登录、模型是私有资源，或账号尚未接受 gated 模型许可。

### 下载暂停或 ComfyUI 重启后会丢失吗？

不会。未完成内容保存在 `.part` 文件中，队列状态会保存到 ComfyUI 用户数据目录。

### 下载完成后为什么模型没有立即出现在列表里？

扩展会主动刷新模型缓存。如果列表仍未更新，请重新打开对应节点的模型下拉框，必要时刷新 ComfyUI 页面。

## 开发与贡献

架构、测试、打包、Manager 生命周期验证和发布流程请参阅 [DEVELOPMENT.md](DEVELOPMENT.md)。

## English

ComfyUI Missing Models Fetcher detects models missing from the current workflow and downloads them into ComfyUI's registered model folders.

The extension always shows a confirmation list before downloading. You choose the source, destination folder, and file name; downloads never start silently.

> This is currently a pre-release build and is not yet published to the ComfyUI Registry. Use the manual installation steps below.

### Highlights

- Automatically scans workflows for missing models and accepts manual model links.
- Supports Hugging Face, ModelScope, and Civitai.
- Shows source, model type, size, hash, and destination before download.
- Uses ComfyUI's runtime folder registry instead of hardcoded installation paths.
- Provides queue controls, priorities, concurrency limits, and bandwidth limits.
- Supports resumable `.part` downloads with HTTP Range and backend restart recovery.
- Verifies SHA-256 when available and reports source or integrity risks.
- Supports disabled, system, HTTP, HTTPS, SOCKS5, and SOCKS5H proxy modes.
- Stores credentials and queue state under ComfyUI user data, outside the extension source.
- Uses a Chinese-first interface with action-bar, menu, and settings entries.

### Install

Clone the repository inside ComfyUI's `custom_nodes` directory:

```bash
git clone https://github.com/Adomess/ComfyUI-Missing-Models-Fetcher.git
```

Install the dependency:

```bash
python -m pip install -r ComfyUI-Missing-Models-Fetcher/requirements.txt
```

Restart ComfyUI. Portable installations should use their bundled Python executable.

### Usage

1. Load a ComfyUI workflow.
2. Open the confirmation panel when missing models are detected, or click the Missing Models action-bar button.
3. Review each source, model directory, and destination path.
4. Select the models to download and confirm.
5. Monitor, pause, resume, or cancel tasks from the queue.

Provider credentials and proxy settings are available under the `MMFetcher` category in ComfyUI settings.

### Safety

- Downloads require explicit user confirmation.
- Files can only be written inside model folders registered by ComfyUI.
- Credentials are stored under ComfyUI user data and are never returned to the frontend in full.
- Authentication headers and cookies are removed on cross-host CDN redirects.
- TLS verification remains enabled.

### Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture, validation, packaging, lifecycle testing, and release instructions.
