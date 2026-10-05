import type { Socket } from 'socket.io-client';

/** Only a verified session starts realtime traffic; logout also cancels retries. */
export function syncSessionSocket(socket: Socket | null, authenticated: boolean) {
	if (!socket) return;
	if (!authenticated) socket.disconnect();
	else if (!socket.connected) socket.connect();
}

/** Socket.IO invokes this on every handshake, including automatic reconnects. */
export const socketSessionAuth = (readToken: () => string | null) =>
	(callback: (auth: { token?: string }) => void) => {
		const token = readToken();
		callback(token ? { token } : {});
	};

/** The server binds authorization to the handshake, not a later user-join event. */
export function reconnectSessionSocket(socket: Socket, timeoutMs = 15000): Promise<void> {
	return new Promise((resolve, reject) => {
		const cleanup = () => {
			clearTimeout(timer);
			socket.off('connect', connected);
			socket.off('connect_error', failed);
		};
		const connected = () => { cleanup(); resolve(); };
		const failed = () => { cleanup(); reject(new Error('Realtime session connection failed')); };
		const timer = setTimeout(failed, timeoutMs);
		socket.on('connect', connected);
		socket.on('connect_error', failed);
		try {
			socket.disconnect();
			socket.connect();
		} catch {
			failed();
		}
	});
}
