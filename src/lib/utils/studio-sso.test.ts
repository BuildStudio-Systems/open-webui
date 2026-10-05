import { describe, expect, it } from 'vitest';
import { sharedSignInAction, sharedSignInCopy, sharedSignInFailed } from './studio-sso';

describe('shared sign-in navigation', () => {
	it.each([
		['', false], ['?sso=login_required', false], ['?sso=complete', false],
		['?sso=expired', true], ['?sso=unavailable', true], ['?sso=access_denied', true]
	])('reports actual shared sign-in failures %s', (query, failed) => {
		expect(sharedSignInFailed(new URL('https://buildstudio-there.com/auth' + query))).toBe(failed);
	});
	it.each([
		['', true, false, 'start'],
		['', false, false, 'none'],
		['', true, true, 'none'],
		['?state=logout', true, false, 'none'],
		['?sso=login_required', true, false, 'none'],
		['?sso=unavailable', true, false, 'none'],
		['?sso=complete', true, false, 'complete'],
		['?sso=complete', true, true, 'complete'],
		['?sso=complete&state=logout', true, false, 'none'],
		['?form=signin', true, false, 'none'],
		['?error=denied', true, false, 'none']
	])('guards %s / enabled=%s / authenticated=%s', (query, enabled, authenticated, expected) => {
		expect(sharedSignInAction(new URL('https://buildstudio-there.com/auth' + query), enabled, authenticated)).toBe(expected);
	});
	it('provides independent English, Japanese and Chinese labels', () => {
		expect(sharedSignInCopy('en-US').button).toBe('Use Systems sign-in');
		expect(sharedSignInCopy('ja-JP').button).toContain('ログイン');
		expect(sharedSignInCopy('zh-CN').button).toContain('登录');
	});
});
