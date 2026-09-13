import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { reconnectSessionSocket, socketSessionAuth } from './socket-session';

function fixture() {
	const handlers = new Map<string, () => void>();
	const socket = {
		on: vi.fn((event: string, fn: () => void) => handlers.set(event, fn)),
		off: vi.fn((event: string) => handlers.delete(event)),
		disconnect: vi.fn(), connect: vi.fn()
	};
	return { socket, handlers, client: socket as unknown as Socket };
}

afterEach(() => { vi.useRealTimers(); });
describe('session socket authentication', () => {
	it('reads the current token on every handshake, including logout', () => {
		let token: string | null = null;
		const auth = socketSessionAuth(() => token);
		const cb = vi.fn();
		auth(cb); expect(cb).toHaveBeenLastCalledWith({});
		token = 'synthetic-session'; auth(cb);
		expect(cb).toHaveBeenLastCalledWith({token: 'synthetic-session'});
		token = null; auth(cb); expect(cb).toHaveBeenLastCalledWith({});
	});
	it('forces a fresh handshake and waits for connection', async () => {
		const { socket, handlers, client } = fixture();
		const promise = reconnectSessionSocket(client);
		expect(socket.disconnect).toHaveBeenCalledOnce();
		expect(socket.connect).toHaveBeenCalledOnce();
		handlers.get('connect')?.();
		await expect(promise).resolves.toBeUndefined();
		expect(handlers.size).toBe(0);
	});
	it('cleans up and rejects refused authentication', async () => {
		const { handlers, client } = fixture();
		const promise = reconnectSessionSocket(client);
		handlers.get('connect_error')?.();
		await expect(promise).rejects.toThrow('Realtime session connection failed');
		expect(handlers.size).toBe(0);
	});
	it('bounds the wait and removes pending listeners', async () => {
		vi.useFakeTimers();
		const { handlers, client } = fixture();
		const promise = reconnectSessionSocket(client, 100);
		const rejected = expect(promise).rejects.toThrow('Realtime session connection failed');
		await vi.advanceTimersByTimeAsync(100);
		await rejected;
		expect(handlers.size).toBe(0);
	});
});
