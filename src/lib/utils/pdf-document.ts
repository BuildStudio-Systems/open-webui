import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url';

// Only use the bundled module, never a mutable window.pdfjsLib supplied by a page.
export async function loadPdfDocument(data: ArrayBuffer | Uint8Array) {
	const pdfjs = await import('pdfjs-dist');
	pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
	const task = pdfjs.getDocument({ data });
	try {
		return await task.promise;
	} catch (error) {
		await task.destroy().catch(() => {});
		throw error;
	}
}

export async function extractPdfText(data: ArrayBuffer | Uint8Array): Promise<string> {
	const document = await loadPdfDocument(data);
	try {
		const pages: string[] = [];
		for (let index = 1; index <= document.numPages; index++) {
			const page = await document.getPage(index);
			const content = await page.getTextContent();
			pages.push(content.items.map((item) => ('str' in item ? item.str : '')).join(' ') + '\n');
		}
		return pages.join('');
	} finally {
		await document.loadingTask.destroy();
	}
}
