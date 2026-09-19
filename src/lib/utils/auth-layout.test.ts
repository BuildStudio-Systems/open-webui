import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import postcss from 'postcss';

const page = readFileSync('src/routes/auth/+page.svelte', 'utf8');
const stylesheet = postcss.parse(page.split('<style>')[1].split('</style>')[0]);
const declarations = (selector: string) => {
	const result: Record<string, string> = {};
	stylesheet.nodes.forEach((node) => {
		if (node.type === 'rule' && node.selector === selector) {
			node.walkDecls((decl) => { result[decl.prop] = decl.value; });
		}
	});
	return result;
};

describe('authentication viewport regression', () => {
	it('bounds the fixed scroll container below the existing language bar', () => {
		expect(declarations('#auth-container')).toMatchObject({
			inset: '44px 0 0', 'min-height': '0', 'overflow-y': 'auto', 'align-items': 'flex-start'
		});
	});
	it('keeps a tall form scrollable instead of shrinking or centering it off screen', () => {
		expect(declarations('#auth-container > div')).toMatchObject({
			'flex-shrink': '0', 'margin-block': 'auto'
		});
	});
	it('does not force the page below the visible viewport', () => {
		expect(declarations('#auth-page')).toMatchObject({ 'min-height': '0', height: 'calc(100dvh - 44px)' });
	});
	it('reduces decorative copy on short stacked layouts without hiding authentication controls', () => {
		let compact = false;
		stylesheet.walkAtRules('media', (media) => {
			if (media.params !== '(max-width: 980px) and (max-height: 820px)') return;
			media.walkRules((rule) => {
				rule.walkDecls('display', (decl) => {
					if (decl.value === 'none') {
						expect(rule.selector).toBe('.there-brand-copy');
						compact = true;
					}
				});
			});
		});
		expect(compact).toBe(true);
	});
});
