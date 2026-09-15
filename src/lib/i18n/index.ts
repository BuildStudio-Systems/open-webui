import i18next from 'i18next';
import resourcesToBackend from 'i18next-resources-to-backend';
import LanguageDetector from 'i18next-browser-languagedetector';
import type { i18n as i18nType } from 'i18next';
import { writable } from 'svelte/store';

const createI18nStore = (i18n: i18nType) => {
	const i18nWritable = writable(i18n);

	i18n.on('initialized', () => {
		i18nWritable.set(i18n);
    if(typeof window !== 'undefined' && window.parent !== window)window.parent.postMessage({type:'buildstudio:ready'},window.location.origin);
	});
	i18n.on('loaded', () => {
		i18nWritable.set(i18n);
	});
	i18n.on('added', () => i18nWritable.set(i18n));
	i18n.on('languageChanged', (lang) => {
		i18nWritable.set(i18n);
		if (typeof document !== 'undefined') {
			document.documentElement.setAttribute('lang', lang);
      if (i18n.isInitialized && window.parent !== window) window.parent.postMessage({type:'buildstudio:locale',locale:lang.startsWith('ja')?'ja':lang.startsWith('zh')?'zh':'en'},window.location.origin);
		}
	});
	return i18nWritable;
};

const createIsLoadingStore = (i18n: i18nType) => {
	const isLoading = writable(false);

	// if loaded resources are empty || {}, set loading to true
	i18n.on('loaded', (resources) => {
		// console.log('loaded:', resources);
		isLoading.set(Object.keys(resources).length === 0);
	});

	// if resources failed loading, set loading to true
	i18n.on('failedLoading', () => {
		isLoading.set(true);
	});

	return isLoading;
};

export const initI18n = (defaultLocale?: string | undefined) => {
	const detectionOrder = ['querystring', 'localStorage'];
	const fallbackDefaultLocale = ['en-US'];

	const loadResource = (language: string, namespace: string) =>
		import(`./locales/${language}/${namespace}.json`);

	i18next
		.use(resourcesToBackend(loadResource))
		.use(LanguageDetector)
		.init({
			debug: false,
			supportedLngs: ['en-US','ja-JP','zh-CN'],
			load: 'currentOnly',
			detection: {
				order: detectionOrder,
				caches: ['localStorage'],
				lookupQuerystring: 'lang',
				lookupLocalStorage: 'locale',
        convertDetectedLanguage: (raw: string) => raw.startsWith('ja') ? 'ja-JP' : raw.startsWith('zh') ? 'zh-CN' : 'en-US'
			},
			fallbackLng: {
				fr: ['fr-FR'],
				default: fallbackDefaultLocale
			},
			ns: 'translation',
			keySeparator: false,
			nsSeparator: false,
			returnEmptyString: false,
			interpolation: {
				escapeValue: false // not needed for svelte as it escapes by default
			}
		});
};

const i18n = createI18nStore(i18next);
const isLoadingStore = createIsLoadingStore(i18next);

export const getLanguages = async () => {
	const languages = (await import(`./locales/languages.json`)).default.filter((l) => ['en-US','ja-JP','zh-CN'].includes(l.code));
	return languages;
};
export const changeLanguage = (raw: string) => {
  const lang = raw.startsWith('ja') ? 'ja-JP' : raw.startsWith('zh') ? 'zh-CN' : 'en-US';
  try { localStorage.setItem('locale',lang) } catch { /* Optional persistence. */ }
  if (i18next.language !== lang) void i18next.changeLanguage(lang);
};
if (typeof window !== 'undefined') window.addEventListener('message', (event) => {
  if (event.source !== window.parent || event.origin !== window.location.origin || event.data?.type !== 'buildstudio:locale' || !['en','ja','zh'].includes(event.data.locale)) return;
  changeLanguage(event.data.locale);
});

export default i18n;
export const isLoading = isLoadingStore;
