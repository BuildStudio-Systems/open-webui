import { afterEach, describe, expect, it, vi } from 'vitest';
import { getBackendConfig } from './index';
vi.mock('$lib/constants', () => ({ WEBUI_BASE_URL: '' }));
vi.mock('$lib/utils', () => ({ convertOpenApiToToolPayload: vi.fn() }));
vi.mock('$lib/utils/tags', () => ({ normalizeTags: vi.fn() }));
vi.mock('./openai', () => ({ getOpenAIModelsDirect: vi.fn() }));
afterEach(() => { vi.unstubAllGlobals(); });

describe('authenticated application configuration', () => {
	it('reads the current bearer token for each request without caching', async () => {
		let token: string | null = 'session-one';
		vi.stubGlobal('localStorage', {getItem: () => token});
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({features:{enable_web_search:true}})));
		vi.stubGlobal('fetch', fetchMock);
		await getBackendConfig();
		token = 'session-two';
		await getBackendConfig();
		expect(new Headers((fetchMock.mock.calls[0] as unknown as [string,RequestInit])[1].headers).get('Authorization')).toBe('Bearer session-one');
		expect(new Headers((fetchMock.mock.calls[1] as unknown as [string,RequestInit])[1].headers).get('Authorization')).toBe('Bearer session-two');
		expect((fetchMock.mock.calls[1] as unknown as [string,RequestInit])[1].cache).toBe('no-store');
	});
	it('keeps the pre-login request anonymous', async () => {
		vi.stubGlobal('localStorage', {getItem: () => null});
		const fetchMock = vi.fn(async () => new Response('{}'));
		vi.stubGlobal('fetch', fetchMock);
		await getBackendConfig();
		expect(new Headers((fetchMock.mock.calls[0] as unknown as [string,RequestInit])[1].headers).has('Authorization')).toBe(false);
	});
});
