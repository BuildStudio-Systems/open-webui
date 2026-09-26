import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import { callSpeechParts, speechLanguage } from './voice-stream';

// Execute the shipped sanitizer and Chat dispatcher, without loading their browser imports.
const utilsTree = ts.createSourceFile(
	'utils.ts',
	readFileSync('src/lib/utils/index.ts', 'utf8'),
	ts.ScriptTarget.ESNext,
	true
);
const cleanerSource = utilsTree.statements
	.filter(
		(node) =>
			ts.isVariableStatement(node) &&
			['removeEmojis', 'removeFormattings', 'cleanText'].includes(
				node.declarationList.declarations[0].name.getText(utilsTree)
			)
	)
	.map((node) => node.getText(utilsTree).replace(/^export /, ''))
	.join('\n');
const clean = new Function(
	`${ts.transpileModule(cleanerSource, { compilerOptions: { target: ts.ScriptTarget.ESNext } }).outputText}; return (text) => removeFormattings(removeEmojis(text));`
)() as (text: string) => string;
const chatScript = readFileSync('src/lib/components/chat/Chat.svelte', 'utf8')
	.split('<script lang="ts">')[1]
	.split('</script>')[0];
const chatTree = ts.createSourceFile('chat.ts', chatScript, ts.ScriptTarget.ESNext, true);
const declaration = chatTree.statements.find(
	(node) =>
		ts.isVariableStatement(node) &&
		node.declarationList.declarations[0].name.getText(chatTree) === 'dispatchCallOverlayAudio'
)!;
const dispatcher = ts.transpileModule(declaration.getText(chatTree), {
	compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
function conversation(splitOn = 'punctuation', active = true) {
	const sent: string[] = [];
	const eventTarget = new EventTarget();
	eventTarget.addEventListener('chat', (event) => {
		sent.push((event as CustomEvent<{ content: string }>).detail.content);
	});
	const dispatch = new Function(
		'callSpeechParts',
		'removeFormattings',
		'removeEmojis',
		'getOutputText',
		'eventTarget',
		'$config',
		'$showCallOverlay',
		`${dispatcher}; return dispatchCallOverlayAudio;`
	)(
		callSpeechParts,
		clean,
		(text: string) => text,
		() => '',
		eventTarget,
		{ audio: { tts: { split_on: splitOn } } },
		active
	);
	return {
		sent,
		dispatch,
		message: { id: 'synthetic-reply', content: '', ttsSentContentPartCount: 0 }
	};
}

describe('call-only multilingual streaming speech', () => {
	it.each([
		['你好。后续还在生成', '你好。'],
		['こんにちは。説明を続け', 'こんにちは。'],
		['Hello. More is still streaming', 'Hello.'],
		['你好！', '你好！'],
		['できますか？', 'できますか？']
	])('dispatches a short complete sentence immediately: %s', (content, expected) => {
		const call = conversation();
		call.message.content = content;
		call.dispatch(call.message);
		expect(call.sent).toEqual([expected]);
	});
	it.each([
		['你好。\n1. 中文。\n2. 最后。', ['你好。', '中文。', '最后。']],
		['标题。\n- 列表。\n+ 另一项。', ['标题。', '列表。', '另一项。']],
		['# 标题。\n第一段。\n> 引文。', ['标题。', '第一段。', '引文。']],
		['Result 3.14.\n1.', ['Result 3.14.', '1.']],
		['你好。第二句！尾部', ['你好。', '第二句！', '尾部']],
		['日本語です。次も話せます？最後', ['日本語です。', '次も話せます？', '最後']],
		[
			'Value 3.14. See https://example.com/a. Final tail',
			['Value 3.14.', 'See https://example.com/a.', 'Final tail']
		],
		['开始。\n```python\nprint("秘密。不要朗读。")\n```\n继续。结尾', ['开始。', '继续。', '结尾']],
		[
			'<details><summary>Thinking</summary>秘密。<details>nested。</details>more。</details>答复。结尾',
			['答复。', '结尾']
		],
		[
			'先说。链接[示例。网站](https://example.com)完成。尾部',
			['先说。', '链接示例。', '网站完成。', '尾部']
		],
		['Use `value 3.14` now. Done.', ['Use value 3.14 now.', 'Done.']],
		// A long opening clause starts speaking at its pause; later sentences stay whole.
		[
			'这是一个比较长的开头句子，后面还有很多内容。结尾',
			['这是一个比较长的开头句子，', '后面还有很多内容。', '结尾']
		],
		['你好，我是泽亚。', ['你好，我是泽亚。']],
		[
			'本日はご利用いただきありがとうございます、続けて説明します。',
			['本日はご利用いただきありがとうございます、', '続けて説明します。']
		],
		[
			'This opening clause is intentionally long enough to speak early, then it continues.',
			['This opening clause is intentionally long enough to speak early,', 'then it continues.']
		],
		[
			'The regional total across every branch was 1,234,567 dollars today. Next',
			['The regional total across every branch was 1,234,567 dollars today.', 'Next']
		],
		[
			'短句。第二句同样很长很长很长很长很长很长，结尾。',
			['短句。', '第二句同样很长很长很长很长很长很长，结尾。']
		],
		[
			'**重要的开场说明要点在这里**，然后继续。',
			['重要的开场说明要点在这里，', '然后继续。']
		]
	])(
		'every-character chunks preserve spoken order and flush final tail once: %s',
		(content, expected) => {
			const call = conversation();
			for (let index = 1; index <= content.length; index++) {
				call.message.content = content.slice(0, index);
				call.dispatch(call.message);
			}
			call.dispatch(call.message, true);
			call.dispatch(call.message, true);
			expect(call.sent).toEqual(expected);
		}
	);
	it.each([
		'<details>private。',
		'~~~js\nsecret。',
		'```js\nsecret。',
		'<details><summary>private。'
	])('never speaks an unfinished hidden block at finish: %s', (content) => {
		expect(callSpeechParts(content, 'punctuation', true, clean)).toEqual([]);
	});
	it('does not prematurely speak a chunk ending in a decimal dot', () => {
		expect(callSpeechParts('The value is 3.', 'punctuation', false, clean)).toEqual([]);
		expect(callSpeechParts('The value is 3.14. Next', 'punctuation', false, clean)).toEqual([
			'The value is 3.14.'
		]);
	});
	it('retains a literal bracketed or backtick tail at final completion', () => {
		expect(callSpeechParts('Array [1, 2]', 'punctuation', true, clean)).toEqual(['Array [1, 2]']);
		expect(callSpeechParts('A literal `', 'punctuation', true, clean)).toEqual(['A literal `']);
	});
	it('keeps clause pauses out of paragraph and whole-reply modes', () => {
		const opening = '这是一个比较长的开头句子，后面';
		expect(callSpeechParts(opening, 'punctuation', false, clean)).toEqual([
			'这是一个比较长的开头句子，'
		]);
		expect(callSpeechParts(opening, 'paragraphs', false, clean)).toEqual([]);
		expect(callSpeechParts(opening, 'none', false, clean)).toEqual([]);
		// An ASCII pause waits for its separator, exactly like an ASCII sentence end.
		const english = 'This opening clause is intentionally long enough to speak early,';
		expect(callSpeechParts(english, 'punctuation', false, clean)).toEqual([]);
		expect(callSpeechParts(english + ' then', 'punctuation', false, clean)).toEqual([english]);
	});
	it('honors paragraph and whole-reply preferences', () => {
		expect(callSpeechParts('你好。再见。', 'paragraphs', false, clean)).toEqual([]);
		expect(callSpeechParts('第一段。\n第二段', 'paragraphs', false, clean)).toEqual(['第一段。']);
		expect(callSpeechParts('第一段。\n', 'paragraphs', false, clean)).toEqual(['第一段。']);
		expect(callSpeechParts('你好。', 'none', false, clean)).toEqual([]);
		expect(callSpeechParts('你好。', 'none', true, clean)).toEqual(['你好。']);
	});
	it('does not dispatch audio when the call is closed', () => {
		const call = conversation('punctuation', false);
		call.message.content = 'Never speak.';
		call.dispatch(call.message, true);
		expect(call.sent).toEqual([]);
	});
	it.each([
		['你好。', 'zh-CN'],
		['Thereを紹介します。', 'ja-JP'],
		['Hello from There.', 'en-US'],
		['123', undefined]
	])('chooses browser fallback language from actual text (%s)', (text, language) => {
		expect(speechLanguage(text)).toBe(language);
	});
});
