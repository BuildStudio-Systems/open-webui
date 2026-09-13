import { describe, expect, it } from 'vitest';
import { hasPendingAssistantResponse } from './response-tasks';

describe('response task completion', () => {
	it('releases completed responses even if background task IDs remain', () => {
		expect(hasPendingAssistantResponse({a: {role: 'assistant', done: true}})).toBe(false);
	});
	it('keeps multi-model generation active until every response finishes', () => {
		expect(hasPendingAssistantResponse({a: {role: 'assistant', done: true}, b: {role: 'assistant', done: false}})).toBe(true);
	});
	it('does not mistake a user or non-leaf historical message for a pending response', () => {
		expect(hasPendingAssistantResponse({u: {role: 'user'}, a: {role: 'assistant', done: false, childrenIds: ['next']}})).toBe(false);
	});
	it('treats an assistant placeholder as pending before its first event', () => {
		expect(hasPendingAssistantResponse({a: {role: 'assistant'}})).toBe(true);
	});
});
