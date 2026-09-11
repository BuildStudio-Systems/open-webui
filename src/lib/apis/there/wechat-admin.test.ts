import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { WeChatAdminApiError, listWeChatAdminChats, listWeChatAdminMessages } from './wechat-admin';

const jsonResponse = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), {
		status,
		headers: { 'Content-Type': 'application/json' }
	});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('WeChat administrator chat API', () => {
	it('requests metadata-only chat pages with administrator authentication', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			jsonResponse({
				items: [
					{
						id: '11111111-1111-4111-8111-111111111111',
						account_ref: 'wx-123456789abc',
						model: 'there-3.8',
						thinking_mode: 'off',
						created_at: 10,
						updated_at: 20,
						message_count: 2,
						turn_count: 1,
						report_count: 0,
						state: 'completed',
						title: 'must not escape the response projection',
						content: 'must not escape the response projection',
						openid: 'must not escape the response projection'
					}
				],
				next_cursor: 'next-page'
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const page = await listWeChatAdminChats('admin-token', null, 50);

		expect(fetchMock).toHaveBeenCalledOnce();
		const [url, init] = fetchMock.mock.calls[0];
		expect(url).toBe('/api/v1/there/admin/wechat/chats?limit=50');
		expect(init).toMatchObject({ method: 'GET', cache: 'no-store' });
		expect(new Headers(init.headers).get('Authorization')).toBe('Bearer admin-token');
		expect(page.next_cursor).toBe('next-page');
		expect(page.items).toHaveLength(1);
		expect(page.items[0]).not.toHaveProperty('title');
		expect(page.items[0]).not.toHaveProperty('content');
		expect(page.items[0]).not.toHaveProperty('openid');
	});

	it('keeps only credential-free HTTPS sources and removes query data', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			jsonResponse({
				items: [
					{
						id: '22222222-2222-4222-8222-222222222222',
						role: 'assistant',
						content: '<img src=x onerror=alert(1)>',
						created_at: 30,
						sources: [
							{
								name: '<script>alert(1)</script>',
								url: 'https://example.com/paper?q=private#part'
							},
							{ name: 'Plain HTTP', url: 'http://example.com/' },
							{ name: 'Script', url: 'javascript:alert(1)' },
							{ name: 'Credentials', url: 'https://user:password@example.com/private' },
							{ name: 'Localhost', url: 'https://localhost/admin' },
							{ name: 'Localhost FQDN', url: 'https://localhost./admin' },
							{ name: 'Internal', url: 'https://service.internal/admin' },
							{ name: 'mDNS', url: 'https://service.local/admin' },
							{ name: 'Single label', url: 'https://printer/admin' },
							{ name: 'Loopback', url: 'https://127.0.0.1/admin' },
							{ name: 'Private', url: 'https://10.0.0.8/admin' },
							{ name: 'Link local', url: 'https://169.254.169.254/latest' },
							{ name: 'IPv6 loopback', url: 'https://[::1]/admin' },
							{ name: 'IPv6 link local', url: 'https://[fe80::1]/admin' },
							{ name: 'Public IP literal', url: 'https://8.8.8.8/admin' },
							{ name: 'Nonstandard port', url: 'https://example.net:8443/admin' }
						]
					}
				],
				next_cursor: null
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const page = await listWeChatAdminMessages(
			'admin-token',
			'11111111-1111-4111-8111-111111111111'
		);

		expect(page.items[0].content).toBe('<img src=x onerror=alert(1)>');
		expect(page.items[0].sources).toEqual([
			{ name: '<script>alert(1)</script>', url: 'https://example.com/paper' }
		]);
	});

	it.each([
		[403, 'not enabled'],
		[503, 'temporarily unavailable']
	])('preserves the actionable status for HTTP %i', async (status, text) => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue(jsonResponse({ detail: 'private detail' }, status))
		);

		try {
			await listWeChatAdminChats('admin-token');
			throw new Error('request unexpectedly succeeded');
		} catch (error) {
			expect(error).toBeInstanceOf(WeChatAdminApiError);
			expect(error).toMatchObject({ status });
			expect((error as Error).message).toContain(text);
			expect((error as Error).message).not.toContain('private detail');
		}
	});
});

describe('WeChat administrator chat page policy', () => {
	it('renders records as escaped text and opens only noreferrer source links', () => {
		const source = readFileSync(
			new URL('../../../routes/(app)/admin/wechat/+page.svelte', import.meta.url),
			'utf8'
		);

		expect(source).not.toContain('{@html');
		expect(source).not.toContain('<input');
		expect(source).not.toContain('deleteChat');
		expect(source).not.toContain('shareChat');
		expect(source).toContain('href={source.url}');
		expect(source).toContain('rel="noreferrer"');
	});
});
