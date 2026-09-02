<script lang="ts">
	import { getContext, tick } from 'svelte';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import DropdownMenu from '$lib/components/common/DropdownMenu.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Check from '$lib/components/icons/Check.svelte';
	import ThinkingBrain from '$lib/components/icons/ThinkingBrain.svelte';
	import {
		THINKING_MODE_OPTIONS,
		type ThinkingMode,
		normalizeThinkingMode
	} from '$lib/utils/thinking';

	const i18n = getContext('i18n');

	export let mode: ThinkingMode = 'off';
	export let disabled = false;
	export let onChange: (mode: ThinkingMode) => void | Promise<void> = () => {};

	let show = false;
	let menuElement: HTMLDivElement | null = null;
	$: activeMode = normalizeThinkingMode(mode);
	$: activeOption =
		THINKING_MODE_OPTIONS.find((option) => option.value === activeMode) ?? THINKING_MODE_OPTIONS[0];
	$: activeLabel = $i18n.t(activeOption.label);
	$: if (disabled) show = false;

	const focusMode = async () => {
		await tick();
		menuElement?.querySelector<HTMLButtonElement>(`[data-thinking-mode="${activeMode}"]`)?.focus();
	};

	const handleOptionKeydown = (event: KeyboardEvent, index: number) => {
		if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
		event.preventDefault();

		const options = Array.from(
			menuElement?.querySelectorAll<HTMLButtonElement>('[data-thinking-mode]:not(:disabled)') ?? []
		);
		if (options.length === 0) return;

		const nextIndex =
			event.key === 'Home'
				? 0
				: event.key === 'End'
					? options.length - 1
					: (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
		options[nextIndex]?.focus();
	};

	const selectMode = async (nextMode: ThinkingMode) => {
		if (disabled) return;
		mode = nextMode;
		show = false;
		await onChange(nextMode);
	};
</script>

<Dropdown
	bind:show
	side="top"
	align="start"
	sideOffset={7}
	visualViewportAware
	onOpenChange={(open) => {
		if (open) void focusMode();
	}}
>
	<Tooltip content={$i18n.t('Thinking: {{MODE}}', { MODE: activeLabel })} touch={false} as="span">
		<button
			type="button"
			id="thinking-mode-button"
			{disabled}
			class="group flex h-[1.875rem] shrink-0 items-center gap-1 rounded-full border px-1.5 transition-colors focus:outline-hidden {activeMode ===
			'off'
				? 'border-transparent bg-transparent text-gray-500 hover:bg-gray-50 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200'
				: 'border-sky-200/50 bg-sky-50 text-sky-600 hover:bg-sky-100 dark:border-sky-500/25 dark:bg-sky-400/10 dark:text-sky-300 dark:hover:bg-sky-500/15'} {disabled
				? 'cursor-not-allowed opacity-50'
				: ''}"
			aria-label={$i18n.t('Thinking: {{MODE}}', { MODE: activeLabel })}
			aria-pressed={activeMode !== 'off'}
		>
			<ThinkingBrain
				className="h-[1.125rem] w-[1.45rem]"
				level={activeOption.level}
				strokeWidth="1.65"
			/>
			<span class="hidden text-[0.6875rem] font-medium leading-none @md:inline">
				{activeLabel}
			</span>
		</button>
	</Tooltip>

	<div slot="content" bind:this={menuElement}>
		<DropdownMenu className="w-64 overflow-hidden p-1!">
			<div class="px-2 pb-1.5 pt-1">
				<div
					class="text-[0.6875rem] font-semibold uppercase tracking-[0.12em] text-gray-400 dark:text-gray-500"
				>
					{$i18n.t('Thinking mode')}
				</div>
				<div class="mt-0.5 text-[0.6875rem] leading-4 text-gray-500 dark:text-gray-400">
					{$i18n.t('Choose speed or reasoning depth for this chat.')}
				</div>
			</div>

			<div class="space-y-0.5">
				{#each THINKING_MODE_OPTIONS as option, index}
					<button
						type="button"
						role="menuitemradio"
						aria-checked={activeMode === option.value}
						aria-disabled={disabled}
						{disabled}
						data-thinking-mode={option.value}
						class="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors {activeMode ===
						option.value
							? 'bg-sky-50 text-sky-700 dark:bg-sky-400/10 dark:text-sky-200'
							: 'text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-800'} disabled:cursor-not-allowed disabled:opacity-50"
						on:click={() => selectMode(option.value)}
						on:keydown={(event) => handleOptionKeydown(event, index)}
					>
						<div
							class="flex h-8 w-10 shrink-0 items-center justify-center rounded-lg {activeMode ===
							option.value
								? 'bg-sky-100/80 dark:bg-sky-400/10'
								: 'bg-gray-50 dark:bg-gray-800'}"
						>
							<ThinkingBrain className="h-5 w-7" level={option.level} strokeWidth="1.55" />
						</div>
						<div class="min-w-0 flex-1">
							<div class="text-[0.8125rem] font-medium leading-4">{$i18n.t(option.label)}</div>
							<div class="text-[0.6875rem] leading-4 text-gray-500 dark:text-gray-400">
								{$i18n.t(option.description)}
							</div>
						</div>
						{#if activeMode === option.value}
							<Check className="size-4 shrink-0" strokeWidth="2" />
						{/if}
					</button>
				{/each}
			</div>
		</DropdownMenu>
	</div>
</Dropdown>
