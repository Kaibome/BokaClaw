import { cache } from "react";
import { cookies, headers } from "next/headers";

import {
  isLocale,
  LOCALE_COOKIE,
  negotiateLocale,
  type Locale,
} from "./config";
import type { MessageKey } from "./messages/en";
import { messagesByLocale } from "./messages";
import { translate } from "./translate";
import type { TranslationParams } from "./types";

/**
 * Resolve the request locale on the server: an explicit cookie wins, then the
 * browser's `Accept-Language`, then English. The result is cached per request
 * so a layout and its `generateMetadata` share one lookup.
 */
export const resolveLocale = cache(async (): Promise<Locale> => {
  const saved = (await cookies()).get(LOCALE_COOKIE)?.value;
  if (isLocale(saved)) return saved;

  const acceptLanguage = (await headers()).get("accept-language") ?? "";
  return negotiateLocale(acceptLanguage);
});

/** Translate a message in a server component (metadata, static copy). */
export function getServerMessage(
  locale: Locale,
  key: MessageKey,
  params?: TranslationParams,
): string {
  return translate(messagesByLocale[locale], locale, key, params);
}
