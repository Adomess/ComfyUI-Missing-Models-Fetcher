import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import {
	directoryInferenceStatus,
	isCurrentResolution,
	mergeProviderSources,
	parseCustomLimit,
	resolveCustomLimitInput,
	resolvedModelSize,
	runTasksWithConcurrency,
	setNumericSelectValue,
} from "./mmf_state.mjs";

// Public IDs remain stable even though the settings category is shortened to MMFetcher.
const API_PREFIX = "/missing-models-fetcher";
const EXTENSION_NAME = "ComfyUI.MissingModelsFetcher";
const OPEN_COMMAND_ID = "MissingModelsFetcher.OpenDialog";

let dialog = null;
let pollTimer = null;
let workflowPromptTimer = null;
let credentialHealthTimer = null;
let lastPromptedWorkflowSignature = "";
const credentialValidationCache = new Map();
const missingModelsActionButton = {
	icon: "pi pi-download",
	label: "缺失模型",
	tooltip: "扫描并下载工作流中的缺失模型",
	class: "mmf-missing-models-action",
	onClick: openDialog,
};
const credentialWarningActionButton = {
	icon: "pi pi-exclamation-triangle",
	label: "",
	tooltip: "",
	class: "mmf-credential-warning-action mmf-action-hidden",
	onClick: openCredentialSettings,
};

function el(tag, attrs = {}, children = []) {
	const node = document.createElement(tag);
	for (const [key, value] of Object.entries(attrs)) {
		if (key === "class") node.className = value;
		else if (key === "text") node.textContent = value;
		else if (key === "html") node.innerHTML = value;
		else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
		else if (value !== undefined && value !== null) node.setAttribute(key, value);
	}
	for (const child of Array.isArray(children) ? children : [children]) {
		if (child === undefined || child === null) continue;
		node.append(child instanceof Node ? child : document.createTextNode(String(child)));
	}
	return node;
}

function createSelectMenu(placeholder = "请选择") {
	let value = "";
	const selectedText = el("span", { class: "mmf-select-value", text: placeholder });
	const menu = el("div", {
		class: "mmf-select-menu",
		role: "listbox",
		hidden: true,
	});
	const trigger = el("button", {
		type: "button",
		class: "mmf-select-trigger",
		"aria-haspopup": "listbox",
		"aria-expanded": "false",
	}, [
		selectedText,
		el("i", { class: "pi pi-chevron-down", "aria-hidden": "true" }),
	]);
	const root = el("div", { class: "mmf-select" }, [trigger, menu]);
	const close = () => {
		menu.hidden = true;
		trigger.setAttribute("aria-expanded", "false");
		root.classList.remove("open");
	};
	trigger.addEventListener("click", () => {
		const opening = menu.hidden;
		document.querySelectorAll(".mmf-select.open").forEach((node) => {
			if (node !== root) {
				node.classList.remove("open");
				const otherMenu = node.querySelector(".mmf-select-menu");
				const otherTrigger = node.querySelector(".mmf-select-trigger");
				if (otherMenu) otherMenu.hidden = true;
				if (otherTrigger) otherTrigger.setAttribute("aria-expanded", "false");
			}
		});
		menu.hidden = !opening;
		trigger.setAttribute("aria-expanded", opening ? "true" : "false");
		root.classList.toggle("open", opening);
	});
	return {
		root,
		get value() {
			return value;
		},
		setOptions(options, preferredValue = "") {
			menu.replaceChildren();
			const normalized = Array.isArray(options) ? options : [];
			const selected = normalized.find((option) => option.value === preferredValue)
				|| normalized[0]
				|| null;
			value = selected?.value || "";
			selectedText.textContent = selected?.label || placeholder;
			trigger.title = selected?.label || placeholder;
			for (const option of normalized) {
				const optionButton = el("button", {
					type: "button",
					class: `mmf-select-option${option.value === value ? " selected" : ""}`,
					role: "option",
					"aria-selected": option.value === value ? "true" : "false",
					title: option.label,
					onclick: () => {
						value = option.value;
						selectedText.textContent = option.label;
						trigger.title = option.label;
						for (const button of menu.querySelectorAll(".mmf-select-option")) {
							const selectedOption = button === optionButton;
							button.classList.toggle("selected", selectedOption);
							button.setAttribute("aria-selected", selectedOption ? "true" : "false");
						}
						close();
					},
				}, [
					el("span", { text: option.label }),
					option.writable === false
						? el("span", { class: "mmf-select-option-note", text: "不可写" })
						: null,
				]);
				menu.append(optionButton);
			}
		},
	};
}

function selectMMFetcherSettingsCategory() {
	if (document.querySelector('div[id^="MMFetcher.API凭据"]')) return true;
	const categoryButton = [...document.querySelectorAll("button")].find(
		(button) => button.textContent?.trim() === "MMFetcher"
	);
	if (!categoryButton) return false;
	categoryButton.click();
	return Boolean(document.querySelector('div[id^="MMFetcher.API凭据"]'));
}

function openCredentialSettings() {
	if (selectMMFetcherSettingsCategory()) return;
	const settingsButton = [...document.querySelectorAll("button")].find((button) => {
		const label = button.getAttribute("aria-label") || button.title || "";
		return label.startsWith("设置 (");
	});
	settingsButton?.click();

	let attempts = 0;
	const selectTimer = setInterval(() => {
		attempts += 1;
		if (selectMMFetcherSettingsCategory() || attempts >= 20) clearInterval(selectTimer);
	}, 50);
}

async function jsonFetch(path, options = {}) {
	const requestOptions = {
		...options,
		headers: {
			...(options.headers || {}),
			...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
		},
	};
	const method = String(requestOptions.method || "GET").toUpperCase();
	let response;
	try {
		response = await api.fetchApi(path, requestOptions);
	} catch (error) {
		if (error?.name === "AbortError") throw error;
		if (method === "GET" && options.retry !== false) {
			await new Promise((resolve) => setTimeout(resolve, 500));
			try {
				response = await api.fetchApi(path, requestOptions);
			} catch (retryError) {
				if (retryError?.name === "AbortError") throw retryError;
				throw new Error("无法连接 ComfyUI 后端，请确认服务正在运行后重试。");
			}
		} else {
			throw new Error("无法连接 ComfyUI 后端，请确认服务正在运行后重试。");
		}
	}
	let data = {};
	try {
		data = await response.json();
	} catch (_) {
		data = {};
	}
	if (!response.ok || data.ok === false) {
		throw new Error(data.error || `请求失败: HTTP ${response.status}`);
	}
	return data;
}

function parseManualInputText(text) {
	const lines = String(text || "")
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter(Boolean);
	const items = [];
	let pendingName = "";
	const urlPattern = /https?:\/\/[^\s]+/gi;
	for (const line of lines) {
		const urls = line.match(urlPattern) || [];
		if (!urls.length) {
			pendingName = line;
			continue;
		}
		const label = line.replace(urlPattern, " ").replace(/\s+/g, " ").trim();
		urls.forEach((url, index) => {
			items.push({
				name: index === 0 ? (label || pendingName) : "",
				url: url.replace(/[),，。；;]+$/, ""),
			});
		});
		pendingName = "";
	}
	if (pendingName) items.push({ name: pendingName, url: "" });
	return items;
}

function currentWorkflow() {
	const graph = app.graph || app.canvas?.graph;
	if (!graph || typeof graph.serialize !== "function") {
		throw new Error("无法读取当前工作流");
	}
	const workflow = graph.serialize();
	workflow.__missing_models_fetcher_runtime = {
		nodes: (graph._nodes || []).map((node) => ({
			id: node.id,
			type: node.type,
			title: node.title,
			properties: node.properties || {},
			widgets: (node.widgets || []).map((widget) => ({
				name: widget.name,
				type: widget.type,
				value: cloneWidgetValue(widget.value),
			})),
		})),
	};
	return workflow;
}

function cloneWidgetValue(value) {
	if (value === null || value === undefined) return value;
	if (["string", "number", "boolean"].includes(typeof value)) return value;
	if (Array.isArray(value)) return value.map((item) => cloneWidgetValue(item));
	if (typeof value === "object") {
		const output = {};
		for (const [key, item] of Object.entries(value)) {
			if (["string", "number", "boolean"].includes(typeof item) || item === null) {
				output[key] = item;
			}
		}
		return output;
	}
	return String(value);
}

function formatBytes(value) {
	if (!value && value !== 0) return "-";
	const units = ["B", "KB", "MB", "GB", "TB"];
	let size = Number(value);
	let index = 0;
	while (size >= 1024 && index < units.length - 1) {
		size /= 1024;
		index += 1;
	}
	return `${size.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
}

function formatEta(seconds) {
	const value = Math.max(0, Math.round(Number(seconds) || 0));
	if (value < 60) return `${value} 秒`;
	if (value < 3600) return `${Math.ceil(value / 60)} 分钟`;
	return `${Math.floor(value / 3600)} 小时 ${Math.ceil((value % 3600) / 60)} 分钟`;
}

function formatStatus(status) {
	return {
		queued: "排队中",
		downloading: "下载中",
		verifying: "校验中",
		paused: "已暂停",
		completed: "已完成",
		canceled: "已取消",
		failed: "失败",
	}[status] || status || "-";
}

const SOURCE_PROVIDERS = [
	{ id: "modelscope", label: "魔搭" },
	{ id: "hf", label: "Hugging Face" },
	{ id: "civitai", label: "Civitai" },
	{ id: "manual", label: "手动输入" },
];

function sourceTooltip(source, fallbackLabel, diagnostics = null) {
	if (!source) return `${fallbackLabel}：未解析到可用链接`;
	const verificationLabels = {
		original: "工作流原始来源",
		hash_verified: "SHA-256 已验证",
		hash_conflict: "SHA-256 不一致",
		hash_mismatch: "不符合工作流 SHA-256",
		hash_unavailable: "缺少工作流要求的 SHA-256",
		metadata_matched: "仓库、路径和大小一致",
		metadata_conflict: "来源 metadata 存在冲突",
		filename_match: "仅文件名匹配",
		same_repo_path: "同仓库路径匹配，未校验内容",
	};
	return [
		fallbackLabel,
		source.repository ? `仓库: ${source.repository}` : "",
		source.revision ? `Revision: ${source.revision}` : "",
		source.file_path ? `文件: ${source.file_path}` : "",
		source.size ? `大小: ${formatBytes(source.size)}` : "",
		`验证: ${verificationLabels[source.verification] || source.verification || "未知"}`,
		source.warning || "",
		source.blocked_reason || "",
		source.confidence_level ? `可信度: ${source.confidence_level}（${source.confidence_score ?? 0}/100）` : "",
		...(Array.isArray(source.confidence_reasons) ? source.confidence_reasons.map((reason) => `依据: ${reason}`) : []),
		diagnostics?.elapsed_ms != null ? `解析耗时: ${diagnostics.elapsed_ms} ms` : "",
		diagnostics?.match_method ? `匹配方式: ${diagnostics.match_method}` : "",
		diagnostics ? `请求: 网络 ${diagnostics.network_requests || 0}，缓存命中 ${diagnostics.cache_hits || 0}，合并等待 ${diagnostics.coalesced_requests || 0}` : "",
		diagnostics?.repositories_checked ? `检查仓库: ${diagnostics.repositories_checked}` : "",
	].filter(Boolean).join("\n");
}

function expectedWorkflowSha256(model) {
	const value = String(model?.hash || "").trim().toLowerCase();
	const type = String(model?.hash_type || "").toLowerCase().replaceAll("-", "");
	return /^[0-9a-f]{64}$/.test(value) && (!type || type === "sha256") ? value : "";
}

function onlyMissingModels(models) {
	const missing = (Array.isArray(models) ? models : [])
		.filter((model) => model && !model.installed)
		.map((model) => {
			if (model.directory_valid === false && model.directory === "combo") {
				return {
					...model,
					directory: "",
					needs_directory: true,
				};
			}
			return model;
		});
	const byName = new Map();
	for (const model of missing) {
		const key = String(model.name || "").replaceAll("\\", "/").toLowerCase();
		if (!byName.has(key)) byName.set(key, []);
		byName.get(key).push(model);
	}
	const reconciled = [];
	for (const sameName of byName.values()) {
		const valid = sameName.filter(
			(model) => model.directory_valid !== false && !model.needs_directory && model.directory
		);
		const weak = sameName.filter((model) => !valid.includes(model));
		if (!valid.length) {
			reconciled.push(...weak);
			continue;
		}
		if (valid.length === 1) {
			const primary = valid[0];
			for (const candidate of weak) {
				if (!primary.url && candidate.url) primary.url = candidate.url;
				const knownUrls = new Set((primary.sources || []).map((source) => source?.url));
				for (const source of candidate.sources || []) {
					if (source?.url && !knownUrls.has(source.url)) {
						primary.sources = [...(primary.sources || []), source];
						knownUrls.add(source.url);
					}
				}
				if (!primary.hash && candidate.hash) {
					primary.hash = candidate.hash;
					primary.hash_type = candidate.hash_type || "";
				}
			}
		}
		reconciled.push(...valid);
	}
	return reconciled;
}

function updateMissingModelsWarning(models) {
	const missingModels = onlyMissingModels(models);
	const hasMissingModels = missingModels.length > 0;
	const tooltip = hasMissingModels
		? `当前工作流缺失 ${missingModels.length} 个模型，点击查看`
		: "";
	missingModelsActionButton.class = `mmf-missing-models-action ${
		hasMissingModels ? "is-missing" : ""
	}`.trim();
	missingModelsActionButton.tooltip = tooltip || "扫描并下载工作流中的缺失模型";

	const actionButton = document.querySelector("button.mmf-missing-models-action");
	if (actionButton) {
		actionButton.classList.toggle("is-missing", hasMissingModels);
		actionButton.setAttribute("aria-label", tooltip || "扫描并下载工作流中的缺失模型");
		actionButton.title = tooltip;
	}
}

function credentialValidationErrorState(error) {
	const message = String(error?.message || error || "");
	return /(HTTP\s*(401|403)|无效|已过期|权限不足)/i.test(message) ? "error" : "warning";
}

function updateCredentialWarning(validation) {
	const providers = Object.values(validation?.providers || {});
	const problems = providers.filter(
		(item) => item?.configured && ["invalid", "warning"].includes(item.status)
	);
	const tooltip = problems.length
		? ["API 凭据验证异常", ...problems.map((item) => `${item.name}：${item.message}`)].join("\n")
		: "";
	credentialWarningActionButton.class = `mmf-credential-warning-action ${
		problems.length ? "" : "mmf-action-hidden"
	}`.trim();
	credentialWarningActionButton.tooltip = tooltip;
	const button = document.querySelector("button.mmf-credential-warning-action");
	if (button) {
		button.classList.toggle("mmf-action-hidden", !problems.length);
		button.setAttribute("aria-label", tooltip || "API 凭据验证正常");
		button.title = tooltip;
	}
}

async function refreshCredentialHealth(forceValidation = false) {
	try {
		const data = await jsonFetch(`${API_PREFIX}/config/validation`, {
			method: forceValidation ? "POST" : "GET",
			...(forceValidation ? { body: "{}" } : {}),
		});
		updateCredentialWarning(data.validation);
		return data.validation;
	} catch (error) {
		updateCredentialWarning({
			providers: {
				monitor: {
					configured: true,
					status: "warning",
					name: "凭据检查",
					message: error.message,
				},
			},
		});
		return null;
	}
}

async function refreshMissingModelsActionState() {
	try {
		const workflow = currentWorkflow();
		const data = await jsonFetch(`${API_PREFIX}/scan`, {
			method: "POST",
			body: JSON.stringify({ workflow }),
		});
		updateMissingModelsWarning(data.models);
	} catch (error) {
		console.debug("[Missing Models Fetcher] Missing model action state unavailable", error);
	}
}

function createProviderApiKeyControl(provider) {
	const providerConfig = {
		hf: {
			name: "Hugging Face",
			credential: "访问令牌",
			configKey: "hf_api_key",
			hasKeyField: "has_hf_api_key",
			maskedKeyField: "hf_api_key_masked",
		},
		civitai: {
			name: "Civitai",
			credential: "API 密钥",
			configKey: "civitai_api_key",
			hasKeyField: "has_civitai_api_key",
			maskedKeyField: "civitai_api_key_masked",
		},
		modelscope: {
			name: "魔搭 ModelScope",
			credential: "访问令牌",
			configKey: "modelscope_api_token",
			hasKeyField: "has_modelscope_api_token",
			maskedKeyField: "modelscope_api_token_masked",
		},
	}[provider];
	const providerName = providerConfig.name;
	const credentialName = providerConfig.credential;
	const { configKey, hasKeyField, maskedKeyField } = providerConfig;
	let hasSavedCredential = false;
	let savedMaskedCredential = "";
	let validationTimer = null;
	let validationGeneration = 0;
	let validationState = "warning";
	let validationText = "正在读取配置…";
	const status = el("i", {
		class: "pi pi-exclamation-triangle mmf-credential-status is-warning",
		title: "正在读取配置…",
		role: "img",
		"aria-label": "正在读取配置",
		"aria-live": "polite",
	});
	const input = el("input", {
		type: "password",
		autocomplete: "off",
		spellcheck: "false",
		placeholder: `输入新的${credentialName}`,
		"aria-label": `${providerName} ${credentialName}`,
	});

	const setStatus = (state, text) => {
		validationState = state;
		validationText = text;
		status.className = `pi mmf-credential-status is-${state} ${
			state === "success" ? "pi-check-circle" :
			state === "error" ? "pi-times-circle" :
			"pi-exclamation-triangle"
		}`;
		status.title = text;
		status.setAttribute("aria-label", text);
	};

	const refreshConfig = async () => {
		try {
			const data = await jsonFetch(`${API_PREFIX}/config`);
			const config = data.config || {};
			hasSavedCredential = Boolean(config[hasKeyField]);
			savedMaskedCredential = String(config[maskedKeyField] || "");
			input.placeholder = hasSavedCredential
				? `已配置：${savedMaskedCredential}`
				: `输入新的${credentialName}`;
			const cached = credentialValidationCache.get(provider);
			if (hasSavedCredential && cached?.maskedCredential === savedMaskedCredential) {
				setStatus(cached.state, cached.text);
			} else {
				if (!hasSavedCredential) credentialValidationCache.delete(provider);
				if (hasSavedCredential) {
					setStatus("warning", "正在验证已保存凭据…");
					void validateSavedKey(savedMaskedCredential);
				} else {
					setStatus("warning", "未填写");
				}
			}
		} catch (error) {
			setStatus("warning", `读取配置失败：${error.message}`);
		}
	};

	const validateKey = async (apiKey) => {
		const requestGeneration = ++validationGeneration;
		setStatus("warning", "正在自动验证…");
		try {
			const data = await jsonFetch(`${API_PREFIX}/config/test`, {
				method: "POST",
				body: JSON.stringify({ provider, api_key: apiKey }),
			});
			if (requestGeneration !== validationGeneration || input.value.trim() !== apiKey) return;
			const account = data.result?.account ? `，账号：${data.result.account}` : "";
			setStatus("success", `验证通过${account}。受限模型仍需对应访问权限。`);
		} catch (error) {
			if (requestGeneration !== validationGeneration || input.value.trim() !== apiKey) return;
			setStatus(credentialValidationErrorState(error), `验证未通过：${error.message}`);
		}
	};

	const validateSavedKey = async (maskedCredential) => {
		const requestGeneration = ++validationGeneration;
		try {
			const data = await jsonFetch(`${API_PREFIX}/config/validation/${provider}`, {
				method: "POST",
				body: "{}",
			});
			updateCredentialWarning(data.validation);
			if (data.result?.status !== "valid") {
				throw new Error(data.result?.message || "验证未通过");
			}
			if (
				requestGeneration !== validationGeneration ||
				!hasSavedCredential ||
				savedMaskedCredential !== maskedCredential
			) return;
			const account = data.result?.account ? `，账号：${data.result.account}` : "";
			const text = `验证通过${account}。受限模型仍需对应访问权限。`;
			setStatus("success", text);
			credentialValidationCache.set(provider, {
				maskedCredential,
				state: "success",
				text,
			});
		} catch (error) {
			if (
				requestGeneration !== validationGeneration ||
				!hasSavedCredential ||
				savedMaskedCredential !== maskedCredential
			) return;
			const state = credentialValidationErrorState(error);
			const text = `验证未通过：${error.message}`;
			setStatus(state, text);
			if (state === "error") {
				credentialValidationCache.set(provider, { maskedCredential, state, text });
			}
		}
	};

	input.addEventListener("input", () => {
		if (validationTimer) clearTimeout(validationTimer);
		validationGeneration += 1;
		const apiKey = input.value.trim();
		if (!apiKey) {
			setStatus("warning", hasSavedCredential ? "已配置，尚未验证" : "未填写");
			return;
		}
		setStatus("warning", "等待自动验证…");
		validationTimer = setTimeout(() => {
			validationTimer = null;
			void validateKey(apiKey);
		}, 800);
	});

	const saveKey = async () => {
		const apiKey = input.value.trim();
		if (!apiKey) {
			setStatus("warning", `请先输入新的${credentialName}`);
			return;
		}
		try {
			const data = await jsonFetch(`${API_PREFIX}/config`, {
				method: "POST",
				body: JSON.stringify({ [configKey]: apiKey }),
			});
			const config = data.config || {};
			hasSavedCredential = true;
			savedMaskedCredential = String(config[maskedKeyField] || "");
			input.value = "";
			input.placeholder = `已配置：${savedMaskedCredential}`;
			const savedState = validationState === "success" ? "success" : validationState;
			const savedText = validationState === "success" ? "已保存并通过验证" : validationText;
			setStatus(savedState, savedText);
			credentialValidationCache.set(provider, {
				maskedCredential: savedMaskedCredential,
				state: savedState,
				text: savedText,
			});
			void refreshCredentialHealth(true);
		} catch (error) {
			setStatus("warning", `保存失败：${error.message}`);
		}
	};

	const clearKey = async () => {
		const confirmed = app.extensionManager?.dialog?.confirm
			? await app.extensionManager.dialog.confirm({
				title: `清除 ${providerName} 凭据`,
				message: `确认清除已保存的 ${providerName} 凭据？`,
			})
			: window.confirm(`确认清除已保存的 ${providerName} 凭据？`);
		if (!confirmed) return;
		try {
			await jsonFetch(`${API_PREFIX}/config/clear`, {
				method: "POST",
				body: JSON.stringify({ provider }),
			});
			hasSavedCredential = false;
			savedMaskedCredential = "";
			credentialValidationCache.delete(provider);
			input.value = "";
			input.placeholder = `输入新的${credentialName}`;
			setStatus("warning", "未填写");
			void refreshCredentialHealth(true);
		} catch (error) {
			setStatus("warning", `清除失败：${error.message}`);
		}
	};

	const root = el("div", { class: "mmf-settings-provider" }, [
		el("div", { class: "mmf-settings-input-row" }, [status, input]),
		el("div", { class: "mmf-settings-actions" }, [
			el("button", { class: "mmf-primary", text: "保存", onclick: saveKey }),
			el("button", { text: "清除", onclick: clearKey }),
		]),
	]);
	void refreshConfig();
	return root;
}

function createProxySettingsControl() {
	let profiles = [];
	let activeProxyId = "";
	let editingId = "";
	const modeName = `mmf-proxy-mode-${Math.random().toString(36).slice(2)}`;
	const modes = Object.fromEntries([[
		"off", "停用代理"], ["system", "使用系统代理设置"], ["custom", "使用自定义代理"],
	].map(([value, label]) => {
		const input = el("input", { type: "radio", name: modeName, value });
		return [value, { input, row: el("label", { class: "mmf-proxy-mode" }, [input, el("span", { text: label })]) }];
	}));
	const scheme = el("select", { class: "mmf-proxy-scheme", "aria-label": "代理协议" }, [
		el("option", { value: "http", text: "HTTP" }),
		el("option", { value: "https", text: "HTTPS" }),
		el("option", { value: "socks5", text: "SOCKS5" }),
		el("option", { value: "socks5h", text: "SOCKS5H（代理 DNS）" }),
	]);
	const profileName = el("input", { type: "text", placeholder: "配置名称（可选）" });
	const host = el("input", { type: "text", class: "mmf-proxy-host", spellcheck: "false", placeholder: "主机名或 IP", "aria-label": "代理主机" });
	const port = el("input", { type: "number", class: "mmf-proxy-port", min: "1", max: "65535", placeholder: "端口", "aria-label": "代理端口" });
	const username = el("input", { type: "text", class: "mmf-proxy-username", autocomplete: "off", placeholder: "用户名（可选）" });
	const password = el("input", { type: "password", class: "mmf-proxy-password", autocomplete: "new-password", placeholder: "密码（可选）" });
	const status = el("span", { class: "mmf-proxy-status", text: "正在读取配置…" });
	const profileList = el("div", { class: "mmf-proxy-list" });
	const addProxy = el("button", { class: "mmf-proxy-add", text: "添加代理" });
	const editor = el("div", { class: "mmf-proxy-editor", hidden: true }, [
		profileName,
		el("div", { class: "mmf-proxy-address" }, [scheme, host, port]),
		username,
		password,
		el("div", { class: "mmf-proxy-editor-actions" }),
	]);
	const customFields = el("div", { class: "mmf-proxy-custom" }, [
		profileList,
		editor,
	]);
	const selectedMode = () => Object.entries(modes).find(([, value]) => value.input.checked)?.[0] || "off";
	const updateVisibility = () => customFields.hidden = selectedMode() !== "custom";
	const saveConfig = async (extra = {}) => jsonFetch(`${API_PREFIX}/config`, {
		method: "POST",
		body: JSON.stringify({ proxy_mode: selectedMode(), proxy_profiles: profiles, active_proxy_id: activeProxyId, ...extra }),
	});
	Object.values(modes).forEach(({ input }) => input.addEventListener("change", () => {
		updateVisibility();
		status.classList.remove("mmf-error-text");
		status.textContent = "正在应用代理模式…";
		void saveConfig().then(() => {
			status.textContent = selectedMode() === "custom"
				? (activeProxyId ? "自定义代理已启用" : "请添加并选择一个自定义代理")
				: selectedMode() === "system" ? "系统代理已启用" : "代理已停用";
		}).catch((error) => {
			status.textContent = `应用失败：${error.message}`;
			status.classList.add("mmf-error-text");
		});
	}));
	const renderProfiles = () => {
		profileList.replaceChildren();
		if (!profiles.length) profileList.append(el("div", { class: "mmf-proxy-empty", text: "尚未添加自定义代理" }));
		for (const profile of profiles) {
			const health = el("span", { class: "mmf-proxy-health checking", text: "检测中…" });
			const radio = el("input", { type: "radio", name: `${modeName}-profile`, checked: profile.id === activeProxyId ? "" : null });
			radio.addEventListener("change", async () => {
				activeProxyId = profile.id;
				modes.custom.input.checked = true;
				await saveConfig();
				status.textContent = `已选择 ${profile.name || `${profile.scheme.toUpperCase()} ${profile.host}:${profile.port}`}`;
			});
			const edit = el("button", { class: "mmf-proxy-row-action", text: "编辑" });
			edit.onclick = () => openEditor(profile);
			const remove = el("button", { class: "mmf-proxy-row-action", text: "删除" });
			remove.onclick = async () => {
				profiles = profiles.filter((item) => item.id !== profile.id);
				if (activeProxyId === profile.id) activeProxyId = profiles[0]?.id || "";
				await saveConfig();
				renderProfiles();
			};
			profileList.append(el("div", { class: "mmf-proxy-row" }, [
				radio,
				el("div", { class: "mmf-proxy-row-copy" }, [
					el("strong", { text: profile.name || `${profile.scheme.toUpperCase()} ${profile.host}:${profile.port}` }),
					el("span", { text: `${profile.scheme.toUpperCase()}  ${profile.host}:${profile.port}${profile.username ? `  用户：${profile.username}` : ""}` }),
					health,
				]),
				edit, remove,
			]));
			void jsonFetch(`${API_PREFIX}/config/proxy/test`, {
				method: "POST", body: JSON.stringify({ profile_id: profile.id }),
			}).then((data) => {
				health.className = "mmf-proxy-health available";
				health.textContent = `可用（${data.result?.latency_ms ?? "-"} ms）`;
			}).catch((error) => {
				health.className = "mmf-proxy-health unavailable";
				health.textContent = `不可用：${error.message}`;
			});
		}
	};
	const closeEditor = () => { editor.hidden = true; editingId = ""; password.value = ""; };
	const openEditor = (profile = null) => {
		editingId = profile?.id || "";
		profileName.value = profile?.name || "";
		scheme.value = profile?.scheme || "http";
		host.value = profile?.host || "";
		port.value = profile?.port || "";
		username.value = profile?.username || "";
		password.value = "";
		password.placeholder = profile?.has_password ? "已保存密码；留空保持不变" : "密码（可选）";
		editor.hidden = false;
	};
	addProxy.onclick = () => {
		if (!modes.custom.input.checked) {
			modes.custom.input.checked = true;
			modes.custom.input.dispatchEvent(new Event("change"));
		} else {
			updateVisibility();
		}
		openEditor();
	};
	const editorActions = editor.querySelector(".mmf-proxy-editor-actions");
	editorActions.append(
		el("button", { text: "取消", onclick: closeEditor }),
		el("button", { class: "mmf-primary", text: "保存代理", onclick: async () => {
			const cleanHost = host.value.trim();
			const cleanPort = Number(port.value);
			if (!cleanHost || !Number.isInteger(cleanPort) || cleanPort < 1 || cleanPort > 65535) {
				status.textContent = "请填写有效的代理主机和端口";
				status.classList.add("mmf-error-text");
				return;
			}
			const old = profiles.find((item) => item.id === editingId);
			const profile = {
				id: editingId || undefined, name: profileName.value.trim(), scheme: scheme.value,
				host: cleanHost, port: cleanPort, username: username.value.trim(),
				password: password.value, keep_password: Boolean(old?.has_password && !password.value),
			};
			if (old) profiles = profiles.map((item) => item.id === editingId ? profile : item);
			else profiles = [...profiles, profile];
			const data = await saveConfig();
			profiles = data.config?.proxy_profiles || profiles;
			if (!activeProxyId && profiles.length) activeProxyId = profiles[0].id;
			await saveConfig();
			closeEditor(); renderProfiles();
			status.textContent = "代理配置已保存";
		} }),
	);

	const refresh = async () => {
		try {
			const data = await jsonFetch(`${API_PREFIX}/config`);
			const config = data.config || {};
			const mode = ["off", "system", "custom"].includes(config.proxy_mode) ? config.proxy_mode : "off";
			modes[mode].input.checked = true;
			profiles = config.proxy_profiles || [];
			activeProxyId = config.active_proxy_id || profiles[0]?.id || "";
			status.textContent = mode === "custom" ? "正在使用自定义代理" : mode === "system" ? "正在使用系统代理设置" : "代理已停用";
			updateVisibility();
			renderProfiles();
		} catch (error) {
			status.textContent = `读取失败：${error.message}`;
			status.classList.add("mmf-error-text");
		}
	};

	const root = el("div", { class: "mmf-settings-provider mmf-proxy-settings" }, [
		el("div", { class: "mmf-proxy-modes" }, [...Object.values(modes).map(({ row }) => row), addProxy]),
		customFields,
		status,
	]);
	const attachStatusToHeading = () => {
		if (!root.isConnected) return false;
		const heading = root.closest(".setting-group")?.querySelector(":scope > h3");
		if (!heading) return false;
		heading.classList.add("mmf-proxy-heading");
		status.classList.add("mmf-proxy-heading-status");
		heading.append(status);
		return true;
	};
	queueMicrotask(() => {
		if (!attachStatusToHeading()) requestAnimationFrame(attachStatusToHeading);
	});
	void refresh();
	return root;
}

class MissingModelsDialog {
	constructor() {
		this.models = [];
		this.manualModels = [];
		this.folders = [];
		this.folderMap = new Map();
		this.queue = { tasks: [] };
		this.root = null;
		this.modelList = null;
		this.manualModelList = null;
		this.manualInput = null;
		this.manualStatus = null;
		this.parseManualButton = null;
		this.missingTabHint = null;
		this.scanButton = null;
		this.downloadMissingButton = null;
		this.downloadManualButton = null;
		this.refreshButton = null;
		this.clearFinishedButton = null;
		this.queueList = null;
		this.concurrencySelect = null;
		this.providerConcurrencySelect = null;
		this.bandwidthSelect = null;
		this.status = null;
		this.activeTab = "missing";
		this.tabButtons = {};
		this.tabPanels = {};
		this.lastWorkflowSignature = "";
		this.scanInProgress = false;
		this.sourceResolveInProgress = false;
		this.sourceResolveGeneration = 0;
		this.manualSourceResolveGeneration = 0;
		this.manualSourceResolveInProgress = false;
		this.sourceResolveControllers = new Set();
		this.manualParseInProgress = false;
	}

	show(options = {}) {
		if (!this.root) {
			this.root = this.create();
			document.body.append(this.root);
		}
		this.root.style.display = "flex";
		this.refreshFolders();
		this.refreshQueue();
		this.refreshDownloadConfig();
		this.startPolling();
		if (!options.skipAutoScan) this.autoScanCurrentWorkflow();
	}

	showScanResult(models, signature) {
		this.models = onlyMissingModels(models);
		this.lastWorkflowSignature = signature;
		this.show({ skipAutoScan: true });
		this.renderModels();
		this.setActiveTab("missing");
		this.setStatus(
			this.models.length
				? `发现 ${this.models.length} 个缺失模型候选，请确认后再下载`
				: "当前工作流没有缺失模型"
		);
		void this.resolveModelSources();
	}

	hide() {
		if (this.root) this.root.style.display = "none";
		this.sourceResolveGeneration += 1;
		this.manualSourceResolveGeneration += 1;
		this.sourceResolveInProgress = false;
		this.manualSourceResolveInProgress = false;
		this.cancelSourceResolveRequests();
		this.stopPolling();
	}

	create() {
		this.status = el("div", { class: "mmf-status", text: "就绪" });
		this.modelList = el("div", { class: "mmf-list" });
		this.manualModelList = el("div", { class: "mmf-list mmf-manual-models" });
		this.queueList = el("div", { class: "mmf-list mmf-queue" });
		this.concurrencySelect = el("select", {
			class: "mmf-concurrency-select",
			title: "总并行数：所有下载站点合计最多同时运行的任务数",
			"aria-label": "总并行下载数",
			onchange: () => this.handleCustomDownloadLimit("total"),
		}, [...[1, 2, 4, 8, 16].map((value) => el("option", {
			value: String(value),
			text: String(value),
		})), el("option", { value: "custom", text: "自定义…" })]);
		this.providerConcurrencySelect = el("select", {
			class: "mmf-concurrency-select",
			title: "每站并行数：每个下载网站最多同时运行的任务数，可避免同一网站请求过多",
			"aria-label": "每站并行下载数",
			onchange: () => this.handleCustomDownloadLimit("provider"),
		}, [...[1, 2, 4, 8, 16].map((value) => el("option", { value: String(value), text: String(value) })), el("option", { value: "custom", text: "自定义…" })]);
		this.bandwidthSelect = el("select", {
			class: "mmf-concurrency-select",
			title: "全局限速：所有下载任务共享的速度上限；选择“不限速”表示不限制",
			"aria-label": "全部下载任务的全局速度上限",
			onchange: () => this.handleCustomDownloadLimit("bandwidth"),
		}, [
			[0, "不限速"], [10, "10 MB/s"], [25, "25 MB/s"], [50, "50 MB/s"],
			[100, "100 MB/s"], [250, "250 MB/s"], [500, "500 MB/s"], [1000, "1000 MB/s"],
		].map(([value, text]) => el("option", { value: String(value), text })));
		this.bandwidthSelect.append(el("option", { value: "custom", text: "自定义…" }));
		this.directoryOptions = el("datalist", { id: "mmf-directory-options" });
		this.manualInput = el("textarea", {
			class: "mmf-manual-input",
			placeholder: "每行输入一个模型链接；也可在链接前填写模型名称。\n仅填写名称时，解析后可继续补充链接。",
			rows: "4",
		});
		this.manualStatus = el("div", {
			class: "mmf-status mmf-manual-status",
			text: "",
			hidden: true,
		});

		const closeButton = el("button", { class: "mmf-icon-button", text: "×", onclick: () => this.hide(), title: "关闭" });
		this.missingTabHint = el("span", {
			class: "mmf-tab-hint",
			text: "扫描当前工作流，确认模型类型和保存目录后开始断点续传下载。",
		});
		this.scanButton = el("button", { class: "mmf-primary", text: "扫描当前工作流", onclick: () => this.scan() });
		this.downloadMissingButton = el("button", {
			class: "mmf-primary",
			text: "下载选中模型",
			onclick: () => this.downloadSelected("workflow"),
		});
		this.downloadManualButton = el("button", {
			class: "mmf-primary",
			text: "下载选中模型",
			hidden: true,
			onclick: () => this.downloadSelected("manual"),
		});
		this.refreshButton = el("button", {
			class: "mmf-queue-icon-button",
			title: "重新读取下载队列状态",
			"aria-label": "刷新队列",
			onclick: () => this.refreshQueue(),
		}, [el("i", { class: "pi pi-refresh", "aria-hidden": "true" })]);
		this.clearFinishedButton = el("button", {
			class: "mmf-queue-icon-button",
			title: "删除已完成、失败和已取消的任务记录；不会删除模型文件或断点文件",
			"aria-label": "删除已结束任务",
			onclick: () => this.clearFinishedTasks(),
		}, [el("i", { class: "pi pi-trash", "aria-hidden": "true" })]);
		this.pauseAllButton = el("button", {
			class: "mmf-queue-icon-button",
			title: "暂停全部排队中、下载中和校验中的任务，并保留断点文件",
			"aria-label": "全部暂停",
			onclick: () => this.bulkControlTasks("pause"),
		}, [el("i", { class: "pi pi-pause", "aria-hidden": "true" })]);
		this.resumeAllButton = el("button", {
			class: "mmf-queue-icon-button",
			title: "继续全部已暂停的任务，并从现有断点恢复",
			"aria-label": "全部继续",
			onclick: () => this.bulkControlTasks("resume"),
		}, [el("i", { class: "pi pi-play", "aria-hidden": "true" })]);
		this.queueSummary = el("span", { class: "mmf-queue-summary" });
		this.parseManualButton = el("button", {
			class: "mmf-primary mmf-parse-button",
			onclick: () => this.parseManualItems(),
		}, [el("span", { text: "解析" })]);
		const manualInputPanel = el("div", { class: "mmf-manual-panel" }, [
			this.manualInput,
			el("div", { class: "mmf-manual-panel-actions" }, [
				this.manualStatus,
				this.parseManualButton,
			]),
		]);
		this.tabButtons = {
			missing: el("button", {
				class: "mmf-tab-button active",
				text: "缺失模型",
				role: "tab",
				onclick: () => this.setActiveTab("missing"),
				"aria-selected": "true",
			}),
			manual: el("button", {
				class: "mmf-tab-button",
				text: "手动新增",
				role: "tab",
				onclick: () => this.setActiveTab("manual"),
				"aria-selected": "false",
			}),
		};
		this.tabPanels = {
			missing: el("div", { class: "mmf-tab-panel active", role: "tabpanel" }, [this.modelList]),
			manual: el("div", { class: "mmf-tab-panel", role: "tabpanel" }, [
				manualInputPanel,
				this.manualModelList,
			]),
		};

		return el("div", { class: "mmf-overlay" }, [
			el("div", { class: "mmf-dialog" }, [
				el("div", { class: "mmf-header" }, [
					el("div", { class: "mmf-header-copy" }, [
						el("h2", { text: "缺失模型下载" }),
					]),
					closeButton,
				]),
				el("div", { class: "mmf-toolbar mmf-main-actions" }, [
					el("div", { class: "mmf-toolbar-copy" }, [
						this.status,
					]),
				]),
				this.directoryOptions,
				el("div", { class: "mmf-columns" }, [
					el("section", { class: "mmf-model-section" }, [
						el("div", { class: "mmf-tabs-row" }, [
							el("div", { class: "mmf-tabs", role: "tablist" }, [
								this.tabButtons.missing,
								this.tabButtons.manual,
							]),
							this.missingTabHint,
							el("div", { class: "mmf-tab-actions" }, [
								this.scanButton,
								this.downloadMissingButton,
								this.downloadManualButton,
							]),
						]),
						this.tabPanels.missing,
						this.tabPanels.manual,
					]),
					el("section", { class: "mmf-queue-section" }, [
						el("div", { class: "mmf-section-head" }, [
							el("div", { class: "mmf-queue-title" }, [el("h3", { text: "下载队列" }), this.queueSummary]),
							el("div", { class: "mmf-section-actions" }, [
								el("div", { class: "mmf-queue-limits" }, [
									el("label", { class: "mmf-concurrency-control" }, [
										el("span", { text: "并行" }),
										this.concurrencySelect,
									]),
									el("label", { class: "mmf-concurrency-control" }, [el("span", { text: "每站" }), this.providerConcurrencySelect]),
									this.bandwidthSelect,
								]),
								el("div", { class: "mmf-queue-buttons" }, [
									this.refreshButton,
									this.pauseAllButton,
									this.resumeAllButton,
									this.clearFinishedButton,
								]),
							]),
						]),
						this.queueList,
					]),
				]),
			]),
		]);
	}

	setActiveTab(tabName) {
		if (!this.tabButtons[tabName] || !this.tabPanels[tabName]) return;
		this.activeTab = tabName;
		for (const [name, button] of Object.entries(this.tabButtons)) {
			const active = name === tabName;
			button.classList.toggle("active", active);
			button.setAttribute("aria-selected", active ? "true" : "false");
			this.tabPanels[name].classList.toggle("active", active);
		}
		if (this.scanButton) this.scanButton.hidden = tabName !== "missing";
		if (this.missingTabHint) this.missingTabHint.hidden = tabName !== "missing";
		if (this.downloadMissingButton) this.downloadMissingButton.hidden = tabName !== "missing";
		if (this.downloadManualButton) this.downloadManualButton.hidden = tabName !== "manual";
	}

	setStatus(text, isError = false) {
		if (!this.status) return;
		this.status.textContent = text;
		this.status.classList.toggle("mmf-error", isError);
	}

	setManualStatus(text, isError = false) {
		if (!this.manualStatus) return;
		this.manualStatus.textContent = text;
		this.manualStatus.hidden = !text;
		this.manualStatus.classList.toggle("mmf-error", isError);
	}

	setManualParsing(active) {
		this.manualParseInProgress = active;
		if (!this.parseManualButton) return;
		this.parseManualButton.disabled = active;
		this.parseManualButton.classList.toggle("is-loading", active);
		this.parseManualButton.setAttribute("aria-busy", active ? "true" : "false");
		this.parseManualButton.replaceChildren(
			...(active
				? [
					el("i", { class: "pi pi-spinner mmf-parse-spinner", "aria-hidden": "true" }),
					el("span", { text: "解析中" }),
				]
				: [el("span", { text: "解析" })])
		);
	}

	async refreshDownloadConfig() {
		if (!this.concurrencySelect) return;
		try {
			const data = await jsonFetch(`${API_PREFIX}/config`);
			setNumericSelectValue(this.concurrencySelect, Math.max(1, Math.min(32, Number(data.config?.download_concurrency) || 1)));
			setNumericSelectValue(this.providerConcurrencySelect, Math.max(1, Math.min(32, Number(data.config?.provider_concurrency) || 1)));
			setNumericSelectValue(this.bandwidthSelect, Math.max(0, Number(data.config?.bandwidth_limit_mbps) || 0));
		} catch (error) {
			console.debug("[Missing Models Fetcher] Download config unavailable", error);
		}
	}

	async handleCustomDownloadLimit(kind) {
		const definitions = {
			total: {
				select: this.concurrencySelect,
				min: 1,
				max: 32,
				integer: true,
				prompt: "请输入总并行任务数（1–32）",
			},
			provider: {
				select: this.providerConcurrencySelect,
				min: 1,
				max: 32,
				integer: true,
				prompt: "请输入每个下载网站的并行任务数（1–32）",
			},
			bandwidth: {
				select: this.bandwidthSelect,
				min: 0,
				max: 100000,
				integer: false,
				prompt: "请输入全局速度上限（MB/s，0 表示不限速）",
				suffix: "",
			},
		};
		const definition = definitions[kind];
		if (!definition) return;
		if (definition.select.value === "custom") {
			const raw = window.prompt(definition.prompt, kind === "bandwidth" ? "1000" : "16");
			if (raw === null) {
				await this.refreshDownloadConfig();
				return;
			}
			const resolution = resolveCustomLimitInput(raw, definition.min, definition.max, definition.integer);
			if (resolution.status === "invalid") {
				this.setStatus(`输入无效：${definition.prompt}`, true);
				await this.refreshDownloadConfig();
				return;
			}
			setNumericSelectValue(definition.select, resolution.value, definition.suffix || "");
		}
		if (kind === "total") await this.updateDownloadConcurrency();
		else await this.updateDownloadLimits();
	}

	async updateDownloadConcurrency() {
		if (!this.concurrencySelect) return;
		const value = Math.max(
			1,
			Math.min(32, Number(this.concurrencySelect.value) || 1)
		);
		this.concurrencySelect.disabled = true;
		try {
			const data = await jsonFetch(`${API_PREFIX}/config`, {
				method: "POST",
				body: JSON.stringify({ download_concurrency: value }),
			});
			setNumericSelectValue(this.concurrencySelect, data.config?.download_concurrency || value);
			this.setStatus(`并行下载数已设为 ${this.concurrencySelect.value}`);
		} catch (error) {
			this.setStatus(`并行下载设置失败：${error.message}`, true);
			await this.refreshDownloadConfig();
		} finally {
			this.concurrencySelect.disabled = false;
		}
	}

	async updateDownloadLimits() {
		const providerConcurrency = Math.max(1, Math.min(32, Number(this.providerConcurrencySelect?.value) || 1));
		const bandwidthLimit = Math.max(0, Number(this.bandwidthSelect?.value) || 0);
		this.providerConcurrencySelect.disabled = true;
		this.bandwidthSelect.disabled = true;
		try {
			await jsonFetch(`${API_PREFIX}/config`, {
				method: "POST",
				body: JSON.stringify({ provider_concurrency: providerConcurrency, bandwidth_limit_mbps: bandwidthLimit }),
			});
			this.setStatus(`单站并行 ${providerConcurrency}，全局限速 ${bandwidthLimit ? `${bandwidthLimit} MB/s` : "关闭"}`);
		} catch (error) {
			this.setStatus(`下载限制设置失败：${error.message}`, true);
			await this.refreshDownloadConfig();
		} finally {
			this.providerConcurrencySelect.disabled = false;
			this.bandwidthSelect.disabled = false;
		}
	}

	async refreshFolders() {
		try {
			const data = await jsonFetch(`${API_PREFIX}/folders`);
			this.folders = data.folders || [];
			this.folderMap = new Map(this.folders.map((folder) => [folder.directory, folder]));
			this.directoryOptions.replaceChildren(
				...this.folders.map((folder) => el("option", { value: folder.directory }))
			);
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	async autoScanCurrentWorkflow() {
		if (this.scanInProgress) return;
		let workflow = null;
		let signature = "";
		try {
			workflow = currentWorkflow();
			signature = JSON.stringify(workflow);
		} catch (error) {
			this.setStatus(error.message, true);
			return;
		}
		if (signature === this.lastWorkflowSignature && this.models.length) return;
		await this.scan({ workflow, signature, automatic: true });
	}

	async scan(options = {}) {
		try {
			if (this.scanInProgress) return;
			this.scanInProgress = true;
			const workflow = options.workflow || currentWorkflow();
			const signature = options.signature || JSON.stringify(workflow);
			this.setStatus(options.automatic ? "正在自动扫描当前工作流..." : "正在扫描当前工作流...");
			const data = await jsonFetch(`${API_PREFIX}/scan`, {
				method: "POST",
				body: JSON.stringify({ workflow }),
			});
			this.models = onlyMissingModels(data.models);
			updateMissingModelsWarning(this.models);
			this.lastWorkflowSignature = signature;
			this.renderModels();
			if (!options.automatic) this.setActiveTab("missing");
			this.setStatus(
				this.models.length
					? `扫描完成，发现 ${this.models.length} 个缺失模型候选，请确认后再下载`
					: "扫描完成，当前工作流没有缺失模型"
			);
			void this.resolveModelSources();
		} catch (error) {
			this.setStatus(error.message, true);
		} finally {
			this.scanInProgress = false;
		}
	}

	async resolveModelSources() {
		if (this.sourceResolveInProgress || !this.models.length) return;
		await this.resolveSourcesProgressively(this.models, "workflow");
	}

	cancelSourceResolveRequests(scope = "") {
		const jobIds = [];
		for (const request of [...this.sourceResolveControllers]) {
			if (!scope || request.scope === scope) {
				request.controller.abort();
				if (request.jobId) jobIds.push(request.jobId);
				this.sourceResolveControllers.delete(request);
			}
		}
		if (jobIds.length) {
			void jsonFetch(`${API_PREFIX}/sources/resolve/cancel`, {
				method: "POST",
				body: JSON.stringify({ job_ids: jobIds }),
			}).catch(() => {});
		}
	}

	async fetchSourceProvider(model, provider, scope, signal, jobId) {
		return jsonFetch(`${API_PREFIX}/sources/resolve/provider`, {
			method: "POST",
			signal,
			body: JSON.stringify({
				provider: provider.id,
				job_id: jobId,
				item: {
					id: model.id,
					name: model.name,
					display_name: model.display_name || "",
					version_name: model.version_name || "",
					url: model.url || "",
					sources: model.sources || [],
					directory: model.directory || "",
					manual_entry: scope !== "workflow",
					hash: model.hash || "",
					hash_type: model.hash_type || "",
				},
			}),
		});
	}

	updateModelSourceError(model) {
		model.source_error = Object.entries(model.source_errors || {})
			.map(([providerId, message]) => {
				const label = SOURCE_PROVIDERS.find((item) => item.id === providerId)?.label || providerId;
				return `${label}: ${message}`;
			})
			.join("；");
	}

	applySourceProviderResult(model, provider, resolved, scope) {
		mergeProviderSources(model, provider.id, resolved.sources);
		model.source_diagnostics ||= {};
		model.source_diagnostics[provider.id] = resolved.diagnostics || null;
		if (scope !== "workflow" && resolved.directory && (!model.directory || model.needs_directory)) {
			model.directory = resolved.directory;
			model.directory_valid = resolved.directory_valid !== false;
			model.needs_directory = resolved.needs_directory === true;
			model.destinationOptions = resolved.destinationOptions || model.destinationOptions || [];
			model.directory_inference = resolved.directory_inference || model.directory_inference || "";
		}
		model.source_errors ||= {};
		if (resolved.error) {
			model.source_resolution[provider.id] = "failed";
			model.source_errors[provider.id] = resolved.error;
		} else {
			const found = (resolved.sources || []).some((source) => source?.provider === provider.id);
			model.source_resolution[provider.id] = found ? "resolved" : "not_found";
			delete model.source_errors[provider.id];
		}
		this.updateModelSourceError(model);
		model.size = resolvedModelSize(model.sources) ?? model.size;
	}

	renderSourceScope(scope) {
		if (scope === "workflow") this.renderModels();
		else this.renderManualModels();
	}

	async retrySourceProvider(model, provider, scope) {
		if (model.source_resolution?.[provider.id] === "pending") return;
		model.source_resolution ||= {};
		model.source_errors ||= {};
		model.source_resolution[provider.id] = "pending";
		delete model.source_errors[provider.id];
		this.updateModelSourceError(model);
		this.renderSourceScope(scope);
		const controller = new AbortController();
		const jobId = `${scope}:${model.id || model.name}:${provider.id}:retry:${Date.now()}`;
		const request = { scope, controller, jobId };
		this.sourceResolveControllers.add(request);
		try {
			const data = await this.fetchSourceProvider(model, provider, scope, controller.signal, jobId);
			this.applySourceProviderResult(model, provider, data.model || {}, scope);
		} catch (error) {
			if (error?.name === "AbortError") return;
			model.source_resolution[provider.id] = "failed";
			model.source_errors[provider.id] = error.message;
			this.updateModelSourceError(model);
		} finally {
			this.sourceResolveControllers.delete(request);
			this.renderSourceScope(scope);
		}
	}

	async resolveSourcesProgressively(models, scope) {
		if (!models.length) return;
		this.cancelSourceResolveRequests(scope);
		const isWorkflow = scope === "workflow";
		const generationKey = isWorkflow
			? "sourceResolveGeneration"
			: "manualSourceResolveGeneration";
		const generation = this[generationKey] + 1;
		this[generationKey] = generation;
		if (isWorkflow) this.sourceResolveInProgress = true;
		else this.manualSourceResolveInProgress = true;

		const providers = SOURCE_PROVIDERS.filter((provider) => provider.id !== "manual");
		for (const model of models) {
			model.source_resolution = Object.fromEntries(
				providers.map((provider) => [provider.id, "pending"])
			);
			model.source_errors = {};
			model.source_diagnostics = {};
			model.source_error = "";
		}
		if (isWorkflow) this.renderModels();
		else this.renderManualModels();

		const isCurrent = () => isCurrentResolution(
			this[generationKey],
			generation,
			isWorkflow ? this.models : this.manualModels,
			models
		);
		const total = models.length * providers.length;
		let completed = 0;
		const updateProgress = () => {
			const message = `正在解析下载站点 ${completed}/${total}，已完成的站点会立即显示`;
			if (isWorkflow) this.setStatus(message);
			else this.setManualStatus(message);
		};
		updateProgress();

		const tasks = [];
		for (const model of models) {
			for (const provider of providers) {
				tasks.push(async () => {
					if (!isCurrent()) return;
					const controller = new AbortController();
					const jobId = `${scope}:${generation}:${model.id || model.name}:${provider.id}`;
					const request = { scope, controller, jobId };
					this.sourceResolveControllers.add(request);
					try {
						const data = await this.fetchSourceProvider(
							model,
							provider,
							scope,
							controller.signal,
							jobId
						);
						if (!isCurrent()) return;
						this.applySourceProviderResult(model, provider, data.model || {}, scope);
					} catch (error) {
						if (!isCurrent()) return;
						if (error?.name === "AbortError") {
							model.source_resolution[provider.id] = "canceled";
							return;
						}
						model.source_resolution[provider.id] = "failed";
						model.source_errors[provider.id] = error.message;
						this.updateModelSourceError(model);
					} finally {
						this.sourceResolveControllers.delete(request);
						if (!isCurrent()) return;
						completed += 1;
						this.renderSourceScope(scope);
						updateProgress();
					}
				});
			}
		}

		await runTasksWithConcurrency(tasks, 6);
		if (!isCurrent()) return;
		if (isWorkflow) {
			this.sourceResolveInProgress = false;
			this.renderModels();
			this.setStatus("下载站点解析完成，请为需要下载的模型选择来源");
		} else {
			this.manualSourceResolveInProgress = false;
			this.renderManualModels();
			this.setManualStatus(`已解析 ${models.length} 个手动模型，请选择下载来源`);
		}
	}

	renderModels() {
		this.renderModelCollection(this.modelList, this.models, "workflow");
	}

	renderManualModels() {
		this.renderModelCollection(this.manualModelList, this.manualModels, "manual");
	}

	renderModelCollection(container, models, scope) {
		container.replaceChildren();
		if (!models.length) {
			if (scope === "workflow") {
				container.append(el("div", { class: "mmf-empty", text: "当前工作流没有缺失模型。" }));
			}
			return;
		}

		const groupBodies = new Map();
		for (const model of models) {
			const displayName = model.display_name || model.name;
			const groupedVersion = scope === "manual" && Boolean(model.group_id);
			let appendTarget = container;
			if (groupedVersion) {
				let groupBody = groupBodies.get(model.group_id);
				if (!groupBody) {
					const versionCount = Number(model.group_version_count)
						|| models.filter((candidate) => candidate.group_id === model.group_id).length;
					groupBody = el("div", { class: "mmf-version-list" });
					const group = el("div", { class: "mmf-model-group" }, [
						el("div", { class: "mmf-model-group-head" }, [
							el("div", { class: "mmf-model-group-copy" }, [
								el("strong", { text: model.group_label || displayName }),
								el("span", { text: "同一模型的不同版本，请分别选择需要下载的版本。" }),
							]),
							el("span", { class: "mmf-badge", text: `${versionCount} 个版本` }),
						]),
						groupBody,
					]);
					container.append(group);
					groupBodies.set(model.group_id, groupBody);
				}
				appendTarget = groupBody;
			}
			const cardDisplayName = model.name || displayName;
			const manualUrlInput = el("input", {
				type: "text",
				value: model.manual_url ?? model.url ?? "",
				placeholder: "粘贴 Hugging Face、魔搭、Civitai 或其他 HTTP(S) 下载链接",
			});
			const manualUrlRow = el("label", { class: "mmf-manual-url" }, [
				el("span", { text: "手动下载链接" }),
				manualUrlInput,
			]);
			manualUrlRow.hidden = scope !== "manual";
			const selectedSourceInfo = el("div", {
				class: "mmf-source-selected mmf-card-link",
				text: "尚未选择下载源",
			});
			const displayedSize = resolvedModelSize(model.sources) ?? model.size;
			const sizeNode = el("span", {
				text: `大小: ${formatBytes(displayedSize)}`,
			});
			const directoryInput = el("input", {
				type: "text",
				value: model.directory || "",
				list: "mmf-directory-options",
				placeholder: "directory，例如 diffusion_models / vae",
			});
			const directorySelect = createSelectMenu("下载时自动选择第一个可写路径");
			const refreshDestinationOptions = () => {
				const folder = this.folderMap.get(directoryInput.value.trim());
				const options = model.destinationOptions?.length && directoryInput.value.trim() === model.directory
					? model.destinationOptions
					: folder?.paths || [];
				if (options.length) {
					const preferred = options.find((option) => option.default && option.writable)
						|| options.find((option) => option.writable)
						|| options[0];
					directorySelect.setOptions(
						options.map((option) => ({
							value: option.path,
							label: option.path,
							writable: option.writable,
						})),
						preferred.path
					);
				} else {
					directorySelect.setOptions([]);
				}
			};
			directoryInput.addEventListener("change", refreshDestinationOptions);
			directoryInput.addEventListener("input", refreshDestinationOptions);
			refreshDestinationOptions();

			const badges = [];
			if (!(model.sources || []).length) badges.push(el("span", { class: "mmf-badge warn", text: "需要选择或填写来源" }));
			if (model.needs_directory) badges.push(el("span", { class: "mmf-badge warn", text: "需要目录" }));
			const directoryStatus = directoryInferenceStatus(model.directory_inference, model.needs_directory);
			badges.push(el("span", {
				class: `mmf-badge mmf-directory-evidence ${directoryStatus.level}`,
				text: directoryStatus.label,
				title: directoryStatus.detail,
			}));

			const sourceByProvider = new Map(
				(model.sources || []).map((source) => [source.provider, source])
			);
			const verifiedSizeSource = [...sourceByProvider.values()].find(
				(source) => (
					source?.verification === "hash_verified"
					&& Number.isFinite(Number(source?.size))
					&& Number(source.size) > 0
				)
			);
			const sourceSize = verifiedSizeSource?.size
				?? resolvedModelSize([...sourceByProvider.values()])
				?? model.size;
			sizeNode.textContent = `大小: ${formatBytes(sourceSize)}`;
			const sourceOptions = el("div", {
				class: "mmf-source-options",
				role: "group",
				"aria-label": `${displayName}${model.version_name ? ` ${model.version_name}` : ""} 下载方式`,
			});
			const state = {
				manualUrlInput,
				directoryInput,
				directorySelect,
				model,
				selectedSource: null,
				sourceButtons: [],
				scope,
			};
			const workflowSha256 = expectedWorkflowSha256(model);
			for (const provider of SOURCE_PROVIDERS) {
				const source = sourceByProvider.get(provider.id);
				const resolutionState = model.source_resolution?.[provider.id] || "idle";
				const resolvingProvider = provider.id !== "manual"
					&& resolutionState === "pending";
				const retryableProvider = provider.id !== "manual"
					&& ["failed", "not_found", "canceled"].includes(resolutionState);
				const manualBlockedReason = provider.id === "manual" && workflowSha256
					? "工作流明确要求 SHA-256，手动链接无法在下载前确认来源 hash，已禁止选择。"
					: "";
				const available = resolvingProvider
					? false
					: provider.id === "manual"
					? !manualBlockedReason
					: Boolean(source) && source.selectable !== false;
				const warningLevel = source?.warning_level || (source?.warning ? "warning" : "");
				const optionTitle = resolvingProvider
					? `正在解析 ${provider.label} 下载源...`
					: resolutionState === "failed"
					? `${provider.label} 解析失败：${model.source_errors?.[provider.id] || "未知错误"}。点击重试。`
					: resolutionState === "not_found"
					? `${provider.label} 未找到匹配文件。点击重试。`
					: resolutionState === "canceled"
					? `${provider.label} 解析已取消。点击重试。`
					: manualBlockedReason
					|| sourceTooltip(source, provider.label, model.source_diagnostics?.[provider.id]);
				const warningText = source?.blocked_reason || source?.warning || manualBlockedReason;
				const statusIcon = resolvingProvider
					? el("i", { class: "pi pi-spinner pi-spin mmf-source-loading", "aria-hidden": "true" })
					: retryableProvider
					? el("i", {
						class: `pi pi-refresh mmf-source-retry${resolutionState === "failed" ? " is-error" : ""}`,
						"aria-hidden": "true",
					})
					: warningText
					? el("i", {
						class: `pi pi-exclamation-triangle mmf-source-warning${warningLevel === "error" || manualBlockedReason ? " is-error" : ""}`,
						title: warningText,
						"aria-label": warningText,
					})
					: resolutionState === "resolved" && source
					? el("i", {
						class: "pi pi-check mmf-source-found",
						title: `${provider.label} 已找到可用来源`,
						"aria-label": `${provider.label} 已找到可用来源`,
					})
					: null;
				const optionButton = el("button", {
					type: "button",
					class: `mmf-source-option${available || retryableProvider ? "" : " disabled"}${resolvingProvider ? " is-resolving" : ""}${resolutionState === "failed" ? " is-error" : ""}${resolutionState === "not_found" || resolutionState === "canceled" ? " is-not-found" : ""}${warningLevel ? ` is-${warningLevel}` : ""}`,
					title: optionTitle,
					"aria-label": provider.label,
					"aria-pressed": "false",
				}, [
					el("span", { class: "mmf-source-option-label", text: provider.label }),
					provider.id === "manual"
						? null
						: el("span", { class: "mmf-source-status-slot", "aria-hidden": statusIcon ? "false" : "true" }, statusIcon),
				]);
				state.sourceButtons.push(optionButton);
				optionButton.disabled = resolvingProvider || (!available && !retryableProvider);
				optionButton.addEventListener("click", () => {
					if (retryableProvider) {
						void this.retrySourceProvider(model, provider, scope);
						return;
					}
					const deselecting = optionButton.classList.contains("active");
					for (const button of state.sourceButtons) {
						button.classList.remove("active");
						button.setAttribute("aria-pressed", "false");
					}
					if (deselecting) {
						state.selectedSource = null;
						model.selected_source_provider = "";
						selectedSourceInfo.textContent = "尚未选择下载源";
						selectedSourceInfo.title = "尚未选择下载源";
						manualUrlRow.hidden = scope !== "manual";
						sizeNode.textContent = `大小: ${formatBytes(sourceSize)}`;
						return;
					}
					optionButton.classList.add("active");
					optionButton.setAttribute("aria-pressed", "true");
					model.selected_source_provider = provider.id;
					state.selectedSource = provider.id === "manual"
						? { provider: "manual", url: manualUrlInput.value.trim() }
						: source;
					manualUrlRow.hidden = scope !== "manual" && provider.id !== "manual";
					selectedSourceInfo.textContent = provider.id === "manual"
						? "手动链接：请输入下载 URL"
						: `${provider.label}: ${source.url}`;
					selectedSourceInfo.title = provider.id === "manual"
						? "手动链接：请输入下载 URL"
						: `${provider.label}: ${source.url}`;
					sizeNode.textContent = `大小: ${formatBytes(source?.size)}`;
				});
				if (model.selected_source_provider === provider.id && available) {
					optionButton.classList.add("active");
					optionButton.setAttribute("aria-pressed", "true");
					state.selectedSource = provider.id === "manual"
						? { provider: "manual", url: manualUrlInput.value.trim() }
						: source;
					manualUrlRow.hidden = scope !== "manual" && provider.id !== "manual";
					selectedSourceInfo.textContent = provider.id === "manual"
						? "手动链接：请输入下载 URL"
						: `${provider.label}: ${source.url}`;
					selectedSourceInfo.title = selectedSourceInfo.textContent;
					sizeNode.textContent = `大小: ${formatBytes(source?.size)}`;
				}
				sourceOptions.append(optionButton);
			}
			manualUrlInput.addEventListener("input", () => {
				model.manual_url = manualUrlInput.value;
				if (state.selectedSource?.provider === "manual") {
					state.selectedSource = { provider: "manual", url: manualUrlInput.value.trim() };
				}
			});

			const sourceSection = el("div", { class: "mmf-source-section" }, [
				el("span", { class: "mmf-field-label", text: "选择下载站点" }),
				sourceOptions,
			]);
			const card = el("div", {
				class: `mmf-card${scope === "manual" ? " mmf-manual-card" : ""}`,
			}, [
				el("div", { class: "mmf-card-top" }, [
					el("div", { class: "mmf-card-summary" }, [
						el("div", { class: "mmf-card-head" }, [
							el("strong", { text: cardDisplayName }),
							el("div", { class: "mmf-badges" }, badges),
						]),
						el("div", { class: "mmf-meta" }, [
							el("span", { text: `类型: ${model.directory || "-"}` }),
							el("span", {
								text: scope === "manual"
									? "添加方式: 手动新增"
									: `节点: ${model.nodeType || "未知节点"}`,
							}),
							sizeNode,
							model.version_name
								? el("span", { text: `版本: ${model.version_name}` })
								: null,
							model.hash ? el("span", { text: `Hash: ${model.hash_type || "auto"}` }) : null,
						]),
						selectedSourceInfo,
					]),
					sourceSection,
				]),
				model.source_error
					? el("div", {
						class: "mmf-task-error",
						text: `${scope === "manual" ? "解析失败" : "部分来源解析失败"}: ${model.source_error}`,
					})
					: null,
				manualUrlRow,
				el("div", { class: "mmf-path-row" }, [
					el("label", {}, [el("span", { text: "模型目录" }), directoryInput]),
					el("label", {}, [el("span", { text: "保存路径" }), directorySelect.root]),
				]),
			]);
			card._mmf = state;
			appendTarget.append(card);
		}
	}

	async parseManualItems() {
		if (this.manualParseInProgress) return;
		const items = parseManualInputText(this.manualInput?.value || "");
		if (!items.length) {
			this.setManualStatus("请先输入模型链接或模型名称", true);
			return;
		}
		this.setActiveTab("manual");
		this.setManualParsing(true);
		try {
			this.setManualStatus(`正在解析 ${items.length} 个手动模型条目...`);
			const data = await jsonFetch(`${API_PREFIX}/manual/parse`, {
				method: "POST",
				body: JSON.stringify({ items, resolve_sources: false }),
			});
			this.manualModels = (data.models || []).map((model) => ({
				...model,
				source_error: model.error || "",
			}));
			this.renderManualModels();
			this.setActiveTab("manual");
			const unresolved = this.manualModels.filter(
				(model) => model.needs_url || model.needs_directory
			).length;
			this.setManualStatus(
				unresolved
					? `已解析 ${this.manualModels.length} 个条目，其中 ${unresolved} 个需要补充链接或目录`
					: `已解析 ${this.manualModels.length} 个手动模型，请选择下载来源`
			);
			if (this.manualModels.length) {
				void this.resolveSourcesProgressively(this.manualModels, "manual");
			}
		} catch (error) {
			this.setManualStatus(`手动模型解析失败：${error.message}`, true);
		} finally {
			this.setManualParsing(false);
		}
	}

	async downloadSelected(scopeName = this.activeTab === "manual" ? "manual" : "workflow") {
		const items = [];
		const invalid = [];
		const container = scopeName === "manual" ? this.manualModelList : this.modelList;
		const cards = [...container.querySelectorAll(".mmf-card")];
		for (const card of cards) {
			const state = card._mmf;
			if (!state?.selectedSource) continue;
			const source = state.selectedSource;
			const url = source?.provider === "manual"
				? state.manualUrlInput.value.trim()
				: String(source?.url || "").trim();
			const directory = state.directoryInput.value.trim();
			if (!source || source.selectable === false || source.blocked_reason || !url || !directory) {
				invalid.push(state.model.name);
				continue;
			}
			const workflowSha256 = expectedWorkflowSha256(state.model);
			const sourceSha256 = String(source.sha256 || "").trim().toLowerCase();
			const validationHash = workflowSha256
				|| (/^[0-9a-f]{64}$/.test(sourceSha256) ? sourceSha256 : "");
			items.push({
				name: state.model.name,
				url,
				provider: source.provider,
				directory,
				destination_path: state.directorySelect.value,
				hash: validationHash,
				hash_type: validationHash ? "sha256" : "",
				workflow_hash: workflowSha256,
				workflow_hash_type: workflowSha256 ? "sha256" : "",
				source_sha256: sourceSha256,
				hash_source: source.hash_source || "",
				size: Number(source.size || state.model.size) || null,
				selectable: source.selectable !== false,
				blocked_reason: source.blocked_reason || "",
			});
		}

		if (invalid.length) {
			this.setStatus(`以下模型尚未选择有效下载源或目录: ${invalid.join("、")}`, true);
			return;
		}
		if (!items.length) {
			this.setStatus("尚未选择任何下载方式", true);
			return;
		}

		try {
			this.setStatus("正在加入下载队列...");
			const data = await jsonFetch(`${API_PREFIX}/downloads`, {
				method: "POST",
				body: JSON.stringify({ items }),
			});
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
			this.setStatus(`已加入 ${items.length} 个下载任务`);
			this.startPolling();
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	async refreshQueue(options = {}) {
		try {
			const previousTasks = new Map((this.queue.tasks || []).map((task) => [task.id, task.status]));
			const data = await jsonFetch(`${API_PREFIX}/downloads`);
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
			const completedNow = (this.queue.tasks || []).some(
				(task) => task.status === "completed" && previousTasks.get(task.id) !== "completed"
			);
			if (completedNow) void refreshMissingModelsActionState();
		} catch (error) {
			if (options.silent) {
				console.debug("[Missing Models Fetcher] Queue refresh unavailable", error);
			} else {
				this.setStatus(error.message, true);
			}
		}
	}

	async clearFinishedTasks() {
		try {
			const data = await jsonFetch(`${API_PREFIX}/downloads/clear`, {
				method: "POST",
				body: JSON.stringify({ statuses: ["completed", "failed", "canceled"] }),
			});
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
			this.setStatus("已删除结束任务记录；模型文件和断点文件均已保留");
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	renderQueue() {
		this.queueList.replaceChildren();
		const tasks = this.queue.tasks || [];
		const summary = this.queue.summary || {};
		if (this.queueSummary) {
			const eta = summary.eta != null && Number.isFinite(Number(summary.eta))
				? `，预计 ${formatEta(Number(summary.eta))}`
				: "";
			this.queueSummary.textContent = `活动 ${summary.active || 0} · 排队 ${summary.queued || 0} · 暂停 ${summary.paused || 0} · ${formatBytes(summary.total_speed || 0)}/s${eta}`;
		}
		if (!tasks.length) {
			this.queueList.append(el("div", { class: "mmf-empty", text: "暂无下载任务。" }));
			return;
		}
		for (const task of tasks) {
			const progress = task.status === "verifying"
				? task.verification_progress ?? 0
				: task.progress ?? 0;
			const isPausing = Boolean(task.pause_requested);
			const isPausedLike = task.status === "paused" || isPausing;
			const currentSpeed = task.status === "verifying"
				? Number(task.verification_speed || 0)
				: Number(task.speed || 0);
			const speedText = isPausedLike || !currentSpeed ? "- MB/s" : `${formatBytes(currentSpeed)}/s`;
			const controls = [];
			if (task.status === "queued") {
				controls.push(el("select", {
					class: "mmf-task-priority",
					title: "任务优先级",
					onchange: (event) => this.setTaskPriority(task.id, event.target.value),
				}, [
					el("option", { value: "1", text: "高", selected: Number(task.priority) === 1 ? "" : null }),
					el("option", { value: "0", text: "普通", selected: Number(task.priority || 0) === 0 ? "" : null }),
					el("option", { value: "-1", text: "低", selected: Number(task.priority) === -1 ? "" : null }),
				]));
				controls.push(el("button", { text: "↑", title: "上移", onclick: () => this.moveTask(task.id, "up") }));
				controls.push(el("button", { text: "↓", title: "下移", onclick: () => this.moveTask(task.id, "down") }));
			}
			if (!isPausing && ["downloading", "verifying", "queued"].includes(task.status)) {
				controls.push(el("button", { text: "暂停", onclick: () => this.controlTask(task.id, "pause") }));
			}
			if (task.status === "failed" && task.restart_required) {
				controls.push(el("button", { text: "重新下载", onclick: () => this.restartTask(task) }));
			} else if (isPausing || task.status === "paused" || task.status === "failed" || task.status === "canceled") {
				controls.push(el("button", { text: "继续", onclick: () => this.controlTask(task.id, "resume") }));
			}
			if (!["completed", "canceled"].includes(task.status)) {
				controls.push(el("button", { text: "取消", onclick: () => this.controlTask(task.id, "cancel") }));
			}

			this.queueList.append(
				el("div", { class: "mmf-card" }, [
					el("div", { class: "mmf-card-head" }, [
						el("strong", { text: task.name, title: task.name }),
						el("span", { class: `mmf-badge ${task.status === "failed" ? "error" : ""}`, text: formatStatus(task.status) }),
					]),
					el("div", { class: "mmf-progress" }, [
						el("div", { class: "mmf-progress-bar", style: `width:${Math.max(0, Math.min(100, progress))}%` }),
					]),
					el("div", { class: "mmf-meta" }, [
						el("span", {
							text: `来源: ${SOURCE_PROVIDERS.find((provider) => provider.id === task.provider)?.label || task.provider || "手动链接"}`,
						}),
						el("span", { text: `${formatBytes(task.downloaded)} / ${formatBytes(task.total)}` }),
						task.status === "verifying"
							? el("span", { text: `Hash 校验: ${(task.verification_progress ?? 0).toFixed(1)}%` })
							: null,
						task.verified_hash ? el("span", { text: "Hash 已校验" }) : null,
					]),
					task.error ? el("div", { class: "mmf-task-error", text: task.error }) : null,
					el("div", { class: "mmf-queue-control-row" }, [
						el("span", { class: "mmf-speed", text: `速度: ${speedText}` }),
						el("div", { class: "mmf-actions" }, controls),
					]),
				])
			);
		}
	}

	async controlTask(taskId, action) {
		try {
			const data = await jsonFetch(`${API_PREFIX}/downloads/${taskId}/${action}`, { method: "POST", body: JSON.stringify({}) });
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	async restartTask(task) {
		if (!window.confirm(`“${task.name}”校验失败。重新下载会删除该任务的断点文件并从头开始，是否继续？`)) return;
		await this.controlTask(task.id, "restart");
	}

	async bulkControlTasks(action) {
		try {
			const data = await jsonFetch(`${API_PREFIX}/downloads/bulk/${action}`, { method: "POST", body: "{}" });
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	async moveTask(taskId, direction) {
		try {
			const data = await jsonFetch(`${API_PREFIX}/downloads/${taskId}/move/${direction}`, { method: "POST", body: "{}" });
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	async setTaskPriority(taskId, priority) {
		try {
			const data = await jsonFetch(`${API_PREFIX}/downloads/${taskId}/priority`, {
				method: "POST",
				body: JSON.stringify({ priority: Number(priority) }),
			});
			this.queue = data.queue || { tasks: [] };
			this.renderQueue();
		} catch (error) {
			this.setStatus(error.message, true);
		}
	}

	startPolling() {
		if (pollTimer) return;
		pollTimer = setInterval(() => this.refreshQueue({ silent: true }), 1500);
	}

	stopPolling() {
		if (pollTimer) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}
}

function injectStyles() {
	if (document.getElementById("mmf-styles")) return;
	document.head.append(
		el("style", {
			id: "mmf-styles",
			text: `
.mmf-overlay{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.55)}
.mmf-dialog{width:min(1180px,92vw);height:min(760px,88vh);background:#202124;color:#f5f5f5;border:1px solid #3b3d42;border-radius:8px;box-shadow:0 18px 64px rgba(0,0,0,.45);display:flex;flex-direction:column;overflow:hidden}
.mmf-header{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid #34363a}
.mmf-header-copy{min-width:0}
.mmf-header h2{margin:0;font-size:20px;letter-spacing:0}
.mmf-icon-button{width:32px;height:32px;padding:0;border:1px solid #d65a5a;border-radius:6px;background:#b33a3a;color:#fff;display:inline-flex;align-items:center;justify-content:center;font:22px/1 Arial,sans-serif;text-align:center}
.mmf-icon-button:hover{background:#c84747;border-color:#e06a6a}
.mmf-toolbar{display:flex;gap:10px;align-items:center;padding:12px 20px;border-bottom:1px solid #34363a}
.mmf-main-actions{justify-content:space-between;align-items:flex-start}
.mmf-toolbar-copy{display:flex;flex:1;min-width:0;flex-direction:column;gap:5px}
.mmf-toolbar-copy p{margin:0;color:#b8bec8;font-size:13px}
.mmf-toolbar-copy .mmf-status{margin:0}
.mmf-toolbar-buttons{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.mmf-columns{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:0;min-height:0;flex:1}
.mmf-columns section{min-width:0;min-height:0;padding:14px 16px;display:flex;flex-direction:column;box-sizing:border-box}
.mmf-columns section:first-child{border-right:1px solid #34363a}
.mmf-columns h3{margin:0 0 10px;font-size:15px;letter-spacing:0}
.mmf-tabs-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:10px;margin-bottom:10px;border-bottom:1px solid #3f4248}
.mmf-tabs{grid-column:1;display:flex;align-items:center;gap:4px;min-width:0}
.mmf-tab-hint{grid-column:2;min-width:0;color:#b8bec8;font-size:12px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mmf-tab-actions{grid-column:3;display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-bottom:4px}
.mmf-tab-button{padding:8px 14px;border:0;border-bottom:2px solid transparent;border-radius:6px 6px 0 0;background:transparent;color:#aeb5bf}
.mmf-tab-button:hover{background:#2d3035;color:#f5f5f5}
.mmf-tab-button.active{border-bottom-color:#4f9cff;background:#292d33;color:#fff}
.mmf-tab-panel{display:none;flex:1;min-height:0;flex-direction:column;gap:10px}
.mmf-tab-panel.active{display:flex}
.mmf-section-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.mmf-section-head h3{margin:0}
.mmf-section-actions{display:flex;align-items:center;gap:8px}
.mmf-queue-section .mmf-section-head{align-items:center;flex-wrap:wrap}
.mmf-queue-section .mmf-section-actions{display:flex;flex:1 1 100%;align-items:center;gap:7px;min-width:0;flex-wrap:nowrap}
.mmf-queue-limits,.mmf-queue-buttons{display:flex;align-items:center;align-content:center;gap:5px;min-width:0;min-height:30px;flex-wrap:nowrap}
.mmf-queue-limits{flex:1 1 auto}
.mmf-queue-buttons{flex:0 0 auto;padding-left:7px;border-left:1px solid #45484f}
.mmf-queue-title{display:flex;align-items:baseline;gap:8px;min-width:0;line-height:1.2}
.mmf-queue-title h3{line-height:1.2}
.mmf-queue-summary{display:inline-flex;align-items:center;color:#abb2bf;font-size:11px;line-height:1.2;white-space:nowrap}
.mmf-concurrency-control{display:inline-flex!important;height:30px;flex-direction:row!important;align-items:center;justify-content:center;gap:3px!important;margin:0;color:#aeb5bf!important;line-height:30px;white-space:nowrap}
.mmf-dialog select.mmf-concurrency-select{width:44px;height:30px;min-height:30px;margin:0;padding:3px 4px;line-height:22px}
.mmf-dialog .mmf-queue-limits>select{width:68px;height:30px;min-height:30px;margin:0;padding:3px 4px;line-height:22px}
.mmf-dialog button.mmf-queue-icon-button{display:inline-flex;width:30px;height:30px;min-width:30px;min-height:30px;margin:0;align-items:center;justify-content:center;padding:0;border-radius:6px;line-height:1}
.mmf-dialog button.mmf-queue-icon-button>i{display:inline-flex;align-items:center;justify-content:center;line-height:1}
.mmf-dialog select.mmf-task-priority{width:auto;min-width:58px;min-height:28px;padding:3px 5px}
.mmf-list{display:flex;flex-direction:column;gap:10px;overflow:auto;min-height:0;padding-right:4px}
.mmf-tab-panel>.mmf-list,.mmf-queue{flex:1}
.mmf-queue-section{gap:10px}
.mmf-manual-panel{display:flex;flex-direction:column;gap:7px;padding:10px;border:1px solid #3f4248;background:#292b2f;border-radius:8px}
.mmf-manual-panel-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px}
.mmf-manual-input{width:100%;min-height:76px;resize:vertical}
.mmf-manual-status{min-width:0;margin:0;padding:0 2px;line-height:1.45}
.mmf-parse-button{margin-left:auto}
.mmf-manual-models{flex:1 1 auto}
.mmf-model-group{border:1px solid #4b5563;background:#25272b;border-radius:8px;overflow:visible;flex:0 0 auto}
.mmf-model-group-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:12px 14px;border-bottom:1px solid #41454c;background:#2d3035}
.mmf-model-group-copy{display:flex;min-width:0;flex-direction:column;gap:4px}
.mmf-model-group-copy strong{font-size:14px;overflow-wrap:anywhere}
.mmf-model-group-copy span{color:#aeb5bf;font-size:12px}
.mmf-version-list{display:flex;flex-direction:column;gap:7px;padding:7px}
.mmf-version-list>.mmf-card{background:#292b2f}
.mmf-manual-card{border-color:#4b5563}
.mmf-card{border:1px solid #3f4248;background:#292b2f;border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;gap:6px}
.mmf-card-top{display:grid;grid-template-columns:minmax(0,1fr) max-content;align-items:start;gap:10px}
.mmf-card-summary{display:flex;min-width:0;flex-direction:column;gap:4px}
.mmf-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.mmf-card-head strong{font-size:13px;white-space:nowrap}
.mmf-queue .mmf-card-head{min-width:0}
.mmf-queue .mmf-card-head strong{display:block;min-width:0;flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mmf-queue .mmf-card-head .mmf-badge{flex:0 0 auto}
.mmf-meta{display:flex;gap:5px 10px;flex-wrap:nowrap;color:#abb2bf;font-size:12px;line-height:1.3}
.mmf-meta span{white-space:nowrap}
.mmf-badges{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.mmf-badge{border:1px solid #4e5561;border-radius:999px;padding:2px 7px;color:#d5dae3;font-size:12px;white-space:nowrap}
.mmf-badge.warn{border-color:#9c7a2f;color:#ffd98a}
.mmf-badge.error{border-color:#a54848;color:#ffb1b1}
.mmf-directory-evidence.verified{border-color:#2f8f4e;color:#79dc91}
.mmf-directory-evidence.manual{border-color:#53749d;color:#9cc7ff}
.mmf-directory-evidence.warning{border-color:#9c7a2f;color:#ffd98a}
.mmf-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.mmf-queue-control-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.mmf-queue-control-row .mmf-actions{justify-content:flex-end;margin-left:auto}
.mmf-speed{color:#abb2bf;font-size:12px;white-space:nowrap}
.mmf-status{margin-left:auto;color:#b8bec8;font-size:13px}
.mmf-status.mmf-error,.mmf-task-error{color:#ff9c9c}
.mmf-empty{padding:16px;border:1px dashed #4a4d55;border-radius:8px;color:#aeb5bf;text-align:center}
.mmf-progress{height:8px;background:#18191b;border-radius:999px;overflow:hidden}
.mmf-progress-bar{height:100%;background:#4f9cff;transition:width .2s ease}
.mmf-task-error{font-size:12px;line-height:1.4}
.mmf-field-label{color:#cbd1da;font-size:12px}
.mmf-source-section{display:flex;width:max-content;min-width:0;max-width:none;flex-direction:column;align-items:flex-end;gap:4px;overflow:visible}
.mmf-source-options{display:flex;gap:4px;align-items:center;justify-content:flex-end;flex-wrap:nowrap;white-space:nowrap}
.mmf-dialog button.mmf-source-option{display:inline-flex;align-items:center;justify-content:center;gap:4px;min-height:28px;padding:4px 7px;border:1px solid #515662;border-radius:6px;background:#34373d;color:#f5f5f5;font-size:12px;line-height:1;vertical-align:middle;white-space:nowrap;cursor:pointer}
.mmf-dialog button.mmf-source-option:hover{background:#414650}
.mmf-dialog button.mmf-source-option.active{background:#1f6feb;border-color:#2f81f7}
.mmf-dialog button.mmf-source-option.is-resolving{
	background:linear-gradient(110deg,#34373d 0%,#536070 45%,#34373d 90%);
	background-size:220% 100%;
	border-color:#6f7f93;
	cursor:wait;
	opacity:1;
	animation:mmf-parse-background 1.1s linear infinite;
}
.mmf-dialog button.mmf-source-option.is-warning{border-color:#9c7a2f}
.mmf-dialog button.mmf-source-option.is-error{border-color:#a54848}
.mmf-dialog button.mmf-source-option.is-not-found{border-color:#8b742e;color:#e5cf83}
.mmf-dialog button.mmf-source-option.disabled{opacity:.42;cursor:not-allowed}
.mmf-source-option-label{display:inline-flex;height:13px;align-items:center;line-height:13px}
.mmf-source-status-slot{display:inline-flex;width:13px;min-width:13px;height:13px;align-items:center;justify-content:center;line-height:1}
.mmf-source-loading{color:#aeb8c7;font-size:11px}
.mmf-source-retry{color:#e5cf83;font-size:11px}
.mmf-source-retry.is-error{color:#ff8181}
.mmf-source-found{color:#56d364;font-size:11px}
.mmf-source-warning{color:#f4c542;font-size:13px}
.mmf-source-warning.is-error{color:#ff6b6b}
.mmf-source-selected{display:block;width:100%;max-width:100%;color:#aeb5bf;font-size:11px;line-height:1.25;overflow:hidden;text-align:right;text-overflow:ellipsis;white-space:nowrap}
.mmf-card-link{text-align:left}
.mmf-manual-url[hidden]{display:none}
.mmf-path-row{display:grid;grid-template-columns:minmax(140px,.6fr) minmax(240px,1.4fr);gap:10px;align-items:end}
.mmf-dialog label{display:flex;flex-direction:column;gap:5px;color:#cbd1da;font-size:12px}
.mmf-dialog input,.mmf-dialog select,.mmf-dialog textarea{min-height:30px;background:#17181b;color:#f5f5f5;border:1px solid #444852;border-radius:6px;padding:6px 8px;box-sizing:border-box;width:100%}
.mmf-dialog button{background:#363941;color:#f5f5f5;border:1px solid #515662;border-radius:6px;padding:7px 10px;cursor:pointer}
.mmf-dialog button:hover{background:#414650}
.mmf-dialog button:active{transform:translateY(1px) scale(.975);filter:brightness(1.12);transition:transform 45ms ease,filter 45ms ease}
.mmf-dialog button:disabled{cursor:not-allowed;opacity:.55;transform:none;filter:none}
.mmf-dialog button.mmf-primary{background:#1f6feb;border-color:#2f81f7}
.mmf-dialog button.mmf-primary:hover{background:#2b7df5}
.mmf-dialog button.mmf-parse-button{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-width:72px}
.mmf-dialog button.mmf-parse-button.is-loading{
	background:linear-gradient(110deg,#1f6feb 0%,#56a0ff 45%,#1f6feb 90%);
	background-size:220% 100%;
	border-color:#69adff;
	cursor:wait;
	animation:mmf-parse-background 1.1s linear infinite;
}
.mmf-parse-spinner{animation:mmf-parse-spin .8s linear infinite}
@keyframes mmf-parse-background{to{background-position:-220% 0}}
@keyframes mmf-parse-spin{to{transform:rotate(360deg)}}
.mmf-select{position:relative;width:100%;min-width:0}
.mmf-dialog button.mmf-select-trigger{display:flex;width:100%;min-height:30px;align-items:center;justify-content:space-between;gap:8px;padding:5px 9px;background:#17181b;border-color:#444852;text-align:left}
.mmf-select.open .mmf-select-trigger{border-color:#2f81f7;box-shadow:0 0 0 1px #2f81f7}
.mmf-select-value{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mmf-select-menu{position:absolute;z-index:30;left:0;right:0;top:calc(100% + 5px);max-height:210px;overflow:auto;padding:5px;background:#202328;border:1px solid #515866;border-radius:7px;box-shadow:0 12px 32px rgba(0,0,0,.5)}
.mmf-select-menu[hidden]{display:none}
.mmf-dialog button.mmf-select-option{display:flex;width:100%;align-items:center;justify-content:space-between;gap:8px;padding:7px 8px;border:0;background:transparent;text-align:left}
.mmf-dialog button.mmf-select-option:hover{background:#343942}
.mmf-dialog button.mmf-select-option.selected{background:#1f4f82;color:#fff}
.mmf-select-option span:first-child{min-width:0;overflow-wrap:anywhere}
.mmf-select-option-note{flex:0 0 auto;color:#ffb1b1;font-size:11px}
.setting-item:has(.mmf-settings-provider){margin-bottom:4px}
.setting-item:has(.mmf-settings-provider) > .flex > .form-label{flex:0 0 294px}
.setting-item:has(.mmf-settings-provider) > .flex > .form-input{flex:1 1 auto;width:auto}
.setting-item:has(.mmf-settings-provider) > .flex > .form-input > div{width:100%}
.setting-item:has(.mmf-proxy-settings) > .flex{display:block;width:100%}
.setting-item:has(.mmf-proxy-settings) > .flex > .form-label{display:none}
.setting-item:has(.mmf-proxy-settings) > .flex > .form-input{display:block;width:100%;max-width:none;min-width:0}
.mmf-settings-provider{display:flex;flex-direction:column;gap:5px;width:100%;padding:2px 0 4px}
.mmf-settings-input-row{display:grid;grid-template-columns:20px minmax(0,1fr);gap:7px;align-items:center}
.mmf-settings-provider input{height:32px;min-height:32px;background:#17181b;color:#f5f5f5;border:1px solid #444852;border-radius:6px;padding:5px 9px;box-sizing:border-box;width:100%}
.mmf-settings-provider input:focus{border-color:#2f81f7;outline:1px solid #2f81f7;outline-offset:-1px}
.mmf-settings-provider button{height:30px;min-height:30px;background:#363941;color:#f5f5f5;border:1px solid #515662;border-radius:6px;padding:4px 9px;cursor:pointer;width:100%}
.mmf-settings-provider button:hover{background:#414650}
.mmf-settings-provider button:active{transform:translateY(1px) scale(.975);filter:brightness(1.12)}
.mmf-settings-provider button.mmf-primary{background:#1f6feb;border-color:#2f81f7}
.mmf-settings-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;align-items:center;margin-left:27px}
.mmf-proxy-settings{gap:10px;max-width:none;padding:0 0 6px}
.mmf-proxy-modes{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding:2px 0}
.mmf-proxy-mode{display:flex!important;align-items:center;gap:7px!important;color:#cbd1da!important;font-size:12px}
.mmf-settings-provider .mmf-proxy-mode input{width:16px;height:16px;min-height:16px;flex:0 0 16px;margin:0;padding:0}
.mmf-proxy-custom{display:flex;flex-direction:column;gap:7px}
.mmf-proxy-list{display:flex;flex-direction:column;gap:6px}
.mmf-proxy-row{display:grid;grid-template-columns:18px minmax(0,1fr) auto auto;align-items:center;gap:7px;padding:7px;border:1px solid #444852;border-radius:6px;background:#202226}
.mmf-settings-provider .mmf-proxy-row>input{width:16px;height:16px;min-height:16px;margin:0;padding:0}
.mmf-proxy-row-copy{display:flex;min-width:0;flex-direction:column;gap:2px}
.mmf-proxy-row-copy strong,.mmf-proxy-row-copy span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mmf-proxy-row-copy span{color:#aeb5bf;font-size:11px}
.mmf-proxy-row-copy .mmf-proxy-health{font-size:11px}
.mmf-proxy-health.checking{color:#d9b44a}
.mmf-proxy-health.available{color:#41c76f}
.mmf-proxy-health.unavailable{color:#ff7b7b}
.mmf-settings-provider button.mmf-proxy-row-action{width:auto;min-width:44px}
.mmf-proxy-empty{padding:9px;border:1px dashed #4a4d55;border-radius:6px;color:#aeb5bf;text-align:center;font-size:12px}
.mmf-settings-provider button.mmf-proxy-add{width:auto;margin-left:auto;flex:0 0 auto}
.mmf-proxy-editor{display:flex;flex-direction:column;gap:6px;padding:8px;border:1px solid #515662;border-radius:6px}
.mmf-proxy-editor[hidden]{display:none}
.mmf-proxy-address{display:grid;grid-template-columns:110px minmax(0,1fr) 90px;gap:6px}
.mmf-proxy-editor-actions{display:flex;justify-content:flex-end;gap:6px}
.mmf-settings-provider .mmf-proxy-editor-actions button{width:auto;min-width:76px}
.mmf-proxy-status{min-height:18px;color:#aeb5bf;font-size:12px;line-height:18px}
.mmf-proxy-heading{display:flex;align-items:center;gap:10px}
.mmf-proxy-heading .mmf-proxy-heading-status{min-height:0;font-size:12px;font-weight:400;line-height:1.2}
.mmf-error-text{color:#ff9c9c!important}
.mmf-credential-status{font-size:16px;text-align:center;cursor:help}
.mmf-credential-status.is-success{color:#3fb950}
.mmf-credential-status.is-error{color:#f85149}
.mmf-credential-status.is-warning{color:#d29922}
.mmf-action-hidden{display:none!important}
button.mmf-credential-warning-action{color:#d29922!important}
button.mmf-missing-models-action.is-missing{background:#b33a3a!important;color:#fff!important}
button.mmf-missing-models-action.is-missing:hover{background:#c44747!important}
button.mmf-missing-models-action.is-missing i,
button.mmf-missing-models-action.is-missing span{color:#fff!important}
button.mmf-missing-models-action:active,
button.mmf-credential-warning-action:active{transform:translateY(1px) scale(.975);filter:brightness(1.12)}
button.mmf-missing-models-action.is-missing::after{
	content:"";
	display:block;
	width:16px;
	height:16px;
	flex:0 0 16px;
	background:#fff;
	-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='24' height='24'%3E%3Cg fill='none' stroke='black' stroke-linecap='round' stroke-linejoin='round' stroke-width='2'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 16v-4m0-4h.01'/%3E%3C/g%3E%3C/svg%3E") center/contain no-repeat;
	mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='24' height='24'%3E%3Cg fill='none' stroke='black' stroke-linecap='round' stroke-linejoin='round' stroke-width='2'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 16v-4m0-4h.01'/%3E%3C/g%3E%3C/svg%3E") center/contain no-repeat;
}
@media(max-width:1100px){.setting-item:has(.mmf-settings-provider) > .flex > .form-label{flex-basis:240px}}
@media(max-width:1280px){.mmf-dialog{width:min(1120px,96vw)}.mmf-columns{grid-template-columns:minmax(0,2fr) minmax(260px,.9fr)}.mmf-columns section{padding:12px}.mmf-card-top{grid-template-columns:minmax(0,1fr) max-content;gap:8px}.mmf-dialog button.mmf-source-option{padding-inline:6px}.mmf-tabs-row{grid-template-columns:auto minmax(120px,1fr) auto}.mmf-tab-hint{font-size:11px}}
@media(max-width:1050px){.mmf-columns{grid-template-columns:minmax(0,1.65fr) minmax(250px,1fr)}.mmf-card-top{grid-template-columns:1fr}.mmf-source-section{width:100%;align-items:flex-start}.mmf-source-options{justify-content:flex-start}.mmf-card-head strong{max-width:100%;overflow:hidden;text-overflow:ellipsis}.mmf-tabs-row{display:flex;align-items:flex-start;flex-wrap:wrap}.mmf-tab-hint{order:3;flex:1 1 100%}.mmf-tab-actions{margin-left:auto}}
@media(max-width:900px){.mmf-columns{grid-template-columns:1fr}.mmf-columns section:first-child{border-right:0;border-bottom:1px solid #34363a}.mmf-dialog{height:94vh}.mmf-status{margin-left:0}.mmf-main-actions{align-items:stretch;flex-direction:column}.mmf-toolbar-buttons{justify-content:flex-start}.mmf-tabs-row{display:flex;align-items:flex-start;flex-direction:column}.mmf-tab-actions{justify-content:flex-start;margin-left:0}.mmf-card-top{grid-template-columns:1fr}.mmf-source-section{min-width:0;max-width:none;align-items:flex-start}.mmf-source-options{justify-content:flex-start}.mmf-path-row{grid-template-columns:1fr}}
`,
		})
	);
}

function openDialog() {
	if (!dialog) dialog = new MissingModelsDialog();
	dialog.show();
}

function scheduleMissingModelsPrompt() {
	if (workflowPromptTimer) clearTimeout(workflowPromptTimer);
	workflowPromptTimer = setTimeout(async () => {
		workflowPromptTimer = null;
		try {
			const workflow = currentWorkflow();
			const signature = JSON.stringify(workflow);
			if (signature === lastPromptedWorkflowSignature) return;
			const [scanData, queueData] = await Promise.all([
				jsonFetch(`${API_PREFIX}/scan`, {
					method: "POST",
					body: JSON.stringify({ workflow }),
				}),
				jsonFetch(`${API_PREFIX}/downloads`),
			]);
			if (JSON.stringify(currentWorkflow()) !== signature) return;
			const allMissingModels = onlyMissingModels(scanData.models);
			updateMissingModelsWarning(allMissingModels);
			const activeTasks = new Set(
				(queueData.queue?.tasks || [])
					.filter((task) => ["queued", "downloading", "verifying", "paused", "completed"].includes(task.status))
					.map((task) => `${task.directory}|${task.name}`)
			);
			const missingModels = allMissingModels.filter(
				(model) => !activeTasks.has(`${model.directory}|${model.name}`)
			);
			if (!missingModels.length) return;
			lastPromptedWorkflowSignature = signature;
			if (!dialog) dialog = new MissingModelsDialog();
			dialog.showScanResult(missingModels, signature);
		} catch (error) {
			console.debug("[Missing Models Fetcher] Workflow scan unavailable", error);
		}
	}, 600);
}

app.registerExtension({
	name: EXTENSION_NAME,
	actionBarButtons: [
		missingModelsActionButton,
		credentialWarningActionButton,
	],
	commands: [
		{
			id: OPEN_COMMAND_ID,
			label: "缺失模型下载",
			icon: "pi pi-download",
			function: openDialog,
		},
	],
	menuCommands: [
		{
			path: ["Extensions", "Missing Models Fetcher"],
			commands: [OPEN_COMMAND_ID],
		},
	],
	settings: [
		{
			id: "MMFetcher.API凭据 · 凭据仅保存在 ComfyUI 用户目录.1Civitai",
			name: "Civitai API 密钥",
			defaultValue: null,
			type: () => createProviderApiKeyControl("civitai"),
		},
		{
			id: "MMFetcher.API凭据 · 凭据仅保存在 ComfyUI 用户目录.2ModelScope",
			name: "魔搭 ModelScope 访问令牌",
			defaultValue: null,
			type: () => createProviderApiKeyControl("modelscope"),
		},
		{
			id: "MMFetcher.API凭据 · 凭据仅保存在 ComfyUI 用户目录.3HuggingFace",
			name: "Hugging Face 访问令牌",
			defaultValue: null,
			type: () => createProviderApiKeyControl("hf"),
		},
		{
			id: "MMFetcher.网络代理.1Proxy",
			name: "代理配置",
			defaultValue: null,
			type: createProxySettingsControl,
		},
	],
	init() {
		injectStyles();
	},
	setup() {
		injectStyles();
		void refreshCredentialHealth(true);
		if (credentialHealthTimer) clearInterval(credentialHealthTimer);
		credentialHealthTimer = setInterval(() => {
			void refreshCredentialHealth(false);
		}, 60_000);
	},
	afterConfigureGraph() {
		scheduleMissingModelsPrompt();
	},
});
