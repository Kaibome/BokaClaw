export const LOCALES = ["en", "zh"] as const;

export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "en";

export const LOCALE_COOKIE = "bokaclaw_locale";

export const LOCALE_LABELS: Record<Locale, string> = {
  en: "English",
  zh: "中文",
};

/** Value written to `<html lang>` and used for BCP-47 formatting. */
export const HTML_LANG: Record<Locale, string> = {
  en: "en",
  zh: "zh-CN",
};

/** Value used for `Intl.*` formatting. */
export const INTL_LOCALE: Record<Locale, string> = {
  en: "en-US",
  zh: "zh-CN",
};

export function isLocale(value: unknown): value is Locale {
  return (
    typeof value === "string" && (LOCALES as readonly string[]).includes(value)
  );
}

/**
 * Pick the first supported language from an `Accept-Language` header.
 * `zh-CN,zh;q=0.9,en;q=0.8` resolves to `zh`; unknown languages fall back
 * to the default locale.
 */
export function negotiateLocale(acceptLanguage: string): Locale {
  const entries = acceptLanguage
    .split(",")
    .map((part) => {
      const [rawTag, ...params] = part.trim().split(";");
      const qParam = params.find((param) => param.trim().startsWith("q="));
      const parsedQ = qParam ? Number.parseFloat(qParam.split("=")[1]) : 1;
      return {
        tag: rawTag.trim().toLowerCase(),
        q: Number.isFinite(parsedQ) ? parsedQ : 0,
      };
    })
    .filter((entry) => entry.tag.length > 0)
    .sort((a, b) => b.q - a.q);

  for (const { tag } of entries) {
    if (tag === "*") continue;
    if (tag === "zh" || tag.startsWith("zh-")) return "zh";
    if (tag === "en" || tag.startsWith("en-")) return "en";
  }

  return DEFAULT_LOCALE;
}
