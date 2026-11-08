import {
  format as formatDateFns,
  formatDistanceToNow,
  type Locale as DateFnsLocale,
} from "date-fns";
import { enUS, zhCN } from "date-fns/locale";

import { INTL_LOCALE, type Locale } from "./config";

const DATE_FNS_LOCALES: Record<Locale, DateFnsLocale> = {
  en: enUS,
  zh: zhCN,
};

export type DateInput = Date | string | number;

function toDate(value: DateInput): Date {
  return value instanceof Date ? value : new Date(value);
}

export function formatDate(
  value: DateInput,
  locale: Locale,
  pattern = "PP",
): string {
  return formatDateFns(toDate(value), pattern, {
    locale: DATE_FNS_LOCALES[locale],
  });
}

export function formatDateTime(
  value: DateInput,
  locale: Locale,
  pattern = "PPpp",
): string {
  return formatDate(value, locale, pattern);
}

export function formatRelativeTime(value: DateInput, locale: Locale): string {
  return formatDistanceToNow(toDate(value), {
    addSuffix: true,
    locale: DATE_FNS_LOCALES[locale],
  });
}

export function formatNumber(
  value: number,
  locale: Locale,
  options?: Intl.NumberFormatOptions,
): string {
  return new Intl.NumberFormat(INTL_LOCALE[locale], options).format(value);
}

export function formatCompactNumber(value: number, locale: Locale): string {
  if (!Number.isFinite(value)) return "0";
  return new Intl.NumberFormat(INTL_LOCALE[locale], {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}
