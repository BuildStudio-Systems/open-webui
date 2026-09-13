// Availability is independent of the per-conversation opt-in state.
export const DEFAULT_WEB_SEARCH_ENABLED = false;

export const canUseWebSearch = (
	role: string | undefined,
	userPermission: boolean | undefined,
	serverEnabled: boolean | undefined,
	modelsCapable: boolean
): boolean =>
	Boolean(
		serverEnabled &&
			modelsCapable &&
			(role === 'admin' || (role === 'user' && userPermission))
	);
