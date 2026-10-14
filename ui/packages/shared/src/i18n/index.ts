import i18n, { createInstance, type i18n as I18nInstance } from 'i18next';
import {
  initReactI18next,
  useTranslation as useI18nextTranslation,
  UseTranslationOptions,
} from 'react-i18next';
import en from './locales/en.json';
import fr from './locales/fr.json';

export const LOCALE_STORAGE_KEY = 'sovereign-ui-locale';
export type AppLocale = 'en' | 'fr';

export function getStoredLocale(): AppLocale {
  if (typeof window === 'undefined') return 'en';
  const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
  return stored === 'fr' ? 'fr' : 'en';
}

export function setStoredLocale(locale: AppLocale): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  document.documentElement.lang = locale;
}

/** True when rendered inside an OpenShift Console dynamic plugin route. */
export function isEmbeddedConsolePlugin(): boolean {
  if (typeof window === 'undefined') return false;
  return window.location.pathname.startsWith('/hybridsovereign');
}

/**
 * OpenShift Console sets SERVER_FLAGS and shares `react-i18next` with plugins.
 * Calling initReactI18next / i18n.init on that shared module replaces the host
 * i18n singleton → raw keys like `console-app~Core platform` in the nav.
 */
export function isOpenShiftConsoleHost(): boolean {
  if (typeof window === 'undefined') return false;
  return !!(window as Window & { SERVER_FLAGS?: unknown }).SERVER_FLAGS;
}

const GLOBAL_I18N_KEY = '__HYBRIDSOVEREIGN_I18N__';

type SovereignWindow = Window & {
  [GLOBAL_I18N_KEY]?: I18nInstance;
};

function getGlobalI18n(): I18nInstance | undefined {
  if (typeof window === 'undefined') return undefined;
  return (window as SovereignWindow)[GLOBAL_I18N_KEY];
}

function setGlobalI18n(instance: I18nInstance): void {
  if (typeof window === 'undefined') return;
  (window as SovereignWindow)[GLOBAL_I18N_KEY] = instance;
}

function buildResources() {
  return {
    en: { translation: en },
    fr: { translation: fr },
  };
}

/**
 * Idempotent i18n bootstrap.
 * Standalone dashboards: default i18next + initReactI18next.
 * Console plugins: isolated createInstance() — never touch shared initReactI18next.
 */
export function initI18n(locale?: AppLocale): I18nInstance {
  const lng = locale ?? getStoredLocale();
  const existing = getGlobalI18n();
  if (existing) {
    if (existing.language !== lng) {
      void existing.changeLanguage(lng);
    }
    return existing;
  }

  if (isOpenShiftConsoleHost()) {
    // Isolated instance — do NOT call initReactI18next (shared with host).
    const isolated = createInstance();
    void isolated.init({
      resources: buildResources(),
      lng,
      fallbackLng: 'en',
      interpolation: { escapeValue: false },
      returnNull: false,
      react: {
        useSuspense: false,
      },
    });
    setGlobalI18n(isolated);
    return isolated;
  }

  i18n.use(initReactI18next).init({
    resources: buildResources(),
    lng,
    fallbackLng: 'en',
    interpolation: { escapeValue: false },
    returnNull: false,
    react: {
      useSuspense: false,
    },
  });
  setGlobalI18n(i18n);
  if (typeof document !== 'undefined') {
    document.documentElement.lang = lng;
  }
  return i18n;
}

/** Ensures translations are loaded before returning the react-i18next hook. */
export function useTranslation(ns?: string, options?: UseTranslationOptions<string>) {
  const i18nInstance = initI18n();
  return useI18nextTranslation(ns, { ...options, i18n: i18nInstance });
}

// Auto-init only for standalone dashboards — never on console host load.
if (typeof window !== 'undefined' && !isOpenShiftConsoleHost()) {
  initI18n();
}

export { i18n };
export { SovereignI18nProvider } from './SovereignI18nProvider';
export type { SovereignI18nProviderProps } from './SovereignI18nProvider';
export { LanguageToggle } from './LanguageToggle';
export type { LanguageToggleProps } from './LanguageToggle';
