import { describe, expect, it } from 'vitest';
import { DEFAULT_PERMISSIONS, fillPermissionDefaults } from './permissions';

describe('permission form defaults', () => {
	it('fills every known group without sharing mutable default objects', () => {
		const permissions = fillPermissionDefaults();
		expect(permissions).toEqual(DEFAULT_PERMISSIONS);
		permissions.chat.delete = false;
		expect(DEFAULT_PERMISSIONS.chat.delete).toBe(true);
		expect(fillPermissionDefaults().chat.delete).toBe(true);
	});

	it('preserves explicit false and only fills omitted fields', () => {
		const overrides = { chat: { delete: false }, workspace: { knowledge: true } };
		const permissions = fillPermissionDefaults(overrides);
		expect(permissions.chat.delete).toBe(false);
		expect(permissions.chat.export).toBe(DEFAULT_PERMISSIONS.chat.export);
		expect(permissions.workspace.knowledge).toBe(true);
		expect(overrides).toEqual({ chat: { delete: false }, workspace: { knowledge: true } });
	});

	it('does not share nested groups between form instances', () => {
		const first = fillPermissionDefaults();
		const second = fillPermissionDefaults();
		for (const group of Object.keys(first) as Array<keyof typeof first>) {
			expect(first[group]).not.toBe(second[group]);
		}
	});
});
