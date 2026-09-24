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
export const normalizeStudioLanguage = (raw: string) => raw.startsWith('ja') ? 'ja-JP' : raw.startsWith('zh') ? 'zh-CN' : 'en-US';
export const changeLanguage = (raw: string) => {
  const lang = normalizeStudioLanguage(raw);
  try { localStorage.setItem('locale',lang) } catch { /* Optional persistence. */ }
  if (i18next.language !== lang) void i18next.changeLanguage(lang);
};

// Per-account language (2026-09-24): the signed-in account keeps its choice in the user settings
// (settings.ui.language). It is applied after sign-in unless the URL names ?lang=, and a choice made
// in the picker while signed in is written back so the next sign-in opens in it (on any browser).
let accountSaver: ((lang: string) => Promise<unknown>) | null = null;
let accountLanguage: string | null = null;
const explicitUrlLanguage = () => {
  try { return typeof window !== 'undefined' && !!new URLSearchParams(window.location.search).get('lang'); } catch { return false; }
};
export const applyAccountLanguage = (raw: unknown, saver: ((lang: string) => Promise<unknown>) | null) => {
  accountSaver = saver;
  if (typeof raw !== 'string' || !raw) { accountLanguage = null; return; }
  accountLanguage = normalizeStudioLanguage(raw);
  if (!explicitUrlLanguage()) changeLanguage(accountLanguage);
};
export const chooseLanguage = (raw: string) => {
  const lang = normalizeStudioLanguage(raw);
  changeLanguage(lang);
  if (accountSaver && accountLanguage !== lang) {
    accountLanguage = lang;
    void accountSaver(lang).catch(() => { /* the browser keeps the choice */ });
  }
};
export const accountLanguageForTests = () => accountLanguage;
if (typeof window !== 'undefined') window.addEventListener('message', (event) => {
  if (event.source !== window.parent || event.origin !== window.location.origin || event.data?.type !== 'buildstudio:locale' || !['en','ja','zh'].includes(event.data.locale)) return;
  changeLanguage(event.data.locale);
});

export default i18n;
export const isLoading = isLoadingStore;
