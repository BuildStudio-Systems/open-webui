import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';

const source = readFileSync('src/lib/components/chat/Chat.svelte', 'utf8');
const script = source.split('<script lang="ts">')[1].split('</script>')[0];
const tree = ts.createSourceFile('chat.ts', script, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
let socketHandler = '';
let submissionCatch = '';
function visit(node: ts.Node) {
	if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'handleOpenAIError') {
		socketHandler = node.initializer!.getText(tree);
	}
	if (
		ts.isCallExpression(node) &&
		ts.isPropertyAccessExpression(node.expression) &&
		node.expression.name.text === 'catch' &&
		node.arguments[0] &&
		node.arguments[0].getText(tree).includes('history.currentId = responseMessageId') &&
		node.arguments[0].getText(tree).includes('responseMessage.done = true')
	) {
		submissionCatch = node.arguments[0].getText(tree);
	}
	ts.forEachChild(node, visit);
}
visit(tree);

function actualHandler(text: string, deps: object) {
	expect(text).not.toBe('');
	const compiled = ts.transpileModule(`const handler = ${text};`, {
		compilerOptions: { target: ts.ScriptTarget.ES2022 }
	}).outputText;
	return new Function(
		'deps',
		`const {toast,$i18n,eventTarget,history,responseMessage,responseMessageId}=deps;
	${compiled}
	return handler;`
	)(deps);
}

afterEach(() => {
	vi.restoreAllMocks();
});
describe('actual Chat error paths release voice conversation', () => {
	it.each([
		{ detail: 'Synthetic denied request' },
		{ error: { message: 'Synthetic model error' } },
		{ error: 'Synthetic string error' },
		{ message: 'Synthetic failure' },
		'Synthetic primitive error',
		null
	])(
		'finishes a failed socket/HTTP error object without a later done event (%j)',
		async (error) => {
			vi.spyOn(console, 'error').mockImplementation(() => {});
			const eventTarget = new EventTarget();
			const finish = vi.fn();
			eventTarget.addEventListener('chat:finish', finish);
			const responseMessage = { id: 'agent-reply', done: false };
			const history = { messages: {} };
			const toast = { error: vi.fn() };
			await actualHandler(socketHandler, {
				eventTarget,
				history,
				toast,
				$i18n: { t: (s: string) => s }
			})(error, responseMessage);
			expect(responseMessage.done).toBe(true);
			expect(finish).toHaveBeenCalledTimes(1);
			expect(finish.mock.calls[0][0].detail.id).toBe('agent-reply');
			expect(history.messages).toHaveProperty('agent-reply', responseMessage);
			expect(toast.error).toHaveBeenCalled();
		}
	);
	it('finishes failed HTTP submission even when no socket response exists', async () => {
		vi.spyOn(console, 'log').mockImplementation(() => {});
		const eventTarget = new EventTarget();
		const finish = vi.fn();
		eventTarget.addEventListener('chat:finish', finish);
		const responseMessage = { id: 'agent-reply', done: false };
		const deps = {
			eventTarget,
			responseMessage,
			responseMessageId: 'agent-reply',
			history: { messages: {}, currentId: null },
			toast: { error: vi.fn() },
			$i18n: { t: (s: string) => s }
		};
		await actualHandler(submissionCatch, deps)(new Error('Synthetic network failure'));
		expect(responseMessage.done).toBe(true);
		expect(finish).toHaveBeenCalledTimes(1);
		expect(finish.mock.calls[0][0].detail.id).toBe('agent-reply');
		expect(deps.toast.error).toHaveBeenCalled();
	});
});
