"use client";

import { Check, Languages } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LOCALE_LABELS, LOCALES } from "@/i18n/config";
import { useI18n } from "@/i18n/provider";
import { cn } from "@/lib/utils";

interface LanguageSwitcherProps {
  /**
   * `dropdown` matches the compact top-bar action cluster; `list` renders
   * inline buttons for the Settings page.
   */
  variant?: "dropdown" | "list";
  className?: string;
}

export function LanguageSwitcher({
  variant = "dropdown",
  className,
}: LanguageSwitcherProps) {
  const { locale, setLocale, t } = useI18n();

  if (variant === "list") {
    return (
      <div className={cn("flex items-center gap-2", className)}>
        {LOCALES.map((item) => (
          <Button
            key={item}
            type="button"
            size="sm"
            variant={locale === item ? "default" : "outline"}
            onClick={() => setLocale(item)}
            aria-pressed={locale === item}
          >
            {LOCALE_LABELS[item]}
          </Button>
        ))}
      </div>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t("common.language")}
          className={cn("flex items-center justify-center", className)}
        >
          <Languages className="size-4 shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="font-fustat">
        {LOCALES.map((item) => (
          <DropdownMenuItem
            key={item}
            onClick={() => setLocale(item)}
            className="flex cursor-pointer items-center justify-between gap-4"
          >
            <span>{LOCALE_LABELS[item]}</span>
            {locale === item && <Check className="size-4" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
