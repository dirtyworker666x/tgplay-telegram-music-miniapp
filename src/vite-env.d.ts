/// <reference types="vite/client" />

interface Window {
  /** OIDC виджет: ответ Telegram → index.html → fetch /api/auth/telegram. */
  tgLoginOnAuth?: (data: unknown) => void;
  /** React (App) назначает в useLayoutEffect: применить JSON ответа /api/auth/telegram. */
  tgplayApplyTelegramWebSession?: (out: Record<string, unknown>) => void;
  /** React: ошибка сети/HTTP/валидации после tgLoginOnAuth. */
  tgplayOnTelegramWebAuthError?: (code: string, detail?: unknown) => void;
}

interface ImportMetaEnv {
  /** Bot ID (client_id) из BotFather для Telegram.Login */
  readonly VITE_TELEGRAM_OAUTH_CLIENT_ID?: string;
}
