// One explicit private copy, with stable bytes and request identity across retries.
export class PrivateShareTransfer {
  private pending: {pdf: Blob; name: string; key: string} | null = null;
  private controller: AbortController | null = null;
  private disposed = false;
  busy = false;
  saved = false;
  get name() { return this.pending?.name ?? ''; }
  get prepared() { return this.pending !== null; }
  dispose() { this.disposed = true; this.controller?.abort(); this.pending = null; }
  async send(token: string, createPdf: () => Promise<Blob | undefined>, request: typeof fetch = fetch) {
    if (this.busy || this.saved || this.disposed) return;
    if (!token.startsWith('bs1_')) throw new Error('central_session_required');
    this.busy = true;
    const controller = new AbortController(); this.controller = controller;
    const deadline = setTimeout(() => controller.abort(), 95000);
    try {
      if (!this.pending) {
        const pdf = await createPdf();
        if (this.disposed || controller.signal.aborted) return;
        if (!pdf || !pdf.size || pdf.size > 32 * 1024 * 1024 || await pdf.slice(0,5).text() !== '%PDF-') throw new Error('invalid_pdf');
        this.pending = {pdf, key: crypto.randomUUID(), name: `there-chat-${new Date().toISOString().replace(/[:.]/g, '-')}.pdf`};
      }
      if (this.disposed || controller.signal.aborted) return;
      const p = this.pending;
      const response = await request('/api/v1/integrations/share/files', {
        method: 'POST', cache: 'no-store', credentials: 'same-origin', redirect: 'error', signal: controller.signal,
        headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/pdf', 'Idempotency-Key': p.key, 'X-File-Name': encodeURIComponent(p.name)},
        body: p.pdf
      });
      if (!response.ok) throw new Error('copy_unconfirmed');
      const receipt = await response.json();
      if (receipt.name !== p.name || typeof receipt.file_id !== 'string' || receipt.url !== 'https://buildstudio-share.com') throw new Error('invalid_receipt');
      if (!this.disposed) this.saved = true;
    } finally { clearTimeout(deadline); this.busy = false; }
  }
}
