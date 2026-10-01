import { afterEach, describe, expect, it, vi } from 'vitest';
const engine = vi.hoisted(() => ({ getDocument: vi.fn(), GlobalWorkerOptions: { workerSrc: '' } }));
vi.mock('pdfjs-dist', () => engine);
vi.mock('pdfjs-dist/build/pdf.worker.mjs?url', () => ({ default: '/bundled-pdf-worker.mjs' }));
import { extractPdfText, loadPdfDocument } from './pdf-document';

afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals(); });

describe('bundled PDF loading and extraction', () => {
	it('cleans up a rejected loading task without hiding the original failure', async () => {
		const destroy = vi.fn().mockRejectedValue(new Error('cleanup failure'));
		engine.getDocument.mockImplementation(() => ({ promise: Promise.reject(new Error('invalid PDF')), destroy }));
		await expect(loadPdfDocument(new Uint8Array())).rejects.toThrow('invalid PDF');
		expect(destroy).toHaveBeenCalledOnce();
	});
	it('loads the bundled engine without depending on a window global', async () => {
		const poisoned = vi.fn(() => { throw new Error('untrusted global used'); });
		vi.stubGlobal('window', { pdfjsLib: { getDocument: poisoned } });
		const document = { numPages: 1 };
		engine.getDocument.mockReturnValue({ promise: Promise.resolve(document) });
		const data = new Uint8Array([1, 2, 3]);
		expect(await loadPdfDocument(data)).toBe(document);
		expect(engine.getDocument).toHaveBeenCalledWith({ data });
		expect(engine.GlobalWorkerOptions.workerSrc).toBe('/bundled-pdf-worker.mjs');
		expect(poisoned).not.toHaveBeenCalled();
	});
	it('extracts all pages in order and releases the document', async () => {
		const destroy = vi.fn().mockResolvedValue(undefined);
		const getPage = vi.fn(async (i) => ({ getTextContent: async () => ({ items: [{ str: `Page ${i}` }, { type: 'markedContent' }] }) }));
		engine.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages: 2, getPage, loadingTask: { destroy } }) });
		expect(await extractPdfText(new Uint8Array())).toBe('Page 1 \nPage 2 \n');
		expect(getPage.mock.calls.map(([n]) => n)).toEqual([1, 2]);
		expect(destroy).toHaveBeenCalledOnce();
	});
	it('releases worker resources even when page extraction fails', async () => {
		const destroy = vi.fn().mockResolvedValue(undefined);
		engine.getDocument.mockReturnValue({ promise: Promise.resolve({ numPages: 1, getPage: async () => { throw new Error('invalid page'); }, loadingTask: { destroy } }) });
		await expect(extractPdfText(new Uint8Array())).rejects.toThrow('invalid page');
		expect(destroy).toHaveBeenCalledOnce();
	});
});
