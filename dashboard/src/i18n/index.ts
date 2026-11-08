// Client-safe i18n surface. `server.ts` must never be re-exported here:
// it imports `next/headers`, which cannot be bundled for the browser.
export {
  DEFAULT_LOCALE,
  HTML_LANG,
  INTL_LOCALE,
  LOCALE_COOKIE,
  LOCALE_LABELS,
  LOCALES,
  isLocale,
  negotiateLocale,
  type Locale,
} from "./config";
export {
  formatCompactNumber,
  formatDate,
  formatDateTime,
  formatNumber,
  formatRelativeTime,
  type DateInput,
} from "./format";
export { messagesByLocale } from "./messages";
export type { MessageKey, Messages } from "./messages/en";
export { I18nProvider, useI18n, type I18nContextValue } from "./provider";
export { translate } from "./translate";
export type { MessageValue, PluralMessage, TranslationParams } from "./types";
