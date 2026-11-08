"use client";

import { useEffect, useState } from "react";
import { Sun, Moon, Monitor } from "lucide-react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";
import { LanguageSwitcher } from "@/components/i18n/language-switcher";
import { useAuth } from "@/hooks/use-auth";
import { useI18n } from "@/i18n/provider";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { AUTH_ENDPOINTS } from "@/utils/api-endpoints";

export default function SettingsPage() {
  const { user, refreshUser } = useAuth();
  const { setTheme } = useTheme();
  const { t } = useI18n();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [savingPassword, setSavingPassword] = useState(false);

  useEffect(() => {
    if (user) {
      setName(user.name);
      setEmail(user.email);
    }
  }, [user]);

  const profileDirty =
    user !== null && (name !== user.name || email !== user.email);
  const profileValid = name.trim().length > 0 && email.trim().length > 0;

  const handleSaveProfile = async () => {
    setSavingProfile(true);
    try {
      await api.patch(AUTH_ENDPOINTS.ME, {
        name: name.trim(),
        email: email.trim(),
      });
      await refreshUser();
      toast({ title: t("settings.profile.updated"), variant: "success" });
    } catch (error) {
      toast({
        title: t("settings.profile.failed"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    } finally {
      setSavingProfile(false);
    }
  };

  const handleChangePassword = async () => {
    if (newPassword !== confirmPassword) {
      toast({
        title: t("settings.password.mismatch"),
        variant: "destructive",
      });
      return;
    }

    setSavingPassword(true);
    try {
      await api.post(AUTH_ENDPOINTS.CHANGE_PASSWORD, {
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      toast({ title: t("settings.password.updated"), variant: "success" });
    } catch (error) {
      toast({
        title: t("settings.password.failed"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    } finally {
      setSavingPassword(false);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold font-fustat">
        {t("settings.title")}
      </h1>

      <Card className="border-memBorder-primary">
        <CardHeader>
          <CardTitle className="text-sm">
            {t("settings.profile.title")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="settings-name" className="text-xs">
                {t("settings.profile.name")}
              </Label>
              <Input
                id="settings-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="settings-email" className="text-xs">
                {t("settings.profile.email")}
              </Label>
              <Input
                id="settings-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
          </div>
          <Button
            onClick={handleSaveProfile}
            disabled={!profileDirty || !profileValid || savingProfile}
          >
            {savingProfile
              ? t("settings.profile.saving")
              : t("settings.profile.save")}
          </Button>
        </CardContent>
      </Card>

      <Card className="border-memBorderPrimary">
        <CardHeader>
          <CardTitle className="text-sm">
            {t("settings.password.title")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1">
            <Label htmlFor="settings-current-password" className="text-xs">
              {t("settings.password.current")}
            </Label>
            <Input
              id="settings-current-password"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="settings-new-password" className="text-xs">
                {t("settings.password.new")}
              </Label>
              <Input
                id="settings-new-password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder={t("settings.password.placeholder")}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="settings-confirm-password" className="text-xs">
                {t("settings.password.confirm")}
              </Label>
              <Input
                id="settings-confirm-password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
            </div>
          </div>
          <Button
            onClick={handleChangePassword}
            disabled={
              !currentPassword ||
              newPassword.length < 8 ||
              !confirmPassword ||
              savingPassword
            }
          >
            {savingPassword
              ? t("settings.profile.saving")
              : t("settings.password.update")}
          </Button>
        </CardContent>
      </Card>

      <Card className="border-memBorderPrimary">
        <CardHeader>
          <CardTitle className="text-sm">
            {t("settings.appearance.title")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4">
            <span className="text-sm text-onSurface-default-secondary">
              {t("settings.appearance.theme")}
            </span>
            <button
              onClick={() => setTheme("light")}
              aria-label={t("settings.appearance.light")}
              className="p-2 rounded hover:bg-surface-default-secondary-hover"
            >
              <Sun className="size-4" />
            </button>
            <button
              onClick={() => setTheme("dark")}
              aria-label={t("settings.appearance.dark")}
              className="p-2 rounded hover:bg-surface-default-secondary-hover"
            >
              <Moon className="size-4" />
            </button>
            <button
              onClick={() => setTheme("system")}
              aria-label={t("settings.appearance.system")}
              className="p-2 rounded hover:bg-surface-default-secondary-hover"
            >
              <Monitor className="size-4" />
            </button>
          </div>
        </CardContent>
      </Card>

      <Card className="border-memBorderPrimary">
        <CardHeader>
          <CardTitle className="text-sm">
            {t("settings.language.title")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-onSurface-default-secondary">
            {t("settings.language.description")}
          </p>
          <LanguageSwitcher variant="list" />
        </CardContent>
      </Card>
    </div>
  );
}
