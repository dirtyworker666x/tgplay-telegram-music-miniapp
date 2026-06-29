import { useEffect, useState } from "react";
import { getWebApp } from "../lib/telegram";

/**
 * True только при запуске по ссылке с ?startapp или isFullscreen.
 * Без проверки viewport — сжатая версия (меню) и полная не должны пересекаться.
 */
export function useFullscreenLaunch(): boolean {
  const [value, setValue] = useState(false);

  useEffect(() => {
    const w = getWebApp();
    if (!w) return;
    const startParam = w.initDataUnsafe?.start_param;
    if (startParam != null && startParam !== "") {
      setValue(true);
      return;
    }
    if (Boolean(w.isFullscreen)) {
      setValue(true);
    }
  }, []);

  return value;
}
