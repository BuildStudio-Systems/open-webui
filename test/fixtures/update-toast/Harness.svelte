<script lang="ts">
	import { setContext } from 'svelte';
	import { writable } from 'svelte/store';
	import UpdateInfoToast from '../../../src/lib/components/layout/UpdateInfoToast.svelte';
	const language = new URLSearchParams(window.location.search).get('lang') ?? 'en';
	const messages = {
		en: ['A new version is available.', 'Update for the latest features and improvements.', 'Close'],
		zh: ['新版本已发布。', '更新以获取最新功能和改进。', '关闭'],
		ja: ['新しいバージョンが利用できます。', '最新機能と改善のために更新してください。', '閉じる']
	};
	const translated = messages[language as keyof typeof messages] ?? messages.en;
	setContext('i18n', writable({ t: (text: string) => text === 'Close' ? translated[2] : text.startsWith('A new version') ? translated[0] : translated[1] }));
	let visible = true;
</script>

<main>
	<h1>THERE update notice regression fixture</h1>
	<p>Isolated component test, not the production application.</p>
	{#if visible}
		<UpdateInfoToast version={{ current: 'test', latest: 'test-next' }} on:close={() => visible = false} />
	{:else}
		<p role="status">Update notice closed</p>
	{/if}
	<div id="composer">
		<label for="message">Message</label>
		<textarea id="message" placeholder="Type a test message"></textarea>
		<button type="button">Send</button>
	</div>
</main>

<style>
	main { min-height: 100dvh; padding: 7rem 1rem 10rem; color: #dbeafe; background: #061329; }
	h1 { font-size: 1.2rem; }
	#composer { position: fixed; bottom: 1rem; left: 1rem; right: 1rem; height: 8rem; padding: 0.5rem; background: #102342; border-radius: 1rem; }
	textarea { display: block; width: 100%; background: transparent; color: white; }
</style>
