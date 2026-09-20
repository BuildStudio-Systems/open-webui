import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const stylesheet = readFileSync('src/tailwind.css', 'utf8');
const picker = readFileSync('src/lib/components/common/StudioLanguagePicker.svelte', 'utf8');

describe('language font contract', () => {
	it.each([
		['en-US', '--studio-font-en', "'Inter'"],
		['ja-JP', '--studio-font-ja', "'Noto Sans JP'"],
		['zh-CN', '--studio-font-zh', "'Noto Sans SC'"]
	])('maps %s to its independent stack', (language, token, leadingFont) => {
		expect(stylesheet).toContain(`${token}: ${leadingFont}`);
		expect(stylesheet).toContain(`html[lang='${language}']`);
		expect(stylesheet).toContain(`--studio-language-font: var(${token})`);
	});

	it('routes body and Tailwind font utilities through the active language token', () => {
		expect(stylesheet).toContain(
			'--font-sans: var(--app-font-family, var(--studio-language-font));'
		);
		expect(stylesheet).toMatch(/body\s*\{\s*font-family:\s*var\(--font-sans\)/);
		expect(stylesheet).toMatch(
			/\.font-primary,\s*\.font-secondary\s*\{\s*font-family:\s*var\(--font-sans\)/
		);
	});

	it('does not let the picker reintroduce body-only font overrides', () => {
		expect(picker).not.toMatch(/html\[lang=.*?\]\s+body/);
	});
});
