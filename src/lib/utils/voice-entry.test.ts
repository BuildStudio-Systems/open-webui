import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { voiceRequest } from './voice-session';

const deferred = <T>() => {
	let resolve!: (value: T) => void;
	let reject!: (error: Error) => void;
	const promise = new Promise<T>((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
};
const source = readFileSync('src/lib/components/chat/MessageInput.svelte', 'utf8');
const block = source
	.split('// Voice entry lifecycle:')[1]
	.split('// End voice entry lifecycle.')[0];
const tree = ts.createSourceFile(
	'entry.ts',
	'//' + block,
	ts.ScriptTarget.ESNext,
	true,
	ts.ScriptKind.TS
);
const isolated = tree.statements
	.filter((node) => !ts.isLabeledStatement(node))
	.map((node) => node.getText(tree))
	.join('\n');
const compiled = ts.transpileModule(isolated, {
	compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
const factory = new Function(
	'deps',
	`
const {beforeNavigate,onDestroy,toast,$i18n,$config,$settings,KokoroWorker,voiceRequest}=deps;
let chatId='chat-a',selectedModels=['there-agent-3.8'],atSelectedModel,embedded=false,prompt='',files=[];
let $_user={id:'synthetic-user',role:'admin'},$showCallOverlay=false,$TTSWorker=deps.sharedWorker??null;
const showCallOverlay={set(value){$showCallOverlay=value;deps.open(value)}};
const showControls={set:deps.controls};
const TTSWorker={set(value){$TTSWorker=value;deps.published(value)}};
${compiled}
syncVoiceContext(JSON.stringify([chatId,selectedModels,atSelectedModel?.id,$_user?.id]));
return {openVoiceCall,endCall:()=>showCallOverlay.set(false),
changeModel(id){selectedModels=[id];syncVoiceContext(JSON.stringify([chatId,selectedModels,atSelectedModel?.id,$_user?.id]));},
state:()=>({voiceOpening,voiceDisposed,sharedWorker:$TTSWorker})};
`
);

function entry(
	options: { localTts?: boolean; init?: () => Promise<void>; sharedWorker?: object } = {}
) {
	const permission = deferred<any>();
	const track = { stop: vi.fn() };
	const stream = { getTracks: () => [track] };
	const workers: any[] = [];
	let destroy!: () => void;
	let navigate!: () => void;
	vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn(() => permission.promise) } });
	vi.stubGlobal('window', { location: { pathname: '/c/chat-a', search: '' } });
	const deps = {
		beforeNavigate: (handler: () => void) => {
			navigate = handler;
		},
		onDestroy: (handler: () => void) => {
			destroy = handler;
		},
		toast: { error: vi.fn() },
		$i18n: { t: (value: string) => value },
		$config: { audio: { stt: { engine: '' } } },
		$settings: { audio: { tts: { engine: options.localTts ? 'browser-kokoro' : '' } } },
		KokoroWorker: class {
			constructor(public dtype: string) {
				workers.push(this);
			}
			init = vi.fn(options.init ?? (async () => {}));
			terminate = vi.fn();
		},
		voiceRequest,
		open: vi.fn(),
		controls: vi.fn(),
		published: vi.fn(),
		sharedWorker: options.sharedWorker
	};
	return {
		api: factory(deps),
		deps,
		permission,
		stream,
		track,
		workers,
		destroy: () => destroy(),
		navigate: () => navigate()
	};
}
afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('actual MessageInput voice entry lifecycle', () => {
	it('coalesces rapid double clicks and cannot reopen after the first call ends', async () => {
		const { api, permission, stream, deps, track } = entry();
		const first = api.openVoiceCall();
		const duplicate = api.openVoiceCall();
		expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
		expect(api.state().voiceOpening).toBe(true);
		permission.resolve(stream);
		await first;
		api.endCall();
		await duplicate;
		expect(deps.open.mock.calls.map(([value]) => value)).toEqual([true, false]);
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(api.state().voiceOpening).toBe(false);
		expect(source).toContain('disabled={voiceOpening}');
		expect(source).toContain('on:click={openVoiceCall}');
	});
	it('releases a late microphone grant after component destruction without opening', async () => {
		const { api, permission, stream, deps, track, destroy } = entry();
		const pending = api.openVoiceCall();
		destroy();
		permission.resolve(stream);
		await pending;
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(deps.open).not.toHaveBeenCalled();
		expect(api.state()).toMatchObject({ voiceOpening: false, voiceDisposed: true });
	});
	it('invalidates navigation even when the user returns to the same pathname', async () => {
		const { api, permission, stream, deps, track, navigate } = entry();
		const pending = api.openVoiceCall();
		navigate();
		window.location.pathname = '/c/chat-b';
		navigate();
		window.location.pathname = '/c/chat-a';
		permission.resolve(stream);
		await pending;
		expect(track.stop).toHaveBeenCalledTimes(1);
		expect(deps.open).not.toHaveBeenCalled();
	});
	it('invalidates an old model selection without clearing a newer opening attempt', async () => {
		const { api, permission, stream, deps } = entry();
		const secondPermission = deferred<any>();
		vi.mocked(navigator.mediaDevices.getUserMedia)
			.mockReturnValueOnce(permission.promise)
			.mockReturnValueOnce(secondPermission.promise);
		const old = api.openVoiceCall();
		api.changeModel('there-3.8');
		const next = api.openVoiceCall();
		permission.resolve(stream);
		await old;
		expect(api.state().voiceOpening).toBe(true);
		expect(deps.open).not.toHaveBeenCalled();
		secondPermission.resolve(stream);
		await next;
		expect(deps.open).toHaveBeenCalledTimes(1);
		expect(deps.open).toHaveBeenCalledWith(true);
	});
	it('aborts pending local TTS initialization on destroy and terminates only the owned worker', async () => {
		const init = deferred<void>();
		const { api, permission, stream, deps, workers, destroy } = entry({
			localTts: true,
			init: () => init.promise
		});
		const pending = api.openVoiceCall();
		permission.resolve(stream);
		await vi.waitFor(() => expect(workers).toHaveLength(1));
		destroy();
		await pending;
		init.resolve();
		await Promise.resolve();
		expect(workers[0].terminate).toHaveBeenCalledTimes(1);
		expect(deps.open).not.toHaveBeenCalled();
		expect(deps.published).not.toHaveBeenCalled();
	});
	it('does not publish a failed worker and allows initialization retry', async () => {
		const init = vi
			.fn()
			.mockRejectedValueOnce(new Error('Synthetic TTS init failed'))
			.mockResolvedValueOnce(undefined);
		const { api, permission, stream, deps, workers } = entry({ localTts: true, init });
		permission.resolve(stream);
		await api.openVoiceCall();
		expect(deps.toast.error).toHaveBeenCalled();
		expect(deps.published).not.toHaveBeenCalled();
		expect(workers[0].terminate).toHaveBeenCalledTimes(1);
		await api.openVoiceCall();
		expect(workers[1].dtype).toBe('fp32');
		expect(deps.published).toHaveBeenCalledTimes(1);
		expect(deps.published).toHaveBeenCalledWith(workers[1]);
		expect(deps.open).toHaveBeenCalledTimes(1);
		expect(deps.open).toHaveBeenCalledWith(true);
	});
	it('does not terminate a previously shared TTS worker', async () => {
		const sharedWorker = { terminate: vi.fn() };
		const { api, permission, stream, destroy, workers } = entry({ localTts: true, sharedWorker });
		permission.resolve(stream);
		await api.openVoiceCall();
		destroy();
		expect(workers).toHaveLength(0);
		expect(sharedWorker.terminate).not.toHaveBeenCalled();
	});
});

describe('actual ChatControls responsive call boundary', () => {
	const controlsSource = readFileSync('src/lib/components/chat/ChatControls.svelte', 'utf8');
	const controlsScript = controlsSource.split('<script lang="ts">')[1].split('</script>')[0];
	const controlsTree = ts.createSourceFile(
		'controls.ts',
		controlsScript,
		ts.ScriptTarget.ESNext,
		true,
		ts.ScriptKind.TS
	);
	let handler = '';
	function visit(node: ts.Node) {
		if (ts.isVariableDeclaration(node) && node.name.getText(controlsTree) === 'handleMediaQuery')
			handler = node.initializer!.getText(controlsTree);
		ts.forEachChild(node, visit);
	}
	visit(controlsTree);
	function controls() {
		const changed = vi.fn();
		const code = ts.transpileModule(`const handleMediaQuery = ${handler};`, {
			compilerOptions: { target: ts.ScriptTarget.ES2022 }
		}).outputText;
		const api = new Function(
			'changed',
			`let largeScreen=false,mediaQueryInitialized=false,$showCallOverlay=true;
		const showCallOverlay={set(value){$showCallOverlay=value;changed(value)}};
		${code}
		return {handleMediaQuery,close:()=>showCallOverlay.set(false)};`
		)(changed);
		return { api, changed };
	}
	it('keeps an explicitly opened call during first layout initialization', () => {
		const { api, changed } = controls();
		api.handleMediaQuery({ matches: true });
		expect(changed).not.toHaveBeenCalled();
	});
	it('ends the call on an actual breakpoint change without restarting an unmuted instance', async () => {
		const { api, changed } = controls();
		api.handleMediaQuery({ matches: false });
		api.handleMediaQuery({ matches: true });
		await Promise.resolve();
		expect(changed.mock.calls).toEqual([[false]]);
	});
	it('does not reopen after hangup while crossing either breakpoint direction', async () => {
		const { api, changed } = controls();
		api.handleMediaQuery({ matches: true });
		api.close();
		api.handleMediaQuery({ matches: false });
		api.handleMediaQuery({ matches: true });
		await Promise.resolve();
		expect(changed.mock.calls).toEqual([[false]]);
	});
});
