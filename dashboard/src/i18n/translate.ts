import type { Locale } from "./config";
import type { MessageKey, Messages } from "./messages/en";
import type { MessageValue, TranslationParams } from "./types";

function interpolate(template: string, params?: TranslationParams): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : match,
  );
}

function resolveValue(
  messages: Messages,
  locale: Locale,
  key: MessageKey,
  params?: TranslationParams,
): MessageValue | undefined {
  const lookup = messages as Record<string, MessageValue | undefined>;

  if (typeof params?.count === "number") {
    const category = new Intl.PluralRules(locale).select(params.count);
    const categoryValue = lookup[`${key}.${category}`];
    if (categoryValue !== undefined) return categoryValue;
    const otherValue = lookup[`${key}.other`];
    if (otherValue !== undefined) return otherValue;
  }

  return lookup[key];
}

/**
 * Look up a message key, resolve simple plural forms and interpolate
 * `{placeholder}` values. Unknown keys return the key itself so missing
 * translations are visible rather than silently rendering `undefined`.
 */
export function translate(
  messages: Messages,
  locale: Locale,
  key: MessageKey,
  params?: TranslationParams,
): string {
  const value = resolveValue(messages, locale, key, params);
  if (value === undefined) return key;

  const template = typeof value === "string" ? value : value.other;
  return interpolate(template, params);
}
