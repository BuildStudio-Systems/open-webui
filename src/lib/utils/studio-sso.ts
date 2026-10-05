// Shared sign-in uses only fixed same-origin endpoints. Tokens never enter URLs.
export function sharedSignInAction(url: URL, enabled: boolean, authenticated: boolean) {
	if (!enabled || url.searchParams.get('state') === 'logout') return 'none';
	if (url.searchParams.get('sso') === 'complete') return 'complete';
	if (authenticated) return 'none';
	if (url.searchParams.has('sso') || url.searchParams.has('error') || url.searchParams.has('form')) return 'none';
	return 'start';
}

export function sharedSignInCopy(language: string) {
	if (language?.startsWith('ja')) return { button: 'Systemsのログインを使用', failed: '共通ログインを完了できませんでした。通常のログインを使用するか、再試行してください。' };
	if (language?.startsWith('zh')) return { button: '使用 Systems 登录状态', failed: '共享登录未完成，可以使用当前登录表单或重试。' };
	return { button: 'Use Systems sign-in', failed: 'Shared sign-in could not finish. Use the login form or try again.' };
}

// A first visit without a Systems session is an ordinary sign-in, not a failure.
export function sharedSignInFailed(url: URL) {
	const result = url.searchParams.get('sso');
	return !!result && !['login_required', 'complete'].includes(result);
}
