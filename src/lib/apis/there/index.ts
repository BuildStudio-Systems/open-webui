import { WEBUI_API_BASE_URL } from '$lib/constants';

export type PersonalHistoryPage = {
	items: {
		chat_id: string;
		message_id: string;
		title: string;
		question: string;
		answer: string;
		truncated: boolean;
	}[];
	page: number;
	has_more: boolean;
	scope: string;
	audit_id?: string;
};
export const getPersonalHistory = (token: string, query: string, page: number, owner?: string) =>
	request<PersonalHistoryPage>(
		token,
		`${owner === undefined ? '/personal/history' : `/admin/personal-history/${encodeURIComponent(owner)}`}?${new URLSearchParams({ query, page: String(page) })}`
	);

export type ThereModule = { id: string; name: string; state: string; detail?: string };
export type ThereKnowledge = {
	id: string;
	name: string;
	description?: string;
	created_at?: number | string;
	state?: string;
	base_type?: 'document' | 'faq';
	type?: 'document' | 'faq';
};
export type ThereDocument = {
	id: string;
	title?: string;
	file_name?: string;
	parse_status?: string;
	created_at?: number | string;
};
export type ThereChunk = {
	id: string;
	content: string;
	content_revision?: number;
	chunk_index?: number;
	chunk_type?: string;
	index_status?: string;
};
export type ThereChunkRevision = {
	id?: string;
	revision: number;
	content: string;
	edited_at?: string;
	created_at?: string;
};
export type ThereFaq = {
	id: string;
	standard_question: string;
	answers: string[];
	similar_questions?: string[];
};
export type ThereFaqInput = {
	question: string;
	answers: string[];
	similar_questions: string[];
};
export type TherePage<T> = { items: T[]; total?: number; has_more?: boolean };
export type ThereSearchResult = {
	id?: string;
	content: string;
	score?: number;
	knowledge_id?: string;
	title?: string;
};
export type ThereSkill = {
	id: string;
	name: string;
	description?: string;
};
export type ThereSkillDetail = ThereSkill & {
	content: string;
	digest: string;
	version: string;
};
export type TherePaper = {
	id?: string;
	title: string;
	url?: string;
	authors?: (string | { name: string })[] | string;
	year?: string | number;
	source?: string;
	abstract?: string;
	doi?: string;
};
export type ThereResearch = {
	items: TherePaper[];
	sources?: { source: string; status: string; returned: number; retryable?: boolean }[];
	partial?: boolean;
};
export type ThereOperation = {
	id: string;
	resource_id?: string;
	action: string;
	state: string;
	error_code?: string;
	created_at?: number | string;
	updated_at?: number | string;
};
export type TherePaperInput = {
	title: string;
	url: string;
	authors: string[];
	year?: string | number;
	source?: string;
	abstract?: string;
};

const detailMessage = (detail: unknown): string | null => {
	if (typeof detail === 'string') return detail;
	if (detail && typeof detail === 'object') {
		if (Array.isArray(detail)) {
			return detail.map(detailMessage).filter(Boolean).join('；') || null;
		}
		const value = detail as Record<string, unknown>;
		for (const key of ['message', 'detail', 'msg', 'error', 'code']) {
			const message = detailMessage(value[key]);
			if (message) return message;
		}
	}
	return null;
};
const intentHeaders = (key: string) => ({ 'Idempotency-Key': key });

const request = async <T>(token: string, path: string, init: RequestInit = {}): Promise<T> => {
	const headers = new Headers(init.headers);
	headers.set('Accept', 'application/json');
	headers.set('Authorization', `Bearer ${token}`);
	if (init.body && !(init.body instanceof FormData)) {
		headers.set('Content-Type', 'application/json');
	}
	const response = await fetch(`${WEBUI_API_BASE_URL}/there${path}`, {
		...init,
		headers,
		cache: 'no-store'
	});
	if (!response.ok) {
		const payload = await response.json().catch(() => null);
		const detail = payload?.detail;
		const message =
			detailMessage(detail) ??
			(response.status === 401
				? '登录已过期，请重新登录。'
				: response.status === 403
					? '当前账号没有执行此操作的权限。'
					: `请求未完成（${response.status}），请稍后重试。`);
		throw new Error(message);
	}
	return response.status === 204 ? (undefined as T) : response.json();
};

const identifier = (id: string) => encodeURIComponent(id);

export type ThereWikiPage = {
	slug: string;
	title: string;
	content?: string;
	version?: number;
	status?: string;
};
export type ThereWikiInput = {
	slug: string;
	title: string;
	content: string;
};
const wikiPath = (id: string) => `/knowledge/${identifier(id)}/wiki`;
const wikiSlug = (slug: string) => new URLSearchParams({ slug });
export const getThereWikiPages = (token: string, id: string, page = 1) =>
	request<TherePage<ThereWikiPage>>(token, `${wikiPath(id)}/pages?page=${page}`);
export const getThereWikiPage = (token: string, id: string, slug: string) =>
	request<{ data: ThereWikiPage }>(token, `${wikiPath(id)}/page?${wikiSlug(slug)}`);
export const searchThereWiki = (token: string, id: string, q: string) =>
	request<{ items: ThereWikiPage[] }>(
		token,
		`${wikiPath(id)}/search?${new URLSearchParams({ q })}`
	);
export const createThereWikiPage = (
	token: string,
	id: string,
	payload: ThereWikiInput,
	key: string
) =>
	request<{ data: ThereWikiPage }>(token, `${wikiPath(id)}/pages`, {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify(payload)
	});
export const updateThereWikiPage = (
	token: string,
	id: string,
	slug: string,
	content: string,
	expectedVersion: number,
	key: string
) =>
	request<{ data: ThereWikiPage }>(token, `${wikiPath(id)}/page?${wikiSlug(slug)}`, {
		method: 'PUT',
		headers: intentHeaders(key),
		body: JSON.stringify({ content, expected_version: expectedVersion })
	});
export const deleteThereWikiPage = (token: string, id: string, slug: string, key: string) =>
	request<{ deleted: boolean }>(token, `${wikiPath(id)}/page?${wikiSlug(slug)}`, {
		method: 'DELETE',
		headers: intentHeaders(key)
	});

export const getThereStatus = (token: string) =>
	request<{ modules: ThereModule[]; version: string }>(token, '/status');

export const getThereKnowledge = (token: string) =>
	request<{ items: ThereKnowledge[] }>(token, '/knowledge');

export const createThereKnowledge = (
	token: string,
	name: string,
	description: string,
	key: string,
	baseType: 'document' | 'faq' = 'document'
) =>
	request<ThereKnowledge>(token, '/knowledge', {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify({ name, description, base_type: baseType })
	});

export const deleteThereKnowledge = (token: string, id: string, key: string) =>
	request<void>(token, `/knowledge/${identifier(id)}`, {
		method: 'DELETE',
		headers: intentHeaders(key)
	});

export const getThereDocuments = (token: string, id: string, page = 1) =>
	request<{ items: ThereDocument[]; total?: number; has_more?: boolean }>(
		token,
		`/knowledge/${identifier(id)}/documents?page=${page}`
	);

export const createThereDocument = (
	token: string,
	id: string,
	title: string,
	content: string,
	key: string
) =>
	request<ThereDocument>(token, `/knowledge/${identifier(id)}/documents/manual`, {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify({ title, content })
	});

export const uploadThereDocument = (token: string, id: string, file: File, key: string) => {
	const body = new FormData();
	body.append('file', file);
	return request<ThereDocument>(token, `/knowledge/${identifier(id)}/documents/file`, {
		method: 'POST',
		headers: intentHeaders(key),
		body
	});
};

export const deleteThereDocument = (token: string, id: string, documentId: string, key: string) =>
	request<void>(token, `/knowledge/${identifier(id)}/documents/${identifier(documentId)}`, {
		method: 'DELETE',
		headers: intentHeaders(key)
	});

export const searchThereKnowledge = (token: string, id: string, query: string) =>
	request<{ items: ThereSearchResult[] }>(token, `/knowledge/${identifier(id)}/search`, {
		method: 'POST',
		body: JSON.stringify({ query, limit: 5 })
	});

export const searchThereCatalog = (token: string, query: string, limit = 30) =>
	request<{ items: ThereSkill[]; total: number }>(
		token,
		`/catalog?${new URLSearchParams({ q: query, limit: String(limit) })}`
	);

export const getThereSkill = (token: string, id: string) =>
	request<ThereSkillDetail>(token, `/catalog/${identifier(id)}`);

export const activateThereSkill = (token: string, id: string, digest: string, key: string) =>
	request<{ skill_id: string; name: string }>(token, `/catalog/${identifier(id)}/activate`, {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify({ digest, reviewed: true })
	});

export const searchThereResearch = (token: string, query: string, limit = 10) =>
	request<ThereResearch>(
		token,
		`/research?${new URLSearchParams({ q: query, limit: String(limit) })}`
	);

export const getTherePapers = (token: string) => request<{ items: TherePaper[] }>(token, '/papers');

export const saveTherePaper = (token: string, paper: TherePaperInput, key: string) =>
	request<TherePaper>(token, '/papers', {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify(paper)
	});

export const getThereOperations = (token: string) =>
	request<{ items: ThereOperation[] }>(token, '/operations');

const documentPath = (id: string, documentId: string) =>
	`/knowledge/${identifier(id)}/documents/${identifier(documentId)}`;
const chunkPath = (id: string, documentId: string, chunkId: string) =>
	`${documentPath(id, documentId)}/chunks/${identifier(chunkId)}`;

export const reparseThereDocument = (token: string, id: string, documentId: string, key: string) =>
	request<unknown>(token, `${documentPath(id, documentId)}/reparse`, {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify({})
	});

export const getThereChunks = (token: string, id: string, documentId: string, page = 1) =>
	request<TherePage<ThereChunk>>(token, `${documentPath(id, documentId)}/chunks?page=${page}`);

export const updateThereChunk = (
	token: string,
	id: string,
	documentId: string,
	chunkId: string,
	content: string,
	expectedRevision: number,
	key: string
) =>
	request<unknown>(token, chunkPath(id, documentId, chunkId), {
		method: 'PUT',
		headers: intentHeaders(key),
		body: JSON.stringify({ content, expected_revision: expectedRevision })
	});

export const getThereChunkRevisions = (
	token: string,
	id: string,
	documentId: string,
	chunkId: string
) =>
	request<{ items: ThereChunkRevision[]; current_revision?: number }>(
		token,
		`${chunkPath(id, documentId, chunkId)}/revisions`
	);

export const restoreThereChunk = (
	token: string,
	id: string,
	documentId: string,
	chunkId: string,
	version: number,
	expectedRevision: number,
	key: string
) =>
	request<unknown>(token, `${chunkPath(id, documentId, chunkId)}/revert`, {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify({ version, expected_revision: expectedRevision })
	});

export const getThereFaq = (token: string, id: string, page = 1) =>
	request<TherePage<ThereFaq>>(token, `/knowledge/${identifier(id)}/faq?page=${page}`);

export const getThereFaqEntry = (token: string, id: string, entryId: string) =>
	request<{ data: ThereFaq }>(
		token,
		`/knowledge/${identifier(id)}/faq/${identifier(entryId)}`
	).then((result) => result.data);

export const createThereFaq = (token: string, id: string, payload: ThereFaqInput, key: string) =>
	request<unknown>(token, `/knowledge/${identifier(id)}/faq`, {
		method: 'POST',
		headers: intentHeaders(key),
		body: JSON.stringify(payload)
	});

export const updateThereFaq = (
	token: string,
	id: string,
	entryId: string,
	payload: ThereFaqInput,
	key: string
) =>
	request<unknown>(token, `/knowledge/${identifier(id)}/faq/${identifier(entryId)}`, {
		method: 'PUT',
		headers: intentHeaders(key),
		body: JSON.stringify(payload)
	});

export const deleteThereFaq = (token: string, id: string, entryId: string, key: string) =>
	request<unknown>(token, `/knowledge/${identifier(id)}/faq/${identifier(entryId)}`, {
		method: 'DELETE',
		headers: intentHeaders(key)
	});
