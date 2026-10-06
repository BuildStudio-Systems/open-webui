<script lang="ts">
  import { getContext, onDestroy, onMount } from 'svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import { PrivateShareTransfer } from '$lib/utils/private-share-transfer';
  const i18n = getContext('i18n');
  export let show = false;
  export let enabled = false;
  export let createPdf: () => Promise<Blob | undefined>;
  export let token: string;
  let transfer = new PrivateShareTransfer();
  let busy = false, failed = false, refusal = '';
  const GUIDANCE: Record<string, string> = {
    share_link_required: 'Sign in to Share once with this account so it can receive files, then retry the same PDF.',
    share_quota: 'Share declined the file: it exceeds the size limit or your remaining quota.',
  };
  const statusAbort = new AbortController();
  onMount(() => {
    const timer = setTimeout(() => statusAbort.abort(), 8000);
    if (token?.startsWith('bs1_')) {
      fetch('/api/v1/integrations/share/status', {headers: {Authorization: `Bearer ${token}`}, cache:'no-store', credentials:'same-origin', redirect:'error', signal:statusAbort.signal})
        .then(async r => { if (r.ok && !statusAbort.signal.aborted) enabled = (await r.json()).enabled === true; })
        .catch(() => {}).finally(() => clearTimeout(timer));
    } else clearTimeout(timer);
    return () => clearTimeout(timer);
  });
  onDestroy(() => { enabled = false; statusAbort.abort(); transfer.dispose(); });
  async function send() {
    if (!enabled || busy || transfer.saved) return;
    busy = true; failed = false; refusal = '';
    try { await transfer.send(token, createPdf); }
    catch (error) { failed = true; refusal = error instanceof Error && error.message in GUIDANCE ? error.message : ''; }
    finally { busy = false; transfer = transfer; }
  }
</script>

<Modal bind:show size="sm">
  <div class="p-6 space-y-4" aria-busy={busy}>
    <h2 class="text-lg font-semibold">{$i18n.t('Save PDF to Share')}</h2>
    <p class="text-sm">{$i18n.t('This copies the current conversation, including tool results, to your own private Share space. Review sensitive content before confirming. No public link is created.')}</p>
    <p class="text-xs text-gray-500">{$i18n.t('Retrying sends the same PDF. Closing this window keeps the pending copy until you leave this conversation or sign out.')}</p>
    {#if transfer.name}<p class="break-all text-xs">{transfer.name}</p>{/if}
    {#if failed}<p role="alert" class="text-sm text-red-600">{$i18n.t(GUIDANCE[refusal] ?? 'The copy is not confirmed. Check your Systems and Share access, then retry the same PDF.')}</p>{/if}
    {#if transfer.saved}
      <p role="status">{$i18n.t('Private PDF saved.')}</p>
      <a class="underline" href={transfer.shareHref} target="_blank" rel="noopener noreferrer">{$i18n.t('Open saved file')}</a>
    {/if}
    <div class="flex gap-3 justify-end">
      <button class="rounded-xl px-4 py-2 text-sm bg-gray-100 dark:bg-gray-800" on:click={() => show = false}>{$i18n.t('Close')}</button>
      <button class="rounded-xl px-4 py-2 text-sm bg-black text-white dark:bg-white dark:text-black disabled:opacity-50" disabled={!enabled || busy || transfer.saved} on:click={send}>
        {$i18n.t(busy ? 'Preparing and saving PDF…' : transfer.saved ? 'Private PDF saved.' : transfer.prepared ? 'Retry the same PDF' : 'Confirm private copy')}
      </button>
    </div>
  </div>
</Modal>
