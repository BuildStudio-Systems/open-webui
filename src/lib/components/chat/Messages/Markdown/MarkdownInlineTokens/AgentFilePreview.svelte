<script lang="ts">
	// In-chat preview of an Agent-delivered file (2026-10-11): images, PDFs and text render
	// inline from an authenticated same-origin fetch; other kinds show a file card. The
	// download links stay beside the link in the parent; this component never navigates.
	import { getContext, onDestroy } from 'svelte';
	import {
		PreviewTooLarge,
		fetchPreview,
		formatBytes,
		previewPlan,
		previewText,
		type PreviewPlan
	} from '$lib/utils/agent-file-preview';

	export let href: string;
	export let done = true;

	const i18n = getContext('i18n');
	const copy = {
		en: {
			loading: 'Loading preview…',
			tooLarge: 'Too large to preview here; use the download links.',
			failed: 'Preview unavailable; use the download links.',
			unavailable: 'Preview unavailable (link expired or access denied).',
			truncated: 'Showing the first part of the text; download for the full file.',
			hide: 'Hide preview',
			show: 'Show preview',
			noPreview: 'No inline preview for this file type.',
			pdfFallback: 'Your browser did not render the PDF inline; use the download links.'
		},
		zh: {
			loading: '正在加载预览…',
			tooLarge: '文件过大，无法在此预览，请使用下载链接。',
			failed: '预览不可用，请使用下载链接。',
			unavailable: '预览不可用（链接已过期或无权访问）。',
			truncated: '仅显示文本开头部分，完整内容请下载。',
			hide: '收起预览',
			show: '展开预览',
			noPreview: '该文件类型不支持内嵌预览。',
			pdfFallback: '浏览器未能内嵌显示 PDF，请使用下载链接。'
		},
		ja: {
			loading: 'プレビューを読み込み中…',
			tooLarge: 'サイズが大きいためここでは表示できません。ダウンロードをご利用ください。',
			failed: 'プレビューを表示できません。ダウンロードをご利用ください。',
			unavailable: 'プレビューを表示できません（リンク期限切れまたはアクセス不可）。',
			truncated: 'テキストの先頭部分のみ表示しています。全文はダウンロードしてください。',
			hide: 'プレビューを閉じる',
			show: 'プレビューを表示',
			noPreview: 'このファイル種別は埋め込み表示に対応していません。',
			pdfFallback: 'ブラウザーで PDF を埋め込み表示できませんでした。ダウンロードをご利用ください。'
		}
	};
	$: language = (($i18n?.language as string | undefined) ?? 'en').toLowerCase();
	$: t = copy[language.startsWith('zh') ? 'zh' : language.startsWith('ja') ? 'ja' : 'en'];

	let plan: PreviewPlan | null = null;
	let state: 'idle' | 'loading' | 'ready' | 'too-large' | 'failed' | 'unavailable' = 'idle';
	let objectUrl = '';
	let text = '';
	let truncated = false;
	let size = 0;
	let open = true;
	let started = '';
	let controller: AbortController | undefined;

	$: plan = typeof window === 'undefined' ? null : previewPlan(href, window.location.origin);
	// Load once per link, only after the message finished streaming so partial links never fetch.
	$: if (done && plan && plan.kind !== 'other' && started !== href) {
		started = href;
		void load(plan);
	}

	const release = () => {
		if (objectUrl) URL.revokeObjectURL(objectUrl);
		objectUrl = '';
	};

	async function load(current: PreviewPlan) {
		controller?.abort();
		controller = new AbortController();
		release();
		state = 'loading';
		text = '';
		truncated = false;
		try {
			const result = await fetchPreview(
				href,
				window.location.origin,
				localStorage.token ?? '',
				current,
				fetch,
				controller.signal
			);
			if (controller.signal.aborted) return;
			size = result.size;
			if (current.kind === 'text') {
				const decoded = await previewText(result.blob);
				text = decoded.text;
				truncated = decoded.truncated;
			} else {
				objectUrl = URL.createObjectURL(result.blob);
			}
			state = 'ready';
		} catch (error) {
			if (controller.signal.aborted) return;
			if (error instanceof PreviewTooLarge) {
				size = error.size;
				state = 'too-large';
			} else if (error instanceof Error && /expired|sign in/.test(error.message)) {
				state = 'unavailable';
			} else {
				state = 'failed';
			}
		}
	}

	onDestroy(() => {
		controller?.abort();
		release();
	});
</script>

{#if plan}
	<span class="agent-file-preview block my-2 max-w-full" data-kind={plan.kind} data-state={state}>
		{#if plan.kind === 'other'}
			<span
				class="inline-flex items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm"
			>
				<span aria-hidden="true">📄</span>
				<span class="font-medium break-all">{plan.name}</span>
				<span class="text-xs text-gray-500">{t.noPreview}</span>
			</span>
		{:else}
			<button
				type="button"
				class="text-xs text-gray-500 underline mb-1"
				aria-expanded={open}
				on:click={() => (open = !open)}>{open ? t.hide : t.show}</button
			>
			{#if open}
				{#if state === 'loading' || state === 'idle'}
					<span class="text-xs text-gray-500" role="status">{t.loading}</span>
				{:else if state === 'too-large'}
					<span class="text-xs text-gray-500">{t.tooLarge} ({formatBytes(size)})</span>
				{:else if state === 'unavailable'}
					<span class="text-xs text-gray-500">{t.unavailable}</span>
				{:else if state === 'failed'}
					<span class="text-xs text-gray-500">{t.failed}</span>
				{:else if plan.kind === 'image'}
					<img
						src={objectUrl}
						alt={plan.name}
						class="rounded-lg max-h-[520px] max-w-full object-contain border border-gray-200 dark:border-gray-700"
						loading="lazy"
					/>
				{:else if plan.kind === 'pdf'}
					<iframe
						src={objectUrl}
						title={plan.name}
						class="w-full rounded-lg border border-gray-200 dark:border-gray-700"
						style="height: min(70vh, 720px);"
					></iframe>
					<span class="block text-xs text-gray-500 mt-1">{plan.name} · {formatBytes(size)}</span>
				{:else if plan.kind === 'text'}
					<pre
						class="whitespace-pre-wrap break-words text-xs rounded-lg border border-gray-200 dark:border-gray-700 p-3 max-h-[480px] overflow-auto bg-gray-50 dark:bg-gray-900">{text}</pre>
					{#if truncated}<span class="block text-xs text-gray-500 mt-1">{t.truncated}</span>{/if}
				{:else if plan.kind === 'audio'}
					<audio controls preload="metadata" src={objectUrl} class="w-full max-w-md"></audio>
				{:else if plan.kind === 'video'}
					<!-- svelte-ignore a11y-media-has-caption -->
					<video controls preload="metadata" src={objectUrl} class="rounded-lg max-h-[520px] max-w-full"
					></video>
				{/if}
			{/if}
		{/if}
	</span>
{/if}
