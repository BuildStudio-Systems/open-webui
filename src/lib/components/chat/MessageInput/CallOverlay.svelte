<script lang="ts">
	import { config, models, settings, showCallOverlay, TTSWorker } from '$lib/stores';
	import { onMount, tick, getContext, onDestroy, createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher();

	import { blobToFile } from '$lib/utils';
	import { generateEmoji } from '$lib/apis';
	import { synthesizeOpenAISpeech, transcribeAudio } from '$lib/apis/audio';

	import { toast } from 'svelte-sonner';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import VideoInputMenu from './CallOverlay/VideoInputMenu.svelte';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { CALL_SILENCE_MS, speechLanguage } from '$lib/utils/voice-stream';
	import {
		isVoiceAbort,
		recordingBlob,
		recordingMimeType,
		voiceRequest
	} from '$lib/utils/voice-session';

	const i18n = getContext('i18n');

	export let eventTarget: EventTarget;
	export let submitPrompt: Function;
	export let stopResponse: Function;
	export let files;
	export let chatId;
	export let modelId;

	let wakeLock: WakeLockSentinel | null = null;

	let model = null;

	let loading = false;
	let confirmed = false;
	let interrupted = false;
	let assistantSpeaking = false;
	let muted = false;

	let emoji = null;
	let camera = false;
	let cameraStream: MediaStream | null = null;
	let cameraGeneration = 0;

	let chatStreaming = false;
	let rmsLevel = 0;
	let hasStartedSpeaking = false;
	let mediaRecorder: MediaRecorder | null = null;
	let audioStream: MediaStream | null = null;
	let audioChunks: Blob[] = [];
	let closed = false;
	const callAbortController = new AbortController();
	let audioContext: AudioContext | null = null;
	let audioAnalyser: AnalyserNode | null = null;
	let analysisFrame = 0;
	const callActive = () => !closed && $showCallOverlay;
	const showVoiceError = (error: unknown) => {
		if (callActive() && !isVoiceAbort(error)) toast.error(`${error}`);
	};

	let videoInputDevices: { deviceId: string; label: string }[] = [];
	let selectedVideoInputDeviceId: string | null = null;

	const getVideoInputDevices = async () => {
		const devices = await navigator.mediaDevices.enumerateDevices();
		videoInputDevices = devices.filter((device) => device.kind === 'videoinput');

		if (!!navigator.mediaDevices.getDisplayMedia) {
			videoInputDevices = [
				...videoInputDevices,
				{
					deviceId: 'screen',
					label: 'Screen Share'
				}
			];
		}

		if (selectedVideoInputDeviceId === null && videoInputDevices.length > 0) {
			const savedDeviceId = localStorage.getItem('selectedVideoInputDeviceId');
			if (savedDeviceId && videoInputDevices.some((d) => d.deviceId === savedDeviceId)) {
				selectedVideoInputDeviceId = savedDeviceId;
			} else {
				selectedVideoInputDeviceId = videoInputDevices[0].deviceId;
			}
		}
	};

	const startCamera = async () => {
		if (!callActive() || camera) return;
		camera = true;
		const generation = ++cameraGeneration;
		try {
			await getVideoInputDevices();
			await tick();
			if (!callActive() || !camera || generation !== cameraGeneration) return;
			await startVideoStream();
		} catch (error) {
			if (generation === cameraGeneration) {
				showVoiceError(error);
				await stopCamera();
			}
		}
	};

	const startVideoStream = async () => {
		const video = document.getElementById('camera-feed') as HTMLVideoElement | null;
		if (!video || !camera || !callActive()) return;
		const generation = ++cameraGeneration;
		let grantedStream: MediaStream | null = null;
		try {
			if (selectedVideoInputDeviceId === 'screen') {
				grantedStream = await navigator.mediaDevices.getDisplayMedia({
					video: true,
					audio: false
				});
			} else {
				grantedStream = await navigator.mediaDevices.getUserMedia({
					video: {
						deviceId: selectedVideoInputDeviceId ? { exact: selectedVideoInputDeviceId } : undefined
					}
				});
			}

			if (!callActive() || !camera || generation !== cameraGeneration) {
				grantedStream.getTracks().forEach((track) => track.stop());
				return;
			}
			cameraStream?.getTracks().forEach((track) => track.stop());
			cameraStream = grantedStream;
			video.srcObject = grantedStream;
			await video.play();
		} catch (error) {
			grantedStream?.getTracks().forEach((track) => track.stop());
			if (generation === cameraGeneration) {
				cameraStream = null;
				camera = false;
				showVoiceError(error);
			}
		}
	};

	const stopVideoStream = async () => {
		cameraGeneration += 1;
		if (cameraStream) {
			const tracks = cameraStream.getTracks();
			tracks.forEach((track) => track.stop());
		}

		cameraStream = null;
	};

	const takeScreenshot = () => {
		const video = document.getElementById('camera-feed');
		const canvas = document.getElementById('camera-canvas');

		if (!canvas) {
			return;
		}

		const context = canvas.getContext('2d');

		// Make the canvas match the video dimensions
		canvas.width = video.videoWidth;
		canvas.height = video.videoHeight;

		// Draw the image from the video onto the canvas
		context.drawImage(video, 0, 0, video.videoWidth, video.videoHeight);

		// Keep captured image content out of developer-console logs.
		const dataURL = canvas.toDataURL('image/png');

		return dataURL;
	};

	const stopCamera = async () => {
		camera = false;
		await stopVideoStream();
	};

	const MIN_DECIBELS = -55;

	const transcribeHandler = async (audioBlob: Blob, filename: string) => {
		// Create a blob from the audio chunks
		if (!audioBlob || audioBlob.size < 100) {
			return;
		}

		await tick();
		if (!callActive()) return;
		const file = blobToFile(audioBlob, filename);
		try {
			const res = await voiceRequest(
				[callAbortController.signal],
				// Conversation language can change every turn, independently of UI/dictation settings.
				(signal) => transcribeAudio(localStorage.token, file, undefined, signal),
				90_000
			);
			// A provider can finish after the user ends a call. Never submit late text.
			if (callActive() && typeof res?.text === 'string' && res.text.trim()) {
				loading = false;
				void Promise.resolve(submitPrompt(res.text, { _raw: true })).catch(showVoiceError);
			}
		} catch (error) {
			showVoiceError(error);
		}
	};

	const stopRecordingCallback = async (_continue = true) => {
		if (callActive()) {
			// deep copy the audioChunks array
			const _audioChunks = audioChunks.slice(0);
			const recorderType = mediaRecorder?.mimeType ?? '';
			const shouldTranscribe = confirmed;
			confirmed = false;

			audioChunks = [];
			mediaRecorder = null;

			if (shouldTranscribe) {
				loading = true;
				emoji = null;

				if (cameraStream) {
					const imageUrl = takeScreenshot();

					files = [
						{
							type: 'image',
							url: imageUrl
						}
					];
				}

				const { blob, filename } = recordingBlob(_audioChunks, recorderType);
				await transcribeHandler(blob, filename);
				loading = false;
			}
			if (_continue && callActive()) void startRecording();
		} else {
			audioChunks = [];
			mediaRecorder = null;

			if (audioStream) {
				const tracks = audioStream.getTracks();
				tracks.forEach((track) => track.stop());
			}
			audioStream = null;
		}
	};

	const startRecording = async () => {
		if (callActive()) {
			try {
				if (!audioStream) {
					const grantedStream = await navigator.mediaDevices.getUserMedia({
						audio: {
							echoCancellation: true,
							noiseSuppression: true,
							autoGainControl: true
						}
					});
					if (!callActive()) {
						grantedStream.getTracks().forEach((track) => track.stop());
						return;
					}
					audioStream = grantedStream;
				}

				const mimeType = recordingMimeType((type) => MediaRecorder.isTypeSupported(type));
				mediaRecorder = new MediaRecorder(audioStream, mimeType ? { mimeType } : undefined);
				const recorder = mediaRecorder;

				recorder.onstart = () => {
					audioChunks = [];
				};

				recorder.ondataavailable = (event) => {
					if (callActive() && mediaRecorder === recorder && hasStartedSpeaking) {
						audioChunks.push(event.data);
					}
				};

				recorder.onstop = () => {
					if (mediaRecorder === recorder) void stopRecordingCallback();
				};

				analyseAudio(audioStream);
			} catch (error) {
				showVoiceError(error);
				endCall();
			}
		}
	};

	const stopAudioStream = async () => {
		const recorder = mediaRecorder;
		mediaRecorder = null;
		confirmed = false;
		audioChunks = [];
		cancelAnimationFrame(analysisFrame);
		analysisFrame = 0;
		audioAnalyser?.disconnect();
		audioAnalyser = null;
		if (audioContext) {
			void audioContext.close().catch(() => {});
			audioContext = null;
		}
		try {
			if (recorder && recorder.state !== 'inactive') {
				recorder.onstop = null;
				recorder.ondataavailable = null;
				recorder.stop();
			}
		} catch (error) {
			console.log('Error stopping audio stream:', error);
		}

		if (!audioStream) return;

		audioStream.getAudioTracks().forEach(function (track) {
			track.stop();
		});

		audioStream = null;
	};

	// Function to calculate the RMS level from time domain data
	const calculateRMS = (data: Uint8Array) => {
		let sumSquares = 0;
		for (let i = 0; i < data.length; i++) {
			const normalizedValue = (data[i] - 128) / 128; // Normalize the data
			sumSquares += normalizedValue * normalizedValue;
		}
		return Math.sqrt(sumSquares / data.length);
	};

	const analyseAudio = (stream: MediaStream) => {
		cancelAnimationFrame(analysisFrame);
		if (!audioContext) {
			audioContext = new AudioContext();
			audioAnalyser = audioContext.createAnalyser();
			audioContext.createMediaStreamSource(stream).connect(audioAnalyser);
		}
		void audioContext.resume().catch(showVoiceError);
		const analyser = audioAnalyser;
		if (!analyser) return;
		analyser.minDecibels = MIN_DECIBELS;
		const recorder = mediaRecorder;

		const bufferLength = analyser.frequencyBinCount;

		const domainData = new Uint8Array(bufferLength);
		const timeDomainData = new Uint8Array(analyser.fftSize);

		let lastSoundTime = Date.now();
		let speechStartedAt = 0;
		hasStartedSpeaking = false;

		const detectSound = () => {
			const processFrame = () => {
				if (!mediaRecorder || mediaRecorder !== recorder || !callActive()) {
					return;
				}

				if (muted || (assistantSpeaking && !($settings?.voiceInterruption ?? false))) {
					// Suppress mic input when muted or when assistant is speaking without interruption enabled
					analyser.maxDecibels = 0;
					analyser.minDecibels = -1;
				} else {
					analyser.minDecibels = MIN_DECIBELS;
					analyser.maxDecibels = -30;
				}

				analyser.getByteTimeDomainData(timeDomainData);
				analyser.getByteFrequencyData(domainData);

				// Calculate RMS level from time domain data
				rmsLevel = calculateRMS(timeDomainData);

				if (muted || (assistantSpeaking && !($settings?.voiceInterruption ?? false))) {
					rmsLevel = 0;
				}

				// Check if initial speech/noise has started
				const hasSound =
					!muted &&
					!loading &&
					(!assistantSpeaking || ($settings?.voiceInterruption ?? false)) &&
					domainData.some((value) => value > 0);
				if (hasSound) {
					if (mediaRecorder && mediaRecorder.state !== 'recording') {
						mediaRecorder.start();
					}

					if (!hasStartedSpeaking) {
						hasStartedSpeaking = true;
						speechStartedAt = Date.now();
						stopAllAudio();
					}

					lastSoundTime = Date.now();
				}

				// Start silence detection only after initial speech/noise has been detected
				if (hasStartedSpeaking) {
					if (
						Date.now() - lastSoundTime > CALL_SILENCE_MS ||
						Date.now() - speechStartedAt > 60_000
					) {
						confirmed = true;

						if (mediaRecorder) {
							mediaRecorder.stop();
							return;
						}
					}
				}

				analysisFrame = window.requestAnimationFrame(processFrame);
			};

			analysisFrame = window.requestAnimationFrame(processFrame);
		};

		detectSound();
	};

	let finishedMessages: Record<string, boolean> = {};
	let currentMessageId: string | null = null;
	let currentUtterance: SpeechSynthesisUtterance | null = null;

	// Get voice: model-specific > user settings > config default
	const getVoiceId = () => {
		// Check for model-specific TTS voice first
		if (model?.info?.meta?.tts?.voice) {
			return model.info.meta.tts.voice;
		}
		// Fall back to user settings or config default
		if ($settings?.audio?.tts?.defaultVoice === $config.audio.tts.voice) {
			return $settings?.audio?.tts?.voice ?? $config?.audio?.tts?.voice;
		}
		return $config?.audio?.tts?.voice;
	};

	let audioAbortController = new AbortController();
	const audioCache = new Map<string, HTMLAudioElement | true | null>();
	const pendingAudio = new Map<string, Promise<HTMLAudioElement | true | null>>();
	const emojiCache = new Map();
	let messages: Record<string, string[]> = {};
	let finishPlayback: (() => void) | null = null;
	// Streamed parts, finish and hangup wake the playback loop at once; its timer is only a
	// safety net. playbackSignal marks the reply whose segment is currently audible.
	let wakePlaybackLoop: (() => void) | null = null;
	let playbackSignal: AbortSignal | null = null;
	const notifyPlaybackLoop = () => {
		const wake = wakePlaybackLoop;
		wakePlaybackLoop = null;
		wake?.();
	};
	const waitForPlaybackWork = () =>
		new Promise<void>((resolve) => {
			const wake = () => {
				clearTimeout(timer);
				if (wakePlaybackLoop === wake) wakePlaybackLoop = null;
				resolve();
			};
			const timer = setTimeout(wake, 100);
			wakePlaybackLoop = wake;
		});

	const clearAudioCache = () => {
		for (const audio of audioCache.values()) {
			if (audio && audio !== true && audio.src.startsWith('blob:')) URL.revokeObjectURL(audio.src);
		}
		audioCache.clear();
		pendingAudio.clear();
		emojiCache.clear();
	};

	const stopAllAudio = (stopGeneration = true) => {
		audioAbortController.abort();
		assistantSpeaking = false;
		interrupted = true;
		if (stopGeneration && chatStreaming) {
			void Promise.resolve(stopResponse()).catch(showVoiceError);
			chatStreaming = false;
		}
		if (currentUtterance) {
			speechSynthesis.cancel();
			currentUtterance = null;
		}
		finishPlayback?.();
		finishPlayback = null;
		const element = document.getElementById('audioElement') as HTMLAudioElement;
		if (element) {
			element.muted = true;
			element.pause();
			element.removeAttribute('src');
			element.load();
		}
		currentMessageId = null;
		messages = {};
		finishedMessages = {};
		clearAudioCache();
		notifyPlaybackLoop();
	};

	const playAudio = (audio: HTMLAudioElement | true, content: string, signal: AbortSignal) => {
		if (!callActive() || signal.aborted) return Promise.resolve();
		return new Promise<void>((resolve, reject) => {
			let settled = false;
			const element = document.getElementById('audioElement') as HTMLAudioElement;
			let utterance: SpeechSynthesisUtterance | null = null;
			const finish = (error?: unknown) => {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				signal.removeEventListener('abort', cancel);
				if (element) {
					element.onended = null;
					element.onerror = null;
					element.onpause = null;
				}
				if (utterance) {
					utterance.onend = null;
					utterance.onerror = null;
				}
				if (currentUtterance === utterance) currentUtterance = null;
				finishPlayback = null;
				if (error) reject(error);
				else resolve();
			};
			const cancel = () => {
				// A late play() resolution from an old reply must not pause the next reply.
				if (settled) return;
				if (utterance) speechSynthesis.cancel();
				else element?.pause();
				finish();
			};
			const timer = setTimeout(() => {
				finish(new Error($i18n.t('Speech playback timed out')));
				if (utterance) speechSynthesis.cancel();
				else element?.pause();
			}, 120_000);
			finishPlayback = () => finish();
			signal.addEventListener('abort', cancel, { once: true });
			try {
				if (audio === true) {
					utterance = new SpeechSynthesisUtterance(content);
					currentUtterance = utterance;
					utterance.rate = $settings.audio?.tts?.playbackRate ?? 1;
					const voices = speechSynthesis.getVoices();
					const explicitVoice = voices.find((voice) => voice.voiceURI === getVoiceId());
					const language = explicitVoice?.lang || speechLanguage(content);
					if (language) utterance.lang = language;
					const voice =
						explicitVoice ||
						voices.find(
							(voice) => language && voice.lang.toLowerCase().startsWith(language.split('-')[0])
						);
					if (voice) utterance.voice = voice;
					utterance.onend = () => finish();
					utterance.onerror = () => finish(new Error($i18n.t('Speech playback failed')));
					// Empty getVoices() is allowed: use the browser default instead of polling forever.
					speechSynthesis.speak(utterance);
				} else if (element) {
					element.src = audio.src;
					element.muted = false;
					element.playbackRate = $settings.audio?.tts?.playbackRate ?? 1;
					element.onended = () => finish();
					element.onerror = () => finish(new Error($i18n.t('Speech playback failed')));
					element.onpause = () => finish();
					void element
						.play()
						.then(() => {
							if (!callActive() || signal.aborted) cancel();
						})
						.catch((error) => finish(error));
				} else finish(new Error($i18n.t('Speech playback failed')));
			} catch (error) {
				finish(error);
			}
		});
	};

	const fetchAudio = async (content: string, signal: AbortSignal) => {
		if (!callActive() || signal.aborted) return null;
		if (audioCache.has(content)) return audioCache.get(content);
		if (pendingAudio.has(content)) return pendingAudio.get(content);
		const pending = (async () => {
			try {
				if ($settings?.showEmojiInCall ?? false) {
					// Decoration must not delay speech synthesis.
					void generateEmoji(localStorage.token, modelId, content, chatId)
						.then((emoji) => {
							if (emoji && callActive() && !signal.aborted) emojiCache.set(content, emoji);
						})
						.catch(() => {});
				}
				if ($settings.audio?.tts?.engine === 'browser-kokoro') {
					const url = await voiceRequest(
						[callAbortController.signal, signal],
						async (requestSignal) => {
							const generated = await $TTSWorker.generate({ text: content, voice: getVoiceId() });
							if (requestSignal.aborted || !callActive()) {
								if (generated) URL.revokeObjectURL(generated);
								throw new DOMException('Voice request cancelled', 'AbortError');
							}
							return generated;
						}
					);
					if (url && callActive() && !signal.aborted) audioCache.set(content, new Audio(url));
					else if (url) URL.revokeObjectURL(url);
				} else if ($config.audio.tts.engine !== '') {
					const blob = await voiceRequest(
						[callAbortController.signal, signal],
						async (requestSignal) => {
							const response = await synthesizeOpenAISpeech(
								localStorage.token,
								getVoiceId(),
								content,
								undefined,
								requestSignal
							);
							if (!response) throw new Error($i18n.t('Speech playback failed'));
							return response.blob();
						}
					);
					if (callActive() && !signal.aborted)
						audioCache.set(content, new Audio(URL.createObjectURL(blob)));
				} else audioCache.set(content, true);
			} catch (error) {
				showVoiceError(error);
				if (callActive() && !signal.aborted) audioCache.set(content, null);
			}
			return audioCache.get(content) ?? null;
		})();
		pendingAudio.set(content, pending);
		try {
			return await pending;
		} finally {
			if (pendingAudio.get(content) === pending) pendingAudio.delete(content);
		}
	};

	const monitorAndPlayAudio = async (id: string, signal: AbortSignal) => {
		try {
			while (callActive() && !signal.aborted && currentMessageId === id) {
				if (messages[id]?.length) {
					const content = messages[id].shift();
					if (content === undefined) continue;
					const audio = await fetchAudio(content, signal);
					if (!callActive() || signal.aborted || currentMessageId !== id) break;
					emoji = emojiCache.get(content) ?? null;
					// At most one look-ahead synthesis; never fan out every streamed sentence.
					if (messages[id]?.length) void fetchAudio(messages[id][0], signal);
					if (audio) {
						playbackSignal = signal;
						try {
							await playAudio(audio, content, signal);
						} catch (error) {
							showVoiceError(error);
						} finally {
							if (playbackSignal === signal) playbackSignal = null;
						}
						if (audio !== true && audio.src.startsWith('blob:')) URL.revokeObjectURL(audio.src);
					}
					// A failed segment is skipped after a visible error, not requeued forever.
					audioCache.delete(content);
				} else if (finishedMessages[id]) break;
				else await waitForPlaybackWork();
			}
		} finally {
			if (currentMessageId === id) assistantSpeaking = false;
		}
	};

	const chatStartHandler = (event: Event) => {
		if (!callActive()) return;
		const { id } = (event as CustomEvent<{ id: string }>).detail;
		if (currentMessageId !== id) {
			stopAllAudio(false);
			currentMessageId = id;
			audioAbortController = new AbortController();
			assistantSpeaking = true;
			void monitorAndPlayAudio(id, audioAbortController.signal).catch(showVoiceError);
		}
		chatStreaming = true;
	};

	const chatEventHandler = (event: Event) => {
		const { id, content } = (event as CustomEvent<{ id: string; content: string }>).detail;
		if (callActive() && currentMessageId === id && typeof content === 'string' && content.trim()) {
			messages[id] ??= [];
			messages[id].push(content);
			// Synthesize the next segment while the current one is audible, so it is ready when
			// playback ends. Still a single look-ahead: only the head of the queue is fetched.
			const signal = audioAbortController.signal;
			if (messages[id].length === 1 && playbackSignal === signal && !signal.aborted)
				void fetchAudio(content, signal);
			notifyPlaybackLoop();
		}
	};

	const chatFinishHandler = (event: Event) => {
		const { id } = (event as CustomEvent<{ id: string }>).detail;
		if (callActive() && currentMessageId === id) {
			finishedMessages[id] = true;
			chatStreaming = false;
			notifyPlaybackLoop();
		}
	};

	const toggleMute = () => {
		muted = !muted;
		if (muted && hasStartedSpeaking) {
			// Abort the ongoing recording so it doesn't accidentally send a partial sentence
			hasStartedSpeaking = false;
			confirmed = false;
			audioChunks = [];
			if (mediaRecorder && mediaRecorder.state === 'recording') {
				mediaRecorder.stop();
			}
		}
	};

	// Only the user toggles microphone mute; a completed AI reply must not undo it.

	const handleKeydown = (e: KeyboardEvent) => {
		// Only handle M key when not typing in an input/textarea
		if (e.key === 'm' || e.key === 'M') {
			const target = e.target as HTMLElement;
			if (
				target.tagName !== 'INPUT' &&
				target.tagName !== 'TEXTAREA' &&
				!target.isContentEditable
			) {
				e.preventDefault();
				toggleMute();
			}
		}
	};

	const requestWakeLock = async () => {
		if (!callActive() || !('wakeLock' in navigator)) return;
		try {
			const lock = await navigator.wakeLock.request('screen');
			if (!callActive()) await lock.release();
			else wakeLock = lock;
		} catch {
			// A wake lock is optional (battery saver/background tabs can deny it).
		}
	};
	const visibilityHandler = () => {
		// Mobile browsers pause animation frames in background tabs. Discard any
		// partial recording instead of relying on the foreground silence timer.
		if (document.visibilityState === 'hidden' && callActive() && !muted) toggleMute();
		if (document.visibilityState === 'visible' && callActive()) void requestWakeLock();
	};

	const disposeCall = () => {
		if (closed) return;
		closed = true;
		callAbortController.abort();
		stopAllAudio();
		void stopAudioStream();
		void stopCamera();
		if (wakeLock) {
			void wakeLock.release().catch(() => {});
			wakeLock = null;
		}
		eventTarget.removeEventListener('chat:start', chatStartHandler);
		eventTarget.removeEventListener('chat', chatEventHandler);
		eventTarget.removeEventListener('chat:finish', chatFinishHandler);
		document.removeEventListener('keydown', handleKeydown);
		document.removeEventListener('visibilitychange', visibilityHandler);
	};
	const endCall = () => {
		disposeCall();
		showCallOverlay.set(false);
		dispatch('close');
	};

	onMount(() => {
		model = $models.find((candidate) => candidate.id === modelId);
		eventTarget.addEventListener('chat:start', chatStartHandler);
		eventTarget.addEventListener('chat', chatEventHandler);
		eventTarget.addEventListener('chat:finish', chatFinishHandler);
		document.addEventListener('keydown', handleKeydown);
		document.addEventListener('visibilitychange', visibilityHandler);
		void requestWakeLock();
		void startRecording();
		return disposeCall;
	});
	onDestroy(disposeCall);
</script>

{#if $showCallOverlay}
	<div class="max-w-lg w-full h-full max-h-[100dvh] flex flex-col justify-between p-3 md:p-6">
		{#if camera}
			<button
				type="button"
				class="flex justify-center items-center w-full h-20 min-h-20"
				on:click={() => {
					if (assistantSpeaking) {
						stopAllAudio();
					}
				}}
			>
				{#if emoji}
					<div
						class="  transition-all rounded-full"
						style="font-size:{rmsLevel * 100 > 4
							? '4.5'
							: rmsLevel * 100 > 2
								? '4.25'
								: rmsLevel * 100 > 1
									? '3.75'
									: '3.5'}rem;width: 100%; text-align:center;"
					>
						{emoji}
					</div>
				{:else if loading || assistantSpeaking}
					<svg
						class="size-12 text-gray-900 dark:text-gray-400"
						viewBox="0 0 24 24"
						fill="currentColor"
						xmlns="http://www.w3.org/2000/svg"
						><style>
							.spinner_qM83 {
								animation: spinner_8HQG 1.05s infinite;
							}
							.spinner_oXPr {
								animation-delay: 0.1s;
							}
							.spinner_ZTLf {
								animation-delay: 0.2s;
							}
							@keyframes spinner_8HQG {
								0%,
								57.14% {
									animation-timing-function: cubic-bezier(0.33, 0.66, 0.66, 1);
									transform: translate(0);
								}
								28.57% {
									animation-timing-function: cubic-bezier(0.33, 0, 0.66, 0.33);
									transform: translateY(-6px);
								}
								100% {
									transform: translate(0);
								}
							}
						</style><circle class="spinner_qM83" cx="4" cy="12" r="3" /><circle
							class="spinner_qM83 spinner_oXPr"
							cx="12"
							cy="12"
							r="3"
						/><circle class="spinner_qM83 spinner_ZTLf" cx="20" cy="12" r="3" /></svg
					>
				{:else}
					<div
						class=" {rmsLevel * 100 > 4
							? ' size-[4.5rem]'
							: rmsLevel * 100 > 2
								? ' size-16'
								: rmsLevel * 100 > 1
									? 'size-14'
									: 'size-12'}  transition-all rounded-full bg-cover bg-center bg-no-repeat"
						style={`background-image: url('${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&voice=true');`}
					/>
				{/if}
				<!-- navbar -->
			</button>
		{/if}

		<div class="flex justify-center items-center flex-1 h-full w-full max-h-full">
			{#if !camera}
				<button
					type="button"
					on:click={() => {
						if (assistantSpeaking) {
							stopAllAudio();
						}
					}}
				>
					{#if emoji}
						<div
							class="  transition-all rounded-full"
							style="font-size:{rmsLevel * 100 > 4
								? '13'
								: rmsLevel * 100 > 2
									? '12'
									: rmsLevel * 100 > 1
										? '11.5'
										: '11'}rem;width:100%;text-align:center;"
						>
							{emoji}
						</div>
					{:else if loading || assistantSpeaking}
						<svg
							class="size-44 text-gray-900 dark:text-gray-400"
							viewBox="0 0 24 24"
							fill="currentColor"
							xmlns="http://www.w3.org/2000/svg"
							><style>
								.spinner_qM83 {
									animation: spinner_8HQG 1.05s infinite;
								}
								.spinner_oXPr {
									animation-delay: 0.1s;
								}
								.spinner_ZTLf {
									animation-delay: 0.2s;
								}
								@keyframes spinner_8HQG {
									0%,
									57.14% {
										animation-timing-function: cubic-bezier(0.33, 0.66, 0.66, 1);
										transform: translate(0);
									}
									28.57% {
										animation-timing-function: cubic-bezier(0.33, 0, 0.66, 0.33);
										transform: translateY(-6px);
									}
									100% {
										transform: translate(0);
									}
								}
							</style><circle class="spinner_qM83" cx="4" cy="12" r="3" /><circle
								class="spinner_qM83 spinner_oXPr"
								cx="12"
								cy="12"
								r="3"
							/><circle class="spinner_qM83 spinner_ZTLf" cx="20" cy="12" r="3" /></svg
						>
					{:else}
						<div
							class=" {rmsLevel * 100 > 4
								? ' size-52'
								: rmsLevel * 100 > 2
									? 'size-48'
									: rmsLevel * 100 > 1
										? 'size-44'
										: 'size-40'} transition-all rounded-full bg-cover bg-center bg-no-repeat"
							style={`background-image: url('${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&voice=true');`}
						/>
					{/if}
				</button>
			{:else}
				<div class="relative flex video-container w-full max-h-full pt-2 pb-4 md:py-6 px-2 h-full">
					<!-- svelte-ignore a11y-media-has-caption -->
					<video
						id="camera-feed"
						autoplay
						class="rounded-2xl h-full min-w-full object-cover object-center"
						playsinline
					/>

					<canvas id="camera-canvas" style="display:none;" />

					<div class=" absolute top-4 md:top-8 left-4">
						<button
							type="button"
							aria-label={$i18n.t('Stop camera')}
							class="p-1.5 text-white cursor-pointer backdrop-blur-xl bg-black/10 rounded-full"
							on:click={() => {
								stopCamera();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 16 16"
								fill="currentColor"
								class="size-6"
							>
								<path
									d="M5.28 4.22a.75.75 0 0 0-1.06 1.06L6.94 8l-2.72 2.72a.75.75 0 1 0 1.06 1.06L8 9.06l2.72 2.72a.75.75 0 1 0 1.06-1.06L9.06 8l2.72-2.72a.75.75 0 0 0-1.06-1.06L8 6.94 5.28 4.22Z"
								/>
							</svg>
						</button>
					</div>
				</div>
			{/if}
		</div>

		<div class="flex flex-col items-center gap-4 pb-4 w-full">
			<button
				type="button"
				class="z-10"
				on:click={() => {
					if (assistantSpeaking) {
						stopAllAudio();
					}
				}}
			>
				<div class="line-clamp-1 text-sm font-normal">
					{#if loading}
						{$i18n.t('Thinking...')}
					{:else if muted}
						{$i18n.t('Muted')}
					{:else if assistantSpeaking}
						{$i18n.t('Tap to interrupt')}
					{:else}
						{$i18n.t('Listening...')}
					{/if}
				</div>
			</button>

			<div class="flex items-center justify-center gap-4 z-10">
				{#if camera}
					<VideoInputMenu
						devices={videoInputDevices}
						on:change={async (e) => {
							selectedVideoInputDeviceId = e.detail;
							localStorage.setItem('selectedVideoInputDeviceId', e.detail);
							await stopVideoStream();
							await startVideoStream();
						}}
					>
						<button
							aria-label={$i18n.t('Switch camera')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							type="button"
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="size-5"
							>
								<path
									fill-rule="evenodd"
									d="M15.312 11.424a5.5 5.5 0 0 1-9.201 2.466l-.312-.311h2.433a.75.75 0 0 0 0-1.5H3.989a.75.75 0 0 0-.75.75v4.242a.75.75 0 0 0 1.5 0v-2.43l.31.31a7 7 0 0 0 11.712-3.138.75.75 0 0 0-1.449-.39Zm1.23-3.723a.75.75 0 0 0 .219-.53V2.929a.75.75 0 0 0-1.5 0V5.36l-.31-.31A7 7 0 0 0 3.239 8.188a.75.75 0 1 0 1.448.389A5.5 5.5 0 0 1 13.89 6.11l.311.31h-2.432a.75.75 0 0 0 0 1.5h4.243a.75.75 0 0 0 .53-.219Z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
					</VideoInputMenu>
				{:else}
					<Tooltip content={$i18n.t('Camera')}>
						<button
							aria-label={$i18n.t('Camera')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							type="button"
							on:click={() => {
								void startCamera();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z"
								/>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM18.75 10.5h.008v.008h-.008V10.5Z"
								/>
							</svg>
						</button>
					</Tooltip>
				{/if}

				<Tooltip content={muted ? $i18n.t('Unmute') + ' (M)' : $i18n.t('Mute') + ' (M)'}>
					<button
						class="p-3 rounded-full transition-colors duration-200 {muted
							? 'bg-red-500 text-white'
							: 'bg-gray-50 dark:bg-gray-900'}"
						type="button"
						aria-label={muted ? $i18n.t('Unmute') : $i18n.t('Mute')}
						on:click={toggleMute}
					>
						{#if muted}
							<!-- Mic Off icon -->
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"
								/>
								<line
									x1="3"
									y1="3"
									x2="21"
									y2="21"
									stroke="currentColor"
									stroke-width="1.5"
									stroke-linecap="round"
								/>
							</svg>
						{:else}
							<!-- Mic On icon -->
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"
								/>
							</svg>
						{/if}
					</button>
				</Tooltip>

				<button
					aria-label={$i18n.t('End call')}
					class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
					on:click={endCall}
					type="button"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 20 20"
						fill="currentColor"
						class="size-5"
					>
						<path
							d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
						/>
					</svg>
				</button>
			</div>
		</div>
	</div>
{/if}
