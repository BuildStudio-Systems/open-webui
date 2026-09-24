import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const i18n = readFileSync('src/lib/i18n/index.ts', 'utf8');
const picker = readFileSync('src/lib/components/common/StudioLanguagePicker.svelte', 'utf8');
const general = readFileSync('src/lib/components/chat/Settings/General.svelte', 'utf8');
const appLayout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');

describe('per-account language contract', () => {
	it('applies the stored account language unless the URL names one', () => {
		expect(i18n).toMatch(/export const applyAccountLanguage = \(raw: unknown, saver:/);
		expect(i18n).toContain("if (!explicitUrlLanguage()) changeLanguage(accountLanguage);");
	});

	it('writes a picker choice back to the account only when it changes', () => {
		expect(i18n).toContain('if (accountSaver && accountLanguage !== lang) {');
		expect(picker).toContain('function choose(code: string) { chooseLanguage(code); open = false }');
		expect(general).toContain('chooseLanguage(lang);');
		expect(general).not.toMatch(/\bchangeLanguage\(lang\)/);
	});

	it('loads and saves the language through the existing user settings (settings.ui.language)', () => {
		expect(appLayout).toContain('applyAccountLanguage($settings?.language, async (language: string) => {');
		expect(appLayout).toContain('await updateUserSettings(localStorage.token, { ui: $settings });');
	});
});
