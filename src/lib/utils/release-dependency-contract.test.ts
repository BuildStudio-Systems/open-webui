import { readFileSync, readdirSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('release dependency contract', () => {
	it('declares the direct editor and build-asset dependencies', () => {
		const { dependencies } = JSON.parse(readFileSync('package.json', 'utf8'));
		for (const name of [
			'@codemirror/autocomplete', '@codemirror/commands', '@codemirror/lang-json',
			'@codemirror/language', '@codemirror/state', '@codemirror/view',
			'@tiptap/extension-italic', 'onnxruntime-web'
		]) {
			expect(dependencies[name], name).toBeTruthy();
		}
	});

	it('provides the JavaScript and WASM assets required by the Vite copy target', () => {
		const files = readdirSync('node_modules/onnxruntime-web/dist');
		expect(files.some((name) => name.includes('.jsep.') && name.endsWith('.mjs'))).toBe(true);
		expect(files.some((name) => name.includes('.jsep.') && name.endsWith('.wasm'))).toBe(true);
	});
});
