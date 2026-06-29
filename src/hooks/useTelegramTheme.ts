import { useEffect } from "react";
import { applyTelegramTheme } from "../lib/telegram";

function getWebApp() {
  if (typeof window === "undefined") return null;
  const w = window as Window & { Telegram?: { WebApp?: { colorScheme?: string; onEvent: (e: string, h: () => void) => void; offEvent: (e: string, h: () => void) => void } } };
  return w.Telegram?.WebApp ?? null;
}

export const useTelegramTheme = () => {
  useEffect(() => {
    applyTelegramTheme();
    const WebApp = getWebApp();
    if (WebApp) {
      try {
        WebApp.onEvent("themeChanged", applyTelegramTheme);
        return () => WebApp.offEvent("themeChanged", applyTelegramTheme);
      } catch {
        return;
      }
    }
  }, []);

  return { isDark: false };
};
