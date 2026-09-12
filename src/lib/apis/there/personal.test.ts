import { afterEach, describe, expect, it, vi } from 'vitest';
import { getPersonalHistory } from './index';
import { getUsers } from '../users';

vi.mock('$lib/constants', () => ({ WEBUI_API_BASE_URL: '/api/v1' }));
afterEach(() => vi.unstubAllGlobals());
function mockResponse(body: unknown, status = 200) {
	const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

describe('Personal history and administrator customer selection', () => {
	it('keeps own history on the personal route with uncached authentication', async () => {
		const fetchMock = mockResponse({ items: [], page: 2, has_more: false, scope: 'personal' });
		await getPersonalHistory('test-session', '问题 & owner=other', 2);
		const [raw, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
		const url = new URL(raw, 'https://there.test');
		expect(url.pathname).toBe('/api/v1/there/personal/history');
		expect(url.searchParams.get('query')).toBe('问题 & owner=other');
		expect(url.searchParams.has('owner')).toBe(false);
		expect(url.searchParams.get('page')).toBe('2');
		expect(options.cache).toBe('no-store');
		expect(new Headers(options.headers).get('Authorization')).toBe('Bearer test-session');
	});
	it('uses the exact selected THERE ID and preserves the audit receipt', async () => {
		mockResponse({ items: [], audit_id: 'audit-receipt', scope: 'admin' });
		const response = await getPersonalHistory('test-session', '', 1, 'there/id?other=1');
		expect(response.audit_id).toBe('audit-receipt');
		expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
			'/api/v1/there/admin/personal-history/there%2Fid%3Fother%3D1?query=&page=1'
		);
	});
	it('does not turn a rejected administrator request into an empty success', async () => {
		const fetchMock = mockResponse({ detail: 'Access denied' }, 403);
		await expect(getPersonalHistory('test-session', '', 1, 'customer')).rejects.toThrow(
			'Access denied'
		);
		expect(fetchMock).toHaveBeenCalledOnce();
	});
	it('uses the existing administrator-only paged user directory without caching', async () => {
		const fetchMock = mockResponse({
			users: [{ id: 'there-id', name: 'Example', email: 'test@example.invalid' }],
			total: 31
		});
		const result = await getUsers(
			'test-session',
			'test+customer@example.invalid',
			undefined,
			undefined,
			2
		);
		const [raw, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
		const url = new URL(raw, 'https://there.test');
		expect(url.pathname).toBe('/api/v1/users/');
		expect(url.searchParams.get('query')).toBe('test+customer@example.invalid');
		expect(url.searchParams.get('page')).toBe('2');
		expect(options.cache).toBe('no-store');
		expect(new Headers(options.headers).get('Authorization')).toBe('Bearer test-session');
		expect(result.users[0].id).toBe('there-id');
	});
});
