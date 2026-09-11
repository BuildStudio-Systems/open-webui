import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	getThereWikiPages,
	getThereWikiPage,
	searchThereWiki,
	createThereWikiPage,
	updateThereWikiPage,
	deleteThereWikiPage
} from './index';

vi.mock('$lib/constants', () => ({ WEBUI_API_BASE_URL: '/api/v1' }));
afterEach(() => vi.unstubAllGlobals());
const setup = (body: unknown, status = 200) => {
	const fetchMock = vi.fn().mockImplementation(async () =>
		new Response(JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json' }
		})
	);
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
};

describe('THERE Wiki workspace API', () => {
	it('uses authenticated, uncached THERE pagination rather than exposing an engine endpoint', async () => {
		const fetchMock = setup({ items: [], has_more: true });
		expect(await getThereWikiPages('session-token', 'local-kb', 2)).toEqual({
			items: [],
			has_more: true
		});
		const [url, init] = fetchMock.mock.calls[0];
		expect(url).toBe('/api/v1/there/knowledge/local-kb/wiki/pages?page=2');
		expect(init.cache).toBe('no-store');
		expect(new Headers(init.headers).get('Authorization')).toBe('Bearer session-token');
	});
	it('encodes the whole slug and search query without query-parameter injection', async () => {
		const fetchMock = setup({ data: {} });
		await getThereWikiPage('session-token', 'local-kb', 'concept/中文&tenant=other');
		const url = new URL(fetchMock.mock.calls[0][0], 'https://there.test');
		expect(url.searchParams.get('slug')).toBe('concept/中文&tenant=other');
		expect(url.searchParams.has('tenant')).toBe(false);
		await searchThereWiki('session-token', 'local-kb', 'RAG & private');
		expect(new URL(fetchMock.mock.calls[1][0], 'https://there.test').searchParams.get('q')).toBe(
			'RAG & private'
		);
	});
	it('passes explicit create intent and leaves draft policy with the server', async () => {
		const fetchMock = setup({ data: { slug: 'rag', version: 1 } });
		const payload = { slug: 'rag', title: 'RAG', content: '    code()\n\n' };
		await createThereWikiPage('session-token', 'local-kb', payload, 'create-intent');
		const [, init] = fetchMock.mock.calls[0];
		expect(init.method).toBe('POST');
		expect(JSON.parse(init.body)).toEqual(payload);
		expect(new Headers(init.headers).get('Idempotency-Key')).toBe('create-intent');
	});
	it('preserves optimistic concurrency and the supplied intent on edit', async () => {
		const fetchMock = setup({ data: { slug: 'rag', version: 4 } });
		await updateThereWikiPage('session-token', 'local-kb', 'rag', 'Updated', 3, 'edit-intent');
		const [, init] = fetchMock.mock.calls[0];
		expect(init.method).toBe('PUT');
		expect(JSON.parse(init.body)).toEqual({ content: 'Updated', expected_version: 3 });
		expect(new Headers(init.headers).get('Idempotency-Key')).toBe('edit-intent');
	});
	it('does not retry an uncertain deletion', async () => {
		const fetchMock = setup(
			{ detail: { message: 'Check operation record', state: 'unknown' } },
			504
		);
		await expect(
			deleteThereWikiPage('session-token', 'local-kb', 'rag', 'delete-intent')
		).rejects.toThrow('Check operation record');
		expect(fetchMock).toHaveBeenCalledOnce();
		expect(fetchMock.mock.calls[0][1].method).toBe('DELETE');
	});
	it('reports disabled Wiki instead of returning an empty list', async () => {
		setup({ detail: 'Wiki is not enabled.' }, 409);
		await expect(getThereWikiPages('session-token', 'local-kb')).rejects.toThrow(
			'Wiki is not enabled.'
		);
	});
});
