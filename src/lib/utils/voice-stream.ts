/** Call-only segmentation; other readers retain their configured batching policy. */
export const CALL_SILENCE_MS = 1200;

export function speechLanguage(text: string): string | undefined {
	if (/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(text)) return 'ja-JP';
	if (/\p{Script=Han}/u.test(text)) return 'zh-CN';
	if (/[a-z]/i.test(text)) return 'en-US';
	return undefined;
}

/** Hide both complete and still-streaming private/details and fenced-code blocks. */
function visibleSpeechText(content: string, final: boolean): string {
	let visible = '';
	let depth = 0;
	let offset = 0;
	for (const match of content.matchAll(/<\/?details\b[^>]*>/gi)) {
		const closing = match[0].startsWith('</');
		if (depth === 0) visible += content.slice(offset, match.index);
		depth = Math.max(0, depth + (closing ? -1 : 1));
		offset = match.index! + match[0].length;
	}
	if (depth === 0) visible += content.slice(offset);
	visible = visible
		.replace(/<[^>]*$/g, '') // A tag may itself span several stream chunks.
		.replace(/(`{3,}|~{3,})[\s\S]*?(?:\1|$)/g, '')
		.replace(/^\s*\|[^\n]*(?:\n|$)/gm, '');
	if (final) return visible;
	return visible
		.replace(/`[^`]*(?:`|$)/g, (span) => (span.length > 1 && span.endsWith('`') ? span : ''))
		.replace(/\[[^\]]*$|\[[^\]]*\]$|\[[^\]]*\]\([^)]*$/g, '');
}

// The first spoken part may end at a clause pause once it carries enough words, so a long
// opening sentence starts playing before the model has finished it. Later parts keep whole
// sentences. Weight counts letters/digits only, so unfinished Markdown markers cannot move it.
const CLAUSE_PAUSE_CJK = /[，、；：]/;
const CLAUSE_PAUSE_ASCII = /[,;:]/;
const CJK_TEXT = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;
export const FIRST_CLAUSE_MIN_CJK = 12;
export const FIRST_CLAUSE_MIN_OTHER = 40;

function spokenWeight(text: string): number {
	return text.match(/[\p{L}\p{N}]/gu)?.length ?? 0;
}

function firstClauseReady(text: string, start: number, index: number): boolean {
	const character = text[index];
	const pause =
		CLAUSE_PAUSE_CJK.test(character) ||
		// "1,000" and "10:30" are not pauses; an ASCII pause needs a following separator.
		(CLAUSE_PAUSE_ASCII.test(character) && /\s/.test(text[index + 1] ?? ''));
	if (!pause) return false;
	const clause = text.slice(start, index);
	const minimum = CJK_TEXT.test(clause) ? FIRST_CLAUSE_MIN_CJK : FIRST_CLAUSE_MIN_OTHER;
	return spokenWeight(clause) >= minimum;
}

/**
 * Return only ready sentences while streaming, and include the remaining tail at finish.
 * ASCII periods wait for a following separator so chunked decimals/URLs stay intact.
 * The cleaner must preserve separators; trim only each ready part, not the stream prefix.
 * No word/character minimum: Chinese and Japanese short sentences must start speaking.
 * Every boundary depends only on the text before it (plus one following separator), so a
 * streamed prefix always yields a prefix of the final parts.
 */
export function callSpeechParts(
	content: string,
	splitOn: string,
	final: boolean,
	clean: (text: string) => string
): string[] {
	const visible = visibleSpeechText(content, final);
	const text = clean(visible);
	if (splitOn === 'none') return final && text.trim() ? [text.trim()] : [];
	const parts: string[] = [];
	let start = 0;
	for (let index = 0; index < text.length; index++) {
		const character = text[index];
		const boundary =
			character === '\n' ||
			(splitOn !== 'paragraphs' &&
				(/[。！？]/.test(character) ||
					(/[.!?]/.test(character) && /\s/.test(text[index + 1] ?? '')))) ||
			(splitOn === 'punctuation' && parts.length === 0 && firstClauseReady(text, start, index));
		if (!boundary) continue;
		const part = text.slice(start, index + 1).trim();
		if (part) parts.push(part);
		start = index + 1;
	}
	if (final && text.slice(start).trim()) parts.push(text.slice(start).trim());
	return parts;
}
