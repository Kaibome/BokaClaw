"use client";

import React from "react";
import { ThemeProvider } from "@/components/theme-provider";
import "@/styles/globals.css";
import { ClientLayout } from "./clientLayout";
import { cn } from "@/lib/utils";
import { Inter, InterDisplay, Roboto, Fustat, DMMono } from "./fonts";
import { Provider } from "react-redux";
import store from "@/store/store";
import { AuthProvider } from "@/lib/auth";
import { HTML_LANG, type Locale } from "@/i18n/config";
import type { MessageKey } from "@/i18n/messages/en";
import { I18nProvider } from "@/i18n/provider";
import dynamic from "next/dynamic";

const Toaster = dynamic(
  () =>
    import("@/components/ui/sonner").then((mod) => ({ default: mod.Toaster })),
  {
    ssr: false,
  },
);

export function DashboardClientLayout({
  initialLocale,
  documentTitleKey,
  children,
}: Readonly<{
  initialLocale: Locale;
  documentTitleKey?: MessageKey;
  children: React.ReactNode;
}>) {
  return (
    <html lang={HTML_LANG[initialLocale]} suppressHydrationWarning>
      <body
        className={cn(
          Inter.className,
          InterDisplay.variable,
          Roboto.variable,
          Fustat.variable,
          DMMono.variable,
        )}
        suppressHydrationWarning
      >
        <I18nProvider
          initialLocale={initialLocale}
          documentTitleKey={documentTitleKey}
        >
          <Provider store={store}>
            <AuthProvider>
              <ThemeProvider
                attribute="class"
                defaultTheme="light"
                enableSystem
                disableTransitionOnChange
              >
                <ClientLayout>{children}</ClientLayout>
                <Toaster />
              </ThemeProvider>
            </AuthProvider>
          </Provider>
        </I18nProvider>
      </body>
    </html>
  );
}
