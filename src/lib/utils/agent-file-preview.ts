/**
 * In-chat preview of Agent-delivered files (2026-10-11).
 *
 * The gateway turns `MEDIA:` directives into same-origin `/api/v1/agent-files/<id>/<name>`
 * links. Those links already stream through the authenticated download proxy; this module
 * decides which of them can be shown inline (image, PDF, text), with which MIME type and
 * under which size cap, so the chat can display the file instead of only offering a download.
 */
import { attachmentUrl, boundedDownloadBlob } from './attachment-download';

export type PreviewKind = 'image' | 'pdf' | 'text' | 'audio' | 'video' | 'other';

const IMAGE_TYPES: Record<string, string> = {
	png: 'image/png',
	jpg: 'image/jpeg',
	jpeg: 'image/jpeg',
	gif: 'image/gif',
	webp: 'image/webp',
	bmp: 'image/bmp',
	avif: 'image/avif'
};
const TEXT_EXTENSIONS = new Set([
	'txt',
	'md',
	'csv',
	'tsv',
	'json',
	'log',
	'yaml',
	'yml',
	'xml',
	'py',
	'js',
	'ts',
	'sh',
	'sql',
	'ini',
	'conf',
	'toml',
	'srt',
	'vtt'
]);
const AUDIO_TYPES: Record<string, string> = {
	mp3: 'audio/mpeg',
	wav: 'audio/wav',
	m4a: 'audio/mp4',
	ogg: 'audio/ogg',
	flac: 'audio/flac'
};
const VIDEO_TYPES: Record<string, string> = { mp4: 'video/mp4', webm: 'video/webm' };

/** Bytes the browser may buffer for an inline preview, by kind. Larger files stay download-only. */
export const PREVIEW_LIMITS: Record<PreviewKind, number> = {
	image: 24 * 1024 * 1024,
	pdf: 48 * 1024 * 1024,
	text: 1024 * 1024,
	audio: 48 * 1024 * 1024,
	video: 64 * 1024 * 1024,
	other: 0
};
/** Characters of a text file shown inline. */
export const TEXT_PREVIEW_CHARS = 200_000;

export interface PreviewPlan {
	kind: PreviewKind;
	mime: string;
	name: string;
	limit: number;
}

/** Return the delivered file name of a same-origin agent-file link, or null. */
export function agentFileName(href: string, origin: string): string | null {
	const url = attachmentUrl(href, origin);
	if (!url || !/^\/api\/v1\/agent-files\/[a-f0-9]{32}\/[^/]+$/.test(url.pathname)) return null;
	try {
		const name = decodeURIComponent(url.pathname.split('/').pop() ?? '');
		return name && name !== '.' && name !== '..' ? name : null;
	} catch {
		return null;
	}
}

/** Decide how an agent file should be previewed from its delivered name. */
export function previewPlan(href: string, origin: string): PreviewPlan | null {
	const name = agentFileName(href, origin);
	if (!name) return null;
	const extension = name.toLowerCase().split('.').pop() ?? '';
	if (!extension || extension === name.toLowerCase()) return { kind: 'other', mime: '', name, limit: 0 };
	if (Object.prototype.hasOwnProperty.call(IMAGE_TYPES, extension))
		return { kind: 'image', mime: IMAGE_TYPES[extension], name, limit: PREVIEW_LIMITS.image };
	if (extension === 'pdf') return { kind: 'pdf', mime: 'application/pdf', name, limit: PREVIEW_LIMITS.pdf };
	if (TEXT_EXTENSIONS.has(extension))
		return { kind: 'text', mime: 'text/plain', name, limit: PREVIEW_LIMITS.text };
	if (Object.prototype.hasOwnProperty.call(AUDIO_TYPES, extension))
		return { kind: 'audio', mime: AUDIO_TYPES[extension], name, limit: PREVIEW_LIMITS.audio };
	if (Object.prototype.hasOwnProperty.call(VIDEO_TYPES, extension))
		return { kind: 'video', mime: VIDEO_TYPES[extension], name, limit: PREVIEW_LIMITS.video };
	return { kind: 'other', mime: '', name, limit: 0 };
}

export interface PreviewResult {
	blob: Blob;
	size: number;
}

export class PreviewTooLarge extends Error {
	constructor(public readonly size: number, public readonly limit: number) {
		super('Preview skipped: file exceeds the inline preview size');
		this.name = 'PreviewTooLarge';
	}
}

/**
 * Fetch the file for an inline preview through the same authenticated route the download uses.
 * Refuses before buffering when the declared size exceeds the plan's limit; the bytes are
 * re-typed with the plan's MIME because the proxy always answers application/octet-stream.
 */
export async function fetchPreview(
	href: string,
	origin: string,
	token: string,
	plan: PreviewPlan,
	fetcher = fetch,
	signal?: AbortSignal
): Promise<PreviewResult> {
	const url = attachmentUrl(href, origin);
	if (!url || plan.kind === 'other' || plan.limit <= 0) throw new Error('Unsupported attachment URL');
	if (!token) throw new Error('Please sign in to download this attachment');
	const response = await fetcher(url.href, {
		headers: { Authorization: `Bearer ${token}` },
		credentials: 'same-origin',
		cache: 'no-store',
		redirect: 'error',
		...(signal ? { signal } : {})
	});
	if (!response.ok) {
		await response.body?.cancel().catch(() => {});
		if (response.status === 401) throw new Error('Please sign in to download this attachment');
		if ([403, 404, 410].includes(response.status))
			throw new Error('Attachment unavailable, expired, or access denied');
		throw new Error('Attachment download failed');
	}
	const declared = Number(response.headers.get('content-length'));
	if (Number.isFinite(declared) && declared > plan.limit) {
		await response.body?.cancel().catch(() => {});
		throw new PreviewTooLarge(declared, plan.limit);
	}
	const raw = await boundedDownloadBlob(response, signal);
	if (raw.size > plan.limit) throw new PreviewTooLarge(raw.size, plan.limit);
	return { blob: new Blob([raw], { type: plan.mime }), size: raw.size };
}

/** Decode a text preview; the caller shows at most TEXT_PREVIEW_CHARS characters. */
export async function previewText(blob: Blob): Promise<{ text: string; truncated: boolean }> {
	const text = await blob.text();
	return { text: text.slice(0, TEXT_PREVIEW_CHARS), truncated: text.length > TEXT_PREVIEW_CHARS };
}

export function formatBytes(size: number): string {
	if (!Number.isFinite(size) || size < 0) return '';
	if (size < 1024) return `${size} B`;
	if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
	if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
	return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
