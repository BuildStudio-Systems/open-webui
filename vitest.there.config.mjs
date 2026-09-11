import { fileURLToPath } from 'node:url';

export default {
	resolve: { alias: { '$lib': fileURLToPath(new URL('./src/lib', import.meta.url)) } },
	test: { environment: 'node', include: ['src/lib/apis/there/wiki.test.ts'] }
};
