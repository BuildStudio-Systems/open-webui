<script lang="ts">
	import { onMount } from 'svelte';
	import { getPersonalHistory, type PersonalHistoryPage } from '$lib/apis/there';
	export let admin = false;
	let owner = '';
	let query = '';
	let page = 1;
	let result: PersonalHistoryPage | null = null;
	let loading = false;
	let error = '';
	async function load(next = 1) {
		if (loading) return;
		result = null;
		error = '';
		if (admin && !owner.trim()) {
			error = '请输入客户用户 ID（用户管理中可查看）。';
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
		if (!admin) void load();
	});
</script>

<section class="space-y-4">
	<h2 class="text-lg font-semibold">{admin ? '管理员调取客户问答' : '个人历史问答'}</h2>
	<p class="text-sm text-gray-500">
		{admin
			? '此入口可调取指定客户与 THERE 的问答，每次访问均记录审计，不会共享给其他客户。'
			: '仅展示你自己的已完成问答。历史 AI 回答不等于已核实事实；删除原会话后不再展示。'}
	</p>
	<form class="flex flex-wrap gap-2" on:submit|preventDefault={() => load()}>
		{#if admin}<input
				aria-label="客户用户 ID"
				class="rounded-lg border bg-transparent p-2"
				bind:value={owner}
				maxlength="128"
				placeholder="客户用户 ID"
				disabled={loading}
			/>{/if}
		<input
			aria-label="问答关键词"
			class="rounded-lg border bg-transparent p-2"
			bind:value={query}
			maxlength="500"
			placeholder="问答关键词"
			disabled={loading}
		/>
		<button class="rounded-lg border px-4 py-2" disabled={loading}
			>{loading ? '加载中…' : '查询'}</button
		>
	</form>
	<p class="text-xs text-gray-500">
		每页检查 20 个会话，关键词在本页问答中匹配；无匹配时可继续翻页。长内容会截断显示。
	</p>
	{#if !admin}<p class="text-xs text-gray-500">
			在个人非共享会话中，THERE 会自动按关键词参考最近 20 个会话中至多 3
			条相关问答，不会训练或修改模型权重。
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
