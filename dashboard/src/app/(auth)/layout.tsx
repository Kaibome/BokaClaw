import "@/styles/globals.css";
import React from "react";
import { Metadata } from "next";
import { Inter, InterDisplay, Roboto, Fustat, DMMono } from "../(root)/fonts";
import { cn } from "@/lib/utils";
import { ThemeProvider } from "@/components/theme-provider";
import { AuthProvider } from "@/lib/auth";
import { HTML_LANG } from "@/i18n/config";
import { I18nProvider } from "@/i18n/provider";
import { getServerMessage, resolveLocale } from "@/i18n/server";

export async function generateMetadata(): Promise<Metadata> {
  const locale = await resolveLocale();
  return {
    title: getServerMessage(locale, "meta.auth.title"),
    description: getServerMessage(locale, "meta.auth.description"),
  };
}

export default async function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const locale = await resolveLocale();

  return (
    <html
      lang={HTML_LANG[locale]}
      className={cn(
        Fustat.variable,
        InterDisplay.variable,
        Inter.variable,
        Roboto.variable,
        DMMono.variable,
      )}
      suppressHydrationWarning
    >
      <body className="font-fustat" suppressHydrationWarning>
        <I18nProvider initialLocale={locale} documentTitleKey="meta.auth.title">
          <AuthProvider>
            <ThemeProvider
              attribute="class"
              defaultTheme="system"
              enableSystem
              disableTransitionOnChange
            >
              {children}
            </ThemeProvider>
          </AuthProvider>
        </I18nProvider>
      </body>
    </html>
  );
}
