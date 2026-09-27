import { describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';
import { keymap } from '@codemirror/view';
import { history } from '@codemirror/commands';
import { javascript } from '@codemirror/lang-javascript';
import { json } from '@codemirror/lang-json';
import { basicSetup } from 'codemirror';

describe('editor dependency graph', () => {
	it('combines application imports and CodeMirror extensions in one state instance', () => {
		expect(() =>
			EditorState.create({
				doc: 'const value = 1;',
				extensions: [basicSetup, javascript(), keymap.of([])]
			})
		).not.toThrow();
	});

	it('accepts JSON and history extensions without duplicate state-class instances', () => {
		const state = EditorState.create({
			doc: '{"value":1}',
			extensions: [basicSetup, json(), history()]
		});
		expect(state.doc.toString()).toBe('{"value":1}');
	});
});
