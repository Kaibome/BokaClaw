export type PluralMessage = {
  one?: string;
  other: string;
};

export type MessageValue = string | PluralMessage;

export type TranslationParams = Record<string, string | number>;
