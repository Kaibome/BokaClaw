"use client";

import { LockedPage } from "@/components/self-hosted/locked-page";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/i18n/provider";

function AnalyticsMockup() {
  const { t } = useI18n();

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {[
          { label: t("analytics.stat.totalOperations"), value: "12,847" },
          { label: t("analytics.stat.avgLatency"), value: "142ms" },
          { label: t("analytics.stat.successRate"), value: "99.7%" },
        ].map((stat) => (
          <Card key={stat.label} className="border-memBorder-primary">
            <CardContent className="p-4">
              <p className="text-xs text-onSurface-default-tertiary">
                {stat.label}
              </p>
              <p className="text-2xl font-semibold mt-1">{stat.value}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card className="border-memBorder-primary">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">
            {t("analytics.chart.title")}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-4">
          <div className="h-[200px] flex items-end gap-1">
            {[40, 65, 45, 80, 55, 90, 70, 85, 60, 95, 75, 50].map((h, i) => (
              <div
                key={i}
                className="flex-1 bg-surface-default-brand rounded-t"
                style={{ height: `${h}%` }}
              />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export default function AnalyticsPage() {
  const { t } = useI18n();

  return (
    <LockedPage
      title={t("analytics.title")}
      description={t("analytics.description")}
      previewContent={<AnalyticsMockup />}
      utmMedium="dashboard-locked-analytics"
    />
  );
}
