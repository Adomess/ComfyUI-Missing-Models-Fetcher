import assert from "node:assert/strict";
import {
	directoryInferenceStatus,
	isCurrentResolution,
	mergeProviderSources,
	parseCustomLimit,
	resolveCustomLimitInput,
	resolvedModelSize,
	runTasksWithConcurrency,
	setNumericSelectValue,
} from "../web/js/mmf_state.mjs";

assert.deepEqual(directoryInferenceStatus("source_metadata"), {
	level: "verified",
	label: "目录依据：站点元数据",
	detail: "模型目录来自下载站点返回的模型类型元数据。",
});
assert.equal(directoryInferenceStatus("source_path").level, "verified");
assert.equal(directoryInferenceStatus("safetensors_header").level, "verified");
assert.equal(directoryInferenceStatus("explicit").level, "manual");
assert.equal(directoryInferenceStatus("filename").level, "warning");
assert.equal(directoryInferenceStatus("", true).label, "目录待确认");

const model = { sources: [{ provider: "hf", url: "old" }, { provider: "civitai", url: "keep" }] };
mergeProviderSources(model, "hf", [{ provider: "hf", url: "new" }, { provider: "modelscope", url: "ignore" }]);
assert.deepEqual(model.sources, [{ provider: "civitai", url: "keep" }, { provider: "hf", url: "new" }]);

assert.equal(resolvedModelSize([
	{ size: 10, verification: "hash_verified" },
	{ size: 20, verification: "metadata_matched" },
]), 10);
assert.equal(resolvedModelSize([{ size: 10 }, { size: 20 }]), null);

let active = 0;
let peak = 0;
const completionOrder = [];
const delays = [35, 5, 15, 1];
await runTasksWithConcurrency(delays.map((delay, index) => async () => {
	active += 1;
	peak = Math.max(peak, active);
	await new Promise((resolve) => setTimeout(resolve, delay));
	completionOrder.push(index);
	active -= 1;
}), 2);
assert.equal(peak, 2);
assert.notDeepEqual(completionOrder, [0, 1, 2, 3]);

const models = [];
assert.equal(isCurrentResolution(2, 2, models, models), true);
assert.equal(isCurrentResolution(3, 2, models, models), false);
assert.equal(isCurrentResolution(2, 2, [], models), false);

assert.equal(parseCustomLimit("32", 1, 32, true), 32);
assert.equal(parseCustomLimit("16.5", 1, 32, true), null);
assert.equal(parseCustomLimit("750.5", 0, 100000, false), 750.5);
assert.equal(parseCustomLimit("100001", 0, 100000, false), null);
assert.equal(parseCustomLimit("not-a-number", 0, 100000, false), null);
assert.deepEqual(resolveCustomLimitInput(null, 1, 32, true), { status: "canceled", value: null });
assert.deepEqual(resolveCustomLimitInput("16.5", 1, 32, true), { status: "invalid", value: null });
assert.deepEqual(resolveCustomLimitInput("24", 1, 32, true), { status: "accepted", value: 24 });

const customTrigger = { value: "custom", textContent: "自定义…", dataset: {} };
const fakeSelect = {
	options: [{ value: "1", dataset: {} }, customTrigger],
	value: "1",
	querySelector(selector) {
		return selector.includes("data-custom-value")
			? this.options.find((option) => option.dataset?.customValue === "true") || null
			: null;
	},
	insertBefore(option, before) {
		const index = before ? this.options.indexOf(before) : this.options.length;
		this.options.splice(index, 0, option);
	},
};
const optionFactory = (attributes) => ({
	value: attributes.value,
	textContent: attributes.text,
	dataset: { customValue: "true" },
	remove() {
		const index = fakeSelect.options.indexOf(this);
		if (index >= 0) fakeSelect.options.splice(index, 1);
	},
});
setNumericSelectValue(fakeSelect, 24, "", optionFactory);
assert.equal(fakeSelect.value, "24");
assert.deepEqual(fakeSelect.options.map((option) => option.value), ["1", "24", "custom"]);
setNumericSelectValue(fakeSelect, 18, "", optionFactory);
assert.equal(fakeSelect.value, "18");
assert.deepEqual(fakeSelect.options.map((option) => option.value), ["1", "18", "custom"]);

console.log("frontend state ok");
