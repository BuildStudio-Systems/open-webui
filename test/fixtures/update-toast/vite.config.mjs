import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
export default {
	root: fileURLToPath(new URL('.', import.meta.url)),
	plugins: [svelte({ configFile: false })],
	resolve: { alias: { '$lib/constants': fileURLToPath(new URL('./constants.ts', import.meta.url)) } },
	server: { host: '127.0.0.1', port: 18764, strictPort: true, fs: { allow: [fileURLToPath(new URL('../../..', import.meta.url))] } }
};
