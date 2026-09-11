// See https://kit.svelte.dev/docs/types#app
// for information about these interfaces
declare global {
	// Injected by vite.config.ts at build time.
	const APP_VERSION: string;
	const APP_BUILD_HASH: string;
	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		// interface Platform {}
	}
}

export {};
