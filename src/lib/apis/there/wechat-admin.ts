import { WEBUI_API_BASE_URL } from '$lib/constants';

export type WeChatAdminChat = {
	id: string;
	account_ref: string;
	model: string;
	thinking_mode: string;
	created_at: number;
	updated_at: number;
	message_count: number;
	turn_count: number;
	report_count: number;
	state: string;
};

export type WeChatAdminSource = {
	name: string;
	url: string;
};

export type WeChatAdminMessage = {
	id: string;
	role: 'user' | 'assistant';
	content: string;
	sources: WeChatAdminSource[];
	created_at: number;
};

export type WeChatAdminPage<T> = {
	items: T[];
	next_cursor: string | null;
};

export class WeChatAdminApiError extends Error {
	status: number;

	constructor(status: number, message: string) {
		super(message);
		this.name = 'WeChatAdminApiError';
		this.status = status;
	}
}

const API_ROOT = `${WEBUI_API_BASE_URL}/there/admin/wechat`;

const isRecord = (value: unknown): value is Record<string, unknown> =>
	typeof value === 'object' && value !== null && !Array.isArray(value);

const safeString = (value: unknown, maximum: number): string =>
	typeof value === 'string' ? value.slice(0, maximum) : '';

const safeCount = (value: unknown): number =>
	typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : 0;

const safeTimestamp = (value: unknown): number =>
	typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : 0;

const safeCursor = (value: unknown): string | null =>
	typeof value === 'string' && value.length > 0 && value.length <= 256 ? value : null;

const BLOCKED_HOST_SUFFIXES = new Set([
	'alt',
	'corp',
	'home',
	'home.arpa',
	'internal',
	'intranet',
	'invalid',
	'lan',
	'local',
	'localdomain',
	'localhost',
	'onion',
	'private',
	'test'
]);

const isPublicHostname = (value: string): boolean => {
	if (!value || value.endsWith('.') || value.includes(':') || value.length > 253) return false;
	const hostname = value.toLowerCase();
	const labels = hostname.split('.');
	if (
		labels.length < 2 ||
		labels.some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
	) {
		return false;
	}
	if (/^\d+$/.test(labels.at(-1) ?? '')) return false;
	return ![...BLOCKED_HOST_SUFFIXES].some(
		(suffix) => hostname === suffix || hostname.endsWith(`.${suffix}`)
	);
};

const safeHttpsUrl = (value: unknown): string | null => {
	if (typeof value !== 'string' || value.length > 2048) return null;
	try {
		const parsed = new URL(value);
		if (
			parsed.protocol !== 'https:' ||
			!isPublicHostname(parsed.hostname) ||
			parsed.username !== '' ||
			parsed.password !== '' ||
			parsed.port !== ''
		) {
			return null;
		}
		parsed.search = '';
		parsed.hash = '';
		return parsed.toString();
	} catch {
		return null;
	}
};

const normalizeChat = (value: unknown): WeChatAdminChat | null => {
	if (!isRecord(value)) return null;
	const id = safeString(value.id, 64);
	const accountRef = safeString(value.account_ref, 64);
	if (!id || !accountRef) return null;
	return {
		id,
		account_ref: accountRef,
		model: safeString(value.model, 128),
		thinking_mode: safeString(value.thinking_mode, 16),
		created_at: safeTimestamp(value.created_at),
		updated_at: safeTimestamp(value.updated_at),
		message_count: safeCount(value.message_count),
		turn_count: safeCount(value.turn_count),
		report_count: safeCount(value.report_count),
		state: safeString(value.state, 32)
	};
};

const normalizeMessage = (value: unknown): WeChatAdminMessage | null => {
	if (!isRecord(value)) return null;
	const id = safeString(value.id, 64);
	const role = value.role === 'user' || value.role === 'assistant' ? value.role : null;
	if (!id || !role) return null;
	const sources = Array.isArray(value.sources)
		? value.sources.slice(0, 32).flatMap((source): WeChatAdminSource[] => {
				if (!isRecord(source)) return [];
				const url = safeHttpsUrl(source.url);
				if (!url) return [];
				return [{ name: safeString(source.name, 256), url }];
			})
		: [];
	return {
		id,
		role,
		content: safeString(value.content, 20_000),
		sources: sources.slice(0, 8),
		created_at: safeTimestamp(value.created_at)
	};
};

const request = async (token: string, path: string): Promise<unknown> => {
	let response: Response;
	try {
		response = await fetch(`${API_ROOT}${path}`, {
			method: 'GET',
			headers: {
				Accept: 'application/json',
				Authorization: `Bearer ${token}`
			},
			cache: 'no-store'
		});
	} catch {
		throw new WeChatAdminApiError(
			0,
			'Unable to reach the WeChat chat records service. Please try again.'
		);
	}

	if (!response.ok) {
		const message =
			response.status === 401
				? 'Your session has expired. Please sign in again.'
				: response.status === 403
					? 'WeChat chat record access is not enabled for this administrator.'
					: response.status === 404
						? 'This WeChat chat record no longer exists.'
						: response.status === 503
							? 'WeChat chat records are temporarily unavailable. Please try again.'
							: `Unable to load WeChat chat records (${response.status}).`;
		throw new WeChatAdminApiError(response.status, message);
	}

	try {
		return await response.json();
	} catch {
		throw new WeChatAdminApiError(502, 'The WeChat chat records service returned invalid data.');
	}
};

const normalizePage = <T>(
	value: unknown,
	normalizeItem: (item: unknown) => T | null
): WeChatAdminPage<T> => {
	if (!isRecord(value) || !Array.isArray(value.items)) {
		throw new WeChatAdminApiError(502, 'The WeChat chat records service returned invalid data.');
	}
	return {
		items: value.items.flatMap((item): T[] => {
			const normalized = normalizeItem(item);
			return normalized ? [normalized] : [];
		}),
		next_cursor: safeCursor(value.next_cursor)
	};
};

const pagePath = (path: string, cursor: string | null, limit: number): string => {
	const query = new URLSearchParams({ limit: String(limit) });
	if (cursor) query.set('cursor', cursor);
	return `${path}?${query.toString()}`;
};

export const listWeChatAdminChats = async (
	token: string,
	cursor: string | null = null,
	limit = 50
): Promise<WeChatAdminPage<WeChatAdminChat>> =>
	normalizePage(await request(token, pagePath('/chats', cursor, limit)), normalizeChat);

export const getWeChatAdminChat = async (
	token: string,
	chatId: string
): Promise<WeChatAdminChat> => {
	const chat = normalizeChat(await request(token, `/chats/${encodeURIComponent(chatId)}`));
	if (!chat) {
		throw new WeChatAdminApiError(502, 'The WeChat chat records service returned invalid data.');
	}
	return chat;
};

export const listWeChatAdminMessages = async (
	token: string,
	chatId: string,
	cursor: string | null = null,
	limit = 100
): Promise<WeChatAdminPage<WeChatAdminMessage>> =>
	normalizePage(
		await request(token, pagePath(`/chats/${encodeURIComponent(chatId)}/messages`, cursor, limit)),
		normalizeMessage
	);
