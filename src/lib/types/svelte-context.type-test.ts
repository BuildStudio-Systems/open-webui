// Compile-time contract assertions, checked by the normal strict svelte-check.
// This function is never invoked: getContext is only legal during component init.
import { getContext } from 'svelte';
import type i18n from '../i18n';

export function assertContextTypes() {
	const translationStore: typeof i18n = getContext('i18n');
	translationStore.subscribe((value) => value.t('Sign in'));

	const otherContext = getContext('unregistered-context-key');
	// @ts-expect-error Unregistered context keys must remain unknown.
	const invalidStore: typeof i18n = otherContext;
	// @ts-expect-error An arbitrary object is not an i18next instance.
	translationStore.set({ language: 123 });
	return invalidStore;
}
