import { readFileSync } from 'node:fs';
import { compile } from 'svelte/compiler';
import ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	attachmentUrl,
	browserAttachmentDownloadUrl,
	saveAttachment,
	MAX_BUFFERED_DOWNLOAD_BYTES
} from './attachment-download';

const origin = 'https://there.example';
const href = '/api/v1/agent-files/' + 'a'.repeat(32) + '/report.txt';
const source = readFileSync(
	'src/lib/components/chat/Messages/Markdown/MarkdownInlineTokens.svelte',
	'utf8'
);
const block = source
	.split('// Attachment intent lifecycle:')[1]
	.split('// End attachment intent lifecycle.')[0];
const tree = ts.createSourceFile(
	'download-entry.ts',
	'//' + block,
	ts.ScriptTarget.ESNext,
	true,
	ts.ScriptKind.TS
);
const isolated = tree.statements
	.filter((node) => !ts.isLabeledStatement(node))
	.map((node) => node.getText(tree))
	.join('\n');
const compiled = ts.transpileModule(isolated, {
	compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
const reactive = tree.statements
	.filter(ts.isLabeledStatement)
	.map((node) => node.statement.getText(tree))
	.join('\n');
const factory = new Function(
	'deps',
	`
const {onDestroy,attachmentUrl,browserAttachmentDownloadUrl,saveAttachment,browserAttachmentSavePicker,fileSaver,toast,
  window,localStorage,fetch,$i18n}=deps;
let attachmentCopy;
${compiled}
${reactive}
return {start:downloadAttachment,browser:requestBrowserAttachment,intent:(href)=>attachmentDownloads.get(href),
  count:()=>attachmentDownloads.size,disposed:()=>attachmentDisposed};
`
);

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (error: Error) => void;
	const promise = new Promise<T>((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
}

const contexts: Array<() => void> = [];
const releasePickers: Array<() => void> = [];
const flush = async () => {
	for (let i = 0; i < 20; i++) await Promise.resolve();
};
function pendingPicker() {
	const handle = { createWritable: vi.fn(async () => new WritableStream()) };
	const pending = deferred<typeof handle>();
	releasePickers.push(() => pending.resolve(handle));
	return { ...pending, handle, picker: vi.fn(() => pending.promise) };
}
function entry(
	picker?: (...args: any[]) => Promise<any>,
	fetcher: (...args: Parameters<typeof fetch>) => ReturnType<typeof fetch> = vi.fn(
		async () => new Response('file')
	),
	language = 'en'
) {
	let destroy!: () => void;
	const deps = {
		onDestroy: (handler: () => void) => {
			destroy = handler;
		},
		attachmentUrl,
		browserAttachmentDownloadUrl,
		saveAttachment,
		browserAttachmentSavePicker: () => picker,
		fileSaver: { saveAs: vi.fn() },
		toast: { error: vi.fn() },
		window: { location: { origin } },
		localStorage: { token: 'synthetic-session' },
		fetch: fetcher,
		$i18n: { language, t: (text: string) => text }
	};
	const api = factory(deps);
	contexts.push(() => destroy());
	return { api, deps, destroy: () => destroy() };
}
afterEach(async () => {
	for (const destroy of contexts.splice(0)) destroy();
	for (const release of releasePickers.splice(0)) release();
	await flush();
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe('actual attachment link lifecycle with real download helper', () => {
	it.each([
		['en', 'A save dialog is already open; finish or cancel it first'],
		['zh-CN', '已有保存窗口等待处理，请先完成或取消该窗口'],
		['ja-JP', '保存ダイアログが開いています。先に保存またはキャンセルしてください']
	])(
		'keeps native picker global single-flight and reports busy state in %s',
		async (language, message) => {
			const native = pendingPicker();
			const first = entry(native.picker);
			const firstRequest = first.api.start(href, 'stream');
			const secondPicker = vi.fn(async () => native.handle);
			const second = entry(secondPicker, undefined, language);
			await second.api.start(href, 'stream');
			expect(secondPicker).not.toHaveBeenCalled();
			expect(second.deps.fetch).not.toHaveBeenCalled();
			expect(second.deps.fileSaver.saveAs).not.toHaveBeenCalled();
			expect(second.deps.toast.error).toHaveBeenCalledOnce();
			expect(second.deps.toast.error).toHaveBeenCalledWith(message);
			expect(second.api.intent(href).phase).toBe('failed');
			expect(first.api.intent(href).phase).toBe('waiting');
			native.reject(new DOMException('user cancelled', 'AbortError'));
			await firstRequest;
		}
	);

	it('does not silently download after a long wait for an unresolved native picker', async () => {
		vi.useFakeTimers();
		const native = pendingPicker();
		const { api, deps } = entry(native.picker);
		const first = api.start(href, 'stream');
		await vi.advanceTimersByTimeAsync(60_000);
		expect(api.intent(href).phase).toBe('waiting');
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		native.reject(new DOMException('user cancelled', 'AbortError'));
		await first;
		expect(api.intent(href).phase).toBe('cancelled');
	});

	it('explicit browser replacement abandons a pending native handle without any late I/O', async () => {
		const native = pendingPicker();
		const { api, deps } = entry(native.picker);
		const old = api.start(href, 'stream');
		const oldIntent = api.intent(href);
		expect(oldIntent.phase).toBe('waiting');
		await flush();
		expect(deps.fetch).not.toHaveBeenCalled();
		const click = new Event('click', { cancelable: true });
		const next = api.browser(click, href);
		const nextIntent = api.intent(href);
		expect(next).toBeUndefined();
		expect(click.defaultPrevented).toBe(false);
		await old;
		expect(oldIntent.controller.signal.aborted).toBe(true);
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(nextIntent.phase).toBe('requested');
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		native.resolve(native.handle);
		await flush();
		expect(native.handle.createWritable).not.toHaveBeenCalled();
		expect(api.intent(href)).toBe(nextIntent);
		expect(nextIntent.phase).toBe('requested');
		expect(deps.toast.error).not.toHaveBeenCalled();
	});

	it('a late native rejection cannot erase the successful replacement or show a stale error', async () => {
		const native = pendingPicker();
		const { api, deps } = entry(native.picker);
		const old = api.start(href, 'stream');
		api.browser(new Event('click', { cancelable: true }), href);
		const current = api.intent(href);
		native.reject(new DOMException('old denied', 'NotAllowedError'));
		await old;
		await flush();
		expect(api.intent(href)).toBe(current);
		expect(current.phase).toBe('requested');
		expect(deps.toast.error).not.toHaveBeenCalled();
	});

	it('coalesces native double-clicks while the original dialog is pending', async () => {
		const native = pendingPicker();
		const { api, deps } = entry(native.picker);
		const first = api.start(href, 'stream');
		await api.start(href, 'stream');
		expect(native.picker).toHaveBeenCalledOnce();
		native.reject(new DOMException('user cancelled', 'AbortError'));
		await first;
		expect(api.intent(href).phase).toBe('cancelled');
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		expect(deps.toast.error).not.toHaveBeenCalled();
	});

	it('never treats a permission denial as consent to fetch or browser-save', async () => {
		const picker = vi.fn(async () => {
			throw new DOMException('denied', 'NotAllowedError');
		});
		const { api, deps } = entry(picker);
		await api.start(href, 'stream');
		expect(api.intent(href).phase).toBe('failed');
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		expect(deps.toast.error).toHaveBeenCalledOnce();
		api.browser(new Event('click', { cancelable: true }), href);
		expect(api.intent(href).phase).toBe('requested');
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
	});

	it('preserves the primary helper fallback and coalesces it when no native picker exists', async () => {
		const response = deferred<Response>();
		const { api, deps } = entry(
			undefined,
			vi.fn(() => response.promise)
		);
		const first = api.start(href, 'stream');
		const intent = api.intent(href);
		expect(intent.mode).toBe('browser');
		expect(intent.phase).toBe('downloading');
		await api.start(href);
		await api.start(href, 'stream');
		expect(api.intent(href)).toBe(intent);
		expect(intent.controller.signal.aborted).toBe(false);
		expect(deps.fetch).toHaveBeenCalledOnce();
		response.resolve(new Response('file'));
		await first;
		expect(deps.fileSaver.saveAs).toHaveBeenCalledOnce();
		expect(api.intent(href)).toMatchObject({ mode: 'browser', phase: 'saved' });
		expect(source).toMatch(
			/\?\s*attachmentCopy\.requested\s*:\s*attachmentCopy\[download\.phase\]/
		);
	});

	it('destroyed components cannot use a late picker handle or start another download', async () => {
		const native = pendingPicker();
		const { api, deps, destroy } = entry(native.picker);
		const first = api.start(href, 'stream');
		destroy();
		native.resolve(native.handle);
		await first;
		const click = new Event('click', { cancelable: true });
		api.browser(click, href);
		expect(click.defaultPrevented).toBe(true);
		expect(api.disposed()).toBe(true);
		expect(api.count()).toBe(0);
		expect(native.handle.createWritable).not.toHaveBeenCalled();
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		expect(deps.toast.error).not.toHaveBeenCalled();
	});

	it('aborts fetch on destroy and cancels a late response even if the fetcher ignores its signal', async () => {
		const response = deferred<Response>();
		const { api, deps, destroy } = entry(
			undefined,
			vi.fn(() => response.promise)
		);
		const first = api.start(href);
		const signal = api.intent(href).controller.signal;
		destroy();
		await first;
		expect(signal.aborted).toBe(true);
		const cancel = vi.fn();
		response.resolve(new Response(new ReadableStream({ cancel })));
		await flush();
		expect(cancel).toHaveBeenCalledOnce();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		expect(deps.toast.error).not.toHaveBeenCalled();
	});

	it('cancels an active bounded reader on destroy without producing a Blob download', async () => {
		const pulling = deferred<void>();
		const cancel = vi.fn();
		const response = new Response(
			new ReadableStream({
				pull() {
					pulling.resolve();
				},
				cancel
			})
		);
		const { api, deps, destroy } = entry(
			undefined,
			vi.fn(async () => response)
		);
		const first = api.start(href);
		await pulling.promise;
		await flush();
		destroy();
		await first;
		expect(cancel).toHaveBeenCalledOnce();
		expect(response.body?.locked).toBe(false);
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
	});

	it('replaces an in-flight native-path fetch with a distinct controller and discards its late response', async () => {
		const oldResponse = deferred<Response>();
		const native = { createWritable: vi.fn(async () => new WritableStream()) };
		const fetcher = vi
			.fn()
			.mockReturnValueOnce(oldResponse.promise)
			.mockResolvedValueOnce(new Response('replacement'));
		const { api, deps } = entry(
			vi.fn(async () => native),
			fetcher
		);
		const old = api.start(href, 'stream');
		await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
		const oldIntent = api.intent(href);
		api.browser(new Event('click', { cancelable: true }), href);
		await old;
		const current = api.intent(href);
		expect(current.controller).not.toBe(oldIntent.controller);
		expect(oldIntent.controller.signal.aborted).toBe(true);
		const cancel = vi.fn();
		oldResponse.resolve(new Response(new ReadableStream({ cancel })));
		await flush();
		expect(cancel).toHaveBeenCalledOnce();
		expect(native.createWritable).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		expect(fetcher).toHaveBeenCalledOnce();
		expect(api.intent(href)).toBe(current);
		expect(current.phase).toBe('requested');
	});

	it('ignores stale fetch failure after replacement success', async () => {
		const oldResponse = deferred<Response>();
		const fetcher = vi
			.fn()
			.mockReturnValueOnce(oldResponse.promise)
			.mockResolvedValueOnce(new Response('replacement'));
		const { api, deps } = entry(
			vi.fn(async () => ({ createWritable: vi.fn() })),
			fetcher
		);
		const old = api.start(href, 'stream');
		await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
		api.browser(new Event('click', { cancelable: true }), href);
		oldResponse.reject(new Error('old network failure'));
		await old;
		await flush();
		expect(api.intent(href).phase).toBe('requested');
		expect(deps.toast.error).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
	});

	it('aborts a late writable without writing or closing after component destruction', async () => {
		const writable = deferred<WritableStream<Uint8Array>>();
		const handle = { createWritable: vi.fn(() => writable.promise) };
		const cancel = vi.fn();
		const response = new Response(new ReadableStream({ cancel }));
		const { api, deps, destroy } = entry(
			vi.fn(async () => handle),
			vi.fn(async () => response)
		);
		const first = api.start(href, 'stream');
		await vi.waitFor(() => expect(handle.createWritable).toHaveBeenCalledOnce());
		destroy();
		await first;
		const write = vi.fn(),
			close = vi.fn(),
			abort = vi.fn();
		writable.resolve(new WritableStream({ write, close, abort }));
		await flush();
		expect(cancel).toHaveBeenCalledOnce();
		expect(abort).toHaveBeenCalledOnce();
		expect(write).not.toHaveBeenCalled();
		expect(close).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
	});

	it('aborts an active streaming destination and source on destroy', async () => {
		const wrote = deferred<void>();
		const cancel = vi.fn();
		let sent = false;
		const response = new Response(
			new ReadableStream({
				pull(controller) {
					if (!sent) {
						sent = true;
						controller.enqueue(new Uint8Array([7]));
					}
				},
				cancel
			})
		);
		const close = vi.fn(),
			abort = vi.fn();
		const picker = vi.fn(async () => ({
			createWritable: async () =>
				new WritableStream({
					write() {
						wrote.resolve();
					},
					close,
					abort
				})
		}));
		const { api, deps, destroy } = entry(
			picker,
			vi.fn(async () => response)
		);
		const first = api.start(href, 'stream');
		await wrote.promise;
		destroy();
		await first;
		expect(cancel).toHaveBeenCalledOnce();
		expect(abort).toHaveBeenCalledOnce();
		expect(close).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
	});

	it('preserves the bounded primary helper fallback without a native picker', async () => {
		const cancel = vi.fn();
		const response = new Response(new ReadableStream({ cancel }), {
			headers: { 'content-length': String(MAX_BUFFERED_DOWNLOAD_BYTES + 1) }
		});
		const { api, deps } = entry(
			undefined,
			vi.fn(async () => response)
		);
		await api.start(href);
		expect(api.intent(href).phase).toBe('failed');
		expect(cancel).toHaveBeenCalledOnce();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		expect(deps.toast.error).toHaveBeenCalledOnce();
	});

	it('does not initiate a download for unsupported targets', async () => {
		const picker = vi.fn();
		const { api, deps } = entry(picker);
		const click = new Event('click', { cancelable: true });
		api.browser(click, 'https://external.example' + href);
		expect(click.defaultPrevented).toBe(true);
		expect(api.count()).toBe(0);
		expect(picker).not.toHaveBeenCalled();
		expect(deps.fetch).not.toHaveBeenCalled();
	});

	it('keeps valid browser clicks synchronous and allows the browser default action', async () => {
		const native = pendingPicker();
		const { api, deps } = entry(native.picker);
		const pending = api.start(href);
		const old = api.intent(href);
		const target = new EventTarget();
		target.addEventListener('click', (event) => api.browser(event, href));
		target.addEventListener('click', () => expect(old.controller.signal.aborted).toBe(true));
		const click = new Event('click', { cancelable: true });
		expect(target.dispatchEvent(click)).toBe(true);
		expect(click.defaultPrevented).toBe(false);
		expect(api.intent(href)).toMatchObject({ mode: 'http', phase: 'requested' });
		expect(deps.fetch).not.toHaveBeenCalled();
		expect(deps.fileSaver.saveAs).not.toHaveBeenCalled();
		await pending;
	});

	it('compiles the real Svelte anchor with HTTP href and a normal default download action', () => {
		const result = compile(source, { filename: 'MarkdownInlineTokens.svelte', generate: 'client' });
		const anchors: any[] = [];
		const visit = (node: any) => {
			if (!node || typeof node !== 'object') return;
			if (node.name === 'a' && node.attributes?.some((a: any) => a.name === 'download'))
				anchors.push(node);
			for (const [key, value] of Object.entries(node)) {
				if (key !== 'parent') {
					if (Array.isArray(value)) value.forEach(visit);
					else visit(value);
				}
			}
		};
		visit(result.ast);
		expect(anchors).toHaveLength(1);
		const anchor = anchors[0];
		expect(anchor.attributes.find((a: any) => a.name === 'href').value[0].expression.name).toBe(
			'browserDownloadHref'
		);
		expect(anchor.attributes.find((a: any) => a.name === 'click').expression.body.callee.name).toBe(
			'requestBrowserAttachment'
		);
		expect(anchor.attributes.find((a: any) => a.name === 'click').modifiers).toEqual([]);
		expect(anchor.attributes.find((a: any) => a.name === 'download').value).toBe(true);
		expect(result.js.code).toContain('requestBrowserAttachment');
		expect(source).toContain('role="status" aria-live="polite"');
		expect(source).not.toContain('≤64 MiB');
		expect(source).toContain("browser: '浏览器下载'");
		expect(source).toContain("browser: 'ブラウザーでダウンロード'");
	});
});
