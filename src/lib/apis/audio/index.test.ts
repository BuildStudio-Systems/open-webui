import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('$lib/constants', () => ({ AUDIO_API_BASE_URL: '/api/v1/audio' }));
import { synthesizeOpenAISpeech, transcribeAudio } from './index';

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('abortable authenticated voice API calls', () => {
	it('sends real recording MIME and language through existing authenticated transcription route', async () => {
		const signal = new AbortController().signal;
		const fetcher = vi.fn(async () => Response.json({ text: 'synthetic transcript' }));
		vi.stubGlobal('fetch', fetcher);
		const file = new File(['synthetic media'], 'recording.mp4', { type: 'audio/mp4' });
		expect(await transcribeAudio('synthetic-token', file, 'ja', signal)).toEqual({
			text: 'synthetic transcript'
		});
		const [url, init] = fetcher.mock.calls[0] as any;
		expect(url).toBe('/api/v1/audio/transcriptions');
		expect(init.signal).toBe(signal);
		expect(init.headers.authorization).toBe('Bearer synthetic-token');
		expect(init.body.get('file').type).toBe('audio/mp4');
		expect(init.body.get('language')).toBe('ja');
	});
	it('passes cancellation through synthesis while retaining voice/model and token', async () => {
		const signal = new AbortController().signal;
		const fetcher = vi.fn(async () => new Response('synthetic audio'));
		vi.stubGlobal('fetch', fetcher);
		await synthesizeOpenAISpeech(
			'synthetic-token',
			'there-ja',
			'Synthetic speech',
			'kokoro',
			signal
		);
		const [url, init] = fetcher.mock.calls[0] as any;
		expect(url).toBe('/api/v1/audio/speech');
		expect(init.signal).toBe(signal);
		expect(init.headers.Authorization).toBe('Bearer synthetic-token');
		expect(JSON.parse(init.body)).toEqual({
			input: 'Synthetic speech',
			voice: 'there-ja',
			model: 'kokoro'
		});
	});
	it.each(['stt', 'tts'])(
		'propagates %s cancellation rather than returning null',
		async (route) => {
			const controller = new AbortController();
			controller.abort();
			vi.stubGlobal(
				'fetch',
				vi.fn(async () => {
					throw new DOMException('Cancelled', 'AbortError');
				})
			);
			const result =
				route === 'stt'
					? transcribeAudio('test', new File(['x'], 'test.wav'), undefined, controller.signal)
					: synthesizeOpenAISpeech('test', 'there-auto', 'test', undefined, controller.signal);
			await expect(result).rejects.toMatchObject({ name: 'AbortError' });
		}
	);
	it.each(['stt', 'tts'])(
		'surfaces %s network errors instead of losing their message',
		async (route) => {
			vi.spyOn(console, 'error').mockImplementation(() => {});
			vi.stubGlobal(
				'fetch',
				vi.fn(async () => {
					throw new TypeError('Synthetic network failure');
				})
			);
			const result =
				route === 'stt'
					? transcribeAudio('test', new File(['x'], 'test.wav'))
					: synthesizeOpenAISpeech('test', 'there-auto', 'test');
			await expect(result).rejects.toBe('Synthetic network failure');
		}
	);
});
