<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		getThereWikiPages,
		getThereWikiPage,
		searchThereWiki,
		createThereWikiPage,
		updateThereWikiPage,
		deleteThereWikiPage,
		type ThereWikiPage
	} from '$lib/apis/there';

	export let knowledgeId: string;
	export let canWrite = false;
	let pages: ThereWikiPage[] = [];
	let selected: ThereWikiPage | null = null;
	let page = 1;
	let hasMore = false;
	let query = '';
	let filtered = false;
	let title = '';
	let slug = '';
	let content = '';
	let creating = false;
	let busy = false;
	let loading = false;
	let error = '';
	let notice = '';
	let generation = 0;
	let alive = true;
	// Keep the same intent after an uncertain response; never silently resubmit a write.
	const intents = new Map<string, string>();
	const inputClass =
		'w-full min-w-0 rounded-xl border border-gray-200 bg-transparent p-2 text-sm dark:border-gray-700';
	const buttonClass =
		'rounded-xl border border-gray-200 px-3 py-2 text-sm disabled:opacity-40 dark:border-gray-700';
	const message = (value: unknown) =>
		value instanceof Error ? value.message : '操作未完成，请检查操作记录。';
	const intent = (signature: string) => {
		let key = intents.get(signature);
		if (!key) {
			key = crypto.randomUUID();
			intents.set(signature, key);
		}
		return key;
	};
	onDestroy(() => {
		alive = false;
		generation++;
	});
	onMount(() => {
		load(1);
	});

	async function load(target: number, search = false) {
		const current = ++generation;
		loading = true;
		error = '';
		try {
			const result = search
				? await searchThereWiki(localStorage.token, knowledgeId, query.trim())
				: await getThereWikiPages(localStorage.token, knowledgeId, target);
			if (!alive || current !== generation) return;
			pages = result.items;
			page = target;
			filtered = search;
			hasMore = !search && 'has_more' in result && result.has_more === true;
		} catch (value) {
			if (alive && current === generation) {
				error = message(value);
				pages = [];
				hasMore = false;
			}
		} finally {
			if (alive && current === generation) loading = false;
		}
	}
	async function open(entry: ThereWikiPage) {
		if (busy || loading) return;
		if ((creating || selected) && !window.confirm('打开其他页面将放弃当前未保存的编辑，是否继续？'))
			return;
		const current = ++generation;
		loading = true;
		error = '';
		try {
			const result = await getThereWikiPage(localStorage.token, knowledgeId, entry.slug);
			if (!alive || current !== generation) return;
			selected = result.data;
			content = selected.content ?? '';
			creating = false;
			notice = '';
		} catch (value) {
			if (alive && current === generation) error = message(value);
		} finally {
			if (alive && current === generation) loading = false;
		}
	}
	async function save() {
		if (!canWrite || busy || loading || !content.trim()) return;
		if (
			!creating &&
			(!selected || !Number.isInteger(selected.version) || (selected.version ?? 0) < 1)
		)
			return;
		const signature = JSON.stringify([
			knowledgeId,
			creating ? 'create' : 'edit',
			creating ? slug.trim() : selected?.slug,
			title.trim(),
			content,
			selected?.version
		]);
		busy = true;
		error = '';
		try {
			const result = creating
				? await createThereWikiPage(
						localStorage.token,
						knowledgeId,
						{ slug: slug.trim(), title: title.trim(), content },
						intent(signature)
					)
				: await updateThereWikiPage(
						localStorage.token,
						knowledgeId,
						selected!.slug,
						content,
						selected!.version!,
						intent(signature)
					);
			if (!alive) return;
			intents.delete(signature);
			selected = result.data;
			creating = false;
			notice = '页面已保存。新建页面默认为草稿，知识库访问权限保持不变。';
			await load(1);
		} catch (value) {
			if (alive) error = message(value);
		} finally {
			if (alive) busy = false;
		}
	}
	async function remove() {
		if (!canWrite || busy || loading || !selected) return;
		if (!window.confirm(`确定删除 Wiki 页面“${selected.title}”？该操作不能从此界面撤销。`)) return;
		const signature = JSON.stringify([knowledgeId, 'delete', selected.slug]);
		busy = true;
		error = '';
		try {
			await deleteThereWikiPage(localStorage.token, knowledgeId, selected.slug, intent(signature));
			if (!alive) return;
			intents.delete(signature);
			selected = null;
			content = '';
			notice = '页面已删除。';
			await load(1);
		} catch (value) {
			if (alive) error = message(value);
		} finally {
			if (alive) busy = false;
		}
	}
</script>

<section class="min-w-0 space-y-3" aria-label="Wiki 知识页面">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h3 class="text-sm font-semibold">Wiki 知识页面</h3>
		<div class="flex flex-wrap gap-2">
			<button type="button" class={buttonClass} disabled={busy || loading} on:click={() => load(1)}
				>刷新列表</button
			>
			{#if canWrite}<button
					type="button"
					class={buttonClass}
					disabled={busy || loading}
					on:click={() => {
						if (
							(creating || selected) &&
							!window.confirm('新建页面将放弃当前未保存的编辑，是否继续？')
						)
							return;
						creating = true;
						selected = null;
						title = '';
						slug = '';
						content = '';
						notice = '';
					}}>新建草稿</button
				>{/if}
		</div>
	</div>
	<p class="text-xs leading-5 text-gray-500">
		使用 THERE 知识库权限。未配置 Wiki 的知识库会提示不可用；页面链接不等于 GraphRAG 已启用。
	</p>
	{#if error}<p role="alert" class="whitespace-pre-wrap break-words text-sm text-red-600">
			{error}
		</p>{/if}
	{#if notice}<p role="status" class="text-sm text-green-700 dark:text-green-400">{notice}</p>{/if}
	<form class="flex gap-2" on:submit|preventDefault={() => load(1, true)}>
		<input
			aria-label="搜索 Wiki 页面"
			class={inputClass}
			bind:value={query}
			maxlength="2000"
			disabled={busy || loading}
			placeholder="搜索 Wiki 页面"
		/>
		<button class={buttonClass} disabled={busy || loading || !query.trim()}>搜索</button>
	</form>
	<div class="grid min-w-0 gap-4 lg:grid-cols-2">
		<div class="min-w-0 space-y-2">
			{#each pages as entry (entry.slug)}
				<button
					type="button"
					class="{buttonClass} block w-full break-words text-left"
					disabled={busy || loading}
					on:click={() => open(entry)}
				>
					<span class="block font-medium">{entry.title || entry.slug}</span>
					<span class="text-xs text-gray-500">{entry.slug} · {entry.status ?? '状态未知'}</span>
				</button>
			{/each}
			{#if !error && !pages.length}<p class="text-sm text-gray-500">
					{loading ? '加载中…' : '暂无匹配页面。'}
				</p>{/if}
			{#if !filtered && (page > 1 || hasMore)}<div class="flex items-center gap-2">
					<button
						type="button"
						class={buttonClass}
						disabled={busy || loading || page === 1}
						on:click={() => load(page - 1)}>上一页</button
					>
					<span class="text-xs">第 {page} 页</span>
					<button
						type="button"
						class={buttonClass}
						disabled={busy || loading || !hasMore}
						on:click={() => load(page + 1)}>下一页</button
					>
				</div>{/if}
		</div>
		{#if selected || creating}<form class="min-w-0 space-y-3" on:submit|preventDefault={save}>
				{#if creating}
					<label class="block text-sm"
						>标题<input
							class={inputClass}
							bind:value={title}
							maxlength="512"
							required
							disabled={busy}
						/></label
					>
					<label class="block text-sm"
						>页面标识<input
							class={inputClass}
							bind:value={slug}
							maxlength="512"
							placeholder="concept/example"
							required
							disabled={busy}
						/></label
					>
				{:else}<h4 class="break-words text-sm font-medium">
						{selected?.title} · 版本 {selected?.version ?? '未知'}
					</h4>{/if}
				<label class="block text-sm"
					>Markdown 正文<textarea
						class="{inputClass} font-mono"
						bind:value={content}
						rows="12"
						maxlength="200000"
						required
						readonly={!canWrite}
						disabled={busy}
					></textarea></label
				>
				{#if canWrite}<div class="flex flex-wrap gap-2">
						<button
							class={buttonClass}
							disabled={busy ||
								loading ||
								!content.trim() ||
								(!creating &&
									(!Number.isInteger(selected?.version) || (selected?.version ?? 0) < 1))}
							>保存</button
						>
						{#if selected}<button
								type="button"
								class={buttonClass}
								disabled={busy || loading}
								on:click={remove}>删除页面</button
							>{/if}
					</div>{/if}
			</form>{/if}
	</div>
</section>
