import { Metadata } from "next";

import { getServerMessage, resolveLocale } from "@/i18n/server";

import { DashboardClientLayout } from "./dashboard-client-layout";

export async function generateMetadata(): Promise<Metadata> {
  const locale = await resolveLocale();
  return {
    title: getServerMessage(locale, "meta.dashboard.title"),
    description: getServerMessage(locale, "meta.dashboard.description"),
  };
}

export default async function DashboardLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const locale = await resolveLocale();
  return (
    <DashboardClientLayout
      initialLocale={locale}
      documentTitleKey="meta.dashboard.title"
    >
      {children}
    </DashboardClientLayout>
  );
}
