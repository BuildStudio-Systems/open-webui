// One explicit private copy, with stable bytes and request identity across retries.
// Stable refusal codes the server answers with (never upstream text); anything else stays 'copy_unconfirmed'.
export const ACTIONABLE_REFUSALS = ['share_link_required', 'share_quota'] as const;
export type Refusal = typeof ACTIONABLE_REFUSALS[number];
async function refusal(response: Response): Promise<string> {
  try {
    const body: unknown = await response.clone().json();
    const detail = typeof body === 'object' && body !== null ? (body as {detail?: unknown}).detail : undefined;
    return typeof detail === 'string' && (ACTIONABLE_REFUSALS as readonly string[]).includes(detail) ? detail : 'copy_unconfirmed';
  } catch { return 'copy_unconfirmed'; }
}
export class PrivateShareTransfer {
  private pending: {pdf: Blob; name: string; key: string} | null = null;
  private controller: AbortController | null = null;
  private disposed = false;
  busy = false;
  saved = false;
  fileId: string | null = null;
  get name() { return this.pending?.name ?? ''; }
  get prepared() { return this.pending !== null; }
  get shareHref() { return this.fileId ? `https://buildstudio-share.com/#file=${this.fileId}` : ''; }
  dispose() { this.disposed = true; this.controller?.abort(); this.pending = null; this.fileId = null; }
  async send(token: string, createPdf: () => Promise<Blob | undefined>, request: typeof fetch = fetch) {
    if (this.busy || this.saved || this.disposed) return;
    if (!token.startsWith('bs1_')) throw new Error('central_session_required');
    this.busy = true;
    const controller = new AbortController(); this.controller = controller;
    const deadline = setTimeout(() => controller.abort(), 95000);
    try {
      if (!this.pending) {
        const pdf = await createPdf();
        if (this.disposed) return;
        if (controller.signal.aborted) throw new Error('copy_timeout');
        if (!pdf || !pdf.size || pdf.size > 32 * 1024 * 1024 || await pdf.slice(0,5).text() !== '%PDF-') throw new Error('invalid_pdf');
        this.pending = {pdf, key: crypto.randomUUID(), name: `there-chat-${new Date().toISOString().replace(/[:.]/g, '-')}.pdf`};
      }
      if (this.disposed) return;
        if (controller.signal.aborted) throw new Error('copy_timeout');
      const p = this.pending;
      const response = await request('/api/v1/integrations/share/files', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin', redirect: 'error', signal: controller.signal,
        headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/pdf', 'Idempotency-Key': p.key, 'X-File-Name': encodeURIComponent(p.name)},
        body: p.pdf
      });
      if (!response.ok) throw new Error(await refusal(response));
      const receipt = await response.json();
      const fileId = typeof receipt.file_id === 'string' ? receipt.file_id : '';
      if (receipt.name !== p.name || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(fileId) || receipt.url !== 'https://buildstudio-share.com') throw new Error('invalid_receipt');
      if (!this.disposed) { this.fileId = fileId; this.saved = true; }
    } finally { clearTimeout(deadline); this.busy = false; }
  }
}
