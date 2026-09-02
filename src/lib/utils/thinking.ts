export type ThinkingMode = 'off' | 'low' | 'medium' | 'high';

const THINKING_MODE_PARAM = 'thinking_mode';

export const THINKING_MODE_OPTIONS: Array<{
	value: ThinkingMode;
	label: string;
	description: string;
	level: 0 | 1 | 2 | 3;
}> = [
	{
		value: 'off',
		label: 'Off',
		description: 'Fastest · direct answers',
		level: 0
	},
	{
		value: 'low',
		label: 'Low',
		description: 'Quick reasoning · everyday tasks',
		level: 1
	},
	{
		value: 'medium',
		label: 'Medium',
		description: 'Balanced · complex questions',
		level: 2
	},
	{
		value: 'high',
		label: 'High',
		description: 'Deep reasoning · slowest',
		level: 3
	}
];

const MODE_TO_EFFORT = {
	low: 'low',
	medium: 'medium',
	high: 'xhigh'
} as const;

const toPlainObject = (value: unknown): Record<string, any> => {
	if (typeof value === 'string') {
		try {
			const parsed = JSON.parse(value);
			return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? { ...parsed } : {};
		} catch {
			return {};
		}
	}

	return value && typeof value === 'object' && !Array.isArray(value) ? { ...(value as any) } : {};
};

export const normalizeThinkingMode = (value: unknown): ThinkingMode => {
	if (value === 'low' || value === 'medium' || value === 'high' || value === 'off') {
		return value;
	}
	if (value === 'xhigh') {
		return 'high';
	}
	return 'off';
};

export const getThinkingModeFromParams = (params: any = {}): ThinkingMode => {
	if (params?.[THINKING_MODE_PARAM] !== undefined) {
		return normalizeThinkingMode(params[THINKING_MODE_PARAM]);
	}

	const customParams = toPlainObject(params?.custom_params);
	const templateParams = toPlainObject(customParams?.chat_template_kwargs);

	if (templateParams?.enable_thinking === false) {
		return 'off';
	}

	const effort = templateParams?.reasoning_effort ?? params?.reasoning_effort;
	if (effort === 'xhigh' || effort === 'high') {
		return 'high';
	}
	if (effort === 'medium') {
		return 'medium';
	}
	if (effort === 'low') {
		return 'low';
	}

	return templateParams?.enable_thinking === true ? 'low' : 'off';
};

export const stripThinkingParams = (params: any = {}) => {
	const nextParams = toPlainObject(params);
	delete nextParams[THINKING_MODE_PARAM];
	delete nextParams.reasoning_effort;

	if (nextParams.custom_params !== undefined) {
		const customParams = toPlainObject(nextParams.custom_params);

		if (customParams.chat_template_kwargs !== undefined) {
			const chatTemplateParams = toPlainObject(customParams.chat_template_kwargs);
			delete chatTemplateParams.enable_thinking;
			delete chatTemplateParams.reasoning_effort;
			delete chatTemplateParams.preserve_thinking;

			if (Object.keys(chatTemplateParams).length > 0) {
				customParams.chat_template_kwargs = chatTemplateParams;
			} else {
				delete customParams.chat_template_kwargs;
			}
		}

		if (Object.keys(customParams).length > 0) {
			nextParams.custom_params = customParams;
		} else {
			delete nextParams.custom_params;
		}
	}

	return nextParams;
};

export const stripThinkingModeParam = (params: any = {}) => {
	const nextParams = toPlainObject(params);
	delete nextParams[THINKING_MODE_PARAM];
	return nextParams;
};

export const setThinkingModeInParams = (params: any = {}, mode: ThinkingMode) => ({
	...stripThinkingParams(params),
	[THINKING_MODE_PARAM]: normalizeThinkingMode(mode)
});

export const applyThinkingModeToParams = (params: any = {}, mode: ThinkingMode) => {
	const normalizedMode = normalizeThinkingMode(mode);
	const nextParams = stripThinkingParams(params);
	const customParams = toPlainObject(nextParams.custom_params);
	const chatTemplateParams = toPlainObject(customParams.chat_template_kwargs);

	if (normalizedMode === 'off') {
		chatTemplateParams.enable_thinking = false;
		chatTemplateParams.preserve_thinking = false;
	} else {
		const effort = MODE_TO_EFFORT[normalizedMode];
		nextParams.reasoning_effort = effort;
		chatTemplateParams.enable_thinking = true;
		chatTemplateParams.reasoning_effort = effort;
		chatTemplateParams.preserve_thinking = true;
	}

	customParams.chat_template_kwargs = chatTemplateParams;
	nextParams.custom_params = customParams;
	return nextParams;
};

export const mergeChatParams = (base: any = {}, override: any = {}) => {
	const merged = { ...toPlainObject(base), ...toPlainObject(override) };
	const baseCustom = toPlainObject(base?.custom_params);
	const overrideCustom = toPlainObject(override?.custom_params);
	const baseTemplate = toPlainObject(baseCustom?.chat_template_kwargs);
	const overrideTemplate = toPlainObject(overrideCustom?.chat_template_kwargs);

	if (Object.keys(baseCustom).length || Object.keys(overrideCustom).length) {
		merged.custom_params = {
			...baseCustom,
			...overrideCustom,
			...(Object.keys(baseTemplate).length || Object.keys(overrideTemplate).length
				? {
						chat_template_kwargs: {
							...baseTemplate,
							...overrideTemplate
						}
					}
				: {})
		};
	}

	return merged;
};

export const modelSupportsThinking = (model: any) => {
	if (!model) return false;
	if (model?.info?.meta?.capabilities?.reasoning === false) return false;

	const profile = model?.info?.meta?.reasoning?.profile ?? model?.info?.meta?.reasoning_profile;
	if (profile === 'qwen3') return true;

	const identity = [
		model?.id,
		model?.name,
		model?.info?.name,
		model?.info?.base_model_id,
		model?.info?.base_model
	]
		.filter(Boolean)
		.join(' ')
		.toLowerCase();

	return (
		identity.includes('there-3.8') || identity.includes('there 3.8') || identity.includes('qwen3.8')
	);
};
