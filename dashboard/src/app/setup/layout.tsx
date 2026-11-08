import "@/styles/globals.css";
import { Metadata } from "next";
import { Inter, Fustat, Roboto, DMMono, InterDisplay } from "../(root)/fonts";
import { cn } from "@/lib/utils";
import { ThemeProvider } from "@/components/theme-provider";
import { AuthProvider } from "@/lib/auth";
import { HTML_LANG } from "@/i18n/config";
import { I18nProvider } from "@/i18n/provider";
import { getServerMessage, resolveLocale } from "@/i18n/server";

export async function generateMetadata(): Promise<Metadata> {
  const locale = await resolveLocale();
  return {
    title: getServerMessage(locale, "meta.setup.title"),
    description: getServerMessage(locale, "meta.setup.description"),
  };
}

export default async function SetupLayout({
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
        <I18nProvider
          initialLocale={locale}
          documentTitleKey="meta.setup.title"
        >
          <AuthProvider>
            <ThemeProvider
              attribute="class"
              defaultTheme="light"
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
