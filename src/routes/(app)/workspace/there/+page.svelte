<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { user } from '$lib/stores';
	import ThereWiki from '$lib/components/workspace/ThereWiki.svelte';
	import TherePersonalHistory from '$lib/components/workspace/TherePersonalHistory.svelte';
	import {
		activateThereSkill,
		createThereDocument,
		createThereKnowledge,
		createThereFaq,
		deleteThereDocument,
		deleteThereKnowledge,
		deleteThereFaq,
		getThereChunks,
		getThereChunkRevisions,
		getThereDocuments,
		getThereKnowledge,
		getThereFaq,
		getThereFaqEntry,
		getThereOperations,
		getTherePapers,
		getThereSkill,
		getThereStatus,
		searchThereCatalog,
		searchThereKnowledge,
		searchThereResearch,
		saveTherePaper,
		reparseThereDocument,
		restoreThereChunk,
		updateThereChunk,
		updateThereFaq,
		uploadThereDocument,
		type ThereDocument,
		type ThereChunk,
		type ThereChunkRevision,
		type ThereFaq,
		type ThereKnowledge,
		type ThereModule,
		type ThereOperation,
		type TherePaper,
		type ThereResearch,
		type ThereSearchResult,
		type ThereSkill,
		type ThereSkillDetail
	} from '$lib/apis/there';

	type Section = 'knowledge' | 'skills' | 'research' | 'status' | 'personal' | 'admin-history';
	const sections: { id: Section; label: string }[] = [
		{ id: 'knowledge', label: '知识' },
		{ id: 'personal', label: '个人问答' },
		{ id: 'admin-history', label: '管理员调取' },
		{ id: 'skills', label: '技能' },
		{ id: 'research', label: '论文' },
		{ id: 'status', label: '运行状态' }
	];
	const inputClass =
		'w-full rounded-xl border border-gray-200 bg-transparent px-3 py-2 text-sm dark:border-gray-700 disabled:opacity-50';
	const primaryClass =
		'rounded-xl bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-40 dark:bg-white dark:text-gray-900';
	const secondaryClass =
		'rounded-xl border border-gray-200 px-3 py-2 text-sm hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:hover:bg-gray-800';
	let section: Section = 'knowledge';
	let errors: Record<Section, string> = {
		knowledge: '',
		skills: '',
		research: '',
		status: '',
		personal: '',
		'admin-history': ''
	};
	let notice = '';
	let modules: ThereModule[] = [];
	let version = '';
	let statusLoading = false;
	let operations: ThereOperation[] = [];
	let knowledge: ThereKnowledge[] = [];
	let knowledgeLoading = false;
	let selectedKnowledge: ThereKnowledge | null = null;
	let documents: ThereDocument[] = [];
	let documentsLoading = false;
	let documentsPage = 1;
	let documentsTotal: number | undefined;
	let documentsHasMore = false;
	let documentsRequest = 0;
	let selectedDocument: ThereDocument | null = null;
	let chunks: ThereChunk[] = [];
	let chunksPage = 1;
	let chunksHasMore = false;
	let chunksLoading = false;
	let chunksRequest = 0;
	let selectedChunk: ThereChunk | null = null;
	let chunkContent = '';
	let chunkRevisions: ThereChunkRevision[] = [];
	let revisionsLoading = false;
	let revisionsRequest = 0;
	let restoreVersion = '';
	let faqEntries: ThereFaq[] = [];
	let faqPage = 1;
	let faqHasMore = false;
	let faqLoading = false;
	let faqRequest = 0;
	let faqEditingId = '';
	let faqFormOpen = false;
	let faqQuestion = '';
	let faqAnswers = [''];
	let faqSimilarQuestions = '';
	let knowledgeBusy = false;
	let newName = '';
	let newDescription = '';
	let newBaseType: 'document' | 'faq' = 'document';
	let showCreate = false;
	let documentTitle = '';
	let documentContent = '';
	let documentFiles: FileList | undefined;
	let documentInput: HTMLInputElement;
	let knowledgeQuery = '';
	let searchResults: ThereSearchResult[] = [];
	let searchedKnowledge = false;
	let searchLoading = false;
	let catalogQuery = '';
	let catalog: ThereSkill[] = [];
	let catalogTotal = 0;
	let catalogLoaded = false;
	let catalogLoading = false;
	let selectedSkill: ThereSkillDetail | null = null;
	let skillLoading = false;
	let skillBusy = false;
	let reviewed = false;
	let skillRequest = 0;
	let researchQuery = '';
	let research: ThereResearch | null = null;
	let researchLoading = false;
	let savedPapers: TherePaper[] = [];
	let papersLoading = false;
	let papersLoaded = false;
	let paperBusy = false;
	let paperKnowledgeId = '';
	let selectedUrlKnowledge = '';
	const intents = new Map<string, { signature: string; key: string }>();

	$: canManageKnowledge =
		$user?.role === 'admin' || Boolean($user?.permissions?.workspace?.knowledge);
	$: canManageSkills = $user?.role === 'admin' || Boolean($user?.permissions?.workspace?.skills);
	$: isFaq = (selectedKnowledge?.base_type ?? selectedKnowledge?.type) === 'faq';
	$: documentKnowledge = knowledge.filter((item) => (item.base_type ?? item.type) !== 'faq');
	$: selectedRevision = chunkRevisions.find((entry) => String(entry.revision) === restoreVersion);
	$: if ($page.url.searchParams.get('knowledge') && knowledge.length) {
		const id = $page.url.searchParams.get('knowledge') ?? '';
		const item = knowledge.find((entry) => entry.id === id);
		if (item && selectedUrlKnowledge !== id) {
			selectedUrlKnowledge = id;
			selectKnowledge(item);
		}
	}

	// Keep a write intent stable after an uncertain response. Only a successful
	// response or changed input starts a new intent; no request is retried here.
	async function writeIntent<T>(
		name: string,
		payload: unknown,
		execute: (key: string) => Promise<T>
	) {
		const signature = JSON.stringify(payload);
		let intent = intents.get(name);
		if (!intent || intent.signature !== signature) {
			intent = { signature, key: crypto.randomUUID() };
			intents.set(name, intent);
		}
		const result = await execute(intent.key);
		if (intents.get(name) === intent) intents.delete(name);
		return result;
	}

	const errorMessage = (error: unknown) =>
		error instanceof Error ? error.message : '操作未完成，请稍后重试。';
	const setError = (target: Section, error: unknown) =>
		(errors = { ...errors, [target]: errorMessage(error) });
	const clearError = (target: Section) => (errors = { ...errors, [target]: '' });
	const safeUrl = (url?: string) => {
		try {
			const parsed = new URL(url ?? '');
			return ['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password
				? parsed.href
				: null;
		} catch {
			return null;
		}
	};
	const authorsText = (authors: TherePaper['authors']) =>
		Array.isArray(authors)
			? authors.map((author) => (typeof author === 'string' ? author : author.name)).join('、')
			: (authors ?? '');
	const savedPaper = (paper: TherePaper) =>
		savedPapers.some((entry) =>
			paper.url ? entry.url === paper.url : entry.title === paper.title
		);
	const operationTime = (value?: number | string) => {
		if (!value) return '';
		const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
		return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
	};
	const parseState = (state?: string) =>
		({
			completed: '已就绪',
			ready: '已就绪',
			success: '已就绪',
			pending: '等待解析',
			processing: '解析中',
			running: '解析中',
			failed: '解析失败',
			error: '解析失败'
		})[state ?? ''] ??
		state ??
		'等待更新';
	const stateLabel = (state: string) =>
		({
			active: '正常',
			ready: '正常',
			healthy: '正常',
			available: '可用',
			configured: '已配置',
			disabled: '未启用',
			unavailable: '不可用',
			degraded: '部分可用',
			error: '异常'
		})[state] ?? state;

	async function loadStatus() {
		statusLoading = true;
		clearError('status');
		try {
			const result = await getThereStatus(localStorage.token);
			modules = result.modules;
			version = result.version;
			operations = (await getThereOperations(localStorage.token)).items;
		} catch (error) {
			setError('status', error);
		} finally {
			statusLoading = false;
		}
	}
	async function loadKnowledge() {
		knowledgeLoading = true;
		clearError('knowledge');
		try {
			knowledge = (await getThereKnowledge(localStorage.token)).items;
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeLoading = false;
		}
	}
	async function loadDocuments(id: string, targetPage = documentsPage) {
		const request = ++documentsRequest;
		documentsLoading = true;
		try {
			const result = await getThereDocuments(localStorage.token, id, targetPage);
			if (selectedKnowledge?.id === id && request === documentsRequest) {
				documents = result.items;
				documentsPage = targetPage;
				documentsTotal = result.total;
				documentsHasMore =
					result.has_more ??
					(typeof result.total === 'number'
						? targetPage * 50 < result.total
						: result.items.length === 50);
			}
		} catch (error) {
			if (selectedKnowledge?.id === id && request === documentsRequest)
				setError('knowledge', error);
		} finally {
			if (request === documentsRequest) documentsLoading = false;
		}
	}
	async function selectKnowledge(item: ThereKnowledge) {
		selectedKnowledge = item;
		paperKnowledgeId = (item.base_type ?? item.type) === 'faq' ? '' : item.id;
		selectedDocument = null;
		chunks = [];
		selectedChunk = null;
		chunkRevisions = [];
		++chunksRequest;
		++revisionsRequest;
		chunksLoading = false;
		revisionsLoading = false;
		faqEntries = [];
		faqPage = 1;
		faqHasMore = false;
		resetFaqForm();
		documents = [];
		documentsPage = 1;
		documentsTotal = undefined;
		documentsHasMore = false;
		searchResults = [];
		searchedKnowledge = false;
		knowledgeQuery = '';
		documentTitle = '';
		documentContent = '';
		documentFiles = undefined;
		if (documentInput) documentInput.value = '';
		clearError('knowledge');
		if ((item.base_type ?? item.type) === 'faq') await loadFaq(item.id, 1);
		else await loadDocuments(item.id);
	}
	async function createKnowledge() {
		if (!newName.trim() || knowledgeBusy) return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			const item = await writeIntent(
				'create-knowledge',
				[newName.trim(), newDescription.trim(), newBaseType],
				(key) =>
					createThereKnowledge(
						localStorage.token,
						newName.trim(),
						newDescription.trim(),
						key,
						newBaseType
					)
			);
			knowledge = [item, ...knowledge];
			newName = '';
			newDescription = '';
			newBaseType = 'document';
			showCreate = false;
			notice =
				(item.base_type ?? item.type) === 'faq'
					? '问答知识库已创建。可以录入标准问题与答案。'
					: '知识库已创建。可以上传文件或写入文本。';
			await selectKnowledge(item);
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function removeKnowledge() {
		const item = selectedKnowledge;
		if (
			!item ||
			knowledgeBusy ||
			!window.confirm(`确认删除知识库“${item.name}”及其中的文档和索引？此操作无法撤销。`)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent('delete-knowledge', item.id, (key) =>
				deleteThereKnowledge(localStorage.token, item.id, key)
			);
			knowledge = knowledge.filter((entry) => entry.id !== item.id);
			selectedKnowledge = null;
			documents = [];
			notice = '知识库及其内容已删除。';
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function addDocument(kind: 'manual' | 'file') {
		const item = selectedKnowledge;
		const file = documentFiles?.[0];
		if (!item || knowledgeBusy || (kind === 'file' && !file)) return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			if (kind === 'file' && file) {
				await writeIntent(
					'upload-document',
					[item.id, file.name, file.size, file.lastModified],
					(key) => uploadThereDocument(localStorage.token, item.id, file, key)
				);
				documentFiles = undefined;
				if (documentInput) documentInput.value = '';
			} else {
				await writeIntent(
					'manual-document',
					[item.id, documentTitle.trim(), documentContent.trim()],
					(key) =>
						createThereDocument(
							localStorage.token,
							item.id,
							documentTitle.trim(),
							documentContent.trim(),
							key
						)
				);
				documentTitle = '';
				documentContent = '';
			}
			notice = '文档已提交，解析和索引可能需要一些时间。';
			await loadDocuments(item.id, 1);
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function removeDocument(document: ThereDocument) {
		const item = selectedKnowledge;
		if (
			!item ||
			knowledgeBusy ||
			!window.confirm(`确认永久删除文档“${document.title || document.file_name || document.id}”？`)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent('delete-document', [item.id, document.id], (key) =>
				deleteThereDocument(localStorage.token, item.id, document.id, key)
			);
			documents = documents.filter((entry) => entry.id !== document.id);
			if (selectedDocument?.id === document.id) {
				selectedDocument = null;
				selectedChunk = null;
				++chunksRequest;
				++revisionsRequest;
			}
			searchResults = [];
			searchedKnowledge = false;
			notice = '文档已删除。';
			await loadDocuments(
				item.id,
				!documents.length && documentsPage > 1 ? documentsPage - 1 : documentsPage
			);
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function searchKnowledge() {
		const item = selectedKnowledge;
		if (!item || !knowledgeQuery.trim() || searchLoading) return;
		searchLoading = true;
		clearError('knowledge');
		try {
			const result = await searchThereKnowledge(localStorage.token, item.id, knowledgeQuery.trim());
			if (selectedKnowledge?.id === item.id) {
				searchResults = result.items;
				searchedKnowledge = true;
			}
		} catch (error) {
			if (selectedKnowledge?.id === item.id) setError('knowledge', error);
		} finally {
			searchLoading = false;
		}
	}
	function resetFaqForm() {
		faqEditingId = '';
		faqFormOpen = false;
		faqQuestion = '';
		faqAnswers = [''];
		faqSimilarQuestions = '';
	}
	async function loadFaq(id: string, targetPage = faqPage) {
		const request = ++faqRequest;
		faqLoading = true;
		try {
			const result = await getThereFaq(localStorage.token, id, targetPage);
			if (selectedKnowledge?.id !== id || request !== faqRequest) return;
			faqEntries = result.items;
			faqPage = targetPage;
			faqHasMore =
				result.has_more ??
				(typeof result.total === 'number'
					? targetPage * 20 < result.total
					: result.items.length === 20);
		} catch (error) {
			if (selectedKnowledge?.id === id && request === faqRequest) setError('knowledge', error);
		} finally {
			if (request === faqRequest) faqLoading = false;
		}
	}
	async function editFaq(entry: ThereFaq) {
		const kb = selectedKnowledge;
		if (!kb || knowledgeBusy) return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			const result = await getThereFaqEntry(localStorage.token, kb.id, entry.id);
			faqEditingId = result.id;
			faqQuestion = result.standard_question;
			faqAnswers = [...result.answers];
			faqSimilarQuestions = (result.similar_questions ?? []).join('\n');
			faqFormOpen = true;
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function saveFaq() {
		const kb = selectedKnowledge;
		if (!kb || knowledgeBusy || !faqQuestion.trim() || !faqAnswers.some((answer) => answer.trim()))
			return;
		const payload = {
			question: faqQuestion.trim(),
			answers: faqAnswers.map((answer) => answer.trim()).filter(Boolean),
			similar_questions: faqSimilarQuestions
				.split('\n')
				.map((question) => question.trim())
				.filter(Boolean)
		};
		if (payload.similar_questions.length > 20) {
			setError('knowledge', new Error('相似问题最多 20 项，请每行填写一个。'));
			return;
		}
		if (
			faqEditingId &&
			!window.confirm(`确认更新问答“${payload.question}”？更新后的问题与答案将影响后续检索。`)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent('save-faq', [kb.id, faqEditingId, payload], (key) =>
				faqEditingId
					? updateThereFaq(localStorage.token, kb.id, faqEditingId, payload, key)
					: createThereFaq(localStorage.token, kb.id, payload, key)
			);
			resetFaqForm();
			notice = '问答已保存，检索索引正在更新。';
			await loadFaq(kb.id, 1);
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function removeFaq(entry: ThereFaq) {
		const kb = selectedKnowledge;
		if (
			!kb ||
			knowledgeBusy ||
			!window.confirm(
				`确认删除问答“${entry.standard_question}”？问题与答案将不再参与检索，此操作无法撤销。`
			)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent('delete-faq', [kb.id, entry.id], (key) =>
				deleteThereFaq(localStorage.token, kb.id, entry.id, key)
			);
			if (faqEditingId === entry.id) resetFaqForm();
			notice = '问答已删除。';
			await loadFaq(kb.id);
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function selectDocument(document: ThereDocument) {
		selectedDocument = document;
		selectedChunk = null;
		chunkRevisions = [];
		restoreVersion = '';
		chunks = [];
		chunksPage = 1;
		chunksHasMore = false;
		++revisionsRequest;
		await loadChunks(1);
	}
	async function loadChunks(targetPage = chunksPage) {
		const kb = selectedKnowledge;
		const document = selectedDocument;
		if (!kb || !document) return;
		const request = ++chunksRequest;
		chunksLoading = true;
		clearError('knowledge');
		try {
			const result = await getThereChunks(localStorage.token, kb.id, document.id, targetPage);
			if (
				request !== chunksRequest ||
				selectedDocument?.id !== document.id ||
				selectedKnowledge?.id !== kb.id
			)
				return;
			chunks = result.items;
			chunksPage = targetPage;
			chunksHasMore =
				result.has_more ??
				(typeof result.total === 'number'
					? targetPage * 20 < result.total
					: result.items.length === 20);
			selectedChunk = null;
			chunkRevisions = [];
			restoreVersion = '';
			++revisionsRequest;
		} catch (error) {
			if (request === chunksRequest) setError('knowledge', error);
		} finally {
			if (request === chunksRequest) chunksLoading = false;
		}
	}
	async function selectChunk(chunk: ThereChunk) {
		const kb = selectedKnowledge;
		const document = selectedDocument;
		if (!kb || !document) return;
		selectedChunk = chunk;
		chunkContent = chunk.content;
		restoreVersion = '';
		chunkRevisions = [];
		const request = ++revisionsRequest;
		revisionsLoading = true;
		try {
			const result = await getThereChunkRevisions(localStorage.token, kb.id, document.id, chunk.id);
			if (request === revisionsRequest) chunkRevisions = result.items;
		} catch (error) {
			if (request === revisionsRequest) setError('knowledge', error);
		} finally {
			if (request === revisionsRequest) revisionsLoading = false;
		}
	}
	async function saveChunk() {
		const kb = selectedKnowledge;
		const document = selectedDocument;
		const chunk = selectedChunk;
		if (
			!kb ||
			!document ||
			!chunk ||
			knowledgeBusy ||
			typeof chunk.content_revision !== 'number' ||
			!chunkContent.trim()
		)
			return;
		if (
			!window.confirm(
				`确认修改当前片段（版本 ${chunk.content_revision}）？修改将更新检索内容，旧版本会保留在版本记录中。`
			)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent(
				'update-chunk',
				[kb.id, document.id, chunk.id, chunkContent, chunk.content_revision],
				(key) =>
					updateThereChunk(
						localStorage.token,
						kb.id,
						document.id,
						chunk.id,
						chunkContent,
						chunk.content_revision as number,
						key
					)
			);
			notice = '片段已更新，请刷新后查看最新索引状态。';
			await loadChunks();
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function restoreChunk() {
		const kb = selectedKnowledge;
		const document = selectedDocument;
		const chunk = selectedChunk;
		const revision = selectedRevision;
		if (
			!kb ||
			!document ||
			!chunk ||
			!revision ||
			knowledgeBusy ||
			typeof chunk.content_revision !== 'number'
		)
			return;
		if (
			!window.confirm(
				`确认将片段恢复为指定历史版本 ${revision.revision} 的内容？当前版本为 ${chunk.content_revision}。这将改变后续检索内容，并产生一个新版本。`
			)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent(
				'restore-chunk',
				[kb.id, document.id, chunk.id, revision.revision, chunk.content_revision],
				(key) =>
					restoreThereChunk(
						localStorage.token,
						kb.id,
						document.id,
						chunk.id,
						revision.revision,
						chunk.content_revision as number,
						key
					)
			);
			notice = `片段已恢复为历史版本 ${revision.revision} 的内容，索引正在更新。`;
			await loadChunks();
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function reparseDocument(document: ThereDocument) {
		const kb = selectedKnowledge;
		if (
			!kb ||
			knowledgeBusy ||
			!window.confirm(
				`确认重新解析“${document.title || document.file_name || document.id}”？重新解析可能替换当前分块和人工编辑内容，并重建索引。`
			)
		)
			return;
		knowledgeBusy = true;
		clearError('knowledge');
		try {
			await writeIntent('reparse-document', [kb.id, document.id], (key) =>
				reparseThereDocument(localStorage.token, kb.id, document.id, key)
			);
			if (selectedDocument?.id === document.id) {
				selectedDocument = null;
				selectedChunk = null;
				++chunksRequest;
				++revisionsRequest;
			}
			notice = '已提交重新解析，请刷新文档解析状态。';
			await loadDocuments(kb.id);
		} catch (error) {
			setError('knowledge', error);
		} finally {
			knowledgeBusy = false;
		}
	}
	async function loadCatalog() {
		if (catalogLoading) return;
		catalogLoading = true;
		clearError('skills');
		try {
			const result = await searchThereCatalog(localStorage.token, catalogQuery.trim());
			catalog = result.items;
			catalogTotal = result.total;
			catalogLoaded = true;
		} catch (error) {
			setError('skills', error);
		} finally {
			catalogLoading = false;
		}
	}
	async function selectSkill(skill: ThereSkill) {
		const request = ++skillRequest;
		selectedSkill = null;
		reviewed = false;
		skillLoading = true;
		clearError('skills');
		try {
			const result = await getThereSkill(localStorage.token, skill.id);
			if (request === skillRequest) selectedSkill = result;
		} catch (error) {
			if (request === skillRequest) setError('skills', error);
		} finally {
			if (request === skillRequest) skillLoading = false;
		}
	}
	async function activateSkill() {
		const skill = selectedSkill;
		if (!skill || !reviewed || skillBusy) return;
		skillBusy = true;
		clearError('skills');
		try {
			const result = await writeIntent('activate-skill', [skill.id, skill.digest], (key) =>
				activateThereSkill(localStorage.token, skill.id, skill.digest, key)
			);
			notice = `“${result.name}”已导入个人技能。在“技能”工作区管理，并在对话中选用。`;
			reviewed = false;
		} catch (error) {
			setError('skills', error);
		} finally {
			skillBusy = false;
		}
	}
	async function searchResearch() {
		if (!researchQuery.trim() || researchLoading) return;
		researchLoading = true;
		clearError('research');
		research = null;
		try {
			research = await searchThereResearch(localStorage.token, researchQuery.trim());
		} catch (error) {
			setError('research', error);
		} finally {
			researchLoading = false;
		}
	}
	async function loadPapers() {
		papersLoading = true;
		try {
			savedPapers = (await getTherePapers(localStorage.token)).items;
			papersLoaded = true;
		} catch (error) {
			setError('research', error);
		} finally {
			papersLoading = false;
		}
	}
	async function savePaper(paper: TherePaper) {
		if (paperBusy) return;
		paperBusy = true;
		clearError('research');
		const payload = {
			title: paper.title,
			url: safeUrl(paper.url) ?? '',
			authors: Array.isArray(paper.authors)
				? paper.authors.map((author) => (typeof author === 'string' ? author : author.name))
				: paper.authors
					? [paper.authors]
					: [],
			year: paper.year,
			source: paper.source,
			abstract: paper.abstract
		};
		try {
			const result = await writeIntent('save-paper', payload, (key) =>
				saveTherePaper(localStorage.token, payload, key)
			);
			savedPapers = [{ ...paper, ...result }, ...savedPapers];
			notice = '论文已收藏到 THERE 主库。';
		} catch (error) {
			setError('research', error);
		} finally {
			paperBusy = false;
		}
	}
	async function importPaperAbstract(paper: TherePaper) {
		if (paperBusy || !paperKnowledgeId || !paper.abstract) return;
		paperBusy = true;
		clearError('research');
		const content = `论文摘要（不是全文）\n\n标题：${paper.title}\n作者：${authorsText(paper.authors)}\n年份：${paper.year ?? ''}\n来源：${paper.source ?? ''}\n原文：${safeUrl(paper.url) ?? '未提供'}\n\n${paper.abstract}`;
		try {
			await writeIntent('import-paper-abstract', [paperKnowledgeId, paper.title, content], (key) =>
				createThereDocument(localStorage.token, paperKnowledgeId, paper.title, content, key)
			);
			notice = '论文摘要已提交知识库索引，仅保存摘要和来源信息，没有下载全文。';
		} catch (error) {
			setError('research', error);
		} finally {
			paperBusy = false;
		}
	}
	function selectSection(value: Section) {
		section = value;
		notice = '';
		if (value === 'skills' && !catalogLoaded) loadCatalog();
		if (value === 'research' && !papersLoaded) loadPapers();
	}
	onMount(() => {
		loadStatus();
		loadKnowledge();
	});
</script>

<div class="mx-auto w-full max-w-6xl space-y-5 px-1 py-5 sm:px-4">
	<header class="space-y-2">
		<p class="text-xs font-semibold uppercase tracking-widest text-gray-500">THERE / 能力工作台</p>
		<h1 class="text-2xl font-semibold tracking-tight">知识、技能与研究，一处协作。</h1>
		<p class="max-w-3xl text-sm leading-6 text-gray-500">
			使用当前 THERE 账号管理知识库、审阅开源技能和检索论文。能力由 WeKnora、Agentic Awesome Skills
			与学术数据源提供。
		</p>
	</header>
	<nav
		aria-label="THERE 能力分类"
		class="flex gap-1 overflow-x-auto border-b border-gray-100 pb-2 dark:border-gray-800"
	>
		{#each sections.filter((item) => item.id !== 'admin-history' || $user?.role === 'admin') as item}
			<button
				type="button"
				aria-pressed={section === item.id}
				on:click={() => selectSection(item.id)}
				class="whitespace-nowrap rounded-xl px-4 py-2 text-sm font-medium {section === item.id
					? 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-white'
					: 'text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-850'}">{item.label}</button
			>
		{/each}
	</nav>
	{#if notice}
		<div
			role="status"
			class="flex items-start justify-between gap-3 rounded-xl bg-green-50 px-4 py-3 text-sm text-green-800 dark:bg-green-950/30 dark:text-green-300"
		>
			<span>{notice}</span><button
				type="button"
				aria-label="关闭提示"
				on:click={() => (notice = '')}>×</button
			>
		</div>
	{/if}
	{#if errors[section]}
		<div
			role="alert"
			class="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300"
		>
			{errors[section]}
		</div>
	{/if}

	{#if section === 'personal'}
		{#key section}<TherePersonalHistory />{/key}
	{:else if section === 'admin-history' && $user?.role === 'admin'}
		{#key section}<TherePersonalHistory admin={true} />{/key}
	{:else if section === 'knowledge'}
		<p
			class="rounded-xl bg-gray-50 px-4 py-3 text-sm leading-6 text-gray-600 dark:bg-gray-850 dark:text-gray-300"
		>
			这里创建的知识库会同步到 THERE 原生知识管理。在普通对话或 Agent
			对话中，通过附件菜单的知识选择加入知识库，即可基于其内容检索和回答。
		</p>
		<div class="grid gap-5 lg:grid-cols-[280px_minmax(0,1fr)]">
			<aside class="space-y-3">
				<div class="flex items-center justify-between">
					<h2 class="font-semibold">我的知识库</h2>
					<button
						type="button"
						class="text-sm text-gray-500"
						disabled={knowledgeLoading || knowledgeBusy}
						on:click={loadKnowledge}>{knowledgeLoading ? '刷新中…' : '刷新'}</button
					>
				</div>
				{#if canManageKnowledge}<button
						type="button"
						class="{secondaryClass} w-full"
						aria-expanded={showCreate}
						on:click={() => (showCreate = !showCreate)}>＋ 新建知识库</button
					>{/if}
				{#if showCreate && canManageKnowledge}
					<form
						on:submit|preventDefault={createKnowledge}
						class="space-y-3 rounded-2xl border border-gray-200 p-3 dark:border-gray-800"
					>
						<label class="block space-y-1 text-sm"
							><span>名称</span><input
								class={inputClass}
								bind:value={newName}
								required
								maxlength="128"
								disabled={knowledgeBusy}
								placeholder="例如：研发资料"
							/></label
						>
						<label class="block space-y-1 text-sm"
							><span>说明</span><textarea
								class={inputClass}
								bind:value={newDescription}
								rows="2"
								maxlength="2000"
								disabled={knowledgeBusy}
								placeholder="这份知识库用于什么？"
							></textarea></label
						>
						<label class="block space-y-1 text-sm"
							><span>类型</span><select
								class={inputClass}
								bind:value={newBaseType}
								disabled={knowledgeBusy}
								><option value="document">文档知识库</option><option value="faq"
									>问答知识库（FAQ）</option
								></select
							></label
						>
						<button class={primaryClass} disabled={knowledgeBusy || !newName.trim()}
							>{knowledgeBusy ? '创建中…' : '创建'}</button
						>
					</form>
				{/if}
				{#if knowledgeLoading && !knowledge.length}<p
						role="status"
						class="py-5 text-sm text-gray-500"
					>
						正在加载知识库…
					</p>{:else if !knowledge.length}<p
						class="rounded-2xl bg-gray-50 p-4 text-sm leading-6 text-gray-500 dark:bg-gray-850"
					>
						还没有可访问的知识库。{canManageKnowledge
							? '创建一个知识库，开始整理资料。'
							: '请联系管理员开通知识库权限。'}
					</p>{/if}
				<div class="max-h-[480px] space-y-2 overflow-y-auto">
					{#each knowledge as item (item.id)}
						<button
							type="button"
							on:click={() => selectKnowledge(item)}
							disabled={knowledgeBusy}
							aria-pressed={selectedKnowledge?.id === item.id}
							class="w-full rounded-2xl border p-4 text-left disabled:opacity-50 {selectedKnowledge?.id ===
							item.id
								? 'border-gray-500 bg-gray-50 dark:bg-gray-850'
								: 'border-gray-100 hover:border-gray-300 dark:border-gray-800'}"
							><span class="mb-1 block text-xs text-gray-400"
								>{(item.base_type ?? item.type) === 'faq' ? '问答 / FAQ' : '文档'}</span
							><span class="block break-words text-sm font-medium">{item.name}</span
							>{#if item.description}<span
									class="mt-1 block line-clamp-2 text-xs leading-5 text-gray-500"
									>{item.description}</span
								>{/if}</button
						>
					{/each}
				</div>
			</aside>
			<main class="min-w-0 space-y-5">
				{#if selectedKnowledge}
					<div class="flex flex-wrap items-start justify-between gap-3">
						<div>
							<h2 class="break-words text-xl font-semibold">{selectedKnowledge.name}</h2>
							<p class="mt-1 text-xs text-gray-400">{isFaq ? '问答知识库 / FAQ' : '文档知识库'}</p>
							<p class="mt-1 text-sm text-gray-500">
								{selectedKnowledge.description || '上传资料后，可以在这里检验检索结果。'}
							</p>
						</div>
						{#if canManageKnowledge}<button
								type="button"
								on:click={removeKnowledge}
								disabled={knowledgeBusy}
								class="rounded-lg px-2 py-1 text-sm text-red-600 disabled:opacity-40"
								>删除知识库</button
							>{/if}
					</div>
					{#if canManageKnowledge && !isFaq}
						<details class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
							<summary class="cursor-pointer text-sm font-medium">添加资料</summary>
							<div class="mt-4 grid gap-5 xl:grid-cols-2">
								<form on:submit|preventDefault={() => addDocument('file')} class="space-y-3">
									<label class="block space-y-2 text-sm"
										><span>上传文件</span><input
											bind:this={documentInput}
											bind:files={documentFiles}
											type="file"
											class="block w-full min-w-0 text-xs file:mr-2 file:rounded-lg file:border-0 file:px-3 file:py-2"
											disabled={knowledgeBusy}
											required
										/></label
									>
									<p class="text-xs leading-5 text-gray-500">
										文件会进入知识解析和索引流程。请仅上传你有权处理的资料。
									</p>
									<button class={primaryClass} disabled={knowledgeBusy || !documentFiles?.length}
										>{knowledgeBusy ? '提交中…' : '上传并索引'}</button
									>
								</form>
								<form on:submit|preventDefault={() => addDocument('manual')} class="space-y-3">
									<label class="block space-y-1 text-sm"
										><span>文本标题</span><input
											class={inputClass}
											bind:value={documentTitle}
											maxlength="200"
											disabled={knowledgeBusy}
											required
										/></label
									><label class="block space-y-1 text-sm"
										><span>正文</span><textarea
											class={inputClass}
											bind:value={documentContent}
											rows="5"
											disabled={knowledgeBusy}
											required
											placeholder="粘贴笔记、研究摘要或内部说明…"
										></textarea></label
									><button
										class={primaryClass}
										disabled={knowledgeBusy || !documentTitle.trim() || !documentContent.trim()}
										>{knowledgeBusy ? '提交中…' : '保存并索引'}</button
									>
								</form>
							</div>
						</details>
					{/if}
					{#if isFaq}
						<section class="space-y-4" aria-label="问答条目">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<h3 class="text-sm font-semibold">标准问答</h3>
								<div class="flex gap-2">
									<button
										type="button"
										class={secondaryClass}
										disabled={faqLoading || knowledgeBusy}
										on:click={() => selectedKnowledge && loadFaq(selectedKnowledge.id)}
										>{faqLoading ? '刷新中…' : '刷新'}</button
									>{#if canManageKnowledge}<button
											type="button"
											class={primaryClass}
											disabled={knowledgeBusy}
											on:click={() => {
												resetFaqForm();
												faqFormOpen = true;
											}}>＋ 新建问答</button
										>{/if}
								</div>
							</div>
							{#if faqFormOpen && canManageKnowledge}
								<form
									on:submit|preventDefault={saveFaq}
									class="space-y-3 rounded-2xl border border-gray-200 p-4 dark:border-gray-800"
								>
									<h4 class="text-sm font-semibold">{faqEditingId ? '编辑问答' : '录入问答'}</h4>
									<label class="block space-y-1 text-sm"
										><span>标准问题</span><textarea
											class={inputClass}
											rows="2"
											bind:value={faqQuestion}
											maxlength="2000"
											required
											disabled={knowledgeBusy}
										></textarea></label
									>
									{#each faqAnswers as answer, index}<div class="flex items-start gap-2">
											<label class="block flex-1 space-y-1 text-sm"
												><span>答案 {index + 1}</span><textarea
													class={inputClass}
													rows="3"
													bind:value={faqAnswers[index]}
													maxlength="30000"
													disabled={knowledgeBusy}
													required
												></textarea></label
											>{#if faqAnswers.length > 1}<button
													type="button"
													class="mt-7 text-xs text-red-600"
													disabled={knowledgeBusy}
													on:click={() =>
														(faqAnswers = faqAnswers.filter(
															(_, answerIndex) => answerIndex !== index
														))}>移除</button
												>{/if}
										</div>{/each}
									<button
										type="button"
										class={secondaryClass}
										disabled={knowledgeBusy || faqAnswers.length >= 20}
										on:click={() => (faqAnswers = [...faqAnswers, ''])}>添加备选答案</button
									>
									<label class="block space-y-1 text-sm"
										><span>相似问题（可选，每行一个，最多 20 个）</span><textarea
											class={inputClass}
											rows="3"
											bind:value={faqSimilarQuestions}
											disabled={knowledgeBusy}
										></textarea></label
									>
									<div class="flex gap-2">
										<button
											class={primaryClass}
											disabled={knowledgeBusy ||
												!faqQuestion.trim() ||
												!faqAnswers.some((answer) => answer.trim())}
											>{knowledgeBusy ? '保存中…' : '保存问答'}</button
										><button
											type="button"
											class={secondaryClass}
											disabled={knowledgeBusy}
											on:click={resetFaqForm}>取消</button
										>
									</div>
								</form>
							{/if}
							{#if !faqEntries.length}<p
									class="rounded-xl bg-gray-50 p-4 text-sm text-gray-500 dark:bg-gray-850"
								>
									{faqLoading
										? '正在读取问答…'
										: '当前页没有问答。可录入标准问题、相似问法与答案。'}
								</p>{/if}
							{#each faqEntries as entry (entry.id)}<article
									class="space-y-2 rounded-2xl border border-gray-200 p-4 dark:border-gray-800"
								>
									<h4 class="break-words text-sm font-medium">{entry.standard_question}</h4>
									{#each entry.answers as answer}<p
											class="whitespace-pre-wrap break-words text-sm leading-6 text-gray-600 dark:text-gray-300"
										>
											{answer}
										</p>{/each}{#if entry.similar_questions?.length}<details
											class="text-xs text-gray-500"
										>
											<summary class="cursor-pointer">相似问法</summary>
											<ul class="mt-2 space-y-1">
												{#each entry.similar_questions as question}<li>{question}</li>{/each}
											</ul>
										</details>{/if}{#if canManageKnowledge}<div class="flex gap-3 pt-1">
											<button
												type="button"
												class="text-xs text-gray-500"
												disabled={knowledgeBusy}
												on:click={() => editFaq(entry)}>编辑</button
											><button
												type="button"
												class="text-xs text-red-600"
												disabled={knowledgeBusy}
												on:click={() => removeFaq(entry)}>删除</button
											>
										</div>{/if}
								</article>{/each}
							{#if faqPage > 1 || faqHasMore}<div
									class="flex items-center justify-between gap-2 text-xs text-gray-500"
								>
									<button
										type="button"
										class={secondaryClass}
										disabled={faqLoading || knowledgeBusy || faqPage === 1}
										on:click={() => selectedKnowledge && loadFaq(selectedKnowledge.id, faqPage - 1)}
										>上一页</button
									><span>第 {faqPage} 页</span><button
										type="button"
										class={secondaryClass}
										disabled={faqLoading || knowledgeBusy || !faqHasMore}
										on:click={() => selectedKnowledge && loadFaq(selectedKnowledge.id, faqPage + 1)}
										>下一页</button
									>
								</div>{/if}
						</section>
					{:else}
						<section class="space-y-3" aria-label="知识库文档">
							<div class="flex items-center justify-between">
								<h3 class="text-sm font-semibold">
									文档 <span class="text-gray-400">{documentsTotal ?? documents.length}</span>
								</h3>
								<button
									type="button"
									class="text-sm text-gray-500"
									disabled={documentsLoading || knowledgeBusy}
									on:click={() => selectedKnowledge && loadDocuments(selectedKnowledge.id)}
									>{documentsLoading ? '更新中…' : '刷新解析状态'}</button
								>
							</div>
							{#if !documents.length}<p
									class="rounded-xl bg-gray-50 p-4 text-sm text-gray-500 dark:bg-gray-850"
								>
									{documentsLoading
										? '正在加载文档…'
										: '还没有文档。添加资料后，解析状态会显示在这里。'}
								</p>{/if}
							<ul class="divide-y divide-gray-100 dark:divide-gray-800">
								{#each documents as document (document.id)}<li
										class="flex items-center justify-between gap-3 py-3"
									>
										<div class="min-w-0">
											<button
												type="button"
												class="break-words text-left text-sm hover:underline"
												disabled={knowledgeBusy}
												on:click={() => selectDocument(document)}
											>
												{document.title || document.file_name || '未命名文档'}
											</button>
											<p class="mt-1 text-xs text-gray-500">{parseState(document.parse_status)}</p>
										</div>
										{#if canManageKnowledge}<div class="flex shrink-0 flex-col items-end gap-2">
												<button
													type="button"
													class="text-xs text-gray-500"
													disabled={knowledgeBusy}
													on:click={() => reparseDocument(document)}>重新解析</button
												><button
													type="button"
													class="shrink-0 text-xs text-red-600 disabled:opacity-40"
													disabled={knowledgeBusy}
													on:click={() => removeDocument(document)}>删除</button
												>
											</div>{/if}
									</li>{/each}
							</ul>
							{#if documentsPage > 1 || documentsHasMore}
								<div class="flex items-center justify-between gap-2 text-xs text-gray-500">
									<button
										type="button"
										class={secondaryClass}
										disabled={documentsLoading || knowledgeBusy || documentsPage === 1}
										on:click={() =>
											selectedKnowledge && loadDocuments(selectedKnowledge.id, documentsPage - 1)}
										>上一页</button
									>
									<span>第 {documentsPage} 页 · 每页最多 50 项</span>
									<button
										type="button"
										class={secondaryClass}
										disabled={documentsLoading || knowledgeBusy || !documentsHasMore}
										on:click={() =>
											selectedKnowledge && loadDocuments(selectedKnowledge.id, documentsPage + 1)}
										>下一页</button
									>
								</div>
							{/if}
						</section>
						{#if selectedDocument}
							<section
								class="space-y-4 rounded-2xl border border-gray-200 p-4 dark:border-gray-800"
								aria-label="文档分块管理"
							>
								<div class="flex flex-wrap items-center justify-between gap-2">
									<div>
										<h3 class="text-sm font-semibold">分块内容与版本</h3>
										<p class="mt-1 break-words text-xs text-gray-500">
											{selectedDocument.title || selectedDocument.file_name || selectedDocument.id}
										</p>
									</div>
									<button
										type="button"
										class={secondaryClass}
										disabled={chunksLoading || knowledgeBusy}
										on:click={() => loadChunks()}>{chunksLoading ? '刷新中…' : '刷新分块'}</button
									>
								</div>
								<p class="text-xs leading-5 text-gray-500">
									点击片段查看全文和历史版本。人工编辑会更新检索内容；重新解析原文可能替换这些分块。
								</p>
								{#if !chunks.length}<p class="text-sm text-gray-500">
										{chunksLoading
											? '正在读取分块…'
											: '当前页没有可显示的分块，请确认文档已解析完成。'}
									</p>{/if}
								<div class="max-h-[280px] space-y-2 overflow-y-auto">
									{#each chunks as chunk (chunk.id)}<button
											type="button"
											class="w-full rounded-xl border p-3 text-left {selectedChunk?.id === chunk.id
												? 'border-gray-500 bg-gray-50 dark:bg-gray-850'
												: 'border-gray-100 dark:border-gray-800'}"
											disabled={knowledgeBusy}
											on:click={() => selectChunk(chunk)}
											><span class="block text-xs text-gray-500"
												>片段 {(chunk.chunk_index ?? chunks.indexOf(chunk)) + 1} · 版本 {chunk.content_revision ??
													'未知'} · {parseState(chunk.index_status)}</span
											><span
												class="mt-1 block line-clamp-3 whitespace-pre-wrap break-words text-sm leading-5"
												>{chunk.content}</span
											></button
										>{/each}
								</div>
								{#if chunksPage > 1 || chunksHasMore}<div
										class="flex items-center justify-between gap-2 text-xs text-gray-500"
									>
										<button
											type="button"
											class={secondaryClass}
											disabled={chunksLoading || knowledgeBusy || chunksPage === 1}
											on:click={() => loadChunks(chunksPage - 1)}>上一页</button
										><span>第 {chunksPage} 页</span><button
											type="button"
											class={secondaryClass}
											disabled={chunksLoading || knowledgeBusy || !chunksHasMore}
											on:click={() => loadChunks(chunksPage + 1)}>下一页</button
										>
									</div>{/if}
								{#if selectedChunk}
									<form
										on:submit|preventDefault={saveChunk}
										class="space-y-3 border-t border-gray-100 pt-4 dark:border-gray-800"
									>
										<label class="block space-y-2 text-sm"
											><span>片段全文 · 当前版本 {selectedChunk.content_revision ?? '未知'}</span
											><textarea
												class={inputClass}
												rows="8"
												bind:value={chunkContent}
												readonly={!canManageKnowledge}
												disabled={knowledgeBusy}
												required
											></textarea></label
										>{#if canManageKnowledge}<button
												class={primaryClass}
												disabled={knowledgeBusy ||
													typeof selectedChunk.content_revision !== 'number' ||
													!chunkContent.trim() ||
													chunkContent === selectedChunk.content}>保存片段修改</button
											>{/if}
									</form>
									<div class="space-y-3">
										<h4 class="text-sm font-semibold">历史版本</h4>
										{#if revisionsLoading}<p class="text-xs text-gray-500">
												正在读取历史版本…
											</p>{:else if !chunkRevisions.length}<p class="text-xs text-gray-500">
												暂无可恢复的历史版本。
											</p>{:else}<label class="block space-y-1 text-sm"
												><span>选择要查看的版本</span><select
													class={inputClass}
													bind:value={restoreVersion}
													disabled={knowledgeBusy}
													><option value="">请选择指定版本</option
													>{#each chunkRevisions as revision (revision.revision)}<option
															value={String(revision.revision)}
															>版本 {revision.revision} · {operationTime(
																revision.edited_at ?? revision.created_at
															)}</option
														>{/each}</select
												></label
											>{#if selectedRevision}<pre
													class="max-h-[300px] overflow-y-auto whitespace-pre-wrap break-words rounded-xl bg-gray-50 p-3 font-mono text-xs leading-6 dark:bg-gray-850">{selectedRevision.content}</pre>
												{#if canManageKnowledge}<button
														type="button"
														class={secondaryClass}
														disabled={knowledgeBusy ||
															typeof selectedChunk.content_revision !== 'number' ||
															selectedRevision.revision === selectedChunk.content_revision}
														on:click={restoreChunk}
														>恢复为版本 {selectedRevision.revision} 的内容</button
													>{/if}{/if}{/if}
									</div>
								{/if}
							</section>
						{/if}
					{/if}
					<section
						class="space-y-3 border-t border-gray-100 pt-5 dark:border-gray-800"
						aria-label="知识检索"
					>
						<h3 class="text-sm font-semibold">检索知识</h3>
						<form on:submit|preventDefault={searchKnowledge} class="flex gap-2">
							<input
								class={inputClass}
								aria-label="知识检索问题"
								bind:value={knowledgeQuery}
								required
								placeholder="输入问题或关键词"
								disabled={searchLoading}
							/><button
								class="{primaryClass} shrink-0"
								disabled={searchLoading || !knowledgeQuery.trim()}
								>{searchLoading ? '检索中…' : '检索'}</button
							>
						</form>
						{#if searchedKnowledge && !searchResults.length}<p class="text-sm text-gray-500">
								没有找到相关片段。可尝试更具体的关键词，或确认文档已解析完成。
							</p>{/if}
						{#each searchResults as result, index}<article
								class="space-y-2 rounded-2xl border border-gray-200 p-4 dark:border-gray-800"
							>
								<div class="flex justify-between text-xs text-gray-500">
									<span>片段 {index + 1}{result.title ? ` · ${result.title}` : ''}</span
									>{#if typeof result.score === 'number'}<span
											>相关度 {result.score.toFixed(3)}</span
										>{/if}
								</div>
								<p class="whitespace-pre-wrap break-words text-sm leading-6">{result.content}</p>
							</article>{/each}
					</section>
					{#if !isFaq}
						<details class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
							<summary class="cursor-pointer text-sm font-medium">Wiki 知识页面管理</summary>
							<div class="mt-4">
								{#key selectedKnowledge.id}<ThereWiki
										knowledgeId={selectedKnowledge.id}
										canWrite={canManageKnowledge}
									/>{/key}
							</div>
						</details>
					{/if}
				{:else}
					<div
						class="rounded-2xl border border-dashed border-gray-200 px-6 py-16 text-center dark:border-gray-700"
					>
						<h2 class="font-medium">让资料成为可检索的知识</h2>
						<p class="mx-auto mt-2 max-w-md text-sm leading-6 text-gray-500">
							选择知识库查看文档、写入文本、上传文件或检索内容。资料访问由 THERE 账号权限控制。
						</p>
					</div>
				{/if}
			</main>
		</div>
	{:else if section === 'skills'}
		<section class="space-y-4" aria-label="技能目录">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<h2 class="font-semibold">开源技能目录</h2>
					<p class="mt-1 text-sm text-gray-500">
						审阅后导入个人原生技能，可在普通对话和 Agent 对话中选择使用。
					</p>
				</div>
				<a class={secondaryClass} href="/workspace/skills">管理我的技能 →</a>
			</div>
			<form on:submit|preventDefault={loadCatalog} class="flex gap-2">
				<input
					class={inputClass}
					aria-label="搜索技能目录"
					bind:value={catalogQuery}
					disabled={catalogLoading}
					placeholder="搜索能力，例如 research、coding、writing"
				/><button class="{primaryClass} shrink-0" disabled={catalogLoading}
					>{catalogLoading ? '搜索中…' : '搜索'}</button
				>
			</form>
			<div class="grid gap-5 lg:grid-cols-[minmax(240px,1fr)_minmax(0,1.5fr)]">
				<div class="space-y-3">
					<p class="text-xs text-gray-500">
						{catalogLoaded
							? `找到 ${catalogTotal} 项，显示 ${catalog.length} 项；可缩小关键词继续查找。`
							: '正在加载技能目录…'}
					</p>
					<div class="max-h-[650px] space-y-2 overflow-y-auto">
						{#each catalog as skill (skill.id)}<button
								type="button"
								class="w-full rounded-2xl border p-4 text-left disabled:opacity-50 {selectedSkill?.id ===
								skill.id
									? 'border-gray-500 bg-gray-50 dark:bg-gray-850'
									: 'border-gray-100 hover:border-gray-300 dark:border-gray-800'}"
								disabled={skillBusy}
								on:click={() => selectSkill(skill)}
								><span class="block break-words text-sm font-medium">{skill.name}</span><span
									class="mt-1 block line-clamp-3 text-xs leading-5 text-gray-500"
									>{skill.description || skill.id}</span
								></button
							>{/each}
					</div>
					{#if catalogLoaded && !catalog.length}<p class="p-4 text-sm text-gray-500">
							没有匹配的技能。试试英文关键词或清空搜索。
						</p>{/if}
				</div>
				<div class="min-w-0 rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
					{#if skillLoading}<p role="status" class="py-8 text-center text-sm text-gray-500">
							正在读取技能全文…
						</p>{:else if selectedSkill}
						<h3 class="break-words font-semibold">{selectedSkill.name}</h3>
						<p class="mt-1 text-xs text-gray-500">目录版本 {selectedSkill.version}</p>
						<div
							class="my-3 rounded-xl bg-amber-50 p-3 text-xs leading-5 text-amber-800 dark:bg-amber-950/30 dark:text-amber-300"
						>
							以下是第三方、不受信任的技能内容。请检查权限要求、外部链接及命令。导入仅保存技能说明，不会自动执行附带脚本。
						</div>
						<pre
							class="max-h-[440px] overflow-y-auto whitespace-pre-wrap break-words rounded-xl bg-gray-50 p-4 font-mono text-xs leading-6 dark:bg-gray-850">{selectedSkill.content}</pre>
						<details class="mt-3 text-xs text-gray-500">
							<summary class="cursor-pointer">内容校验摘要</summary>
							<p class="mt-2 break-all font-mono">{selectedSkill.digest}</p>
						</details>
						{#if canManageSkills}<label class="my-4 flex items-start gap-2 text-sm"
								><input
									type="checkbox"
									class="mt-1"
									bind:checked={reviewed}
									disabled={skillBusy}
								/><span>我已审阅全文，确认将此版本导入我的 THERE 技能。</span></label
							><button
								type="button"
								class={primaryClass}
								disabled={!reviewed || skillBusy || !selectedSkill.digest}
								on:click={activateSkill}>{skillBusy ? '导入中…' : '导入个人技能'}</button
							>{:else}<p class="mt-4 text-sm text-gray-500">
								当前账号没有创建技能的权限。请联系管理员。
							</p>{/if}
					{:else}<p class="py-16 text-center text-sm leading-6 text-gray-500">
							选择一个技能查看全文。<br />导入前需要明确审阅并确认。
						</p>{/if}
				</div>
			</div>
		</section>
	{:else if section === 'research'}
		<section class="space-y-4" aria-label="论文检索">
			<div>
				<h2 class="font-semibold">跨来源论文检索</h2>
				<p class="mt-1 text-sm leading-6 text-gray-500">
					合并多个学术来源的结果，保留作者、年份与原文链接。检索词会发送给已配置的外部学术数据源，请勿包含内网机密。
				</p>
			</div>
			<form on:submit|preventDefault={searchResearch} class="flex gap-2">
				<input
					class={inputClass}
					aria-label="论文检索关键词"
					bind:value={researchQuery}
					required
					disabled={researchLoading}
					placeholder="输入论文题目、研究问题或英文关键词"
				/><button
					class="{primaryClass} shrink-0"
					disabled={researchLoading || !researchQuery.trim()}
					>{researchLoading ? '检索中…' : '检索论文'}</button
				>
			</form>
			{#if canManageKnowledge}
				<div class="space-y-2 rounded-xl bg-gray-50 p-4 dark:bg-gray-850">
					<label class="flex flex-col gap-2 text-sm sm:flex-row sm:items-center"
						><span class="shrink-0">摘要导入目标</span><select
							class={inputClass}
							bind:value={paperKnowledgeId}
							disabled={paperBusy}
							><option value="">选择知识库</option
							>{#each documentKnowledge as item (item.id)}<option value={item.id}
									>{item.name}</option
								>{/each}</select
						></label
					>
					<p class="text-xs leading-5 text-gray-500">
						“摘要入库”只保存论文摘要、作者与出处，不下载或保存论文全文。{!documentKnowledge.length
							? '请先在知识区创建文档知识库。'
							: ''}
					</p>
				</div>
			{/if}
			{#if researchLoading}<p role="status" class="py-10 text-center text-sm text-gray-500">
					正在检索学术来源并整理结果…
				</p>{:else if research}
				{#if research.partial}<p
						role="status"
						class="rounded-xl bg-amber-50 p-3 text-sm text-amber-800 dark:bg-amber-950/30 dark:text-amber-300"
					>
						部分数据源暂不可用。以下为已返回的结果，并不代表完整覆盖。
					</p>{/if}
				<p class="text-xs text-gray-500">
					返回 {research.items.length} 篇文献。请阅读原文核实结论与引用。
				</p>
				{#each research.items as paper}<article
						class="space-y-2 rounded-2xl border border-gray-200 p-5 dark:border-gray-800"
					>
						<h3 class="font-medium leading-6">
							{#if safeUrl(paper.url)}<a
									class="hover:underline"
									href={safeUrl(paper.url)}
									target="_blank"
									rel="noopener noreferrer">{paper.title} ↗</a
								>{:else}{paper.title}{/if}
						</h3>
						<p class="text-xs leading-5 text-gray-500">
							{[paper.year, paper.source, authorsText(paper.authors)].filter(Boolean).join(' · ')}
						</p>
						{#if paper.abstract}<details class="text-sm">
								<summary class="cursor-pointer text-gray-500">摘要</summary>
								<p class="mt-2 whitespace-pre-wrap leading-6">{paper.abstract}</p>
							</details>{/if}{#if paper.doi}<p class="break-all text-xs text-gray-400">
								DOI: {paper.doi}
							</p>{/if}
						<div class="flex flex-wrap gap-2 pt-2">
							<button
								type="button"
								class={secondaryClass}
								on:click={() => savePaper(paper)}
								disabled={paperBusy || savedPaper(paper)}
								>{savedPaper(paper) ? '已收藏' : '收藏论文'}</button
							>{#if canManageKnowledge}<button
									type="button"
									class={secondaryClass}
									on:click={() => importPaperAbstract(paper)}
									disabled={paperBusy || !paperKnowledgeId || !paper.abstract}>摘要入库</button
								>{/if}
						</div>
					</article>{/each}
				{#if !research.items.length}<p class="py-10 text-center text-sm text-gray-500">
						没有找到论文。尝试更通用的研究主题或英文关键词。
					</p>{/if}
			{:else}<div
					class="rounded-2xl border border-dashed border-gray-200 py-14 text-center dark:border-gray-700"
				>
					<p class="font-medium">从问题开始，找到可以追溯的文献</p>
					<p class="mt-2 text-sm text-gray-500">例如：retrieval augmented generation evaluation</p>
				</div>{/if}
			<section
				class="space-y-3 border-t border-gray-100 pt-5 dark:border-gray-800"
				aria-label="我的论文收藏"
			>
				<div class="flex items-center justify-between">
					<h3 class="text-sm font-semibold">
						我的论文收藏 <span class="text-gray-400">{savedPapers.length}</span>
					</h3>
					<button
						type="button"
						class="text-sm text-gray-500"
						disabled={papersLoading || paperBusy}
						on:click={loadPapers}>{papersLoading ? '刷新中…' : '刷新'}</button
					>
				</div>
				{#if !savedPapers.length}<p class="text-sm text-gray-500">
						{papersLoading ? '正在读取收藏…' : '还没有收藏。检索后可将论文保存到 THERE 主库。'}
					</p>{/if}
				{#each savedPapers as paper}<article
						class="rounded-xl border border-gray-100 p-4 dark:border-gray-800"
					>
						<h4 class="text-sm font-medium">
							{#if safeUrl(paper.url)}<a
									href={safeUrl(paper.url)}
									target="_blank"
									rel="noopener noreferrer"
									class="hover:underline">{paper.title} ↗</a
								>{:else}{paper.title}{/if}
						</h4>
						<p class="mt-1 text-xs text-gray-500">
							{[paper.year, paper.source, authorsText(paper.authors)].filter(Boolean).join(' · ')}
						</p>
						{#if paper.abstract}<details class="mt-2 text-sm">
								<summary class="cursor-pointer text-gray-500">摘要</summary>
								<p class="mt-2 whitespace-pre-wrap leading-6">{paper.abstract}</p>
							</details>{/if}{#if canManageKnowledge}<button
								type="button"
								class="{secondaryClass} mt-3"
								on:click={() => importPaperAbstract(paper)}
								disabled={paperBusy || !paperKnowledgeId || !paper.abstract}>摘要入库</button
							>{/if}
					</article>{/each}
			</section>
		</section>
	{:else}
		<section class="space-y-4" aria-label="运行状态">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="font-semibold">能力运行状态</h2>
					<p class="mt-1 text-xs text-gray-500">
						{version ? `THERE 集成版本 ${version}` : '统一能力入口'}
					</p>
				</div>
				<button type="button" class={secondaryClass} on:click={loadStatus} disabled={statusLoading}
					>{statusLoading ? '检查中…' : '刷新状态'}</button
				>
			</div>
			<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
				{#each modules as module (module.id)}<article
						class="rounded-2xl border border-gray-200 p-5 dark:border-gray-800"
					>
						<div class="flex items-center justify-between gap-3">
							<h3 class="text-sm font-semibold">{module.name}</h3>
							<span class="rounded-full bg-gray-100 px-2 py-1 text-xs dark:bg-gray-800"
								>{stateLabel(module.state)}</span
							>
						</div>
						<p class="mt-3 break-words text-sm leading-6 text-gray-500">
							{module.detail || '暂无更多状态信息。'}
						</p>
					</article>{/each}
			</div>
			{#if !modules.length}<p class="py-8 text-sm text-gray-500">
					{statusLoading ? '正在检查能力状态…' : '暂无状态信息，请刷新重试。'}
				</p>{/if}
			<section
				class="space-y-3 border-t border-gray-100 pt-5 dark:border-gray-800"
				aria-label="能力操作记录"
			>
				<h3 class="text-sm font-semibold">最近操作</h3>
				<p class="text-xs leading-5 text-gray-500">
					写入请求带有去重标识。发生网络错误时，保留原输入再次提交会复用同一标识；系统不会自动重试写入。遇到待确认状态，请先核对记录。
				</p>
				{#if !operations.length}<p class="text-sm text-gray-500">暂无可显示的操作记录。</p>{/if}
				<ul class="divide-y divide-gray-100 dark:divide-gray-800">
					{#each operations as operation (operation.id)}<li class="space-y-1 py-3">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<span class="text-sm font-medium">{operation.action}</span><span
									class="text-xs text-gray-500">{stateLabel(operation.state)}</span
								>
							</div>
							<p class="break-all text-xs text-gray-500">
								{operationTime(operation.updated_at ?? operation.created_at)}{operation.resource_id
									? ` · ${operation.resource_id}`
									: ''}
							</p>
							{#if operation.error_code}<p class="text-xs text-red-600">
									{operation.error_code}
								</p>{/if}
						</li>{/each}
				</ul>
			</section>
			<p class="text-xs leading-6 text-gray-500">
				统一账号与权限、知识索引、技能导入和论文检索由 THERE
				管理。此页状态不替代完整端到端验证；上传资料后可在知识区检验实际检索结果。
			</p>
		</section>
	{/if}
</div>
