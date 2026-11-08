"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Lock } from "lucide-react";
import { useI18n } from "@/i18n/provider";

interface LockedPageProps {
  title: string;
  description: string;
  previewContent: React.ReactNode;
}

export function LockedPage({
  title,
  description,
  previewContent,
}: LockedPageProps) {
  const { t } = useI18n();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold font-fustat flex items-center gap-2">
          {title}
          <Lock className="size-4 text-onSurface-default-tertiary" />
        </h1>
        <p className="text-sm text-onSurface-default-secondary mt-1">
          {description}
        </p>
      </div>

      <div className="opacity-60 pointer-events-none select-none">
        {previewContent}
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="flex flex-col sm:flex-row items-center gap-4 py-6">
          <div className="flex-1">
            <p className="text-sm font-medium">{t("locked.message")}</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
