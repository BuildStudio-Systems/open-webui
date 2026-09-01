<script lang="ts">
	import { getContext, createEventDispatcher } from 'svelte';
	import { fade } from 'svelte/transition';

	const dispatch = createEventDispatcher();

	import {
		config,
		user,
		models as _models,
		temporaryChatEnabled,
		selectedFolder
	} from '$lib/stores';
	import { refreshChatList, refreshFolderChatLists } from '$lib/stores/chatList';

	import Suggestions from './Suggestions.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import EyeSlash from '$lib/components/icons/EyeSlash.svelte';
	import MessageInput from './MessageInput.svelte';
	import FolderPlaceholder from './Placeholder/FolderPlaceholder.svelte';
	import FolderTitle from './Placeholder/FolderTitle.svelte';

	const i18n = getContext('i18n');

	export let createMessagePair: Function;
	export let stopResponse: Function;

	export let autoScroll = false;

	export let atSelectedModel: Model | undefined;
	export let selectedModels: [''];

	export let history;

	export let prompt = '';
	export let files = [];
	export let messageInput = null;

	export let selectedToolIds = [];
	export let selectedSkillIds = [];
	export let selectedFilterIds = [];
	export let pendingOAuthTools = [];

	export let showCommands = false;

	export let imageGenerationEnabled = false;
	export let codeInterpreterEnabled = false;
	export let webSearchEnabled = false;
	export let toolApprovalMode = 'full';
	export let onToolApprovalModeChange: Function = () => {};
	export let oauthRedirectHandler: Function = () => {};

	export let onUpload: Function = (e) => {};
	export let onUpdate: (data?: { file?: any }) => void = () => {};
	export let onSelect = (e) => {};
	export let onChange = (e) => {};
	export let onWebSearchToggle: Function = () => {};
	export let messageQueue: { id: string; prompt: string; files: any[] }[] = [];
	export let onQueueSendNow: (id: string) => void = () => {};
	export let onQueueEdit: (id: string) => void = () => {};
	export let onQueueDelete: (id: string) => void = () => {};
	export let askUser = {
		show: false,
		questions: [],
		allowOther: true,
		timeoutMs: null,
		onConfirm: (_value: any) => {},
		onCancel: () => {}
	};

	export let dragged = false;

	let models = [];
	let selectedModelIdx = 0;

	$: if (selectedModels.length > 0) {
		selectedModelIdx = models.length - 1;
	}

	$: models = selectedModels.map((id) => $_models.find((m) => m.id === id));

	// True when viewing a shared folder the current user doesn't own AND lacks write access
	$: folderReadOnly =
		$selectedFolder != null &&
		$selectedFolder.user_id !== $user?.id &&
		$selectedFolder.permission !== 'write';
</script>

<div class="there-main-landing m-auto w-full max-w-[66rem] px-5 @2xl:px-16 py-16 text-center">
	{#if $temporaryChatEnabled}
		<Tooltip
			content={$i18n.t("This chat won't appear in history and your messages will not be saved.")}
			className="w-full flex justify-center mb-0.5"
			placement="top"
		>
			<div class="flex items-center gap-1.5 text-gray-500 text-xs my-1 w-fit">
				<EyeSlash strokeWidth="2" className="size-3.5" />{$i18n.t('Temporary Chat')}
			</div>
		</Tooltip>
	{/if}

	<div class="w-full text-3xl text-gray-800 dark:text-gray-100 text-center flex items-center gap-4">
		<div class="w-full flex flex-col justify-center items-center">
			{#if $selectedFolder}
				<FolderTitle
					folder={$selectedFolder}
					readOnly={folderReadOnly}
					onUpdate={async () => {
						await Promise.all([refreshChatList(localStorage.token), refreshFolderChatLists(null)]);
					}}
					onDelete={async () => {
						await Promise.all([refreshChatList(localStorage.token), refreshFolderChatLists(null)]);

						selectedFolder.set(null);
					}}
				/>
			{:else}
				<div class="there-agent-intro" in:fade={{ duration: 180 }}>
					<div class="there-agent-identity">
						<img src="/assets/buildstudio-there-emblem.png" alt="There" draggable="false" />
						<div>
							<div class="there-agent-eyebrow"><i></i> BUILDSTUDIO AI AGENT</div>
							<div class="there-agent-status">LOCAL SYSTEM · ONLINE</div>
						</div>
					</div>

					<h1>Welcome back, <span>{$user?.name ?? 'User'}.</span></h1>
					<p>I’m There, BuildStudio’s AI agent. How can I help you today?</p>

					{#if models[selectedModelIdx]?.name}
						<button
							class="there-active-model"
							type="button"
							on:click={() => {
								selectedModelIdx = (selectedModelIdx + 1) % models.length;
							}}
						>
							<span></span>{models[selectedModelIdx]?.name}
						</button>
					{/if}
				</div>
			{/if}

			<div
				class="there-message-composer text-base font-normal @md:max-w-3xl w-full py-3 {atSelectedModel
					? 'mt-2'
					: ''}"
			>
				{#if !($selectedFolder && folderReadOnly)}
					<MessageInput
						bind:this={messageInput}
						{history}
						bind:selectedModels
						bind:files
						bind:prompt
						bind:autoScroll
						bind:selectedToolIds
						bind:selectedSkillIds
						bind:selectedFilterIds
						bind:imageGenerationEnabled
						bind:codeInterpreterEnabled
						bind:webSearchEnabled
						bind:atSelectedModel
						bind:showCommands
						bind:dragged
						{pendingOAuthTools}
						{oauthRedirectHandler}
						{toolApprovalMode}
						{onToolApprovalModeChange}
						{stopResponse}
						{createMessagePair}
						placeholder="Ask There anything..."
						{onChange}
						{onUpload}
						{onUpdate}
						{messageQueue}
						{onQueueSendNow}
						{onQueueEdit}
						{onQueueDelete}
						{askUser}
						{onWebSearchToggle}
						on:chatVariables
						on:submit={(e) => {
							dispatch('submit', e.detail);
						}}
					/>
				{/if}
			</div>
		</div>
	</div>

	{#if $selectedFolder}
		<div class="mx-auto px-4 md:max-w-3xl md:px-6 min-h-62" in:fade={{ duration: 200, delay: 200 }}>
			<FolderPlaceholder folder={$selectedFolder} />
		</div>
	{:else}
		<div class="there-suggestions mx-auto max-w-3xl mt-3" in:fade={{ duration: 200, delay: 200 }}>
			<div class="mx-5">
				<Suggestions
					className="grid grid-cols-1 @md:grid-cols-2 gap-2"
					suggestionPrompts={atSelectedModel?.info?.meta?.suggestion_prompts ??
						models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
						$config?.default_prompt_suggestions ??
						[]}
					inputValue={prompt}
					{onSelect}
				/>
			</div>
		</div>
	{/if}
</div>

<style>
	.there-main-landing {
		position: relative;
		z-index: 1;
	}

	.there-agent-intro {
		display: flex;
		max-width: 760px;
		margin: 0 auto 4px;
		flex-direction: column;
		align-items: center;
	}

	.there-agent-identity {
		display: flex;
		align-items: center;
		gap: 13px;
		margin-bottom: 24px;
		padding: 8px 13px 8px 9px;
		border: 1px solid rgba(104, 138, 215, 0.2);
		border-radius: 14px;
		background: rgba(6, 17, 40, 0.58);
		box-shadow: 0 14px 32px rgba(2, 7, 18, 0.2);
		backdrop-filter: blur(14px);
	}

	.there-agent-identity img {
		width: 48px;
		height: 48px;
		object-fit: contain;
		filter: drop-shadow(0 7px 12px rgba(2, 8, 23, 0.34));
	}

	.there-agent-identity > div {
		text-align: left;
	}

	.there-agent-eyebrow,
	.there-agent-status {
		font-family: 'Cascadia Mono', Consolas, monospace;
		font-weight: 650;
		line-height: 1.25;
	}

	.there-agent-eyebrow {
		display: flex;
		align-items: center;
		gap: 7px;
		color: #a9bce7;
		font-size: 9px;
		letter-spacing: 0.16em;
	}

	.there-agent-eyebrow i {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: #55d4b1;
		box-shadow: 0 0 10px rgba(85, 212, 177, 0.9);
	}

	.there-agent-status {
		margin-top: 5px;
		color: #5f78ad;
		font-size: 8px;
		letter-spacing: 0.12em;
	}

	.there-agent-intro h1 {
		margin: 0;
		color: #f4f7ff;
		font-size: clamp(34px, 4.2vw, 52px);
		font-weight: 680;
		letter-spacing: -0.055em;
		line-height: 1.05;
	}

	.there-agent-intro h1 span {
		color: #86a4ff;
	}

	.there-agent-intro p {
		margin: 14px 0 0;
		color: #8fa2ca;
		font-size: 13px;
		line-height: 1.65;
	}

	.there-active-model {
		display: flex;
		align-items: center;
		gap: 7px;
		margin-top: 14px;
		padding: 6px 10px;
		border: 1px solid rgba(101, 136, 218, 0.2);
		border-radius: 999px;
		color: #91a7d2;
		background: rgba(8, 21, 49, 0.5);
		font-family: 'Cascadia Mono', Consolas, monospace;
		font-size: 9px;
		letter-spacing: 0.08em;
	}

	.there-active-model span {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: #5d86ff;
		box-shadow: 0 0 10px rgba(93, 134, 255, 0.8);
	}

	.there-message-composer {
		margin-top: 14px;
	}

	.there-message-composer :global(#message-input-container) {
		border: 1px solid rgba(101, 136, 218, 0.28) !important;
		border-radius: 18px !important;
		background: rgba(8, 21, 49, 0.72) !important;
		box-shadow:
			0 18px 48px rgba(2, 7, 18, 0.32),
			inset 0 1px 0 rgba(255, 255, 255, 0.025) !important;
		backdrop-filter: blur(18px);
	}

	.there-message-composer :global(#message-input-container:focus-within) {
		border-color: rgba(87, 128, 255, 0.7) !important;
		box-shadow:
			0 20px 55px rgba(2, 7, 18, 0.38),
			0 0 0 3px rgba(76, 120, 255, 0.08) !important;
	}

	.there-suggestions :global([role='listitem']) {
		min-height: 54px;
		padding: 10px 12px !important;
		border: 1px solid rgba(101, 136, 218, 0.14);
		border-radius: 12px !important;
		background: rgba(8, 21, 49, 0.34) !important;
	}

	.there-suggestions :global([role='listitem']:hover) {
		border-color: rgba(92, 130, 231, 0.35);
		background: rgba(12, 30, 67, 0.64) !important;
		transform: translateY(-1px);
	}

	@media (max-width: 640px) {
		.there-main-landing {
			padding-top: 36px;
			padding-bottom: 36px;
		}

		.there-agent-identity {
			margin-bottom: 18px;
		}

		.there-agent-intro p {
			max-width: 310px;
		}
	}
</style>
