"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import {
  buildProviderConfig,
  DEFAULT_EMBEDDER_MODELS,
  DEFAULT_LLM_MODELS,
  getEffectiveConfig,
} from "@/utils/self-hosted-config";
import { useAuth } from "@/hooks/use-auth";
import { useApiQuery } from "@/hooks/use-api-query";
import { useI18n } from "@/i18n/provider";

type BundledProviders = {
  llm: string[];
  embedder: string[];
};

export default function ConfigurationPage() {
  const { isAdmin } = useAuth();
  const { t } = useI18n();
  const [isSaving, setIsSaving] = useState(false);
  const [llmProvider, setLlmProvider] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [embedderProvider, setEmbedderProvider] = useState("");
  const [embedderModel, setEmbedderModel] = useState("");

  const { data: config, isLoading: isPrefilling } = useApiQuery(
    async () => {
      const res = await api.get(MEMORY_ENDPOINTS.CONFIGURE);
      return getEffectiveConfig(res.data);
    },
    { errorToast: t("configuration.failedToLoad") },
  );

  const { data: providers } = useApiQuery<BundledProviders>(
    async () => {
      const res = await api.get<BundledProviders>(
        MEMORY_ENDPOINTS.CONFIGURE_PROVIDERS,
      );
      return res.data;
    },
    { errorToast: t("configuration.failedToLoadProviders") },
  );

  useEffect(() => {
    if (!config) return;
    setLlmProvider((current) => current || config.llm?.provider || "");
    setLlmModel((current) => current || config.llm?.config?.model || "");
    setEmbedderProvider(
      (current) => current || config.embedder?.provider || "",
    );
    setEmbedderModel(
      (current) => current || config.embedder?.config?.model || "",
    );
  }, [config]);

  const handleSave = async () => {
    setIsSaving(true);

    try {
      const llm = buildProviderConfig({
        provider: llmProvider,
        model: llmModel,
        apiKey: llmApiKey,
      });
      const embedder = buildProviderConfig({
        provider: embedderProvider,
        model: embedderModel,
      });

      const newConfig: Record<string, unknown> = {
        version: "v1.1",
      };

      if (llm) {
        newConfig.llm = llm;
      }

      if (embedder) {
        newConfig.embedder = embedder;
      }

      await api.post(MEMORY_ENDPOINTS.CONFIGURE, newConfig);
      toast({ title: t("configuration.saved"), variant: "success" });
    } catch (error) {
      toast({
        title: t("configuration.saveFailed"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold font-fustat">
          {t("configuration.title")}
        </h1>
        {isPrefilling && (
          <p className="text-sm text-onSurface-default-tertiary">
            {t("configuration.loading")}
          </p>
        )}
      </div>

      <Card className="border-memBorder-primary">
        <CardHeader>
          <CardTitle className="text-sm">
            {t("configuration.llmSection")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label className="text-xs">{t("configuration.provider")}</Label>
              <Select
                value={llmProvider}
                onValueChange={(value) => {
                  setLlmProvider(value);
                  setLlmApiKey("");
                  setLlmModel(DEFAULT_LLM_MODELS[value] ?? "");
                }}
                disabled={!isAdmin || !providers}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={t("configuration.selectProvider")}
                  />
                </SelectTrigger>
                <SelectContent>
                  {providers?.llm.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">{t("configuration.model")}</Label>
              <Input
                placeholder="gpt-4.1-nano-2025-04-14"
                value={llmModel}
                onChange={(e) => setLlmModel(e.target.value)}
                disabled={!isAdmin}
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">{t("configuration.apiKey")}</Label>
            <Input
              type="password"
              placeholder="sk-..."
              value={llmApiKey}
              onChange={(e) => setLlmApiKey(e.target.value)}
              disabled={!isAdmin}
            />
          </div>
        </CardContent>
      </Card>

      <Card className="border-memBorder-primary">
        <CardHeader>
          <CardTitle className="text-sm">
            {t("configuration.embeddingSection")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label className="text-xs">{t("configuration.provider")}</Label>
              <Select
                value={embedderProvider}
                onValueChange={(value) => {
                  setEmbedderProvider(value);
                  setEmbedderModel(DEFAULT_EMBEDDER_MODELS[value] ?? "");
                }}
                disabled={!isAdmin || !providers}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={t("configuration.selectProvider")}
                  />
                </SelectTrigger>
                <SelectContent>
                  {providers?.embedder.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">{t("configuration.model")}</Label>
              <Input
                placeholder="text-embedding-3-small"
                value={embedderModel}
                onChange={(e) => setEmbedderModel(e.target.value)}
                disabled={!isAdmin}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <p className="text-xs text-onSurface-default-tertiary">
        {t("configuration.needAnotherPrefix")}
        <a
          href="https://docs.bokaclaw.ai/memory/operations"
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-4 hover:text-onSurface-default-primary"
        >
          {t("configuration.setupGuide")}
        </a>
        {t("configuration.needAnotherSuffix")}
      </p>

      {isAdmin && (
        <Button onClick={handleSave} disabled={isSaving}>
          {isSaving ? t("common.saving") : t("configuration.save")}
        </Button>
      )}
    </div>
  );
}
