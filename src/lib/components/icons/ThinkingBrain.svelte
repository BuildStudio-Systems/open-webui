<script lang="ts">
	export let className = 'w-7 h-5';
	export let level: 0 | 1 | 2 | 3 = 0;
	export let strokeWidth = '1.5';
</script>

<svg
	aria-hidden="true"
	xmlns="http://www.w3.org/2000/svg"
	fill="none"
	viewBox="0 0 30 24"
	stroke="currentColor"
	stroke-width={strokeWidth}
	class={className}
>
	<path
		stroke-linecap="round"
		stroke-linejoin="round"
		d="M10.25 4.1A3.25 3.25 0 0 0 4.4 6.08a3.1 3.1 0 0 0-1.25 5.78 3.7 3.7 0 0 0 2.4 6.07 3.18 3.18 0 0 0 4.7 1.92V4.1Zm0 3.4A2.5 2.5 0 0 0 7.8 5m2.45 8.1a2.8 2.8 0 0 1-2.8-2.8m2.8 6.1a2.5 2.5 0 0 0-2.5 2.5M12.75 4.1a3.25 3.25 0 0 1 5.85 1.98 3.1 3.1 0 0 1 1.25 5.78 3.7 3.7 0 0 1-2.4 6.07 3.18 3.18 0 0 1-4.7 1.92V4.1Zm0 3.4A2.5 2.5 0 0 1 15.2 5m-2.45 8.1a2.8 2.8 0 0 0 2.8-2.8m-2.8 6.1a2.5 2.5 0 0 1 2.5 2.5"
	/>
	<rect
		x="22"
		y="16"
		width="1.8"
		height="4"
		rx="0.9"
		fill="currentColor"
		stroke="none"
		opacity={level >= 1 ? 1 : 0.18}
	/>
	<rect
		x="25"
		y="12"
		width="1.8"
		height="8"
		rx="0.9"
		fill="currentColor"
		stroke="none"
		opacity={level >= 2 ? 1 : 0.18}
	/>
	<rect
		x="28"
		y="8"
		width="1.8"
		height="12"
		rx="0.9"
		fill="currentColor"
		stroke="none"
		opacity={level >= 3 ? 1 : 0.18}
	/>
</svg>
