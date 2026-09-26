const extensions: Record<string, string> = {
	python: 'py',
	py: 'py',
	javascript: 'js',
	js: 'js',
	typescript: 'ts',
	ts: 'ts',
	tsx: 'tsx',
	jsx: 'jsx',
	html: 'html',
	css: 'css',
	json: 'json',
	yaml: 'yaml',
	yml: 'yml',
	markdown: 'md',
	md: 'md',
	sql: 'sql',
	bash: 'sh',
	shell: 'sh',
	sh: 'sh',
	powershell: 'ps1',
	ps1: 'ps1',
	java: 'java',
	c: 'c',
	cpp: 'cpp',
	csharp: 'cs',
	go: 'go',
	rust: 'rs',
	ruby: 'rb',
	php: 'php',
	swift: 'swift',
	kotlin: 'kt',
	xml: 'xml',
	svg: 'svg',
	csv: 'csv',
	mermaid: 'mmd',
	r: 'r'
};

const imageExtensions: Record<string, string> = {
	'image/png': 'png',
	'image/jpeg': 'jpg',
	'image/webp': 'webp',
	'image/gif': 'gif',
	'image/avif': 'avif',
	'image/bmp': 'bmp',
	'image/svg+xml': 'svg'
};

export async function fetchImageDownload(
	src: string,
	origin: string,
	token: string,
	fetcher = fetch
) {
	const url = new URL(src, origin);
	const dataImage = /^data:image\/(?:png|jpeg|webp|gif|avif|bmp|svg\+xml)[;,]/i.test(src);
	if (
		url.username ||
		url.password ||
		(!dataImage && !['https:', 'http:', 'blob:'].includes(url.protocol))
	) {
		throw new Error('Unsupported image URL');
	}
	if (url.protocol === 'blob:' && url.origin !== new URL(origin).origin)
		throw new Error('Unsupported image URL');
	const local = url.origin === new URL(origin).origin && ['http:', 'https:'].includes(url.protocol);
	const response = await fetcher(url.href, {
		headers: local && token ? { Authorization: `Bearer ${token}` } : {},
		credentials: local ? 'same-origin' : 'omit',
		redirect: 'error',
		cache: 'no-store'
	});
	if (!response.ok) throw new Error('Failed to download image');
	const type = (response.headers.get('content-type') ?? '').split(';')[0].toLowerCase();
	if (!Object.prototype.hasOwnProperty.call(imageExtensions, type)) {
		await response.body?.cancel();
		throw new Error('Failed to download image');
	}
	const blob = await boundedDownloadBlob(response);
	return { blob, filename: `image.${imageExtensions[type]}` };
}

export function safeDownloadName(name: string, fallback = 'attachment'): string {
	const value = name
		.split(/[\\/]/)
		.pop()
		?.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, '_')
		.replace(/[. ]+$/g, '')
		.slice(0, 180);
	return value && value !== '.' && value !== '..' ? value : fallback;
}

export function codeDownloadName(language: string): string {
	const key = language.trim().toLowerCase();
	return `code.${Object.prototype.hasOwnProperty.call(extensions, key) ? extensions[key] : 'txt'}`;
}

/** Only known same-origin download endpoints receive a session credential. */
export function attachmentUrl(href: string, origin: string): URL | null {
	try {
		const url = new URL(href, origin);
		if (url.origin !== new URL(origin).origin || url.username || url.password) return null;
		if (
			/^\/api\/v1\/agent-files\/[a-f0-9]{32}\/[^/]+$/.test(url.pathname) ||
			/^\/api\/v1\/files\/[^/]+\/content(?:\/[^/]+)?$/.test(url.pathname) ||
			/^\/api\/v1\/videos\/jobs\/[^/]+\/content$/.test(url.pathname)
		)
			return url;
	} catch {
		/* Malformed links never receive credentials. */
	}
	return null;
}

/** Browser-owned HTTP download, using the existing same-origin login cookie. */
export function browserAttachmentDownloadUrl(href: string, origin: string): string | null {
	const url = attachmentUrl(href, origin);
	if (!url || !['https:', 'http:'].includes(url.protocol)) return null;
	try {
		// Encoded separators must not change which server route receives the request.
		if (
			url.pathname.split('/').some((part) => {
				const decoded = decodeURIComponent(part);
				return /[\\/\u0000-\u001f\u007f]/.test(decoded) || decoded === '.' || decoded === '..';
			})
		)
			return null;
	} catch {
		return null;
	}
	// Never copy model-supplied tokens, flags or fragments into a navigable URL.
	url.search = '';
	url.hash = '';
	if (/^\/api\/v1\/files\//.test(url.pathname)) {
		// /content/html is a preview route that ignores attachment=true. Route only
		// that reserved suffix through the file endpoint; other display-name routes
		// can also stream generated text whose record has no stored file path.
		const suffix = url.pathname.match(/\/content\/([^/]+)$/)?.[1];
		if (suffix && decodeURIComponent(suffix) === 'html') {
			url.pathname = url.pathname.slice(0, url.pathname.lastIndexOf('/'));
		}
		url.searchParams.set('attachment', 'true');
	}
	return url.href;
}

export function responseDownloadName(disposition: string | null, fallback: string): string {
	const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
	if (encoded) {
		try {
			return safeDownloadName(decodeURIComponent(encoded), fallback);
		} catch {
			/* legacy name */
		}
	}
	const plain = disposition?.match(/filename="([^"]+)"|filename=([^;]+)/i);
	return safeDownloadName(plain?.[1] ?? plain?.[2]?.trim() ?? fallback, fallback);
}

function throwIfDownloadAborted(signal?: AbortSignal) {
	if (signal?.aborted) throw new DOMException('Attachment download cancelled', 'AbortError');
}

/** Settle our intent on abort, while safely discarding unabortable late results. */
function downloadRequest<T>(
	request: Promise<T>,
	signal?: AbortSignal,
	discard?: (value: T) => void | Promise<unknown>
): Promise<T> {
	return new Promise((resolve, reject) => {
		let finished = false;
		const abort = () => {
			if (finished) return;
			finished = true;
			signal?.removeEventListener('abort', abort);
			reject(new DOMException('Attachment download cancelled', 'AbortError'));
		};
		signal?.addEventListener('abort', abort, { once: true });
		if (signal?.aborted) abort();
		request.then(
			(value) => {
				if (finished) {
					if (discard)
						void Promise.resolve()
							.then(() => discard(value))
							.catch(() => {});
					return;
				}
				finished = true;
				signal?.removeEventListener('abort', abort);
				resolve(value);
			},
			(error) => {
				if (finished) return;
				finished = true;
				signal?.removeEventListener('abort', abort);
				reject(error);
			}
		);
	});
}

async function attachmentResponse(
	href: string,
	origin: string,
	token: string,
	fetcher = fetch,
	signal?: AbortSignal
) {
	const url = attachmentUrl(href, origin);
	if (!url) throw new Error('Unsupported attachment URL');
	if (!token) throw new Error('Please sign in to download this attachment');
	throwIfDownloadAborted(signal);
	const response = await downloadRequest(
		fetcher(url.href, {
			headers: { Authorization: `Bearer ${token}` },
			credentials: 'same-origin',
			cache: 'no-store',
			redirect: 'error',
			...(signal ? { signal } : {})
		}),
		signal,
		(late) => late.body?.cancel()
	);
	if (signal?.aborted) await response.body?.cancel().catch(() => {});
	throwIfDownloadAborted(signal);
	if (!response.ok) {
		await response.body?.cancel();
		if (response.status === 401) throw new Error('Please sign in to download this attachment');
		if ([403, 404, 410].includes(response.status))
			throw new Error('Attachment unavailable, expired, or access denied');
		throw new Error('Attachment download failed');
	}
	let fallback = 'attachment';
	try {
		fallback = safeDownloadName(decodeURIComponent(url.pathname.split('/').pop() ?? ''), fallback);
	} catch {
		/* fallback */
	}
	return {
		response,
		filename: responseDownloadName(response.headers.get('content-disposition'), fallback)
	};
}

// Browsers without a file-system picker must never buffer an unbounded response.
export const MAX_BUFFERED_DOWNLOAD_BYTES = 64 * 1024 * 1024;

export async function boundedDownloadBlob(response: Response, signal?: AbortSignal): Promise<Blob> {
	if (signal?.aborted) {
		await response.body?.cancel().catch(() => {});
		throwIfDownloadAborted(signal);
	}
	const tooLarge = () =>
		new Error(
			'Attachment exceeds the 64 MiB browser buffer limit; use a desktop browser with streaming save support'
		);
	if (Number(response.headers.get('content-length')) > MAX_BUFFERED_DOWNLOAD_BYTES) {
		await response.body?.cancel();
		throw tooLarge();
	}
	if (!response.body) throw new Error('Attachment download failed');
	const reader = response.body.getReader();
	const abort = () => {
		void reader.cancel().catch(() => {});
	};
	signal?.addEventListener('abort', abort, { once: true });
	const chunks: ArrayBuffer[] = [];
	let size = 0;
	try {
		while (true) {
			throwIfDownloadAborted(signal);
			const { done, value } = await downloadRequest(reader.read(), signal);
			throwIfDownloadAborted(signal);
			if (done) break;
			size += value.byteLength;
			if (size > MAX_BUFFERED_DOWNLOAD_BYTES) throw tooLarge();
			chunks.push(new Uint8Array(value).buffer);
		}
		return new Blob(chunks, { type: response.headers.get('content-type') ?? '' });
	} catch (error) {
		await reader.cancel().catch(() => {});
		throw error;
	} finally {
		signal?.removeEventListener('abort', abort);
		reader.releaseLock();
	}
}

export async function fetchAttachment(
	href: string,
	origin: string,
	token: string,
	fetcher = fetch,
	signal?: AbortSignal
) {
	const { response, filename } = await attachmentResponse(href, origin, token, fetcher, signal);
	return { blob: await boundedDownloadBlob(response, signal), filename };
}

export type AttachmentSavePicker = (options: { suggestedName: string }) => Promise<{
	createWritable(): Promise<WritableStream<Uint8Array>>;
}>;

// Native pickers cannot be dismissed by AbortSignal. Keep this single-flight
// guard until the native request settles, even if its UI intent was abandoned.
let activeAttachmentPicker: symbol | undefined;

async function chooseAttachmentFile(
	picker: AttachmentSavePicker,
	suggestedName: string,
	signal?: AbortSignal
) {
	throwIfDownloadAborted(signal);
	if (activeAttachmentPicker)
		throw new Error('A save dialog is already open; finish or cancel it first');
	const intent = Symbol();
	activeAttachmentPicker = intent;
	let request: ReturnType<AttachmentSavePicker>;
	try {
		// Invoke synchronously in the original user click, not after an await.
		request = picker({ suggestedName });
	} catch (error) {
		activeAttachmentPicker = undefined;
		throw error;
	}
	const settled = request.finally(() => {
		if (activeAttachmentPicker === intent) activeAttachmentPicker = undefined;
	});
	return downloadRequest(settled, signal);
}

/** Must be called directly from the click handler, before any network await. */
export async function saveAttachment(
	href: string,
	origin: string,
	token: string,
	saveBlob: (blob: Blob, filename: string) => void,
	picker?: AttachmentSavePicker,
	fetcher = fetch,
	suggestedName?: string,
	signal?: AbortSignal
): Promise<'saved' | 'cancelled'> {
	const url = attachmentUrl(href, origin);
	if (!url) throw new Error('Unsupported attachment URL');
	if (!token) throw new Error('Please sign in to download this attachment');
	throwIfDownloadAborted(signal);
	if (!picker) {
		const result = await fetchAttachment(href, origin, token, fetcher, signal);
		throwIfDownloadAborted(signal);
		saveBlob(result.blob, result.filename);
		return 'saved';
	}
	let name = 'attachment';
	try {
		name = safeDownloadName(decodeURIComponent(url.pathname.split('/').pop() ?? ''));
	} catch {
		/* fallback */
	}
	let handle: Awaited<ReturnType<AttachmentSavePicker>>;
	try {
		handle = await chooseAttachmentFile(picker, safeDownloadName(suggestedName ?? name), signal);
	} catch (error) {
		if (error instanceof Error && error.name === 'AbortError') return 'cancelled';
		throw error; // No silent fallback after a permission denial or cancellation.
	}
	throwIfDownloadAborted(signal);
	const { response } = await attachmentResponse(href, origin, token, fetcher, signal);
	if (!response.body) throw new Error('Attachment download failed');
	let writable: WritableStream<Uint8Array>;
	try {
		throwIfDownloadAborted(signal);
		writable = await downloadRequest(handle.createWritable(), signal, (late) => late.abort());
	} catch (error) {
		await response.body.cancel().catch(() => {});
		throw error;
	}
	// pipeTo applies backpressure and aborts the temporary write on read failure.
	await response.body.pipeTo(writable, signal ? { signal } : undefined);
	return 'saved';
}

export function browserAttachmentSavePicker(): AttachmentSavePicker | undefined {
	if (typeof window === 'undefined') return undefined;
	const target = window as Window & { showSaveFilePicker?: AttachmentSavePicker };
	return target.showSaveFilePicker?.bind(target);
}
