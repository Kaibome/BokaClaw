"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  DEFAULT_LOCALE,
  HTML_LANG,
  LOCALE_COOKIE,
  type Locale,
} from "./config";
import {
  formatCompactNumber as formatCompactNumberValue,
  formatDate as formatDateValue,
  formatNumber as formatNumberValue,
  formatRelativeTime as formatRelativeTimeValue,
  type DateInput,
} from "./format";
import type { MessageKey } from "./messages/en";
import { messagesByLocale } from "./messages";
import { translate } from "./translate";
import type { TranslationParams } from "./types";

export interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, params?: TranslationParams) => string;
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string;
  formatCompactNumber: (value: number) => string;
  formatDate: (value: DateInput, pattern?: string) => string;
  formatDateTime: (value: DateInput, pattern?: string) => string;
  formatRelativeTime: (value: DateInput) => string;
}

interface I18nProviderProps {
  initialLocale: Locale;
  /** Optional metadata title key so a client-side switch updates `<title>`. */
  documentTitleKey?: MessageKey;
  children: ReactNode;
}

const FALLBACK: I18nContextValue = {
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (key, params) =>
    translate(messagesByLocale[DEFAULT_LOCALE], DEFAULT_LOCALE, key, params),
  formatNumber: (value, options) =>
    formatNumberValue(value, DEFAULT_LOCALE, options),
  formatCompactNumber: (value) =>
    formatCompactNumberValue(value, DEFAULT_LOCALE),
  formatDate: (value, pattern) =>
    formatDateValue(value, DEFAULT_LOCALE, pattern),
  formatDateTime: (value, pattern) =>
    formatDateValue(value, DEFAULT_LOCALE, pattern ?? "PPpp"),
  formatRelativeTime: (value) => formatRelativeTimeValue(value, DEFAULT_LOCALE),
};

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({
  initialLocale,
  documentTitleKey,
  children,
}: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale);

  // Keep the latest values available to the referentially stable callbacks
  // below. A changing `t` identity would re-fire every `useCallback` that
  // depends on it (notably the agent chat's streaming effects).
  const localeRef = useRef(locale);
  localeRef.current = locale;
  const documentTitleKeyRef = useRef(documentTitleKey);
  documentTitleKeyRef.current = documentTitleKey;

  useEffect(() => {
    setLocaleState(initialLocale);
  }, [initialLocale]);

  const t = useCallback((key: MessageKey, params?: TranslationParams) => {
    const current = localeRef.current;
    return translate(messagesByLocale[current], current, key, params);
  }, []);

  const setLocale = useCallback((next: Locale) => {
    localeRef.current = next;
    setLocaleState(next);

    if (typeof document === "undefined") return;

    document.cookie = `${LOCALE_COOKIE}=${next}; path=/; max-age=31536000; samesite=lax`;
    document.documentElement.lang = HTML_LANG[next];

    const titleKey = documentTitleKeyRef.current;
    if (titleKey) {
      document.title = translate(messagesByLocale[next], next, titleKey);
    }
  }, []);

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      t,
      formatNumber: (input, options) =>
        formatNumberValue(input, locale, options),
      formatCompactNumber: (input) => formatCompactNumberValue(input, locale),
      formatDate: (input, pattern) => formatDateValue(input, locale, pattern),
      formatDateTime: (input, pattern) =>
        formatDateValue(input, locale, pattern ?? "PPpp"),
      formatRelativeTime: (input) => formatRelativeTimeValue(input, locale),
    }),
    [locale, setLocale, t],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  // Rendering outside a provider should degrade to English rather than crash
  // the whole page (for example an isolated component in a future boundary).
  return context ?? FALLBACK;
}
