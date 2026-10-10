import { describe, expect, it } from 'vitest';
import {
	PREVIEW_LIMITS,
	PreviewTooLarge,
	agentFileName,
	fetchPreview,
	formatBytes,
	previewPlan,
	previewText
} from './agent-file-preview';

const ORIGIN = 'https://there.example';
const ID = 'a'.repeat(32);
const link = (name: string) => `/api/v1/agent-files/${ID}/${encodeURIComponent(name)}`;

describe('previewPlan', () => {
	it('recognises only same-origin agent-file links', () => {
		expect(agentFileName(link('report.pdf'), ORIGIN)).toBe('report.pdf');
		expect(agentFileName(`https://other.example${link('report.pdf')}`, ORIGIN)).toBeNull();
		expect(agentFileName('/api/v1/files/abc/content', ORIGIN)).toBeNull();
		expect(agentFileName(`/api/v1/agent-files/${ID}/..`, ORIGIN)).toBeNull();
		expect(previewPlan('https://evil.example/x.png', ORIGIN)).toBeNull();
	});

	it('maps the delivered name to a preview kind, MIME and size cap', () => {
		expect(previewPlan(link('四半期 report.PDF'), ORIGIN)).toEqual({
			kind: 'pdf',
			mime: 'application/pdf',
			name: '四半期 report.PDF',
			limit: PREVIEW_LIMITS.pdf
		});
		expect(previewPlan(link('logo.jpeg'), ORIGIN)?.mime).toBe('image/jpeg');
		expect(previewPlan(link('notes.md'), ORIGIN)?.kind).toBe('text');
		expect(previewPlan(link('clip.mp4'), ORIGIN)?.kind).toBe('video');
		expect(previewPlan(link('voice.m4a'), ORIGIN)?.mime).toBe('audio/mp4');
		expect(previewPlan(link('archive.zip'), ORIGIN)).toEqual({ kind: 'other', mime: '', name: 'archive.zip', limit: 0 });
		expect(previewPlan(link('README'), ORIGIN)?.kind).toBe('other');
	});
});

function response(body: Uint8Array<ArrayBuffer>, headers: Record<string, string> = {}, status = 200) {
	return new Response(status === 200 ? new Blob([body]) : null, { status, headers });
}

describe('fetchPreview', () => {
	const plan = previewPlan(link('photo.png'), ORIGIN)!;

	it('sends the session credential only to the same-origin route and re-types the bytes', async () => {
		const calls: Array<[string, RequestInit | undefined]> = [];
		const fetcher = async (input: RequestInfo | URL, init?: RequestInit) => {
			calls.push([String(input), init]);
			return response(new Uint8Array([1, 2, 3]), { 'content-type': 'application/octet-stream', 'content-length': '3' });
		};
		const result = await fetchPreview(link('photo.png'), ORIGIN, 'tok', plan, fetcher as typeof fetch);
		expect(result.size).toBe(3);
		expect(result.blob.type).toBe('image/png');
		expect(calls[0][0]).toBe(`${ORIGIN}${link('photo.png')}`);
		expect((calls[0][1]?.headers as Record<string, string>).Authorization).toBe('Bearer tok');
		expect(calls[0][1]?.redirect).toBe('error');
	});

	it('refuses before buffering when the declared size exceeds the cap', async () => {
		let bodyRead = false;
		const fetcher = async () => {
			const res = response(new Uint8Array(10), { 'content-length': String(PREVIEW_LIMITS.image + 1) });
			const original = res.body!;
			Object.defineProperty(res, 'body', {
				get() {
					bodyRead = true;
					return original;
				}
			});
			return res;
		};
		await expect(fetchPreview(link('photo.png'), ORIGIN, 'tok', plan, fetcher as typeof fetch)).rejects.toBeInstanceOf(
			PreviewTooLarge
		);
		expect(bodyRead).toBe(true); // only to cancel it
	});

	it('does not preview without a session, for other kinds, or on an error status', async () => {
		await expect(fetchPreview(link('photo.png'), ORIGIN, '', plan, (async () => response(new Uint8Array())) as typeof fetch)).rejects.toThrow(
			'sign in'
		);
		const other = previewPlan(link('archive.zip'), ORIGIN)!;
		await expect(fetchPreview(link('archive.zip'), ORIGIN, 'tok', other, (async () => response(new Uint8Array())) as typeof fetch)).rejects.toThrow(
			'Unsupported'
		);
		await expect(
			fetchPreview(link('photo.png'), ORIGIN, 'tok', plan, (async () => response(new Uint8Array(), {}, 404)) as typeof fetch)
		).rejects.toThrow('expired');
	});
});

describe('text and sizes', () => {
	it('bounds the text shown inline', async () => {
		const long = 'x'.repeat(200_010);
		const result = await previewText(new Blob([long]));
		expect(result.text.length).toBe(200_000);
		expect(result.truncated).toBe(true);
		expect(await previewText(new Blob(['hello'])))
			.toEqual({ text: 'hello', truncated: false });
	});

	it('formats sizes for the file card', () => {
		expect(formatBytes(512)).toBe('512 B');
		expect(formatBytes(2048)).toBe('2.0 KB');
		expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
		expect(formatBytes(-1)).toBe('');
	});
});
