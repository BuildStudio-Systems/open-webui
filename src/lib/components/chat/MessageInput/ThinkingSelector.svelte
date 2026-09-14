<script lang="ts">
	import { getContext } from 'svelte';
	import Bolt from '$lib/components/icons/Bolt.svelte';
	import {
		THINKING_MODE_OPTIONS,
		normalizeThinkingMode,
		type ThinkingMode
	} from '$lib/utils/thinking';

	const i18n = getContext('i18n');

	export let mode: ThinkingMode = 'off';
	export let disabled = false;
	export let onChange: (mode: ThinkingMode) => void | Promise<void> = () => {};

	const labels = ['Fast', 'Light', 'Medium', 'Advanced'];
	const labelKey = (label: string) => `Thinking level: ${label}`;
	$: activeMode = normalizeThinkingMode(mode);
	$: activeIndex = THINKING_MODE_OPTIONS.findIndex((option) => option.value === activeMode);
	$: activeLabel = $i18n.t(labelKey(labels[activeIndex]), { defaultValue: labels[activeIndex] });
	$: description = $i18n.t(THINKING_MODE_OPTIONS[activeIndex].description);

	const selectLevel = (event: Event) => {
		if (disabled) return;
		const index = (event.currentTarget as HTMLInputElement).valueAsNumber;
		const option = THINKING_MODE_OPTIONS[index];
		if (!option || option.value === activeMode) return;
		mode = option.value;
		void onChange(option.value);
	};
</script>

<div class="thinking-slider" class:disabled data-thinking-mode={activeMode} dir="ltr">
	<div class="thinking-heading" aria-hidden="true">
		<Bolt className="size-3.5" />
		<span class="thinking-label">{activeLabel}</span>
	</div>
	<div class="thinking-range" style:--progress={`${(activeIndex / 3) * 100}%`}>
		<div class="thinking-track" aria-hidden="true">
			{#each THINKING_MODE_OPTIONS as option, index}
				<span
					class="thinking-stop"
					class:filled={index < activeIndex}
					style:left={`${(index / 3) * 100}%`}
				></span>
			{/each}
		</div>
		<input
			type="range"
			min="0"
			max="3"
			step="1"
			value={activeIndex}
			{disabled}
			aria-label={$i18n.t('Thinking level')}
			aria-valuetext={`${activeLabel} · ${description}`}
			title={`${activeLabel} · ${description}`}
			on:input={selectLevel}
			on:keydown={(event) => {
				// Choosing a level must never submit the surrounding message form.
				if (event.key === 'Enter') event.preventDefault();
			}}
		/>
	</div>
	<div class="thinking-levels" aria-hidden="true">
		{#each labels as label, index}
			<span class:active={index === activeIndex}
				>{$i18n.t(labelKey(label), { defaultValue: label })}</span
			>
		{/each}
	</div>
</div>

<style>
	.thinking-slider {
		--slider-accent: #3b82f6;
		--slider-track: #e5e7eb;
		--slider-dot: #9ca3af;
		width: 11rem;
		max-width: 100%;
		box-sizing: border-box;
		padding: 2px 4px 4px;
		color: #6b7280;
		flex-shrink: 0;
	}
	:global(.dark) .thinking-slider {
		--slider-track: #374151;
		--slider-dot: #9ca3af;
		color: #9ca3af;
	}
	.thinking-heading {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 11px;
		line-height: 16px;
	}
	.thinking-label {
		color: #2563eb;
		font-weight: 600;
	}
	:global(.dark) .thinking-label {
		color: #93c5fd;
	}
	.thinking-range {
		position: relative;
		height: 32px;
	}
	.thinking-track {
		position: absolute;
		left: 12px;
		right: 12px;
		top: calc(50% - 9px);
		height: 18px;
		border-radius: 999px;
		background: linear-gradient(
			to right,
			var(--slider-accent) 0%,
			var(--slider-accent) var(--progress),
			var(--slider-track) var(--progress),
			var(--slider-track) 100%
		);
		pointer-events: none;
	}
	.thinking-stop {
		position: absolute;
		top: 7px;
		width: 4px;
		height: 4px;
		border-radius: 50%;
		background: var(--slider-dot);
		transform: translateX(-50%);
	}
	.thinking-stop.filled {
		background: #bfdbfe;
	}
	input[type='range'] {
		position: relative;
		display: block;
		width: 100%;
		height: 32px;
		margin: 0;
		padding: 0;
		appearance: none;
		background: transparent;
		cursor: pointer;
		touch-action: pan-y;
		border-radius: 999px;
	}
	input[type='range']:focus-visible {
		outline: 2px solid var(--slider-accent);
		outline-offset: 1px;
	}
	input[type='range']::-webkit-slider-runnable-track {
		height: 18px;
		background: transparent;
	}
	input[type='range']::-moz-range-track {
		height: 18px;
		background: transparent;
	}
	input[type='range']::-webkit-slider-thumb {
		appearance: none;
		width: 24px;
		height: 24px;
		margin-top: -3px;
		border: 1px solid #d1d5db;
		border-radius: 50%;
		background: #fff;
		box-shadow: 0 1px 3px #0003;
	}
	input[type='range']::-moz-range-thumb {
		width: 22px;
		height: 22px;
		border: 1px solid #d1d5db;
		border-radius: 50%;
		background: #fff;
		box-shadow: 0 1px 3px #0003;
	}
	.thinking-levels {
		display: flex;
		justify-content: space-between;
		font-size: 10px;
		line-height: 14px;
	}
	.thinking-levels .active {
		color: #2563eb;
		font-weight: 600;
	}
	:global(.dark) .thinking-levels .active {
		color: #93c5fd;
	}
	.disabled {
		opacity: 0.5;
	}
	input[type='range']:disabled {
		cursor: not-allowed;
	}
	@media (pointer: coarse) {
		/* A larger touch target without enlarging the visible track or thumb. */
		.thinking-range,
		input[type='range'] {
			height: 44px;
		}
	}
	@media (forced-colors: active) {
		.thinking-track {
			background: Canvas;
			border: 1px solid CanvasText;
		}
		.thinking-stop,
		.thinking-stop.filled {
			background: CanvasText;
		}
		input[type='range'] {
			appearance: auto;
		}
		.thinking-label,
		.thinking-levels .active {
			color: Highlight;
		}
	}
</style>
