/** Browser-independent voice helpers. No microphone or network access at import. */
export const recordingMimeType = (supported: (type: string) => boolean): string | undefined =>
	['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/wav'].find(
		supported
	);

export const recordingBlob = (chunks: Blob[], recorderType = '') => {
	const type = chunks.find((chunk) => chunk.type)?.type || recorderType || 'audio/webm';
	const extension = type.split(';')[0].split('/')[1] || 'webm';
	return { blob: new Blob(chunks, { type }), filename: `recording.${extension}` };
};

export const isVoiceAbort = (error: unknown) =>
	error instanceof Error && error.name === 'AbortError';

/** Abort both fetch and its body read; settle even if a provider ignores abort. */
export async function voiceRequest<T>(
	signals: AbortSignal[],
	operation: (signal: AbortSignal) => Promise<T>,
	timeoutMs = 60_000
): Promise<T> {
	const controller = new AbortController();
	const aborted = () => controller.abort(new DOMException('Voice request cancelled', 'AbortError'));
	for (const signal of signals) {
		if (signal.aborted) aborted();
		signal.addEventListener('abort', aborted, { once: true });
	}
	const timer = setTimeout(
		() => controller.abort(new DOMException('Voice request timed out', 'TimeoutError')),
		timeoutMs
	);
	let rejectAbort: () => void;
	const cancellation = new Promise<never>((_, reject) => {
		rejectAbort = () => reject(controller.signal.reason);
		if (controller.signal.aborted) rejectAbort();
		else controller.signal.addEventListener('abort', rejectAbort, { once: true });
	});
	try {
		if (controller.signal.aborted) return await cancellation;
		return await Promise.race([operation(controller.signal), cancellation]);
	} finally {
		clearTimeout(timer);
		for (const signal of signals) signal.removeEventListener('abort', aborted);
		controller.signal.removeEventListener('abort', rejectAbort!);
	}
}
