import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { isVoiceAbort, recordingBlob, recordingMimeType, voiceRequest } from './voice-session';

const deferred = <T>() => {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
};

afterEach(() => {
	vi.useRealTimers();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('voice request and recording boundaries', () => {
	it('uses supported MP4 on Safari-style recorders, preserving real blob MIME and extension', async () => {
		expect(recordingMimeType((type) => type === 'audio/mp4')).toBe('audio/mp4');
		const recording = recordingBlob([new Blob(['synthetic AAC'], { type: 'audio/mp4' })]);
		expect(recording.filename).toBe('recording.mp4');
		expect(recording.blob.type).toBe('audio/mp4');
		expect(await recording.blob.text()).toBe('synthetic AAC');
	});
	it('preserves WebM/Opus and uses recorder type when chunks have no type', () => {
		expect(recordingBlob([new Blob(['x'], { type: 'audio/webm;codecs=opus' })]).filename).toBe(
			'recording.webm'
		);
		expect(recordingBlob([new Blob(['x'])], 'audio/ogg;codecs=opus').blob.type).toBe(
			'audio/ogg;codecs=opus'
		);
		expect(recordingMimeType(() => false)).toBeUndefined();
	});
	it('does not start an operation for an already-ended call', async () => {
		const controller = new AbortController();
		controller.abort();
		const operation = vi.fn();
		await expect(voiceRequest([controller.signal], operation)).rejects.toMatchObject({
			name: 'AbortError'
		});
		expect(operation).not.toHaveBeenCalled();
	});
	it('cancels the request and settles even when a provider ignores abort', async () => {
		const controller = new AbortController();
		const late = deferred<string>();
		let requestSignal: AbortSignal;
		const pending = voiceRequest([controller.signal], async (signal) => {
			requestSignal = signal;
			return late.promise;
		});
		const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
		controller.abort();
		await assertion;
		expect(requestSignal!.aborted).toBe(true);
		late.resolve('must not revive call');
	});
	it('times out visibly and clears request timers on success', async () => {
		vi.useFakeTimers();
		const pending = voiceRequest([], () => new Promise(() => {}), 10);
		const assertion = expect(pending).rejects.toMatchObject({ name: 'TimeoutError' });
		await vi.advanceTimersByTimeAsync(10);
		await assertion;
		expect(await voiceRequest([], async () => 'done')).toBe('done');
		expect(vi.getTimerCount()).toBe(0);
		expect(isVoiceAbort(new DOMException('', 'AbortError'))).toBe(true);
	});
});

// Execute the actual component script with injected browser/service boundaries.
// This is not a rendered-browser test and never opens the real microphone.
const source = readFileSync('src/lib/components/chat/MessageInput/CallOverlay.svelte', 'utf8');
const script = source.split('<script lang="ts">')[1].split('</script>')[0];
const tree = ts.createSourceFile(
	'voice.ts',
	script,
	ts.ScriptTarget.ESNext,
	true,
	ts.ScriptKind.TS
);
const isolated = tree.statements
	.filter((node) => !ts.isImportDeclaration(node) && !ts.isLabeledStatement(node))
	.map((node) => {
		if (
			ts.isVariableStatement(node) &&
			node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)
		) {
			return node.declarationList.declarations
				.map(
					(decl) => 'let ' + decl.name.getText(tree) + ' = deps.' + decl.name.getText(tree) + ';'
				)
				.join('\n');
		}
		return node.getText(tree);
	})
	.join('\n');
const compiled = ts.transpileModule(isolated, {
	compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
const instantiate = new Function(
	'deps',
	`
const {getContext,createEventDispatcher,tick,onMount,onDestroy,toast,blobToFile,
transcribeAudio,synthesizeOpenAISpeech,generateEmoji,isVoiceAbort,recordingBlob,
recordingMimeType,voiceRequest,$models,$settings,$config,$TTSWorker,$i18n}=deps;
let $showCallOverlay=true;
const showCallOverlay={set(value){$showCallOverlay=value;}};
${compiled}
return {startRecording,transcribeHandler,endCall,chatStartHandler,chatEventHandler,chatFinishHandler,toggleMute,
startCamera,stopCamera,startVideoStream,stopVideoStream,visibilityHandler,
state:()=>({closed,assistantSpeaking,modelId,messages,audioStream,audioContext,muted,camera,cameraStream,mediaRecorder})};
`
);

function overlay(overrides: Record<string, unknown> = {}) {
	const track = { stop: vi.fn() };
	const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
	const contextClose = vi.fn(async () => {});
	const audioElement: any = {
		pause: vi.fn(),
		load: vi.fn(),
		removeAttribute: vi.fn(),
		play: vi.fn(async () => {
			queueMicrotask(() => audioElement.onended?.());
		})
	};
	const analyser = {
		disconnect: vi.fn(),
		frequencyBinCount: 16,
		fftSize: 32,
		getByteFrequencyData: vi.fn((_data: Uint8Array) => {}),
		getByteTimeDomainData: vi.fn((_data: Uint8Array) => {})
	};
	vi.stubGlobal(
		'AudioContext',
		class {
			createAnalyser() {
				return analyser;
			}
			createMediaStreamSource() {
				return { connect: vi.fn() };
			}
			resume = vi.fn(async () => {});
			close = contextClose;
		}
	);
	vi.stubGlobal(
		'MediaRecorder',
		class {
			static isTypeSupported = (type: string) => type === 'audio/mp4';
			state = 'inactive';
			mimeType = 'audio/mp4';
			onstart: any;
			onstop: any;
			ondataavailable: any;
			start() {
				this.state = 'recording';
				this.onstart?.();
			}
			stop() {
				this.state = 'inactive';
				this.ondataavailable?.({
					data: new Blob(['synthetic recording'.repeat(20)], { type: this.mimeType })
				});
				this.onstop?.();
			}
		}
	);
	vi.stubGlobal('navigator', {
		mediaDevices: {
			getUserMedia: vi.fn(async () => stream),
			enumerateDevices: vi.fn(async () => [])
		}
	});
	vi.stubGlobal('document', {
		getElementById: () => audioElement,
		addEventListener: vi.fn(),
		removeEventListener: vi.fn(),
		visibilityState: 'visible'
	});
	vi.stubGlobal(
		'requestAnimationFrame',
		vi.fn(() => 1)
	);
	vi.stubGlobal('cancelAnimationFrame', vi.fn());
	vi.stubGlobal('window', { requestAnimationFrame: globalThis.requestAnimationFrame });
	vi.stubGlobal('localStorage', { token: 'synthetic-voice-token', getItem: vi.fn() });
	vi.stubGlobal(
		'Audio',
		class {
			constructor(public src: string) {}
		}
	);
	vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic-voice');
	vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
	const deps: any = {
		getContext: () => ({ t: (value: string) => value }),
		createEventDispatcher: () => vi.fn(),
		$i18n: { t: (value: string) => value },
		tick: async () => {},
		onMount: vi.fn(),
		onDestroy: vi.fn(),
		toast: { error: vi.fn() },
		blobToFile: (blob: Blob, name: string) => new File([blob], name, { type: blob.type }),
		transcribeAudio: vi.fn(async () => ({ text: 'Synthetic Agent question' })),
		synthesizeOpenAISpeech: vi.fn(async () => ({ blob: async () => new Blob(['synthetic MP3']) })),
		generateEmoji: vi.fn(async () => null),
		isVoiceAbort,
		recordingBlob,
		recordingMimeType,
		voiceRequest,
		$models: [],
		$settings: { audio: { tts: {} } },
		$config: { audio: { tts: { engine: 'openai', voice: 'there-auto' } } },
		$TTSWorker: null,
		eventTarget: new EventTarget(),
		submitPrompt: vi.fn(async () => {}),
		stopResponse: vi.fn(),
		files: [],
		chatId: 'synthetic-chat',
		modelId: 'there-agent-3.8',
		...overrides
	};
	return { api: instantiate(deps), deps, track, contextClose, audioElement, stream, analyser };
}

describe('real CallOverlay script with synthetic browser/audio', () => {
	it('submits transcription to the existing Agent chat without changing selected model', async () => {
		const { api, deps } = overlay();
		await api.transcribeHandler(
			new Blob(['x'.repeat(200)], { type: 'audio/mp4' }),
			'recording.mp4'
		);
		expect(deps.submitPrompt).toHaveBeenCalledWith('Synthetic Agent question', { _raw: true });
		expect(api.state().modelId).toBe('there-agent-3.8');
		expect(deps.transcribeAudio.mock.calls[0][1].type).toBe('audio/mp4');
		api.endCall();
	});
	it('does not send late recognition after hangup, even if provider ignores abort', async () => {
		const late = deferred<any>();
		const { api, deps } = overlay({ transcribeAudio: vi.fn(() => late.promise) });
		const pending = api.transcribeHandler(new Blob(['x'.repeat(200)]), 'recording.webm');
		await Promise.resolve();
		await Promise.resolve();
		api.endCall();
		late.resolve({ text: 'Do not send' });
		await pending;
		expect(deps.submitPrompt).not.toHaveBeenCalled();
		expect(deps.transcribeAudio.mock.calls[0][3].aborted).toBe(true);
	});
	it('releases microphone and AudioContext on hangup and is idempotent', async () => {
		const { api, track, contextClose } = overlay();
		await api.startRecording();
		api.endCall();
		api.endCall();
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(contextClose).toHaveBeenCalledTimes(1);
		expect(api.state()).toMatchObject({ closed: true, audioStream: null, audioContext: null });
	});
	it('stops a microphone permission result arriving after call end', async () => {
		const { api, stream, track } = overlay();
		const permission = deferred<any>();
		vi.mocked(navigator.mediaDevices.getUserMedia).mockReturnValue(permission.promise);
		const opening = api.startRecording();
		api.endCall();
		permission.resolve(stream);
		await opening;
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(api.state().audioStream).toBeNull();
	});
	it('shows microphone permission failure and closes without submitting', async () => {
		const { api, deps } = overlay();
		vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(
			new DOMException('Denied', 'NotAllowedError')
		);
		await api.startRecording();
		expect(deps.toast.error).toHaveBeenCalled();
		expect(deps.submitPrompt).not.toHaveBeenCalled();
		expect(api.state().closed).toBe(true);
	});
	it('immediately stops and discards the partial recording on mute', async () => {
		const { api, deps, analyser } = overlay();
		analyser.getByteFrequencyData.mockImplementation((data: Uint8Array) => data.fill(10));
		await api.startRecording();
		const frame = vi.mocked(requestAnimationFrame).mock.calls.at(-1)![0];
		frame(0);
		const recorder = api.state().mediaRecorder;
		expect(recorder.state).toBe('recording');
		api.toggleMute();
		await Promise.resolve();
		expect(recorder.state).toBe('inactive');
		expect(api.state().muted).toBe(true);
		expect(deps.transcribeAudio).not.toHaveBeenCalled();
		expect(deps.submitPrompt).not.toHaveBeenCalled();
		api.endCall();
	});
	it('discards in-progress audio on backgrounding and does not auto-unmute on return', async () => {
		const { api, deps, analyser } = overlay();
		analyser.getByteFrequencyData.mockImplementation((data: Uint8Array) => data.fill(10));
		await api.startRecording();
		vi.mocked(requestAnimationFrame).mock.calls.at(-1)![0](0);
		const recorder = api.state().mediaRecorder;
		Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
		api.visibilityHandler();
		await Promise.resolve();
		expect(recorder.state).toBe('inactive');
		expect(deps.transcribeAudio).not.toHaveBeenCalled();
		Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
		api.visibilityHandler();
		expect(api.state().muted).toBe(true);
		api.endCall();
	});
	it('returns to listening after a chat failure finishes without any spoken segment', async () => {
		vi.useFakeTimers();
		const { api, deps } = overlay();
		api.chatStartHandler({ detail: { id: 'failed' } });
		api.chatFinishHandler({ detail: { id: 'failed' } });
		await vi.advanceTimersByTimeAsync(100);
		expect(api.state().assistantSpeaking).toBe(false);
		expect(deps.synthesizeOpenAISpeech).not.toHaveBeenCalled();
		api.endCall();
	});
	it('preserves real MP4 bytes and extension after voice/silence detection', async () => {
		vi.useFakeTimers();
		const { api, deps, analyser } = overlay();
		analyser.getByteFrequencyData.mockImplementation((data: Uint8Array) => data.fill(10));
		await api.startRecording();
		vi.mocked(requestAnimationFrame).mock.calls.at(-1)![0](0);
		analyser.getByteFrequencyData.mockImplementation((data: Uint8Array) => data.fill(0));
		await vi.advanceTimersByTimeAsync(2100);
		vi.mocked(requestAnimationFrame).mock.calls.at(-1)![0](2100);
		await vi.advanceTimersByTimeAsync(1);
		expect(deps.transcribeAudio).toHaveBeenCalledTimes(1);
		expect(deps.transcribeAudio.mock.calls[0][1]).toMatchObject({
			type: 'audio/mp4',
			name: 'recording.mp4'
		});
		expect(deps.submitPrompt).toHaveBeenCalledTimes(1);
		api.endCall();
	});
	it('stops a camera permission result arriving after camera is disabled', async () => {
		const { api, track, stream } = overlay();
		const permission = deferred<any>();
		vi.mocked(navigator.mediaDevices.getUserMedia).mockReturnValue(permission.promise);
		const opening = api.startCamera();
		await vi.waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1));
		await api.stopCamera();
		permission.resolve(stream);
		await opening;
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(api.state()).toMatchObject({ camera: false, cameraStream: null });
		api.endCall();
	});
	it('discards a stale camera-switch stream and closes the current one on hangup', async () => {
		const { api } = overlay();
		const first = deferred<any>();
		const second = deferred<any>();
		const firstTrack = { stop: vi.fn() };
		const secondTrack = { stop: vi.fn() };
		vi.mocked(navigator.mediaDevices.getUserMedia)
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise);
		const opening = api.startCamera();
		await vi.waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1));
		await api.stopVideoStream();
		const switching = api.startVideoStream();
		second.resolve({ getTracks: () => [secondTrack] });
		await switching;
		first.resolve({ getTracks: () => [firstTrack] });
		await opening;
		expect(firstTrack.stop).toHaveBeenCalledTimes(1);
		expect(secondTrack.stop).not.toHaveBeenCalled();
		api.endCall();
		expect(secondTrack.stop).toHaveBeenCalledTimes(1);
	});
	it('reports TTS failure and returns to listening instead of requeueing forever', async () => {
		vi.useFakeTimers();
		const { api, deps } = overlay({
			synthesizeOpenAISpeech: vi.fn(async () => {
				throw new Error('Synthetic TTS failure');
			})
		});
		api.chatStartHandler({ detail: { id: 'reply' } });
		api.chatEventHandler({ detail: { id: 'reply', content: 'Synthetic response.' } });
		api.chatFinishHandler({ detail: { id: 'reply' } });
		await vi.advanceTimersByTimeAsync(300);
		expect(deps.synthesizeOpenAISpeech).toHaveBeenCalledTimes(1);
		expect(deps.toast.error).toHaveBeenCalled();
		expect(api.state().assistantSpeaking).toBe(false);
		api.endCall();
	});
	it('aborts in-flight TTS body read and never plays its late audio', async () => {
		vi.useFakeTimers();
		const body = deferred<Blob>();
		const { api, deps, audioElement } = overlay({
			synthesizeOpenAISpeech: vi.fn(async () => ({ blob: () => body.promise }))
		});
		api.chatStartHandler({ detail: { id: 'reply' } });
		api.chatEventHandler({ detail: { id: 'reply', content: 'Synthetic response.' } });
		await vi.advanceTimersByTimeAsync(100);
		api.endCall();
		body.resolve(new Blob(['late synthetic audio']));
		await vi.advanceTimersByTimeAsync(200);
		expect(deps.synthesizeOpenAISpeech.mock.calls[0][4].aborted).toBe(true);
		expect(audioElement.play).not.toHaveBeenCalled();
		expect(URL.createObjectURL).not.toHaveBeenCalled();
	});
	it('plays synthetic streamed Agent reply, revokes audio URL and preserves chat generation', async () => {
		vi.useFakeTimers();
		const { api, deps, audioElement } = overlay();
		api.chatStartHandler({ detail: { id: 'reply' } });
		api.chatEventHandler({ detail: { id: 'reply', content: 'Synthetic response.' } });
		api.chatFinishHandler({ detail: { id: 'reply' } });
		await vi.advanceTimersByTimeAsync(300);
		expect(audioElement.play).toHaveBeenCalledTimes(1);
		expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:synthetic-voice');
		expect(deps.stopResponse).not.toHaveBeenCalled();
		expect(api.state().assistantSpeaking).toBe(false);
		api.endCall();
	});
	it('reports blocked playback, leaves user mute intact and recovers for the next reply', async () => {
		vi.useFakeTimers();
		const { api, deps, audioElement } = overlay();
		audioElement.play.mockRejectedValueOnce(new DOMException('Autoplay denied', 'NotAllowedError'));
		api.toggleMute();
		api.chatStartHandler({ detail: { id: 'blocked' } });
		api.chatEventHandler({ detail: { id: 'blocked', content: 'Synthetic response.' } });
		api.chatFinishHandler({ detail: { id: 'blocked' } });
		await vi.advanceTimersByTimeAsync(300);
		expect(deps.toast.error).toHaveBeenCalled();
		expect(api.state()).toMatchObject({ assistantSpeaking: false, muted: true });
		api.chatStartHandler({ detail: { id: 'retry' } });
		api.chatEventHandler({ detail: { id: 'retry', content: 'Synthetic next response.' } });
		api.chatFinishHandler({ detail: { id: 'retry' } });
		await vi.advanceTimersByTimeAsync(300);
		expect(audioElement.play).toHaveBeenCalledTimes(2);
		expect(api.state().assistantSpeaking).toBe(false);
		api.endCall();
	});
	it('does not let a late old play promise pause the next Agent reply', async () => {
		vi.useFakeTimers();
		const firstPlay = deferred<void>();
		const { api, audioElement } = overlay();
		audioElement.play
			.mockImplementationOnce(() => firstPlay.promise)
			.mockImplementationOnce(async () => {});
		api.chatStartHandler({ detail: { id: 'first' } });
		api.chatEventHandler({ detail: { id: 'first', content: 'First response.' } });
		await vi.advanceTimersByTimeAsync(100);
		api.chatStartHandler({ detail: { id: 'second' } });
		api.chatEventHandler({ detail: { id: 'second', content: 'Second response.' } });
		api.chatFinishHandler({ detail: { id: 'second' } });
		await vi.advanceTimersByTimeAsync(100);
		const pauses = audioElement.pause.mock.calls.length;
		firstPlay.resolve();
		await vi.advanceTimersByTimeAsync(100);
		expect(audioElement.play).toHaveBeenCalledTimes(2);
		expect(audioElement.pause).toHaveBeenCalledTimes(pauses);
		expect(api.state().assistantSpeaking).toBe(true);
		audioElement.onended();
		await vi.advanceTimersByTimeAsync(100);
		expect(api.state().assistantSpeaking).toBe(false);
		api.endCall();
	});
});
