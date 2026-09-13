import { describe, expect, it, vi } from 'vitest';
import {
	attachmentUrl,
	codeDownloadName,
	fetchAttachment,
	fetchImageDownload,
	responseDownloadName,
	safeDownloadName
} from './attachment-download';

const origin = 'https://there.example';
const path = '/api/v1/agent-files/' + 'a'.repeat(32) + '/report.pdf';

describe('attachment download', () => {
	it('preserves image bytes and MIME extension without sending credentials externally', async () => {
		const fetcher = vi
			.fn()
			.mockResolvedValue(
				new Response(new Uint8Array([255, 216, 255]), { headers: { 'content-type': 'image/jpeg' } })
			);
		const result = await fetchImageDownload(
			'https://images.example/image.jpg',
			origin,
			'secret',
			fetcher
		);
		expect(result.filename).toBe('image.jpg');
		expect(result.blob.type).toBe('image/jpeg');
		expect(fetcher.mock.calls[0][1].headers).toEqual({});
		expect(fetcher.mock.calls[0][1].credentials).toBe('omit');
	});
	it('decodes a JPEG data URL without relabelling it PNG', async () => {
		const result = await fetchImageDownload('data:image/jpeg;base64,/9j/', origin, 'secret');
		expect(result.filename).toBe('image.jpg');
		expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(
			new Uint8Array([255, 216, 255])
		);
	});
	it('rejects non-image downloads and unsafe protocols', async () => {
		const fetcher = vi
			.fn()
			.mockResolvedValue(new Response('login', { headers: { 'content-type': 'text/html' } }));
		await expect(fetchImageDownload('/login', origin, 'secret', fetcher)).rejects.toThrow();
		fetcher.mockClear();
		await expect(
			fetchImageDownload('file:///etc/passwd', origin, 'secret', fetcher)
		).rejects.toThrow();
		await expect(
			fetchImageDownload('data:text/html,<script>', origin, 'secret', fetcher)
		).rejects.toThrow();
		expect(fetcher).not.toHaveBeenCalled();
	});
	it.each([
		['python', 'py'],
		[' TypeScript ', 'ts'],
		['markdown', 'md'],
		['../exe', 'txt'],
		['constructor', 'txt'],
		['__proto__', 'txt'],
		['', 'txt']
	])('code extension %s', (language, ext) => {
		expect(codeDownloadName(language)).toBe('code.' + ext);
	});
	it.each([
		path,
		'/api/v1/files/abc/content',
		'/api/v1/files/abc/content/report.docx',
		'/api/v1/videos/jobs/abc/content'
	])('accepts known local endpoint %s', (href) => {
		expect(attachmentUrl(href, origin)?.origin).toBe(origin);
	});
	it.each([
		'https://evil.example' + path,
		'//evil.example' + path,
		'javascript:alert(1)',
		'/api/config',
		'/api/v1/files/abc',
		'https://user:pass@there.example' + path
	])('rejects credential target %s', (href) => {
		expect(attachmentUrl(href, origin)).toBeNull();
	});
	it('sanitizes header filenames and supports Unicode', () => {
		expect(
			responseDownloadName("attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf", 'file')
		).toBe('报告.pdf');
		expect(responseDownloadName('attachment; filename="../../report.pdf"', 'file')).toBe(
			'report.pdf'
		);
		expect(responseDownloadName("attachment; filename*=UTF-8''%ZZ", 'file')).toBe('file');
		expect(safeDownloadName('..')).toBe('attachment');
	});
	it('downloads exact bytes with current token, no redirects and no caching', async () => {
		const fetcher = vi.fn().mockResolvedValue(
			new Response(new Uint8Array([0, 255, 42]), {
				headers: { 'content-disposition': 'attachment; filename="report.pdf"' }
			})
		);
		const result = await fetchAttachment(path, origin, 'test-session', fetcher);
		expect(fetcher).toHaveBeenCalledWith(origin + path, {
			headers: { Authorization: 'Bearer test-session' },
			credentials: 'same-origin',
			cache: 'no-store',
			redirect: 'error'
		});
		expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(new Uint8Array([0, 255, 42]));
		expect(result.filename).toBe('report.pdf');
	});
	it('never fetches external or unauthenticated attachments', async () => {
		const fetcher = vi.fn();
		await expect(
			fetchAttachment('https://evil.example' + path, origin, 'secret', fetcher)
		).rejects.toThrow('Unsupported');
		await expect(fetchAttachment(path, origin, '', fetcher)).rejects.toThrow('sign in');
		expect(fetcher).not.toHaveBeenCalled();
	});
	it.each([401, 403, 404, 410, 500])('does not save an error body (%s)', async (status) => {
		const fetcher = vi.fn().mockResolvedValue(new Response('private upstream error', { status }));
		await expect(fetchAttachment(path, origin, 'session', fetcher)).rejects.toThrow();
	});
});
