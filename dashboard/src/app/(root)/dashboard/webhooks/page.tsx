"use client";

import { LockedPage } from "@/components/self-hosted/locked-page";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/provider";

function WebhooksMockup() {
  const { t } = useI18n();

  return (
    <Card className="border-memBorder-primary">
      <CardContent className="p-6 space-y-4">
        <div className="space-y-2">
          <Label>{t("webhooks.form.endpointUrl")}</Label>
          <Input placeholder="https://your-app.com/webhooks/mem0" disabled />
        </div>
        <div className="space-y-2">
          <Label>{t("webhooks.form.events")}</Label>
          <div className="flex flex-col gap-2">
            {[
              "memory.created",
              "memory.updated",
              "memory.deleted",
              "search.performed",
            ].map((event) => (
              <div key={event} className="flex items-center gap-2">
                <Checkbox disabled checked={event === "memory.created"} />
                <Label className="text-sm font-normal">{event}</Label>
              </div>
            ))}
          </div>
        </div>
        <div className="space-y-2">
          <Label>{t("webhooks.form.signingSecret")}</Label>
          <Input placeholder="whsec_..." disabled type="password" />
        </div>
        <Button disabled>{t("webhooks.form.create")}</Button>
      </CardContent>
    </Card>
  );
}

export default function WebhooksPage() {
  const { t } = useI18n();

  return (
    <LockedPage
      title={t("webhooks.title")}
      description={t("webhooks.description")}
      previewContent={<WebhooksMockup />}
      utmMedium="dashboard-locked-webhooks"
    />
  );
}
