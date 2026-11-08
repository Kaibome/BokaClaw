"use client";

import * as React from "react";
import Link from "next/link";
import {
  Activity,
  Bot,
  ChartLine,
  ChevronDown,
  FlaskConical,
  FolderInput,
  GalleryVerticalEnd,
  KeyRound,
  Settings,
  Tags,
  Users,
  Waypoints,
  WebhookIcon,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useSelector } from "react-redux";
import { RootState } from "@/store/store";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarGroupLabel,
} from "@/components/ui/sidebar";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages/en";

interface NavItem {
  titleKey: MessageKey;
  url: string;
  icon: LucideIcon;
  active: boolean;
}

export function MainNav({
  className,
  ...props
}: React.HTMLAttributes<HTMLElement>) {
  const pathname = usePathname();
  const isSidebarCollapsed = useSelector(
    (state: RootState) => state.layout.isSidebarCollapsed,
  );
  const [isCloudOpen, setIsCloudOpen] = React.useState(true);
  const { t } = useI18n();

  const activityItems: NavItem[] = [
    {
      titleKey: "nav.agent",
      url: "/dashboard/agent",
      icon: Bot,
      active: pathname === "/dashboard/agent",
    },
    {
      titleKey: "nav.requests",
      url: "/dashboard/requests",
      icon: Activity,
      active: pathname === "/dashboard/requests",
    },
    {
      titleKey: "nav.trace",
      url: "/dashboard/trace",
      icon: Waypoints,
      active: pathname === "/dashboard/trace",
    },
    {
      titleKey: "nav.eval",
      url: "/dashboard/eval",
      icon: FlaskConical,
      active: pathname.startsWith("/dashboard/eval"),
    },
    {
      titleKey: "nav.memories",
      url: "/dashboard/memories",
      icon: GalleryVerticalEnd,
      active: pathname === "/dashboard/memories",
    },
    {
      titleKey: "nav.entities",
      url: "/dashboard/entities",
      icon: Users,
      active: pathname === "/dashboard/entities",
    },
  ];

  const cloudItems: NavItem[] = [
    {
      titleKey: "nav.categories",
      url: "/dashboard/categories",
      icon: Tags,
      active: pathname === "/dashboard/categories",
    },
    {
      titleKey: "nav.webhooks",
      url: "/dashboard/webhooks",
      icon: WebhookIcon,
      active: pathname === "/dashboard/webhooks",
    },
    {
      titleKey: "nav.analytics",
      url: "/dashboard/analytics",
      icon: ChartLine,
      active: pathname === "/dashboard/analytics",
    },
    {
      titleKey: "nav.export",
      url: "/dashboard/export",
      icon: FolderInput,
      active: pathname === "/dashboard/export",
    },
  ];

  const accountItems: NavItem[] = [
    {
      titleKey: "nav.apiKeys",
      url: "/dashboard/api-keys",
      icon: KeyRound,
      active: pathname === "/dashboard/api-keys",
    },
    {
      titleKey: "nav.configuration",
      url: "/dashboard/configuration",
      icon: Wrench,
      active: pathname === "/dashboard/configuration",
    },
    {
      titleKey: "nav.settings",
      url: "/dashboard/settings",
      icon: Settings,
      active: pathname === "/dashboard/settings",
    },
  ];

  return (
    <Sidebar
      collapsible={isSidebarCollapsed ? "icon" : undefined}
      className={cn(className, "border-r-0 w-full mb-0 bg-transparent")}
      {...props}
    >
      <SidebarContent>
        <SidebarGroup>
          <SidebarMenu className="gap-0">
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">
                    {t("nav.activity")}
                  </SidebarGroupLabel>
                )}
                {activityItems.map((item) => {
                  const label = t(item.titleKey);
                  return (
                    <SidebarMenuItem key={item.url}>
                      <SidebarMenuButton
                        asChild
                        collapsed={isSidebarCollapsed}
                        active={item.active}
                        tooltip={isSidebarCollapsed ? label : undefined}
                      >
                        <Link
                          href={item.url}
                          className={cn(
                            "flex items-center w-full",
                            isSidebarCollapsed
                              ? "justify-center mx-auto"
                              : "gap-1.5",
                          )}
                        >
                          <item.icon className="size-4 shrink-0" />
                          {!isSidebarCollapsed && <span>{label}</span>}
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </div>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <Collapsible
                open={isCloudOpen}
                onOpenChange={setIsCloudOpen}
                className="flex flex-col gap-0"
              >
                {!isSidebarCollapsed && (
                  <CollapsibleTrigger asChild>
                    <SidebarGroupLabel className="cursor-pointer mb-0">
                      {t("nav.cloudFeatures")}
                      <ChevronDown
                        className={cn(
                          "size-3 transition-transform duration-200",
                          isCloudOpen ? "" : "-rotate-90",
                        )}
                      />
                    </SidebarGroupLabel>
                  </CollapsibleTrigger>
                )}
                <CollapsibleContent className="flex flex-col gap-0">
                  {cloudItems.map((item) => {
                    const label = t(item.titleKey);
                    return (
                      <SidebarMenuItem key={item.url}>
                        <SidebarMenuButton
                          asChild
                          collapsed={isSidebarCollapsed}
                          active={item.active}
                          tooltip={isSidebarCollapsed ? label : undefined}
                        >
                          <Link
                            href={item.url}
                            className={cn(
                              "flex items-center w-full",
                              isSidebarCollapsed
                                ? "justify-center mx-auto"
                                : "gap-1.5",
                            )}
                          >
                            <item.icon className="size-4 shrink-0" />
                            {!isSidebarCollapsed && (
                              <>
                                <span>{label}</span>
                                <Badge
                                  variant="outline"
                                  className="ml-auto text-memGold-600 border-memGold-300 typo-caption-sm px-1.5 py-0"
                                >
                                  {t("nav.pro")}
                                </Badge>
                              </>
                            )}
                          </Link>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    );
                  })}
                </CollapsibleContent>
              </Collapsible>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">
                    {t("nav.account")}
                  </SidebarGroupLabel>
                )}
                {accountItems.map((item) => {
                  const label = t(item.titleKey);
                  return (
                    <SidebarMenuItem key={item.url}>
                      <SidebarMenuButton
                        asChild
                        collapsed={isSidebarCollapsed}
                        active={item.active}
                        tooltip={isSidebarCollapsed ? label : undefined}
                      >
                        <Link
                          href={item.url}
                          className={cn(
                            "flex items-center w-full",
                            isSidebarCollapsed
                              ? "justify-center mx-auto"
                              : "gap-1.5",
                          )}
                        >
                          <item.icon className="size-4 shrink-0" />
                          {!isSidebarCollapsed && <span>{label}</span>}
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </div>
            </div>
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarRail />
    </Sidebar>
  );
}
