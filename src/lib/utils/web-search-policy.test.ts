import { describe, expect, it } from 'vitest';
import { canUseWebSearch, DEFAULT_WEB_SEARCH_ENABLED } from './web-search-policy';

describe('web search opt-in policy', () => {
	it('starts off independently of account role', () => {
		expect(DEFAULT_WEB_SEARCH_ENABLED).toBe(false);
	});
	it('allows an ordinary permitted user without admin privileges', () => {
		expect(canUseWebSearch('user', true, true, true)).toBe(true);
	});
	it('allows administrators without forcing the toggle on', () => {
		expect(canUseWebSearch('admin', false, true, true)).toBe(true);
		expect(DEFAULT_WEB_SEARCH_ENABLED).toBe(false);
	});
	it('retains server, capability and user permission gates', () => {
		expect(canUseWebSearch('user', false, true, true)).toBe(false);
		expect(canUseWebSearch('user', true, false, true)).toBe(false);
		expect(canUseWebSearch('user', true, true, false)).toBe(false);
	});
	it('does not grant access to anonymous or pending accounts', () => {
		expect(canUseWebSearch(undefined, true, true, true)).toBe(false);
		expect(canUseWebSearch('pending', true, true, true)).toBe(false);
	});
});
