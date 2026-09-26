<script lang="ts">
	import DOMPurify from 'dompurify';
	import { toast } from 'svelte-sonner';
	import fileSaver from 'file-saver';
	import {
		attachmentUrl,
		saveAttachment,
		browserAttachmentSavePicker,
		type AttachmentSavePicker
	} from '$lib/utils/attachment-download';

	import type { Token } from 'marked';
	import { getContext, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';

	const i18n = getContext('i18n');

	import { WEBUI_BASE_URL } from '$lib/constants';
	import { copyToClipboard, unescapeHtml } from '$lib/utils';

	import Image from '$lib/components/common/Image.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import Source from './Source.svelte';
	import HtmlToken from './HTMLToken.svelte';
	import TextToken from './MarkdownInlineTokens/TextToken.svelte';
	import CodespanToken from './MarkdownInlineTokens/CodespanToken.svelte';
	import MentionToken from './MarkdownInlineTokens/MentionToken.svelte';
	import NoteLinkToken from './MarkdownInlineTokens/NoteLinkToken.svelte';
	import SourceToken from './SourceToken.svelte';

	export let id: string;
	export let done = true;
	export let tokens: Token[];
	export let sourceIds = [];
	export let onSourceClick: Function = () => {};

	// Attachment intent lifecycle: native save dialogs may outlive this component.
	type DownloadMode = 'stream' | 'browser';
	type DownloadPhase = 'waiting' | 'downloading' | 'saved' | 'cancelled' | 'failed';
	type DownloadIntent = { controller: AbortController; mode: DownloadMode; phase: DownloadPhase };
	let attachmentDownloads = new Map<string, DownloadIntent>();
	let attachmentDisposed = false;
	const downloadCopy = {
		en: {
			browser: 'Browser download (≤64 MiB)',
			stream: 'Save as (streaming, supports large files)',
			alreadyOpen: 'A save dialog is already open; finish or cancel it first',
			waiting: 'Waiting for the system save dialog…',
			downloading: 'Downloading…',
			saved: 'Saved',
			requested: 'Browser download requested',
			cancelled: 'Download cancelled',
			failed: 'Download failed'
		},
		zh: {
			browser: '浏览器下载（≤64 MiB）',
			stream: '另存为（流式保存，支持大文件）',
			alreadyOpen: '已有保存窗口等待处理，请先完成或取消该窗口',
			waiting: '等待系统保存窗口确认…',
			downloading: '正在下载…',
			saved: '已保存',
			requested: '已请求浏览器下载',
			cancelled: '下载已取消',
			failed: '下载失败'
		},
		ja: {
			browser: 'ブラウザーでダウンロード（≤64 MiB）',
			stream: '名前を付けて保存（大きいファイルに対応）',
			alreadyOpen: '保存ダイアログが開いています。先に保存またはキャンセルしてください',
			waiting: 'システムの保存ダイアログを待っています…',
			downloading: 'ダウンロード中…',
			saved: '保存しました',
			requested: 'ブラウザーへダウンロードを送信しました',
			cancelled: 'ダウンロードをキャンセルしました',
			failed: 'ダウンロードに失敗しました'
		}
	};
	$: attachmentCopy =
		downloadCopy[
			($i18n.language ?? 'en').toLowerCase().startsWith('zh')
				? 'zh'
				: ($i18n.language ?? 'en').toLowerCase().startsWith('ja')
					? 'ja'
					: 'en'
		];
	const attachmentPending = (intent?: DownloadIntent) =>
		intent?.phase === 'waiting' || intent?.phase === 'downloading';
	const currentAttachment = (href: string, intent: DownloadIntent) =>
		!attachmentDisposed &&
		attachmentDownloads.get(href) === intent &&
		!intent.controller.signal.aborted;
	const setAttachmentPhase = (href: string, intent: DownloadIntent, phase: DownloadPhase) => {
		if (!currentAttachment(href, intent)) return;
		intent.phase = phase;
		attachmentDownloads = new Map(attachmentDownloads);
	};
	const downloadAttachment = async (href: string, mode: DownloadMode) => {
		if (attachmentDisposed || !attachmentUrl(href, window.location.origin)) return;
		const previous = attachmentDownloads.get(href);
		if (attachmentPending(previous)) {
			// Only an explicit browser-download click may replace a pending picker.
			if (mode !== 'browser' || previous?.mode === 'browser') return;
			previous?.controller.abort();
		}
		const nativePicker = mode === 'stream' ? browserAttachmentSavePicker() : undefined;
		const intent: DownloadIntent = {
			controller: new AbortController(),
			mode: nativePicker ? 'stream' : 'browser',
			phase: nativePicker ? 'waiting' : 'downloading'
		};
		attachmentDownloads = new Map(attachmentDownloads).set(href, intent);
		const picker: AttachmentSavePicker | undefined = nativePicker
			? async (options) => {
					const handle = await nativePicker(options);
					setAttachmentPhase(href, intent, 'downloading');
					return handle;
				}
			: undefined;
		try {
			const result = await saveAttachment(
				href,
				window.location.origin,
				localStorage.token ?? '',
				(blob, filename) => {
					if (currentAttachment(href, intent)) fileSaver.saveAs(blob, filename);
				},
				picker,
				fetch,
				undefined,
				intent.controller.signal
			);
			setAttachmentPhase(href, intent, result === 'cancelled' ? 'cancelled' : 'saved');
		} catch (error) {
			if (!currentAttachment(href, intent)) return;
			if (error instanceof Error && error.name === 'AbortError') {
				setAttachmentPhase(href, intent, 'cancelled');
			} else {
				setAttachmentPhase(href, intent, 'failed');
				toast.error(
					error instanceof Error &&
						error.message === 'A save dialog is already open; finish or cancel it first'
						? attachmentCopy.alreadyOpen
						: $i18n.t(error instanceof Error ? error.message : 'Attachment download failed')
				);
			}
		}
	};
	onDestroy(() => {
		attachmentDisposed = true;
		for (const intent of attachmentDownloads.values()) intent.controller.abort();
		attachmentDownloads.clear();
	});
	// End attachment intent lifecycle.

	/**
	 * Check if a URL is a same-origin note link and return the note ID if so.
	 */
	const getNoteIdFromHref = (href: string): string | null => {
		try {
			const url = new URL(href, window.location.origin);
			if (url.origin === window.location.origin) {
				const match = url.pathname.match(/^\/notes\/([^/]+)$/);
				if (match) {
					return match[1];
				}
			}
		} catch {
			// Invalid URL
		}
		return null;
	};

	/**
	 * Handle link clicks - intercept same-origin app URLs for in-app navigation
	 */
	const handleLinkClick = async (e: MouseEvent, href: string) => {
		if (attachmentUrl(href, window.location.origin)) {
			e.preventDefault();
			await downloadAttachment(href, 'stream');
			return;
		}
		try {
			const url = new URL(href, window.location.origin);
			// Check if same origin and an in-app route
			if (
				url.origin === window.location.origin &&
				(url.pathname.startsWith('/notes/') ||
					url.pathname.startsWith('/c/') ||
					url.pathname.startsWith('/channels/'))
			) {
				e.preventDefault();
				goto(url.pathname + url.search + url.hash);
			}
		} catch {
			// Invalid URL, let browser handle it
		}
	};
</script>

{#each tokens as token, tokenIdx (tokenIdx)}
	{#if token.type === 'escape'}
		{unescapeHtml(token.text)}
	{:else if token.type === 'html'}
		<HtmlToken {id} {token} {onSourceClick} />
	{:else if token.type === 'link'}
		{@const noteId = getNoteIdFromHref(token.href)}
		{#if noteId}
			<NoteLinkToken {noteId} href={token.href} />
		{:else}
			{@const download = attachmentDownloads.get(token.href)}
			{@const isAttachment =
				typeof window !== 'undefined' && attachmentUrl(token.href, window.location.origin)}
			<a
				href={token.href}
				target="_blank"
				rel="nofollow"
				title={isAttachment ? attachmentCopy.stream : token.title}
				aria-busy={isAttachment ? attachmentPending(download) : undefined}
				on:click={(e) => handleLinkClick(e, token.href)}
			>
				{#if token.tokens}
					<svelte:self id={`${id}-a`} tokens={token.tokens} {onSourceClick} {done} />
				{:else}{token.text}{/if}
			</a>
			{#if isAttachment}
				<button
					type="button"
					class="ms-2 inline-block text-xs underline disabled:opacity-50"
					disabled={download?.mode === 'browser' && attachmentPending(download)}
					on:click={() => downloadAttachment(token.href, 'browser')}
					>{attachmentCopy.browser}</button
				>
				{#if download}
					<span class="ms-2 text-xs text-gray-500" role="status" aria-live="polite">
						{download.phase === 'saved' && download.mode === 'browser'
							? attachmentCopy.requested
							: attachmentCopy[download.phase]}
					</span>
				{/if}
			{/if}
		{/if}
	{:else if token.type === 'image'}
		<Image src={token.href} alt={token.text} allowExternal={true} downloadable={true} />
	{:else if token.type === 'strong'}
		<strong><svelte:self id={`${id}-strong`} tokens={token.tokens} {onSourceClick} /></strong>
	{:else if token.type === 'em'}
		<em><svelte:self id={`${id}-em`} tokens={token.tokens} {onSourceClick} /></em>
	{:else if token.type === 'codespan'}
		<CodespanToken {token} {done} />
	{:else if token.type === 'br'}
		<br />
	{:else if token.type === 'del'}
		<del><svelte:self id={`${id}-del`} tokens={token.tokens} {onSourceClick} /></del>
	{:else if token.type === 'underline'}
		<u><svelte:self id={`${id}-underline`} tokens={token.tokens} {onSourceClick} /></u>
	{:else if token.type === 'inlineKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={token?.displayMode ?? false} />
		{/if}
	{:else if token.type === 'iframe'}
		<iframe
			src="{WEBUI_BASE_URL}/api/v1/files/{token.fileId}/content"
			title={token.fileId}
			width="100%"
			frameborder="0"
			on:load={(e) => {
				try {
					e.currentTarget.style.height =
						e.currentTarget.contentWindow.document.body.scrollHeight + 20 + 'px';
				} catch {}
			}}
		></iframe>
	{:else if token.type === 'mention'}
		<MentionToken {token} />
	{:else if token.type === 'footnote'}
		{@html DOMPurify.sanitize(
			`<sup class="footnote-ref footnote-ref-text">${token.escapedText}</sup>`
		) || ''}
	{:else if token.type === 'citation'}
		{#if (sourceIds ?? []).length > 0}
			<SourceToken {id} {token} {sourceIds} onClick={onSourceClick} />
		{:else}
			<TextToken {token} {done} />
		{/if}
	{:else if token.type === 'text'}
		<TextToken {token} {done} />
	{/if}
{/each}
