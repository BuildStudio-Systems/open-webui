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
	const blob = await response.blob();
	const type = blob.type.split(';')[0].toLowerCase();
	if (!Object.prototype.hasOwnProperty.call(imageExtensions, type))
		throw new Error('Failed to download image');
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

export async function fetchAttachment(
	href: string,
	origin: string,
	token: string,
	fetcher = fetch
) {
	const url = attachmentUrl(href, origin);
	if (!url) throw new Error('Unsupported attachment URL');
	if (!token) throw new Error('Please sign in to download this attachment');
	const response = await fetcher(url.href, {
		headers: { Authorization: `Bearer ${token}` },
		credentials: 'same-origin',
		cache: 'no-store',
		redirect: 'error'
	});
	if (!response.ok) {
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
		blob: await response.blob(),
		filename: responseDownloadName(response.headers.get('content-disposition'), fallback)
	};
}
