import { fileURLToPath } from 'node:url';

export default {
	resolve: { alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) } },
	test: {
		environment: 'node',
		include: [
			'src/lib/apis/audio/*.test.ts',
			'src/lib/utils/voice-session.test.ts',
			'src/lib/utils/voice-chat-events.test.ts',
			'src/lib/utils/voice-entry.test.ts',
			'src/lib/apis/there/*.test.ts',
			'src/lib/constants/permissions.test.ts',
			'src/lib/utils/attachment-download.test.ts',
			'src/lib/utils/language-fonts.test.ts'
		]
	}
};
