import type { Locale } from "../config";
import en, { type Messages } from "./en";
import zh from "./zh";

export const messagesByLocale: Record<Locale, Messages> = {
  en,
  zh,
};
