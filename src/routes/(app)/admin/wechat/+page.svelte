<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';

	import { user } from '$lib/stores';
	import {
		WeChatAdminApiError,
		getWeChatAdminChat,
		listWeChatAdminChats,
		listWeChatAdminMessages,
		type WeChatAdminChat,
		type WeChatAdminMessage
	} from '$lib/apis/there/wechat-admin';

	type LoadError = {
		kind: 'forbidden' | 'unavailable' | 'not-found' | 'other';
		message: string;
	};

	let chats: WeChatAdminChat[] = [];
	let chatCursor: string | null = null;
	let listLoading = true;
	let listAppending = false;
	let listError: LoadError | null = null;

	let selectedSummary: WeChatAdminChat | null = null;
	let selectedChat: WeChatAdminChat | null = null;
	let messages: WeChatAdminMessage[] = [];
	let messageCursor: string | null = null;
	let detailLoading = false;
	let messagesAppending = false;
	let detailError: LoadError | null = null;
	let detailGeneration = 0;

	const dateFormatter = new Intl.DateTimeFormat(undefined, {
		year: 'numeric',
		month: 'short',
		day: 'numeric',
		hour: '2-digit',
		minute: '2-digit'
	});

	const formatTimestamp = (timestamp: number): string =>
		timestamp > 0 ? dateFormatter.format(new Date(timestamp * 1000)) : 'Unknown';

	const isoTimestamp = (timestamp: number): string | undefined =>
		timestamp > 0 ? new Date(timestamp * 1000).toISOString() : undefined;

	const stateLabel = (state: string): string =>
		state === 'completed' ? 'Completed' : state === 'incomplete' ? 'Incomplete' : 'Unknown';

	const thinkingLabel = (mode: string): string => {
		const normalized = mode.toLowerCase();
		return ['off', 'low', 'medium', 'high'].includes(normalized)
			? normalized.charAt(0).toUpperCase() + normalized.slice(1)
			: 'Unknown';
	};

	const loadError = (error: unknown): LoadError => {
		if (error instanceof WeChatAdminApiError) {
			if (error.status === 403) return { kind: 'forbidden', message: error.message };
			if (error.status === 503) return { kind: 'unavailable', message: error.message };
			if (error.status === 404) return { kind: 'not-found', message: error.message };
			return { kind: 'other', message: error.message };
		}
		return { kind: 'other', message: 'Unable to load WeChat chat records.' };
	};

	const loadChats = async (reset = false) => {
		if (listLoading && !reset) return;
		if (reset) {
			listLoading = true;
			chats = [];
			chatCursor = null;
		} else {
			listAppending = true;
		}
		listError = null;

		try {
			const page = await listWeChatAdminChats(localStorage.token, reset ? null : chatCursor, 50);
			if (reset) {
				chats = page.items;
			} else {
				const known = new Set(chats.map((chat) => chat.id));
				chats = [...chats, ...page.items.filter((chat) => !known.has(chat.id))];
			}
			chatCursor = page.next_cursor;
		} catch (error) {
			listError = loadError(error);
		} finally {
			listLoading = false;
			listAppending = false;
		}
	};

	const selectChat = async (summary: WeChatAdminChat) => {
		const generation = ++detailGeneration;
		selectedSummary = summary;
		selectedChat = summary;
		messages = [];
		messageCursor = null;
		messagesAppending = false;
		detailError = null;
		detailLoading = true;

		try {
			const detail = await getWeChatAdminChat(localStorage.token, summary.id);
			if (generation !== detailGeneration) return;
			selectedChat = detail;

			const page = await listWeChatAdminMessages(localStorage.token, summary.id, null, 100);
			if (generation !== detailGeneration) return;
			messages = page.items;
			messageCursor = page.next_cursor;
		} catch (error) {
			if (generation !== detailGeneration) return;
			detailError = loadError(error);
		} finally {
			if (generation === detailGeneration) detailLoading = false;
		}
	};

	const closeDetail = () => {
		detailGeneration += 1;
		selectedSummary = null;
		selectedChat = null;
		messages = [];
		messageCursor = null;
		messagesAppending = false;
		detailError = null;
		detailLoading = false;
	};

	const loadMoreMessages = async () => {
		if (!selectedChat || !messageCursor || messagesAppending) return;
		const generation = detailGeneration;
		messagesAppending = true;
		detailError = null;
		try {
			const page = await listWeChatAdminMessages(
				localStorage.token,
				selectedChat.id,
				messageCursor,
				100
			);
			if (generation !== detailGeneration) return;
			const known = new Set(messages.map((message) => message.id));
			messages = [...messages, ...page.items.filter((message) => !known.has(message.id))];
			messageCursor = page.next_cursor;
		} catch (error) {
			if (generation === detailGeneration) detailError = loadError(error);
		} finally {
			if (generation === detailGeneration) messagesAppending = false;
		}
	};

	onMount(async () => {
		if ($user?.role !== 'admin') {
			await goto('/', { replaceState: true });
			return;
		}
		await loadChats(true);
	});
</script>

<div class="mx-auto flex h-full w-full max-w-7xl flex-col px-4 pb-6 pt-3 sm:px-6">
	<header class="mb-4 flex-none">
		<h1 class="text-xl font-semibold text-gray-900 dark:text-gray-100">WeChat Chat Records</h1>
		<p class="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
			Read-only access to consented Mini Program conversations. Opening a conversation is recorded
			in the administrator audit log.
		</p>
	</header>

	{#if listLoading}
		<div class="flex flex-1 items-center justify-center" aria-live="polite">
			<div class="text-sm text-gray-500 dark:text-gray-400">Loading WeChat chat records…</div>
		</div>
	{:else if listError}
		<div
			class="rounded-2xl border px-5 py-4 text-sm {listError.kind === 'forbidden'
				? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200'
				: 'border-red-200 bg-red-50 text-red-800 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-200'}"
			role="alert"
		>
			<div class="font-medium">
				{listError.kind === 'forbidden'
					? 'WeChat record access is disabled'
					: listError.kind === 'unavailable'
						? 'Record service unavailable'
						: 'Unable to load records'}
			</div>
			<div class="mt-1 opacity-80">{listError.message}</div>
			{#if listError.kind !== 'forbidden'}
				<button
					type="button"
					class="mt-3 rounded-lg border border-current px-3 py-1.5 font-medium hover:bg-black/5 dark:hover:bg-white/5"
					on:click={() => loadChats(true)}
				>
					Try again
				</button>
			{/if}
		</div>
	{:else}
		<div class="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(20rem,0.9fr)_minmax(0,1.4fr)]">
			<section
				class="min-h-0 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900"
				aria-labelledby="wechat-record-list-heading"
			>
				<div class="border-b border-gray-100 px-4 py-3 dark:border-gray-800">
					<h2 id="wechat-record-list-heading" class="text-sm font-medium">Conversations</h2>
					<p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
						Metadata only. Message content loads after selection.
					</p>
				</div>

				<div class="max-h-full overflow-y-auto p-2">
					{#if chats.length === 0}
						<div class="px-3 py-12 text-center text-sm text-gray-500 dark:text-gray-400">
							No consented WeChat chat records are available.
						</div>
					{:else}
						<ul class="space-y-2">
							{#each chats as chat (chat.id)}
								<li>
									<button
										type="button"
										class="w-full rounded-xl border px-3 py-3 text-left transition {selectedSummary?.id ===
										chat.id
											? 'border-blue-400 bg-blue-50 dark:border-blue-700 dark:bg-blue-950/30'
											: 'border-gray-100 hover:border-gray-300 hover:bg-gray-50 dark:border-gray-800 dark:hover:border-gray-700 dark:hover:bg-gray-850'}"
										aria-pressed={selectedSummary?.id === chat.id}
										on:click={() => selectChat(chat)}
									>
										<div class="flex items-start justify-between gap-3">
											<div class="min-w-0">
												<div class="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
													{chat.account_ref}
												</div>
												<div class="mt-0.5 truncate font-mono text-[11px] text-gray-400">
													{chat.id}
												</div>
											</div>
											<span
												class="flex-none rounded-full px-2 py-0.5 text-[11px] font-medium {chat.state ===
												'completed'
													? 'bg-green-100 text-green-700 dark:bg-green-950/60 dark:text-green-300'
													: 'bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-300'}"
											>
												{stateLabel(chat.state)}
											</span>
										</div>

										<div
											class="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-gray-500 dark:text-gray-400"
										>
											<div class="truncate">Model: {chat.model || 'Unknown'}</div>
											<div>Thinking: {thinkingLabel(chat.thinking_mode)}</div>
											<div>{chat.turn_count} turns · {chat.message_count} messages</div>
											<div>{chat.report_count} reports</div>
										</div>
										<time
											class="mt-2 block text-xs text-gray-400"
											datetime={isoTimestamp(chat.updated_at)}
										>
											Updated {formatTimestamp(chat.updated_at)}
										</time>
									</button>
								</li>
							{/each}
						</ul>

						{#if chatCursor}
							<div class="flex justify-center py-4">
								<button
									type="button"
									class="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800"
									disabled={listAppending}
									on:click={() => loadChats(false)}
								>
									{listAppending ? 'Loading…' : 'Load more conversations'}
								</button>
							</div>
						{/if}
					{/if}
				</div>
			</section>

			<section
				class="min-h-0 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900"
				aria-labelledby="wechat-record-detail-heading"
			>
				{#if !selectedChat}
					<div class="flex h-full min-h-64 items-center justify-center px-6 text-center">
						<div>
							<h2 id="wechat-record-detail-heading" class="text-sm font-medium">
								Select a conversation
							</h2>
							<p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
								Messages are fetched only after you select a record.
							</p>
						</div>
					</div>
				{:else}
					<div class="flex h-full min-h-0 flex-col">
						<div class="flex-none border-b border-gray-100 px-4 py-3 dark:border-gray-800">
							<div class="flex items-start justify-between gap-3">
								<div class="min-w-0">
									<h2 id="wechat-record-detail-heading" class="truncate text-sm font-medium">
										{selectedChat.account_ref}
									</h2>
									<div class="mt-0.5 break-all font-mono text-[11px] text-gray-400">
										{selectedChat.id}
									</div>
								</div>
								<button
									type="button"
									class="flex-none rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
									on:click={closeDetail}
									aria-label="Close conversation details"
								>
									Close
								</button>
							</div>
							<div
								class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400"
							>
								<span>{selectedChat.model || 'Unknown model'}</span>
								<span>{thinkingLabel(selectedChat.thinking_mode)} thinking</span>
								<span>{stateLabel(selectedChat.state)}</span>
								<span>{selectedChat.turn_count} turns</span>
								<span>{selectedChat.message_count} messages</span>
								<span>{selectedChat.report_count} reports</span>
								<time datetime={isoTimestamp(selectedChat.created_at)}>
									Created {formatTimestamp(selectedChat.created_at)}
								</time>
							</div>
						</div>

						<div class="min-h-0 flex-1 overflow-y-auto p-4">
							{#if detailError}
								<div
									class="mb-4 rounded-xl border px-4 py-3 text-sm {detailError.kind === 'forbidden'
										? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200'
										: 'border-red-200 bg-red-50 text-red-800 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-200'}"
									role="alert"
								>
									<div>{detailError.message}</div>
									{#if detailError.kind === 'unavailable' && selectedSummary}
										<button
											type="button"
											class="mt-2 rounded-lg border border-current px-3 py-1 text-xs font-medium hover:bg-black/5 dark:hover:bg-white/5"
											on:click={() => selectChat(selectedSummary)}
										>
											Try again
										</button>
									{/if}
								</div>
							{/if}

							{#if detailLoading}
								<div
									class="py-12 text-center text-sm text-gray-500 dark:text-gray-400"
									aria-live="polite"
								>
									Loading conversation messages…
								</div>
							{:else if messages.length === 0 && !detailError}
								<div class="py-12 text-center text-sm text-gray-500 dark:text-gray-400">
									No reviewed messages are available for this conversation.
								</div>
							{:else}
								<ol class="space-y-4">
									{#each messages as message (message.id)}
										<li
											class="rounded-xl border p-3 {message.role === 'user'
												? 'border-blue-100 bg-blue-50/70 dark:border-blue-900/60 dark:bg-blue-950/20'
												: 'border-gray-100 bg-gray-50 dark:border-gray-800 dark:bg-gray-850'}"
										>
											<div class="flex items-center justify-between gap-3 text-xs">
												<span class="font-medium"
													>{message.role === 'user' ? 'WeChat user' : 'There'}</span
												>
												<time class="text-gray-400" datetime={isoTimestamp(message.created_at)}>
													{formatTimestamp(message.created_at)}
												</time>
											</div>
											<div
												class="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-gray-800 dark:text-gray-200"
											>
												{message.content}
											</div>

											{#if message.sources.length > 0}
												<div class="mt-3 border-t border-black/5 pt-2 dark:border-white/10">
													<div
														class="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400"
													>
														Sources
													</div>
													<ul class="space-y-1">
														{#each message.sources as source, sourceIndex}
															<li class="text-xs">
																<a
																	class="break-all text-blue-600 hover:underline dark:text-blue-400"
																	href={source.url}
																	target="_blank"
																	rel="noreferrer"
																	referrerpolicy="no-referrer"
																>
																	{source.name || `Source ${sourceIndex + 1}`}
																</a>
															</li>
														{/each}
													</ul>
												</div>
											{/if}
										</li>
									{/each}
								</ol>

								{#if messageCursor}
									<div class="flex justify-center py-5">
										<button
											type="button"
											class="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800"
											disabled={messagesAppending}
											on:click={loadMoreMessages}
										>
											{messagesAppending ? 'Loading…' : 'Load more messages'}
										</button>
									</div>
								{/if}
							{/if}
						</div>
					</div>
				{/if}
			</section>
		</div>
	{/if}
</div>
