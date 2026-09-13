import { describe, expect, it, vi } from 'vitest';
import {
	attachmentUrl,
	boundedDownloadBlob,
	MAX_BUFFERED_DOWNLOAD_BYTES,
	saveAttachment,
	codeDownloadName,
	fetchAttachment,
	fetchImageDownload,
	responseDownloadName,
	safeDownloadName
} from './attachment-download';

const origin = 'https://there.example';
const path = '/api/v1/agent-files/' + 'a'.repeat(32) + '/report.pdf';

describe('attachment download', () => {
	it('streams 128 MiB with backpressure beyond the compatibility buffer cap', async () => {
		let produced = 0;
		let written = 0;
		let closed = false;
		const chunk = new Uint8Array(1024 * 1024).fill(84);
		const response = new Response(
			new ReadableStream({
				pull(controller) {
					if (produced === 128) {
						controller.close();
						return;
					}
					produced++;
					controller.enqueue(chunk);
				}
			}),
			{ headers: { 'content-length': String(128 * 1024 * 1024) } }
		);
		const picker = vi.fn().mockResolvedValue({
			createWritable: async () =>
				new WritableStream({
					async write(value) {
						expect(value.byteLength).toBe(chunk.byteLength);
						expect(value[0]).toBe(84);
						written++;
						expect(produced - written).toBeLessThanOrEqual(2);
						await Promise.resolve();
					},
					close() {
						closed = true;
					}
				})
		});
		expect(
			await saveAttachment(
				path,
				origin,
				'session',
				vi.fn(),
				picker,
				vi.fn().mockResolvedValue(response)
			)
		).toBe('saved');
		expect(written).toBe(128);
		expect(closed).toBe(true);
	});
	it('cancels the response if the destination cannot be opened', async () => {
		const cancel = vi.fn();
		const response = new Response(new ReadableStream({ cancel }));
		const picker = vi
			.fn()
			.mockResolvedValue({ createWritable: vi.fn().mockRejectedValue(new Error('disk denied')) });
		await expect(
			saveAttachment(path, origin, 'session', vi.fn(), picker, vi.fn().mockResolvedValue(response))
		).rejects.toThrow('disk denied');
		expect(cancel).toHaveBeenCalledOnce();
	});
	it('does not show a picker for unsafe or unsigned attachment links', async () => {
		const picker = vi.fn();
		await expect(
			saveAttachment('https://evil.example' + path, origin, 'secret', vi.fn(), picker)
		).rejects.toThrow('Unsupported');
		await expect(saveAttachment(path, origin, '', vi.fn(), picker)).rejects.toThrow('sign in');
		expect(picker).not.toHaveBeenCalled();
	});
	it('also bounds inline image downloads', async () => {
		const response = new Response('small', {
			headers: {
				'content-type': 'image/png',
				'content-length': String(MAX_BUFFERED_DOWNLOAD_BYTES + 1)
			}
		});
		await expect(
			fetchImageDownload('/image.png', origin, 'session', vi.fn().mockResolvedValue(response))
		).rejects.toThrow('64 MiB');
	});
	it('streams into the chosen file without allocating a Blob', async () => {
		const bytes: number[] = [];
		const close = vi.fn();
		const response = new Response(new Uint8Array([0, 255, 42]));
		const blob = vi.spyOn(response, 'blob');
		const picker = vi.fn().mockResolvedValue({
			createWritable: async () =>
				new WritableStream({
					write(chunk) {
						bytes.push(...chunk);
					},
					close
				})
		});
		const fetcher = vi.fn().mockResolvedValue(response);
		const save = vi.fn();
		const promise = saveAttachment(path, origin, 'session', save, picker, fetcher);
		expect(picker).toHaveBeenCalledWith({ suggestedName: 'report.pdf' });
		expect(fetcher).not.toHaveBeenCalled();
		expect(await promise).toBe('saved');
		expect(bytes).toEqual([0, 255, 42]);
		expect(close).toHaveBeenCalledOnce();
		expect(blob).not.toHaveBeenCalled();
		expect(save).not.toHaveBeenCalled();
	});
	it('cancels before fetching and never silently falls back on picker errors', async () => {
		const fetcher = vi.fn();
		const save = vi.fn();
		const picker = vi.fn().mockRejectedValue(new DOMException('cancelled', 'AbortError'));
		expect(await saveAttachment(path, origin, 'session', save, picker, fetcher)).toBe('cancelled');
		picker.mockRejectedValue(new DOMException('denied', 'NotAllowedError'));
		await expect(saveAttachment(path, origin, 'session', save, picker, fetcher)).rejects.toThrow(
			'denied'
		);
		expect(fetcher).not.toHaveBeenCalled();
		expect(save).not.toHaveBeenCalled();
	});
	it('does not open the destination writer for an authorization error', async () => {
		const createWritable = vi.fn();
		const picker = vi.fn().mockResolvedValue({ createWritable });
		await expect(
			saveAttachment(
				path,
				origin,
				'session',
				vi.fn(),
				picker,
				vi.fn().mockResolvedValue(new Response('denied', { status: 403 }))
			)
		).rejects.toThrow('access denied');
		expect(createWritable).not.toHaveBeenCalled();
	});
	it('aborts a partial stream on network failure rather than closing it', async () => {
		const abort = vi.fn();
		const close = vi.fn();
		const response = new Response(
			new ReadableStream({
				start(controller) {
					controller.error(new Error('connection reset'));
				}
			})
		);
		const picker = vi
			.fn()
			.mockResolvedValue({ createWritable: async () => new WritableStream({ abort, close }) });
		await expect(
			saveAttachment(path, origin, 'session', vi.fn(), picker, vi.fn().mockResolvedValue(response))
		).rejects.toThrow('connection reset');
		expect(abort).toHaveBeenCalledOnce();
		expect(close).not.toHaveBeenCalled();
	});
	it('rejects declared oversized files before reading', async () => {
		const cancel = vi.fn();
		const response = new Response(new ReadableStream({ cancel }), {
			headers: { 'content-length': String(MAX_BUFFERED_DOWNLOAD_BYTES + 1) }
		});
		await expect(boundedDownloadBlob(response)).rejects.toThrow('64 MiB');
		expect(cancel).toHaveBeenCalledOnce();
	});
	it('enforces the buffer limit even without a truthful Content-Length', async () => {
		const cancel = vi.fn();
		const chunk = new Uint8Array(1024 * 1024);
		const response = new Response(
			new ReadableStream({
				pull(controller) {
					controller.enqueue(chunk);
				},
				cancel
			}),
			{ headers: { 'content-length': '1' } }
		);
		await expect(boundedDownloadBlob(response)).rejects.toThrow('64 MiB');
		expect(cancel).toHaveBeenCalledOnce();
	});
	it('uses the bounded compatibility path when a picker is unavailable', async () => {
		const save = vi.fn();
		expect(
			await saveAttachment(
				path,
				origin,
				'session',
				save,
				undefined,
				vi.fn().mockResolvedValue(new Response('ok'))
			)
		).toBe('saved');
		expect(save).toHaveBeenCalledOnce();
		expect(await save.mock.calls[0][0].text()).toBe('ok');
	});
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
