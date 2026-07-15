export function resolvedModelSize(sources) {
	const available = (Array.isArray(sources) ? sources : [])
		.filter((source) => source?.selectable !== false)
		.map((source) => ({
			size: Number(source?.size),
			verification: source?.verification || "",
		}))
		.filter((source) => Number.isFinite(source.size) && source.size > 0);
	if (!available.length) return null;
	const verifiedSizes = new Set(
		available.filter((source) => source.verification === "hash_verified").map((source) => source.size)
	);
	if (verifiedSizes.size === 1) return [...verifiedSizes][0];
	const allSizes = new Set(available.map((source) => source.size));
	return allSizes.size === 1 ? [...allSizes][0] : null;
}

export function directoryInferenceStatus(inference, needsDirectory = false) {
	const normalized = String(inference || "").trim();
	if (needsDirectory || !normalized) {
		return {
			level: "warning",
			label: "目录待确认",
			detail: "无法从多个来源得到一致的模型目录，请手动选择。",
		};
	}
	const statuses = {
		explicit: {
			level: "manual",
			label: "目录依据：手动选择",
			detail: "模型目录由用户或工作流明确指定。",
		},
		source_metadata: {
			level: "verified",
			label: "目录依据：站点元数据",
			detail: "模型目录来自下载站点返回的模型类型元数据。",
		},
		source_path: {
			level: "verified",
			label: "目录依据：仓库路径",
			detail: "模型目录根据仓库中的完整文件路径确定。",
		},
		safetensors_header: {
			level: "verified",
			label: "目录依据：文件结构",
			detail: "模型目录根据 safetensors 文件头中的张量结构确定。",
		},
		filename: {
			level: "warning",
			label: "目录依据：文件名推测",
			detail: "仅根据文件名推测模型目录，请在下载前确认。",
		},
	};
	return statuses[normalized] || {
		level: "warning",
		label: "目录依据：兼容推断",
		detail: "模型目录由兼容规则推断，请在下载前确认。",
	};
}

export function mergeProviderSources(model, provider, incomingSources) {
	const incoming = (Array.isArray(incomingSources) ? incomingSources : [])
		.filter((source) => source?.provider === provider);
	if (!incoming.length) return;
	const retained = (Array.isArray(model.sources) ? model.sources : [])
		.filter((source) => source?.provider !== provider);
	model.sources = [...retained, ...incoming];
}

export async function runTasksWithConcurrency(tasks, concurrency = 6) {
	let nextIndex = 0;
	const workerCount = Math.min(Math.max(1, concurrency), tasks.length);
	const workers = Array.from({ length: workerCount }, async () => {
		while (nextIndex < tasks.length) {
			const index = nextIndex;
			nextIndex += 1;
			await tasks[index]();
		}
	});
	await Promise.all(workers);
}

export function isCurrentResolution(currentGeneration, expectedGeneration, currentModels, expectedModels) {
	return currentGeneration === expectedGeneration && currentModels === expectedModels;
}

export function parseCustomLimit(raw, min, max, integer = false) {
	const parsed = Number(String(raw ?? "").trim());
	if (!Number.isFinite(parsed) || parsed < min || parsed > max) return null;
	if (integer && !Number.isInteger(parsed)) return null;
	return parsed;
}

export function resolveCustomLimitInput(raw, min, max, integer = false) {
	if (raw === null) return { status: "canceled", value: null };
	const value = parseCustomLimit(raw, min, max, integer);
	return value === null
		? { status: "invalid", value: null }
		: { status: "accepted", value };
}

export function setNumericSelectValue(select, value, suffix = "", optionFactory = null) {
	if (!select) return;
	const normalized = String(value);
	select.querySelector('option[data-custom-value="true"]')?.remove();
	if (![...select.options].some((option) => option.value === normalized)) {
		const attributes = {
			value: normalized,
			text: `${normalized}${suffix}`,
			"data-custom-value": "true",
		};
		let customOption;
		if (optionFactory) {
			customOption = optionFactory(attributes);
		} else {
			customOption = document.createElement("option");
			customOption.value = attributes.value;
			customOption.textContent = attributes.text;
			customOption.dataset.customValue = "true";
		}
		const customTrigger = [...select.options].find((option) => option.value === "custom");
		select.insertBefore(customOption, customTrigger || null);
	}
	select.value = normalized;
}
