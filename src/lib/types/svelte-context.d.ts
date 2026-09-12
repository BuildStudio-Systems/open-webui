import 'svelte';

// The root layout provides the shared writable i18next store under this key.
// Keep the literal-key overload tied to that implementation; other context
// keys retain Svelte's generic/unknown contract.
declare module 'svelte' {
	export function getContext(key: 'i18n'): typeof import('../i18n').default;
}
