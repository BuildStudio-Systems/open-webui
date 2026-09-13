import { fileURLToPath } from 'node:url';

export default {
	resolve: { alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) } },
	test: {
		environment: 'node',
		include: [
			'src/lib/apis/there/*.test.ts',
			'src/lib/constants/permissions.test.ts',
			'src/lib/utils/attachment-download.test.ts'
		]
	}
};
