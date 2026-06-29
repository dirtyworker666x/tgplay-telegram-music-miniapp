/**
 * Аналитика: события в бэкенд (заходы, клики, прослушивания, ошибки).
 * Отдельно от бота — только наш API.
 */

import { getAuthorizationHeaderValue } from "./api";

const SESSION_KEY = "tgplay_session_id";

function getApiBase(): string {
  if (typeof window !== "undefined" && window.location?.origin) return "";
  return import.meta.env.VITE_API_BASE || "http://localhost:8000";
}

function getSessionId(): string {
  if (typeof window === "undefined") return "";
  try {
    let id = sessionStorage.getItem(SESSION_KEY);
    if (!id) {
      id = "s_" + Math.random().toString(36).slice(2) + "_" + Date.now().toString(36);
      sessionStorage.setItem(SESSION_KEY, id);
    }
    return id;
  } catch {
    return "s_fallback_" + Date.now().toString(36);
  }
}

function getAuthHeader(): string {
  if (typeof window === "undefined") return "";
  return getAuthorizationHeaderValue() ?? "";
}

export type AnalyticsEvent =
  | "app_open"
  | "search"
  | "track_play"
  | "track_finish"
  | "button_add_playlist"
  | "button_add_send"
  | "button_remove"
  | "button_share_channel"
  | "button_share_chat"
  | "button_share_track"
  | "button_share_story"
  | "button_share_chat_direct"
  | "button_share_playlist"
  | "button_profile_open"
  | "button_profile_from_player"
  | "button_profile_logout_web"
  | "button_create_playlist"
  | "button_reset_search"
  | "button_recommendations_refresh"
  | "button_my_wave"
  | "button_web_login_open_bot"
  | "button_telegram_oauth_open"
  | "button_dislike_track"
  | "recommendations_load"
  | "button_download"
  | "button_add_to_favorites"
  | "button_add_to_custom_playlist"
  | "button_share_to_users"
  | "error";

/** Отправка события (fire-and-forget, не блокирует UI). */
export function trackEvent(
  event: AnalyticsEvent,
  payload?: Record<string, unknown>
): void {
  const base = getApiBase();
  const url = `${base}/api/analytics/event`;
  const sessionId = getSessionId();
  const auth = getAuthHeader();
  const body = JSON.stringify({
    event,
    payload: payload ?? {},
    session_id: sessionId,
  });
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (auth) headers["Authorization"] = auth;

  fetch(url, { method: "POST", headers, body }).catch(() => {});
}
