/**
 * OAuth redirect + PKCE: ключи sessionStorage и redirect_uri для обмена `code` на Bearer (см. api.ts).
 */
export const TELEGRAM_OAUTH_CALLBACK_PATH = "/auth/telegram/callback";

export const TG_OAUTH_STATE_KEY = "tgplay.telegram.oauth.state.v1";
export const TG_OAUTH_VERIFIER_KEY = "tgplay.telegram.oauth.verifier.v1";

export function getTelegramOAuthRedirectUri(): string {
  if (typeof window === "undefined" || !window.location?.origin) return "";
  const path = TELEGRAM_OAUTH_CALLBACK_PATH.startsWith("/")
    ? TELEGRAM_OAUTH_CALLBACK_PATH
    : `/${TELEGRAM_OAUTH_CALLBACK_PATH}`;
  return `${window.location.origin.replace(/\/$/, "")}${path}`;
}
