type ProviderConfig = {
  provider?: string;
  config?: {
    model?: string;
    api_key?: string;
  };
};

export type EffectiveConfig = {
  llm?: ProviderConfig;
  embedder?: ProviderConfig;
};

// Mirrors the server defaults so switching provider never carries a
// model name that belongs to the previous provider.
export const DEFAULT_LLM_MODELS: Record<string, string> = {
  openai: "gpt-5-mini",
  anthropic: "claude-sonnet-4-6",
  gemini: "gemini-2.0-flash",
  deepseek: "deepseek-chat",
};

export const DEFAULT_EMBEDDER_MODELS: Record<string, string> = {
  openai: "text-embedding-3-small",
  gemini: "models/gemini-embedding-001",
  fastembed: "BAAI/bge-small-zh-v1.5",
};

export const getEffectiveConfig = (data: unknown): EffectiveConfig | null => {
  if (!data || typeof data !== "object") {
    return null;
  }

  const record = data as Record<string, unknown>;
  return (
    (record.effective_config as EffectiveConfig) ||
    (record.config as EffectiveConfig) ||
    (record as EffectiveConfig)
  );
};

export const buildProviderConfig = ({
  provider,
  model,
  apiKey,
}: {
  provider: string;
  model: string;
  apiKey?: string;
}) => {
  if (!provider) {
    return undefined;
  }

  return {
    provider,
    config: {
      model: model || undefined,
      api_key: apiKey || undefined,
    },
  };
};
