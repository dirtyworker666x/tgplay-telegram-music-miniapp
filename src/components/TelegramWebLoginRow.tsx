import { useEffect, useState } from "react";
import {
  getInitData,
  initTelegramWebLogin,
  loadTelegramOidcLoginSdk,
  openTelegramWebLogin,
} from "../lib/telegram";

type Props = {
  /** false — уже Mini App или веб-сессия; кнопку не показываем */
  show?: boolean;
};

/**
 * Официальный SDK: скрипт без data-* auto-init (там нет nonce), затем Telegram.Login.init с nonce.
 * https://core.telegram.org/bots/telegram-login
 */
export function TelegramWebLoginRow({ show = true }: Props) {
  const [loginReady, setLoginReady] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !show || getInitData()) return;
    let cancelled = false;
    setLoginReady(false);
    void (async () => {
      try {
        await loadTelegramOidcLoginSdk();
      } catch (e) {
        console.error("[TGPlay] telegram-login.js:", e);
        return;
      }
      if (cancelled) return;
      const ok = initTelegramWebLogin((data) => {
        window.tgLoginOnAuth?.(data);
      });
      if (!ok) console.error("[TGPlay] Telegram.Login.init failed (no SDK?)");
      if (!cancelled && ok) setLoginReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [show]);

  if (!show || getInitData()) return null;

  return (
    <div className="flex w-full justify-center pt-1 pb-2" data-tgplay-telegram-login-row>
      <button
        type="button"
        className="tg-auth-button"
        data-style="shine"
        disabled={!loginReady}
        title={loginReady ? undefined : "Загружается виджет входа Telegram…"}
        onClick={() => {
          if (!loginReady) return;
          openTelegramWebLogin();
        }}
      >
        {loginReady ? "Войти через Telegram" : "Подготовка входа…"}
      </button>
    </div>
  );
}
