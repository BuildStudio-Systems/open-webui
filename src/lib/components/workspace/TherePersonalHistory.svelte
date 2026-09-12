<script lang="ts">
	import { onMount } from 'svelte';
	import { getPersonalHistory, type PersonalHistoryPage } from '$lib/apis/there';
	import { getUsers } from '$lib/apis/users';
	export let admin = false;
	let owner = '';
	let ownerLabel = '';
	let userQuery = '';
	let users: { id: string; name: string; email: string }[] = [];
	let userPage = 1;
	let userTotal = 0;
	let usersLoading = false;
	let usersError = '';
	let query = '';
	let page = 1;
	let result: PersonalHistoryPage | null = null;
	let loading = false;
	let error = '';
	function clearHistory() {
		result = null;
		error = '';
		page = 1;
	}
	async function findUsers(next = 1) {
		if (!admin || usersLoading || loading) return;
		usersLoading = true;
		usersError = '';
		users = [];
		userTotal = 0;
		try {
			const response = await getUsers(
				localStorage.token,
				userQuery.trim(),
				undefined,
				undefined,
				next
			);
			if (!response || !Array.isArray(response.users))
				throw new Error('客户列表加载失败，请重试。');
			users = response.users;
			userTotal = response.total;
			userPage = next;
		} catch (e) {
			usersError = String(e);
		} finally {
			usersLoading = false;
		}
	}
	function selectOwner(customer: { id: string; name: string; email: string }) {
		if (loading) return;
		owner = customer.id;
		ownerLabel = `${customer.name} (${customer.email})`;
		clearHistory();
	}
	async function load(next = 1) {
		if (loading) return;
		result = null;
		error = '';
		if (admin && !owner.trim()) {
			error = '请先查找并选择客户，或输入 THERE 用户 ID。';
			return;
		}
		loading = true;
		try {
			result = await getPersonalHistory(
				localStorage.token,
				query,
				next,
				admin ? owner.trim() : undefined
			);
			page = next;
		} catch (e) {
			error = String(e);
		} finally {
			loading = false;
		}
	}
	onMount(() => {
		if (admin) void findUsers();
		else void load();
	});
</script>

<section class="space-y-4">
	<h2 class="text-lg font-semibold">{admin ? '管理员调取客户问答' : '个人历史问答'}</h2>
	<p class="text-sm text-gray-500">
		{admin
			? '此入口可调取指定客户与 THERE 的问答，每次访问均记录审计，不会共享给其他客户。'
			: '仅展示你自己的已完成问答。历史 AI 回答不等于已核实事实；删除原会话后不再展示。'}
	</p>
	{#if admin}
		<div class="space-y-3 rounded-xl border p-4">
			<form class="flex flex-wrap gap-2" on:submit|preventDefault={() => findUsers()}>
				<input
					aria-label="客户姓名或邮箱"
					class="min-w-0 flex-1 rounded-lg border bg-transparent p-2"
					bind:value={userQuery}
					maxlength="128"
					placeholder="客户姓名或邮箱"
					disabled={usersLoading || loading}
					on:input={() => {
						users = [];
						userTotal = 0;
						owner = '';
						ownerLabel = '';
						clearHistory();
					}}
				/>
				<button class="rounded-lg border px-4 py-2" disabled={usersLoading || loading}
					>{usersLoading ? '查找中…' : '查找客户'}</button
				>
			</form>
			<p class="text-xs text-gray-500">
				这里只选择已登录过 THERE 的客户，不修改统一账号或权限。选择客户后，点击下方“查询”调取问答。
			</p>
			{#if usersError}<p role="alert" class="text-red-500">{usersError}</p>{/if}
			<ul class="max-h-64 space-y-1 overflow-y-auto">
				{#each users as customer (customer.id)}
					<li>
						<button
							type="button"
							class="w-full rounded-lg border p-2 text-left break-words hover:bg-gray-100 dark:hover:bg-gray-800"
							aria-pressed={owner === customer.id}
							disabled={loading}
							on:click={() => selectOwner(customer)}
							>{customer.name} · {customer.email}{owner === customer.id ? '（已选择）' : ''}</button
						>
					</li>
				{/each}
			</ul>
			{#if !usersLoading && !usersError && users.length === 0}
				<p class="text-sm text-gray-500">当前没有客户结果，请输入姓名或邮箱后点击“查找客户”。</p>
			{/if}
			{#if userTotal > 0}
				<div class="flex flex-wrap gap-3 text-sm">
					<button
						type="button"
						disabled={usersLoading || loading || userPage <= 1}
						on:click={() => findUsers(userPage - 1)}>上一页客户</button
					>
					<span>客户第 {userPage} 页 · 共 {userTotal} 人</span>
					<button
						type="button"
						disabled={usersLoading || loading || userPage * 30 >= userTotal}
						on:click={() => findUsers(userPage + 1)}>下一页客户</button
					>
				</div>
			{/if}
			{#if ownerLabel}<p class="text-sm break-words">当前客户：{ownerLabel}</p>{/if}
		</div>
	{/if}
	<form class="flex flex-wrap gap-2" on:submit|preventDefault={() => load()}>
		{#if admin}<input
				aria-label="客户用户 ID"
				class="rounded-lg border bg-transparent p-2"
				bind:value={owner}
				on:input={() => {
					ownerLabel = '';
					clearHistory();
				}}
				maxlength="128"
				placeholder="THERE 用户 ID（也可手动输入）"
				disabled={loading}
			/>{/if}
		<input
			aria-label="问答关键词"
			class="rounded-lg border bg-transparent p-2"
			bind:value={query}
			on:input={clearHistory}
			maxlength="500"
			placeholder="问答关键词"
			disabled={loading}
		/>
		<button class="rounded-lg border px-4 py-2" disabled={loading}
			>{loading ? '加载中…' : '查询'}</button
		>
	</form>
	<p class="text-xs text-gray-500">
		{query.trim()
			? '关键词在该用户的全部历史问答中匹配，每页展示 20 条匹配问答。'
			: '未输入关键词时，每页浏览 20 个会话；输入关键词可搜索全部历史问答。'}
		长内容会截断显示。
	</p>
	{#if !admin}<p class="text-xs text-gray-500">
			在个人非共享会话中，THERE 会自动从你的全部历史问答中检索至多 3
			条关键词相关问答。原记录删除后不再召回；不会训练模型权重或共享给其他客户。
		</p>{/if}
	{#if error}<p role="alert" class="text-red-500">{error}</p>{/if}
	{#if result}
		{#each result.items as item}
			<article class="space-y-2 rounded-xl border p-4">
				<h3 class="font-medium">{item.title}</h3>
				<p class="whitespace-pre-wrap break-words">
					<span class="font-semibold">问：</span>{item.question}
				</p>
				<p class="whitespace-pre-wrap break-words">
					<span class="font-semibold">答：</span>{item.answer}
				</p>
				{#if item.truncated}<p class="text-xs text-gray-500">内容较长，当前为截断预览。</p>{/if}
			</article>
		{:else}<p>本页没有匹配的已完成问答。</p>{/each}
		<div class="flex gap-3">
			<button disabled={loading || page <= 1} on:click={() => load(page - 1)}>上一页</button>
			<span>第 {page} 页</span>
			<button disabled={loading || !result.has_more} on:click={() => load(page + 1)}>下一页</button>
		</div>
		{#if result.audit_id}<p class="text-xs text-gray-500">访问审计：{result.audit_id}</p>{/if}
	{/if}
</section>
