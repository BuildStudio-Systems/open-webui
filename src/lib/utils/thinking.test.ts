import { describe, expect, it } from 'vitest';

import {
	applyThinkingModeToParams,
	getThinkingModeFromParams,
	mergeChatParams,
	modelSupportsThinking,
	normalizeThinkingMode,
	preserveThinkingModeForCreatedChat,
	resolveChatThinkingMode,
	setThinkingModeInParams,
	stripThinkingModeParam,
	stripThinkingParams
} from './thinking';

describe('thinking mode parameters', () => {
	it('defaults an unconfigured new chat to Fast (thinking off)', () => {
		expect(resolveChatThinkingMode()).toBe('off');
		expect(resolveChatThinkingMode({ params: { temperature: 0.7 } })).toBe('off');
	});

	it.each(['low', 'medium', 'high'])(
		'does not restore %s from the unscoped home-page draft into a new chat',
		(draftMode) => {
			expect(resolveChatThinkingMode({ params: {}, draftMode, hasChatId: false })).toBe('off');
		}
	);

	it.each(['off', 'low', 'medium', 'high'] as const)(
		'restores %s from the current existing chat draft',
		(draftMode) => {
			expect(
				resolveChatThinkingMode({
					params: { thinking_mode: 'medium' },
					draftMode,
					hasChatId: true
				})
			).toBe(draftMode);
		}
	);

	it('keeps the current chat selection when there is no scoped draft value', () => {
		expect(resolveChatThinkingMode({ params: { thinking_mode: 'high' }, hasChatId: true })).toBe(
			'high'
		);
		expect(
			resolveChatThinkingMode({
				params: { thinking_mode: 'low' },
				draftMode: null,
				hasChatId: true
			})
		).toBe('low');
	});

	it.each([
		{ thinking_mode: 'high' },
		{ reasoning_effort: 'medium' },
		{
			custom_params: {
				chat_template_kwargs: {
					enable_thinking: true,
					reasoning_effort: 'xhigh',
					preserve_thinking: true
				}
			}
		}
	])('keeps new chat requests off despite an older global setting: %j', (globalParams) => {
		const chatParams = {};
		const requestParams = applyThinkingModeToParams(
			mergeChatParams(globalParams, chatParams),
			resolveChatThinkingMode({ params: chatParams, globalParams })
		);

		expect(requestParams.reasoning_effort).toBeUndefined();
		expect(requestParams.thinking_mode).toBeUndefined();
		expect(requestParams.custom_params.chat_template_kwargs).toEqual({
			enable_thinking: false,
			preserve_thinking: false
		});
	});

	it.each(['off', 'low', 'medium', 'high'] as const)(
		'restores an existing chat explicit %s selection independently of global defaults',
		(mode) => {
			const chatParams = setThinkingModeInParams({ temperature: 0.7 }, mode);
			const restoredMode = resolveChatThinkingMode({
				params: chatParams,
				globalParams: { thinking_mode: 'high', reasoning_effort: 'xhigh' }
			});
			const requestParams = applyThinkingModeToParams(
				mergeChatParams({ thinking_mode: 'high', reasoning_effort: 'xhigh' }, chatParams),
				restoredMode
			);

			expect(restoredMode).toBe(mode);
			expect(getThinkingModeFromParams(requestParams)).toBe(mode);
			expect(requestParams.temperature).toBe(0.7);
		}
	);

	it('retains legacy thinking parameters saved on an existing chat', () => {
		expect(getThinkingModeFromParams({ reasoning_effort: 'xhigh' })).toBe('high');
		expect(
			getThinkingModeFromParams({
				custom_params: { chat_template_kwargs: { enable_thinking: true } }
			})
		).toBe('low');
	});

	it.each(['off', 'low', 'medium', 'high'] as const)(
		'keeps the local %s slider choice when the embedded chat creation response arrives',
		(mode) => {
			const createdParams = { temperature: 0.8, thinking_mode: 'medium', note_id: 'note-1' };
			const localParams = { temperature: 0.3, thinking_mode: mode, local_only: true };
			const restoredParams = preserveThinkingModeForCreatedChat(createdParams, localParams);

			expect(restoredParams).toEqual({ ...createdParams, thinking_mode: mode });
			expect(createdParams.thinking_mode).toBe('medium');
			expect(resolveChatThinkingMode({ params: restoredParams })).toBe(mode);
		}
	);

	it('does not override server params without an explicit local slider choice', () => {
		const createdParams = { temperature: 0.8, note_id: 'note-1' };
		expect(preserveThinkingModeForCreatedChat(createdParams, { temperature: 0.3 })).toEqual(
			createdParams
		);
	});

	it('normalizes the Qwen xhigh effort to the customer-facing High mode', () => {
		expect(normalizeThinkingMode('xhigh')).toBe('high');
		expect(normalizeThinkingMode('invalid')).toBe('off');
	});

	it('limits the selector to the configured There/Qwen 3.8 profile', () => {
		expect(modelSupportsThinking({ name: 'There-3.8' })).toBe(true);
		expect(
			modelSupportsThinking({
				name: 'Custom workspace model',
				info: { meta: { reasoning: { profile: 'qwen3' } } }
			})
		).toBe(true);
		expect(
			modelSupportsThinking({
				name: 'Qwen3-8B',
				info: { meta: { capabilities: { reasoning: true } } }
			})
		).toBe(false);
	});

	it.each([
		['low', 'low'],
		['medium', 'medium'],
		['high', 'xhigh']
	] as const)('maps %s to the matching Qwen reasoning effort', (mode, effort) => {
		const params = applyThinkingModeToParams(
			{
				custom_params: {
					top_k: 20,
					chat_template_kwargs: { existing: true }
				}
			},
			mode
		);

		expect(params.reasoning_effort).toBe(effort);
		expect(params.custom_params.top_k).toBe(20);
		expect(params.custom_params.chat_template_kwargs).toEqual({
			existing: true,
			enable_thinking: true,
			reasoning_effort: effort,
			preserve_thinking: true
		});
		expect(getThinkingModeFromParams(params)).toBe(mode);
	});

	it('turns thinking off explicitly without discarding unrelated custom parameters', () => {
		const params = applyThinkingModeToParams(
			{
				reasoning_effort: 'medium',
				custom_params: {
					top_k: 20,
					chat_template_kwargs: { reasoning_effort: 'medium', existing: true }
				}
			},
			'off'
		);

		expect(params.reasoning_effort).toBeUndefined();
		expect(params.custom_params.top_k).toBe(20);
		expect(params.custom_params.chat_template_kwargs).toEqual({
			existing: true,
			enable_thinking: false,
			preserve_thinking: false
		});
		expect(getThinkingModeFromParams(params)).toBe('off');
	});

	it('stores the customer-facing selection without persisting provider-specific fields', () => {
		const params = setThinkingModeInParams(
			{
				reasoning_effort: 'low',
				custom_params: {
					top_k: 20,
					chat_template_kwargs: { enable_thinking: true, existing: true }
				}
			},
			'high'
		);

		expect(params).toEqual({
			thinking_mode: 'high',
			custom_params: { top_k: 20, chat_template_kwargs: { existing: true } }
		});
		expect(getThinkingModeFromParams(params)).toBe('high');
	});

	it('removes every thinking field before sending to an unsupported model', () => {
		const params = stripThinkingParams({
			thinking_mode: 'medium',
			reasoning_effort: 'medium',
			custom_params: {
				top_k: 20,
				chat_template_kwargs: {
					enable_thinking: true,
					reasoning_effort: 'medium',
					preserve_thinking: true,
					existing: true
				}
			}
		});

		expect(params).toEqual({
			custom_params: { top_k: 20, chat_template_kwargs: { existing: true } }
		});
	});

	it('removes only the selector marker for models outside the Qwen profile', () => {
		const params = stripThinkingModeParam({
			thinking_mode: 'high',
			reasoning_effort: 'medium',
			custom_params: { chat_template_kwargs: { provider_option: true } }
		});

		expect(params).toEqual({
			reasoning_effort: 'medium',
			custom_params: { chat_template_kwargs: { provider_option: true } }
		});
	});

	it('deep-merges chat-template parameters before applying a mode', () => {
		const merged = mergeChatParams(
			{
				custom_params: {
					top_k: 20,
					chat_template_kwargs: { preserve_thinking: true, source: 'settings' }
				}
			},
			{
				custom_params: {
					repetition_penalty: 1,
					chat_template_kwargs: { source: 'chat' }
				}
			}
		);

		expect(merged.custom_params).toEqual({
			top_k: 20,
			repetition_penalty: 1,
			chat_template_kwargs: { preserve_thinking: true, source: 'chat' }
		});
	});

	it('parses JSON chat-template parameters instead of spreading string characters', () => {
		const params = applyThinkingModeToParams(
			{
				custom_params: {
					chat_template_kwargs: '{"existing":true,"enable_thinking":false}'
				}
			},
			'low'
		);

		expect(params.custom_params.chat_template_kwargs).toEqual({
			existing: true,
			enable_thinking: true,
			reasoning_effort: 'low',
			preserve_thinking: true
		});
	});
});
