<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { getContext, onDestroy, onMount, tick } from 'svelte';
	const i18n = getContext('i18n');

	import Modal from '$lib/components/common/Modal.svelte';
	import SearchInput from './Sidebar/SearchInput.svelte';
	import {
		getChatById,
		getChatList,
		getChatListBySearchText,
		cloneChatById,
		deleteChatById,
		archiveChatById,
		updateChatById,
		updateChatFolderIdById,
		markChatUnreadById,
		getAllTags
	} from '$lib/apis/chats';
	import Spinner from '../common/Spinner.svelte';

	import dayjs from '$lib/dayjs';
	import localizedFormat from 'dayjs/plugin/localizedFormat';
	import calendar from 'dayjs/plugin/calendar';
	import Loader from '../common/Loader.svelte';
	import { createMessagesList } from '$lib/utils';
	import { getOutputText } from '$lib/components/chat/Messages/structuredOutput';
	import { config, user, chatId as currentChatId, tags } from '$lib/stores';
	import { refreshChatList } from '$lib/stores/chatList';
	import Messages from '../chat/Messages.svelte';
	import { goto } from '$app/navigation';
	import EditPencilIcon from './Sidebar/icons/EditPencil.svelte';
	import NotesIcon from './Sidebar/icons/Notes.svelte';

	import ChatMenu from './Sidebar/ChatMenu.svelte';
	import ShareChatModal from '../chat/ShareChatModal.svelte';
	import DeleteConfirmDialog from '../common/ConfirmDialog.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import Sparkles from '../icons/Sparkles.svelte';
	import ArchiveBox from '../icons/ArchiveBox.svelte';
	import GarbageBin from '../icons/GarbageBin.svelte';
	import { generateTitle } from '$lib/apis';
	dayjs.extend(calendar);
	dayjs.extend(localizedFormat);

	export let show = false;
	export let onClose = () => {};

	let showShareChatModal = false;
	let showDeleteConfirm = false;
	let menuChatId = '';
	let menuChatTitle = '';

	let editingChatId = null;
	let editingChatTitle = '';

	let shiftKey = false;

	const onShiftKeyDown = (e) => {
		if (e.key === 'Shift') shiftKey = true;
	};

	const onShiftKeyUp = (e) => {
		if (e.key === 'Shift') shiftKey = false;
	};
	let generating = false;

	const refreshSidebar = async () => {
		await refreshChatList(localStorage.token, { refreshPinned: true });
	};

	const cloneChatHandler = async (id) => {
		const chat = chatList?.find((c) => c.id === id);
		const res = await cloneChatById(
			localStorage.token,
			id,
			$i18n.t('Clone of {{TITLE}}', {
				TITLE: chat?.title ?? 'Chat'
			})
		).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			await refreshSidebar();
			await searchHandler();
		}
	};

	const markUnreadHandler = async (id) => {
		const res = await markChatUnreadById(localStorage.token, id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			await refreshSidebar();
		}
	};

	const archiveChatHandler = async (id) => {
		try {
			await archiveChatById(localStorage.token, id);

			chatList = chatList?.filter((c) => c.id !== id) ?? null;

			if ($currentChatId === id) {
				await goto('/');
				currentChatId.set('');
			}

			await refreshSidebar();
			toast.success($i18n.t('Chat archived.'));
		} catch (error) {
			toast.error($i18n.t('Failed to archive chat.'));
		}
	};

	const deleteChatHandler = async (id) => {
		const res = await deleteChatById(localStorage.token, id).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			chatList = chatList?.filter((c) => c.id !== id) ?? null;
			tags.set(await getAllTags(localStorage.token));

			if ($currentChatId === id) {
				await goto('/');
				currentChatId.set('');
			}

			await refreshSidebar();
		}
	};

	const moveChatHandler = async (chatId, folderId) => {
		if (chatId && folderId) {
			const res = await updateChatFolderIdById(localStorage.token, chatId, folderId).catch(
				(error) => {
					toast.error(`${error}`);
					return null;
				}
			);

			if (res) {
				chatList = chatList?.filter((c) => c.id !== chatId) ?? null;
				await refreshSidebar();
				toast.success($i18n.t('Chat moved successfully'));
			}
		}
	};

	const renameHandler = async (id) => {
		editingChatId = id;
		editingChatTitle = chatList?.find((c) => c.id === id)?.title ?? '';

		await tick();
		const input = document.getElementById(`search-chat-title-input-${id}`);
		if (input) {
			input.focus();
			input.select();
		}
	};

	const confirmRename = async () => {
		if (!editingChatId) return;

		const trimmed = editingChatTitle.trim();
		if (trimmed === '') {
			toast.error($i18n.t('Title cannot be an empty string.'));
			return;
		}

		await updateChatById(localStorage.token, editingChatId, { title: trimmed });

		if (chatList) {
			chatList = chatList.map((c) => (c.id === editingChatId ? { ...c, title: trimmed } : c));
		}

		editingChatId = null;
		editingChatTitle = '';
		await refreshSidebar();
	};

	const cancelRename = () => {
		editingChatId = null;
		editingChatTitle = '';
	};

	const generateTitleHandler = async () => {
		if (!editingChatId || generating) return;

		generating = true;
		const chat = await getChatById(localStorage.token, editingChatId).catch(() => null);

		if (!chat) {
			toast.error($i18n.t('Failed to load chat'));
			generating = false;
			return;
		}

		const chatContent = chat.chat;
		const history = chatContent?.history;
		let msgList = [];

		if (history?.messages && history?.currentId) {
			msgList = createMessagesList(history, history.currentId).map((m: any) => ({
				role: m.role,
				content: getOutputText(m.output) || m.content || ''
			}));
		} else {
			msgList = (chatContent?.messages ?? []).map((m: any) => ({
				role: m.role,
				content: getOutputText(m.output) || m.content || ''
			}));
		}

		let model = '';
		if (history?.messages && history?.currentId) {
			let currentId = history.currentId;
			while (currentId) {
				const msg = history.messages[currentId];
				if (!msg) break;
				if (msg.role === 'assistant' && msg.model) {
					model = msg.model;
					break;
				}
				currentId = msg.parentId;
			}
		}
		if (!model) {
			model = chatContent?.models?.at(0) ?? '';
		}

		editingChatTitle = '';

		const generatedTitle = await generateTitle(localStorage.token, model, msgList).catch(
			(error) => {
				toast.error(`${error}`);
				return null;
			}
		);

		if (generatedTitle) {
			editingChatTitle = generatedTitle;
		}

		generating = false;

		if (generatedTitle) {
			await confirmRename();
		}
	};

	let actions = [
		{
			label: $i18n.t('Start a new conversation'),
			onClick: async () => {
				await goto(`/${query ? `?q=${query}` : ''}`);
				show = false;
				onClose();
			},
			icon: EditPencilIcon
		}
	];

	let query = '';
	let page = 1;

	let chatList = null;

	let chatListLoading = false;
	let allChatsLoaded = false;

	let searchDebounceTimeout;

	let selectedIdx = null;
	let selectedChat = null;

	let selectedModels = [''];
	let history = null;
	let messages = null;
	let messagesContainerElement: HTMLElement | null = null;
	const messagesContainerId = 'chat-preview';

	const searchFilterPrefixes = ['tag:', 'folder:', 'pinned:', 'archived:', 'shared:'];

	const getSnippetQuery = (query: string) => {
		return query
			.trim()
			.split(/\s+/)
			.filter(
				(word) => !searchFilterPrefixes.some((prefix) => word.toLowerCase().startsWith(prefix))
			)
			.join(' ')
			.trim();
	};

	const getHighlightedSnippet = (snippet: string, query: string) => {
		const match = getSnippetQuery(query).toLowerCase();
		const matchIndex = match ? snippet.toLowerCase().indexOf(match) : -1;

		if (matchIndex === -1) {
			return [{ text: snippet, highlight: false }];
		}

		const start = Math.max(matchIndex - 60, 0);
		const end = Math.min(matchIndex + match.length + 80, snippet.length);
		const visibleSnippet = `${start > 0 ? '...' : ''}${snippet.slice(start, end)}${
			end < snippet.length ? '...' : ''
		}`;
		const index = visibleSnippet.toLowerCase().indexOf(match);

		return [
			{ text: visibleSnippet.slice(0, index), highlight: false },
			{ text: visibleSnippet.slice(index, index + match.length), highlight: true },
			{ text: visibleSnippet.slice(index + match.length), highlight: false }
		].filter((part) => part.text);
	};

	$: if (!chatListLoading && chatList) {
		loadChatPreview(selectedIdx);
	}

	const scrollPreviewToBottom = async () => {
		await tick();
		requestAnimationFrame(() => {
			if (messagesContainerElement) {
				messagesContainerElement.scrollTop = messagesContainerElement.scrollHeight;

				requestAnimationFrame(() => {
					if (messagesContainerElement) {
						messagesContainerElement.scrollTop = messagesContainerElement.scrollHeight;
					}
				});
			}
		});
		setTimeout(() => {
			if (messagesContainerElement) {
				messagesContainerElement.scrollTop = messagesContainerElement.scrollHeight;
			}
		}, 80);
	};

	const loadChatPreview = async (selectedIdx) => {
		if (!chatList || chatList.length === 0 || selectedIdx === null) {
			selectedChat = null;
			messages = null;
			history = null;
			selectedModels = [''];
			return;
		}

		const selectedChatIdx = selectedIdx - actions.length;
		if (selectedChatIdx < 0 || selectedChatIdx >= chatList.length) {
			selectedChat = null;
			messages = null;
			history = null;
			selectedModels = [''];
			return;
		}

		const chatId = chatList[selectedChatIdx].id;

		const chat = await getChatById(localStorage.token, chatId).catch(async (error) => {
			return null;
		});

		if (chat) {
			selectedChat = chat;

			if (chat?.chat?.history) {
				selectedModels =
					(chat?.chat?.models ?? undefined) !== undefined
						? chat?.chat?.models
						: [chat?.chat?.models ?? ''];

				history = chat?.chat?.history;
				messages = [];
				await scrollPreviewToBottom();
			} else {
				messages = [];
			}
		} else {
			toast.error($i18n.t('Failed to load chat preview'));
			selectedChat = null;
			messages = null;
			history = null;
			selectedModels = [''];
			return;
		}
	};

	const searchHandler = async () => {
		if (!show) {
			return;
		}

		if (searchDebounceTimeout) {
			clearTimeout(searchDebounceTimeout);
		}

		page = 1;
		chatList = null;
		if (query === '') {
			chatList = await getChatList(localStorage.token, page);
		} else {
			searchDebounceTimeout = setTimeout(async () => {
				chatList = await getChatListBySearchText(localStorage.token, query, page);

				if ((chatList ?? []).length === 0) {
					allChatsLoaded = true;
				} else {
					allChatsLoaded = false;
				}
			}, 500);
		}

		selectedChat = null;
		messages = null;
		history = null;
		selectedModels = [''];

		if ((chatList ?? []).length === 0) {
			allChatsLoaded = true;
		} else {
			allChatsLoaded = false;
		}
	};

	const loadMoreChats = async () => {
		chatListLoading = true;
		page += 1;

		let newChatList = [];

		if (query) {
			newChatList = await getChatListBySearchText(localStorage.token, query, page);
		} else {
			newChatList = await getChatList(localStorage.token, page);
		}

		// once the bottom of the list has been reached (no results) there is no need to continue querying
		allChatsLoaded = newChatList.length === 0;

		if (newChatList.length > 0) {
			const existingIds = new Set(chatList.map((c) => c.id));
			const uniqueNewChats = newChatList.filter((c) => !existingIds.has(c.id));
			chatList = [...chatList, ...uniqueNewChats];
		}

		chatListLoading = false;
	};

	$: if (show) {
		searchHandler();
	} else {
		editingChatId = null;
		editingChatTitle = '';
		generating = false;
	}

	const onKeyDown = (e) => {
		// Ignore keydown fired while confirming an IME composition (e.g. Japanese/Chinese/Korean)
		// so confirming the composition with Enter doesn't trigger search actions (#26172).
		if (e.isComposing || e.keyCode === 229) {
			return;
		}

		const searchOptions = document.getElementById('search-options-container');
		if (searchOptions || !show) {
			return;
		}

		// Don't handle navigation/activation keys while editing a chat title
		if (editingChatId) {
			return;
		}

		if (e.code === 'Escape') {
			show = false;
			onClose();
		} else if (e.code === 'Enter') {
			const item = document.querySelector(`[data-arrow-selected="true"]`);
			if (item) {
				item?.click();
				show = false;
			}

			return;
		} else if (e.code === 'ArrowDown') {
			const searchInput = document.getElementById('search-input');

			if (searchInput) {
				// check if focused on the search input
				if (document.activeElement === searchInput) {
					searchInput.blur();
					selectedIdx = 0;
					return;
				}
			}

			selectedIdx = Math.min(selectedIdx + 1, (chatList ?? []).length - 1 + actions.length);
		} else if (e.code === 'ArrowUp') {
			if (selectedIdx === 0) {
				const searchInput = document.getElementById('search-input');

				if (searchInput) {
					// check if focused on the search input
					if (document.activeElement !== searchInput) {
						searchInput.focus();
						selectedIdx = 0;
						return;
					}
				}
			}

			selectedIdx = Math.max(selectedIdx - 1, 0);
		}

		const item = document.querySelector(`[data-arrow-selected="true"]`);
		item?.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
	};

	onMount(() => {
		actions = [
			...actions,
			...(($config?.features?.enable_notes ?? false) &&
			($user?.role === 'admin' || ($user?.permissions?.features?.notes ?? true))
				? [
						{
							label: $i18n.t('Create a new note'),
							onClick: async () => {
								await goto(`/notes?content=${query}`);
								show = false;
								onClose();
							},
							icon: NotesIcon
						}
					]
				: [])
		];

		document.addEventListener('keydown', onKeyDown);
		document.addEventListener('keydown', onShiftKeyDown);
		document.addEventListener('keyup', onShiftKeyUp);
	});

	onDestroy(() => {
		if (searchDebounceTimeout) {
			clearTimeout(searchDebounceTimeout);
		}
		document.removeEventListener('keydown', onKeyDown);
		document.removeEventListener('keydown', onShiftKeyDown);
		document.removeEventListener('keyup', onShiftKeyUp);
	});
</script>

<ShareChatModal bind:show={showShareChatModal} chatId={menuChatId} />

<DeleteConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete chat?')}
	on:confirm={() => {
		deleteChatHandler(menuChatId);
	}}
>
	<div class="text-sm text-gray-500 flex-1 line-clamp-3">
		{$i18n.t('This will delete')} <span class="font-normal">{menuChatTitle}</span>.
	</div>
</DeleteConfirmDialog>

<Modal
	size="xl"
	bind:show
	containerClassName="buildstudio-search-overlay p-3"
	className="buildstudio-search-modal"
>
	<div class="buildstudio-search-shell py-3 text-[#dce8ff]">
		<div class="buildstudio-search-header px-5 pb-3">
			<div class="flex items-center gap-2.5">
				<img
					src="/assets/buildstudio-there-emblem.png?v=buildstudio-there-20260901"
					alt=""
					class="size-7 object-contain"
				/>
				<div>
					<div class="text-[0.65rem] font-semibold tracking-[0.22em] text-[#6f93d8]">
						BUILDSTUDIO THERE
					</div>
					<div class="text-sm font-medium text-[#e8f0ff]">Search your workspace</div>
				</div>
			</div>
			<kbd>ESC</kbd>
		</div>

		<div class="buildstudio-search-input-wrap px-4 pb-3">
			<SearchInput
				bind:value={query}
				on:input={searchHandler}
				placeholder={$i18n.t('Search')}
				showClearButton={true}
				onFocus={() => {
					selectedIdx = null;
					messages = null;
				}}
				onKeydown={(e) => {
					if (e.code === 'Enter' && (chatList ?? []).length > 0) {
						const item = document.querySelector(`[data-arrow-selected="true"]`);
						if (item) {
							item?.click();
						}

						show = false;
						return;
					} else if (e.code === 'ArrowDown') {
						selectedIdx = Math.min(selectedIdx + 1, (chatList ?? []).length - 1 + actions.length);
					} else if (e.code === 'ArrowUp') {
						selectedIdx = Math.max(selectedIdx - 1, 0);
					} else {
						selectedIdx = 0;
					}

					const item = document.querySelector(`[data-arrow-selected="true"]`);
					item?.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
				}}
			/>
		</div>

		<div class="buildstudio-search-columns flex px-4 pb-1">
			<div
				class="buildstudio-search-list flex flex-col overflow-y-auto max-h-full scrollbar-hidden w-full flex-1 pr-3"
			>
				<div class="buildstudio-search-section-label w-full pb-2 px-2">
					{$i18n.t('Actions')}
				</div>

				{#each actions as action, idx (action.label)}
					<button
						class="buildstudio-search-action w-full flex items-center rounded-lg text-sm py-1.5 px-2.5 hover:bg-gray-50/70 dark:hover:bg-gray-850/50 {selectedIdx ===
						idx
							? 'buildstudio-search-selected'
							: ''}"
						data-arrow-selected={selectedIdx === idx ? 'true' : undefined}
						draggable="false"
						on:mouseenter={() => {
							selectedIdx = idx;
						}}
						on:click={async () => {
							await action.onClick();
						}}
					>
						<div class="pr-2">
							<svelte:component this={action.icon} />
						</div>
						<div class=" flex-1 text-left">
							<div class="text-ellipsis line-clamp-1 w-full">
								{$i18n.t(action.label)}
							</div>
						</div>
					</button>
				{/each}

				{#if chatList}
					<div aria-hidden="true" class="h-px my-3" />

					{#if chatList.length === 0}
						<div class="text-xs text-gray-500 dark:text-gray-400 text-center px-5 py-4">
							{$i18n.t('No results found')}
						</div>
					{/if}

					{#each chatList as chat, idx (chat.id)}
						{#if idx === 0 || (idx > 0 && chat.time_range !== chatList[idx - 1].time_range)}
							<div
								class="buildstudio-search-section-label w-full {idx === 0
									? ''
									: 'pt-4'} pb-1.5 px-2"
							>
								{$i18n.t(chat.time_range)}
								<!-- localisation keys for time_range to be recognized from the i18next parser (so they don't get automatically removed):
							{$i18n.t('Today')}
							{$i18n.t('Yesterday')}
							{$i18n.t('Previous 7 days')}
							{$i18n.t('Previous 30 days')}
							{$i18n.t('January')}
							{$i18n.t('February')}
							{$i18n.t('March')}
							{$i18n.t('April')}
							{$i18n.t('May')}
							{$i18n.t('June')}
							{$i18n.t('July')}
							{$i18n.t('August')}
							{$i18n.t('September')}
							{$i18n.t('October')}
							{$i18n.t('November')}
							{$i18n.t('December')}
							-->
							</div>
						{/if}

						<!-- svelte-ignore a11y-no-static-element-interactions -->
						<div
							class="buildstudio-search-chat w-full flex justify-between items-center rounded-lg text-sm py-1.5 pl-2.5 pr-32 hover:bg-gray-50/70 dark:hover:bg-gray-850/50 group/item relative {selectedIdx ===
							idx + actions.length
								? 'buildstudio-search-selected'
								: ''}"
							data-arrow-selected={selectedIdx === idx + actions.length ? 'true' : undefined}
							on:mouseenter={() => {
								selectedIdx = idx + actions.length;
							}}
						>
							{#if editingChatId === chat.id}
								<div class="flex-1 min-w-0">
									<input
										id="search-chat-title-input-{chat.id}"
										bind:value={editingChatTitle}
										class="bg-transparent w-full outline-none"
										placeholder={generating ? $i18n.t('Generating...') : ''}
										disabled={generating}
										on:keydown={(e) => {
											e.stopPropagation();
											if (e.key === 'Enter') {
												e.preventDefault();
												confirmRename();
											} else if (e.key === 'Escape') {
												e.preventDefault();
												cancelRename();
											}
										}}
										on:blur={() => {
											if (!generating) {
												confirmRename();
											}
										}}
									/>
								</div>

								<div class="flex items-center shrink-0 pl-1">
									<Tooltip content={$i18n.t('Generate')}>
										<button
											class="self-center dark:hover:text-white transition disabled:cursor-not-allowed"
											disabled={generating}
											on:mousedown|preventDefault={() => {}}
											on:click|preventDefault|stopPropagation={() => {
												generateTitleHandler();
											}}
										>
											{#if generating}
												<Spinner className="size-4" />
											{:else}
												<Sparkles strokeWidth="2" />
											{/if}
										</button>
									</Tooltip>
								</div>
							{:else}
								<a
									class="flex-1 min-w-0"
									href="/c/{chat.id}"
									draggable="false"
									on:click={async () => {
										await goto(`/c/${chat.id}`);
										show = false;
										onClose();
									}}
								>
									<div class="text-ellipsis line-clamp-1 w-full">
										{chat?.title}
									</div>
									{#if chat?.snippet}
										<div class="text-xs text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">
											{#each getHighlightedSnippet(chat.snippet, query) as part}
												{#if part.highlight}
													<mark
														class="rounded bg-yellow-200/70 px-0.5 text-inherit dark:bg-yellow-500/30"
													>
														{part.text}
													</mark>
												{:else}
													{part.text}
												{/if}
											{/each}
										</div>
									{/if}
								</a>
							{/if}

							<div
								class="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-3 pl-6 shrink-0"
							>
								<div class="text-gray-500 dark:text-gray-400 text-xs">
									{$i18n.t(
										dayjs(chat?.updated_at * 1000).calendar(null, {
											sameDay: '[Today]',
											nextDay: '[Tomorrow]',
											nextWeek: 'dddd',
											lastDay: '[Yesterday]',
											lastWeek: '[Last] dddd',
											sameElse: 'L'
										})
									)}
								</div>

								{#if editingChatId !== chat.id}
									{#if shiftKey}
										<div class="flex items-center space-x-1.5">
											<Tooltip content={$i18n.t('Archive')} className="flex items-center">
												<button
													class="self-center dark:hover:text-white transition"
													on:click|stopPropagation={() => {
														archiveChatHandler(chat.id);
													}}
													type="button"
												>
													<ArchiveBox className="size-4 translate-y-[0.5px]" strokeWidth="2" />
												</button>
											</Tooltip>

											{#if $user?.role === 'admin' || ($user?.permissions?.chat?.delete ?? true)}
												<Tooltip content={$i18n.t('Delete')}>
													<button
														class="self-center dark:hover:text-white transition"
														on:click|stopPropagation={() => {
															deleteChatHandler(chat.id);
														}}
														type="button"
													>
														<GarbageBin strokeWidth="2" />
													</button>
												</Tooltip>
											{/if}
										</div>
									{:else}
										<div class="flex items-center">
											<ChatMenu
												chatId={chat.id}
												shareHandler={() => {
													menuChatId = chat.id;
													showShareChatModal = true;
												}}
												{moveChatHandler}
												cloneChatHandler={() => {
													cloneChatHandler(chat.id);
												}}
												archiveChatHandler={() => {
													archiveChatHandler(chat.id);
												}}
												renameHandler={() => {
													renameHandler(chat.id);
												}}
												markUnreadHandler={() => {
													markUnreadHandler(chat.id);
												}}
												deleteHandler={() => {
													menuChatId = chat.id;
													menuChatTitle = chat.title;
													showDeleteConfirm = true;
												}}
												onClose={() => {}}
												onPinChange={async () => {
													await refreshSidebar();
													await searchHandler();
												}}
											>
												<button
													aria-label="Chat Menu"
													class="self-center dark:hover:text-white transition"
												>
													<svg
														xmlns="http://www.w3.org/2000/svg"
														viewBox="0 0 16 16"
														fill="currentColor"
														class="w-4 h-4"
													>
														<path
															d="M2 8a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0ZM6.5 8a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0ZM12.5 6.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z"
														/>
													</svg>
												</button>
											</ChatMenu>
										</div>
									{/if}
								{/if}
							</div>
						</div>
					{/each}

					{#if !allChatsLoaded}
						<Loader
							on:visible={(e) => {
								if (!chatListLoading) {
									loadMoreChats();
								}
							}}
						>
							<div class="w-full flex justify-center py-4 text-xs animate-pulse items-center gap-2">
								<Spinner className=" size-4" />
								<div class=" ">{$i18n.t('Loading...')}</div>
							</div>
						</Loader>
					{/if}
				{:else}
					<div class="w-full h-full flex justify-center items-center">
						<Spinner className="size-5" />
					</div>
				{/if}
			</div>
			<div
				id={messagesContainerId}
				bind:this={messagesContainerElement}
				class="buildstudio-search-preview hidden md:flex md:flex-1 w-full overflow-y-auto scrollbar-hidden @container"
			>
				{#if messages === null}
					<div
						class="w-full h-full flex justify-center items-center text-gray-500 dark:text-gray-400 text-sm"
					>
						{$i18n.t('Select a conversation to preview')}
					</div>
				{:else}
					<div class="w-full h-full flex flex-col">
						<Messages
							className="h-full flex pt-4 pb-8 w-full"
							chatId={`chat-preview-${selectedChat?.id ?? ''}`}
							user={$user}
							readOnly={true}
							{selectedModels}
							bind:history
							autoScroll={true}
							{messagesContainerId}
							messagesCount={8}
							sendMessage={() => {}}
							continueResponse={() => {}}
							regenerateResponse={() => {}}
						/>
					</div>
				{/if}
			</div>
		</div>
	</div>
</Modal>

<style>
	:global(.buildstudio-search-overlay) {
		background: rgba(1, 7, 20, 0.78) !important;
		backdrop-filter: blur(10px);
	}

	:global(.buildstudio-search-modal) {
		position: relative;
		overflow: hidden;
		border: 1px solid rgba(60, 103, 171, 0.72) !important;
		border-radius: 1.65rem !important;
		background:
			radial-gradient(circle at 12% 0%, rgba(43, 83, 167, 0.2), transparent 34%),
			linear-gradient(145deg, rgba(8, 24, 52, 0.99), rgba(3, 12, 29, 0.995)) !important;
		box-shadow:
			0 36px 100px rgba(0, 0, 0, 0.58),
			0 0 0 1px rgba(93, 139, 255, 0.06) inset !important;
		color: #dce8ff !important;
	}

	:global(.buildstudio-search-modal::before) {
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		background: linear-gradient(90deg, #ff344f, #5c74ff 54%, #28d7df);
		content: '';
	}

	.buildstudio-search-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		border-bottom: 1px solid rgba(68, 105, 164, 0.28);
	}

	.buildstudio-search-header kbd {
		border: 1px solid rgba(100, 136, 193, 0.42);
		border-radius: 0.45rem;
		background: rgba(13, 35, 72, 0.72);
		padding: 0.22rem 0.45rem;
		font-size: 0.6rem;
		letter-spacing: 0.12em;
		color: #91a9d6;
	}

	.buildstudio-search-input-wrap {
		padding-top: 0.75rem;
	}

	:global(.buildstudio-search-modal #search-container) {
		margin: 0;
		padding: 0;
	}

	:global(.buildstudio-search-modal #chat-search) {
		min-height: 3rem;
		align-items: center;
		border: 1px solid rgba(65, 110, 184, 0.62);
		border-radius: 0.9rem;
		background: rgba(5, 18, 42, 0.84);
		box-shadow: 0 0 0 3px rgba(58, 101, 197, 0.06);
	}

	:global(.buildstudio-search-modal #chat-search > div:first-child) {
		padding-left: 0.9rem;
		color: #80a6ef;
	}

	:global(.buildstudio-search-modal #search-input) {
		color: #e9f1ff !important;
	}

	:global(.buildstudio-search-modal #search-input::placeholder) {
		color: #6984b4;
	}

	.buildstudio-search-columns {
		gap: 0;
	}

	.buildstudio-search-list {
		height: min(40rem, calc(100dvh - 11.5rem));
		border-right: 1px solid rgba(63, 99, 154, 0.28);
	}

	.buildstudio-search-preview {
		height: min(40rem, calc(100dvh - 11.5rem));
		background: linear-gradient(180deg, rgba(7, 20, 45, 0.46), rgba(3, 13, 31, 0.22));
	}

	.buildstudio-search-section-label {
		font-size: 0.65rem;
		font-weight: 600;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: #6f8fc8;
	}

	.buildstudio-search-action,
	.buildstudio-search-chat {
		min-height: 2.35rem;
		color: #cbdaf5;
		transition:
			background-color 140ms ease,
			color 140ms ease,
			box-shadow 140ms ease;
	}

	.buildstudio-search-action:hover,
	.buildstudio-search-chat:hover,
	.buildstudio-search-action.buildstudio-search-selected,
	.buildstudio-search-chat.buildstudio-search-selected,
	.buildstudio-search-action[data-arrow-selected='true'],
	.buildstudio-search-chat[data-arrow-selected='true'] {
		background: linear-gradient(90deg, rgba(39, 83, 155, 0.55), rgba(21, 49, 97, 0.35)) !important;
		box-shadow: inset 2px 0 0 #5c83ff;
		color: #ffffff;
	}

	:global(.buildstudio-search-modal mark) {
		background: rgba(61, 105, 210, 0.42) !important;
		color: #ffffff !important;
	}

	@media (max-width: 767px) {
		.buildstudio-search-header {
			padding-inline: 1rem;
		}

		.buildstudio-search-list {
			border-right: 0;
			padding-right: 0;
		}
	}
</style>
