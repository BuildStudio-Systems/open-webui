// Explicit browser export. No cookies, bearer tokens, URLs or executable prompts cross origins.
export const SHARE_ORIGIN = 'https://buildstudio-share.com';
export const THERE_ORIGIN = 'https://buildstudio-there.com';
export const MAX_PDF = 32 * 1024 * 1024;
const protocol = 'buildstudio.private-pdf.v1';
const channelPattern = /^[a-f0-9]{32}$/;
export function validMetadata(m) {
  return !!m && Object.keys(m).sort().join(',') === 'name,size,type' &&
    typeof m.name === 'string' && [...m.name].length <= 240 && m.name.trim() === m.name && !!m.name &&
    !/[\x00-\x1f\x7f/\\]/.test(m.name) && m.type === 'application/pdf' &&
    Number.isSafeInteger(m.size) && m.size >= 5 && m.size <= MAX_PDF;
}
export async function readBoundedPdf(response, signal) {
  if (!response.ok || !/^application\/pdf(?:;|$)/i.test(response.headers.get('content-type') || '') || !response.body) throw new Error('pdf_unavailable');
  const reader = response.body.getReader(), chunks = []; let size = 0;
  try {
    while (true) {
      if (signal.aborted) throw new Error('cancelled');
      const r = await reader.read(); if (r.done) break;
      size += r.value.byteLength; if (size > MAX_PDF) throw new Error('pdf_limit'); chunks.push(r.value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    if (new TextDecoder().decode(bytes.subarray(0, 5)) !== '%PDF-') throw new Error('invalid_pdf');
    return bytes.buffer;
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
function base(win, peer, origin, channel, onState, ttl) {
  if (!peer || !channelPattern.test(channel)) throw new Error('invalid_handoff');
  let active = true;
  const deadline = Date.now() + ttl;
  const current = () => active && Date.now() < deadline && !peer.closed;
  const send = data => { if (current()) peer.postMessage({ protocol, channel, ...data }, origin); };
  let listener = () => {};
  const stop = state => { if (!active) return; active = false; clearTimeout(timer); win.removeEventListener('message', listener); onState(state); };
  const timer = setTimeout(() => stop('expired'), ttl);
  return { current, send, stop, listen(fn) { listener = event => {
    if (current() && event.origin === origin && event.source === peer && event.data?.protocol === protocol && event.data.channel === channel) void fn(event.data);
  }; win.addEventListener('message', listener); } };
}
export function createPdfSender({win, peer, channel, metadata, load, onState=()=>{}, ttl=300000}) {
  if (!validMetadata(metadata)) throw new Error('invalid_pdf');
  let admitted = false, delivered = false; const abort = new AbortController();
  const b = base(win, peer, THERE_ORIGIN, channel, state => { abort.abort(); clearInterval(offers); onState(state); }, ttl);
  const offer = () => { if (!admitted) b.send({kind:'offer',metadata}); };
  const offers = setInterval(offer, 1000);
  b.listen(async m => {
    if (m.kind === 'cancel') return b.stop('cancelled');
    if (m.kind === 'failed') return b.stop('failed');
    if (m.kind === 'complete' && delivered) return b.stop('complete');
    if (m.kind !== 'accept' || admitted) return;
    admitted = true; clearInterval(offers); onState('transferring');
    try {
      const bytes = await load(abort.signal);
      if (!b.current()) return;
      if (!(bytes instanceof ArrayBuffer) || bytes.byteLength !== metadata.size) throw new Error('changed_pdf');
      delivered = true; b.send({kind:'pdf',metadata,bytes}); onState('delivered');
    } catch { if (b.current()) { b.send({kind:'failed'}); b.stop('failed'); } }
  });
  offer(); onState('waiting');
  return { cancel() { b.send({kind:'cancel'}); b.stop('cancelled'); } };
}
export function createPdfReceiver({win, peer, channel, onOffer, receive, onState=()=>{}, ttl=300000}) {
  let metadata = null, accepted = false, processing = false;
  const b = base(win, peer, SHARE_ORIGIN, channel, onState, ttl);
  b.listen(async m => {
    if (m.kind === 'cancel' || m.kind === 'failed') return b.stop(m.kind === 'cancel' ? 'cancelled' : 'failed');
    if (m.kind === 'offer' && !metadata && validMetadata(m.metadata)) { metadata = {...m.metadata}; onOffer(metadata); return; }
    if (m.kind !== 'pdf' || !accepted || processing || !metadata) return;
    processing = true;
    try {
      if (!validMetadata(m.metadata) || m.metadata.name !== metadata.name || m.metadata.size !== metadata.size ||
          !(m.bytes instanceof ArrayBuffer) || m.bytes.byteLength !== metadata.size ||
          new TextDecoder().decode(new Uint8Array(m.bytes,0,5)) !== '%PDF-') throw new Error('invalid_pdf');
      onState('processing'); await receive(new win.File([m.bytes], metadata.name, {type:'application/pdf'}), b.current);
      if (b.current()) { b.send({kind:'complete'}); b.stop('complete'); }
    } catch { if (b.current()) { b.send({kind:'failed'}); b.stop('failed'); } }
  });
  return {
    accept() { if (b.current() && metadata && !accepted) { accepted=true; onState('receiving'); b.send({kind:'accept'}); } },
    cancel() { b.send({kind:'cancel'}); b.stop('cancelled'); }
  };
}
