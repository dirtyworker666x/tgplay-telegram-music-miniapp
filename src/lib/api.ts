import type { Track, PlaylistsResponse, PlaylistMeta, SharedPlaylistResponse } from "../types";

export { mergeLibraryArtworkIntoTracks } from "./mergeLibraryArtwork";
import { normalizeTrack } from "./normalizeTrack";
import { getShareableTrackId, getStartParamTrackId } from "./shareTrackId";

export { normalizeTrack };
import { clearTelegramOidcNonce, getInitData, getTelegramUser, peekTelegramOidcNonceForAuth } from "./telegram";
import {
  getTelegramOAuthRedirectUri,
  TELEGRAM_OAUTH_CALLBACK_PATH,
  TG_OAUTH_STATE_KEY,
  TG_OAUTH_VERIFIER_KEY,
} from "./telegramOAuthPkce";

export const WEB_SESSION_STORAGE_KEY = "tgplay.web.session.v1";

/** Устаревший флаг из старой логики приоритета Bearer; Mini App чистит при старте Telegram-авторизации. */
const WEB_AUTH_PREFERRED_KEY = "tgplay.webAuthPreferred.v1";

/** Mini App: сбросить устаревший флаг (раньше влиял на выбор заголовка Authorization). */
export function clearWebAuthPreferred(): void {
  try {
    sessionStorage.removeItem(WEB_AUTH_PREFERRED_KEY);
  } catch {
    /* ignore */
  }
}

export type WebSessionUser = { id: number; first_name: string; username?: string };

type StoredWebSession = { accessToken: string; user: WebSessionUser };

function readStoredWebSession(): StoredWebSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(WEB_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const o = JSON.parse(raw) as { accessToken?: string; user?: WebSessionUser };
    if (!o?.accessToken || o.user?.id == null) return null;
    return { accessToken: o.accessToken, user: o.user };
  } catch {
    return null;
  }
}

/** Токен веб-сессии после OAuth (Bearer), если нет Mini App initData */
export function getWebAccessToken(): string | null {
  return readStoredWebSession()?.accessToken ?? null;
}

export function setWebSession(accessToken: string, user: WebSessionUser): void {
  _sessionExpiredFired = false;
  try {
    localStorage.setItem(WEB_SESSION_STORAGE_KEY, JSON.stringify({ accessToken, user }));
  } catch {
    /* ignore quota / private mode */
  }
}

export function clearWebSession(): void {
  try {
    localStorage.removeItem(WEB_SESSION_STORAGE_KEY);
    clearWebAuthPreferred();
  } catch {
    /* ignore */
  }
}

/** Веб-сессия Bearer + следы OAuth/PKCE в sessionStorage (кнопка «Выйти» в профиле / браузер). */
export function clearTelegramWebAuthStorage(): void {
  clearWebSession();
  clearTelegramOidcNonce();
  try {
    sessionStorage.removeItem(TG_OAUTH_STATE_KEY);
    sessionStorage.removeItem(TG_OAUTH_VERIFIER_KEY);
  } catch {
    /* ignore */
  }
}

export function getStoredWebSessionUser(): WebSessionUser | null {
  return readStoredWebSession()?.user ?? null;
}

/** Для аналитики: тот же заголовок, что и в API-запросах */
export function getAuthorizationHeaderValue(): string | null {
  const initData = getInitData();
  const tok = getWebAccessToken();
  const miniUserId = getTelegramUser()?.id ?? null;
  const webUserId = getStoredWebSessionUser()?.id ?? null;
  // В PWA/браузере может остаться initData из прошлой Telegram-сессии.
  // Если есть web Bearer и user_id не совпадает (или mini user отсутствует), выбираем Bearer.
  // Иначе профайл/аудио могут уходить в "чужой" tma-контекст.
  if (tok && initData) {
    if (miniUserId == null) return `Bearer ${tok}`;
    if (webUserId != null && webUserId !== miniUserId) return `Bearer ${tok}`;
  }
  // Mini App: при совпадающем/единственном tma-контексте используем подписанный initData.
  if (initData) return `tma ${initData}`;
  if (tok) return `Bearer ${tok}`;
  return null;
}

/** Бот для ссылок шеринга (deep link) */
export const BOT_USERNAME = "tgplayxbot";

/**
 * API: относительные URL — запросы идут на тот же хост, с которого открыт Mini App.
 */
function getApiBase(): string {
  if (typeof window !== "undefined" && window.location?.origin) return "";
  return import.meta.env.VITE_API_BASE || "http://localhost:8000";
}
const API_BASE = getApiBase();

/**
 * Глобальный обработчик 401 (истёк initData / невалидная сессия).
 * App.tsx подписывается через onSessionExpired().
 */
let _sessionExpiredListeners: Array<() => void> = [];
export function onSessionExpired(cb: () => void): () => void {
  _sessionExpiredListeners.push(cb);
  return () => { _sessionExpiredListeners = _sessionExpiredListeners.filter((f) => f !== cb); };
}
let _sessionExpiredFired = false;
function _fireSessionExpired() {
  if (_sessionExpiredFired) return;
  _sessionExpiredFired = true;
  _sessionExpiredListeners.forEach((cb) => { try { cb(); } catch {} });
}

/** Был ли в запросе заголовок Authorization (гость без токена не должен ловить «сессия устарела» на 401). */
function requestInitHadAuthorization(opts: RequestInit): boolean {
  const h = opts.headers;
  if (h == null) return false;
  if (typeof Headers !== "undefined" && h instanceof Headers) {
    const v = h.get("Authorization");
    return typeof v === "string" && v.trim().length > 0;
  }
  if (Array.isArray(h)) {
    return h.some(
      ([k, v]) =>
        String(k).toLowerCase() === "authorization" && String(v ?? "").trim().length > 0,
    );
  }
  const rec = h as Record<string, string>;
  for (const key of Object.keys(rec)) {
    if (key.toLowerCase() === "authorization" && String(rec[key] ?? "").trim().length > 0) return true;
  }
  return false;
}

/** Fetch с таймаутом и retry при 503 — не виснет при медленном VPN */
const fetchWithTimeout = async (
  url: string,
  opts: RequestInit = {},
  timeoutMs = 18000,
  retries = 2,
): Promise<Response> => {
  for (let attempt = 0; attempt <= retries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const externalSignal = opts.signal;
    if (externalSignal) {
      if (externalSignal.aborted) { clearTimeout(timer); throw new DOMException("Aborted", "AbortError"); }
      externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
    }
    try {
      const resp = await fetch(url, { ...opts, signal: controller.signal });
      clearTimeout(timer);
      // 401 при отправленной сессии — протухший tma/Bearer; гость без Authorization не трогаем
      if (resp.status === 401 && requestInitHadAuthorization(opts)) {
        _fireSessionExpired();
        return resp;
      }
      if (resp.status === 503 && attempt < retries) {
        await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
        continue;
      }
      return resp;
    } catch (err) {
      clearTimeout(timer);
      if (externalSignal?.aborted) throw err;
      if (attempt < retries) {
        await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
        continue;
      }
      throw err;
    }
  }
  throw new Error("Request failed after retries");
};

const authHeaders = (): Record<string, string> => {
  const auth = getAuthorizationHeaderValue();
  if (auth) {
    return {
      Accept: "application/json",
      Authorization: auth,
      "Content-Type": "application/json",
    };
  }
  return { Accept: "application/json" };
};

/**
 * Ранняя регистрация numeric Telegram user id на бэкенде (рассылки).
 * Дублирует заголовок Authorization на других запросах, но срабатывает до первого поиска.
 */
export async function registerMiniAppIdentity(): Promise<void> {
  const auth = getAuthorizationHeaderValue();
  const initData = getInitData();
  if (!auth) return;
  try {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: auth,
    };
    const body: Record<string, string> = {};
    if (initData && auth.startsWith("tma ")) {
      body.initData = initData;
    }
    await fetchWithTimeout(
      `${API_BASE}/api/me/register`,
      { method: "POST", headers, body: JSON.stringify(body) },
      8000,
      1,
    );
  } catch {
    /* fire-and-forget */
  }
}

/** Аватар профиля через бэкенд (Bot API); без VPN прямые photo_url из Mini App часто не открываются. */
export async function fetchMyProfilePhotoBlob(expectedUserId?: number): Promise<Blob | null> {
  const webToken = getWebAccessToken();
  const webUserId = getStoredWebSessionUser()?.id ?? null;
  let auth = getAuthorizationHeaderValue();
  // В PWA при авторизации через Telegram нужен именно текущий Bearer-пользователь,
  // иначе stale initData может подтянуть фото прошлого аккаунта.
  if (
    webToken &&
    typeof expectedUserId === "number" &&
    webUserId != null &&
    webUserId === expectedUserId
  ) {
    auth = `Bearer ${webToken}`;
  }
  if (!auth) return null;
  try {
    const params = new URLSearchParams();
    if (typeof expectedUserId === "number") params.set("expected_user_id", String(expectedUserId));
    params.set("_", String(Date.now()));
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/me/photo?${params.toString()}`,
      {
        method: "GET",
        cache: "no-store",
        headers: {
          Authorization: auth,
          Accept: "image/*,*/*",
          "Cache-Control": "no-cache",
          Pragma: "no-cache",
        },
      },
      12000,
      0,
    );
    if (!resp.ok) return null;
    return await resp.blob();
  } catch {
    return null;
  }
}

// ─── Search ─────────────────────────────────────────────────────

const SEARCH_FIRST_PAGE = 100;
const SEARCH_PAGE_SIZE = 20;

export class SearchRateLimitedError extends Error {
  retryAfterSec: number | null;
  constructor(message: string, retryAfterSec: number | null) {
    super(message);
    this.name = "SearchRateLimitedError";
    this.retryAfterSec = retryAfterSec;
  }
}

/**
 * Взвешенный jitter для retry после 429: предотвращает одновременный шторм запросов
 * от всех клиентов после истечения одного и того же retry_after_sec.
 *
 * Распределение (в секундах):
 *   - 40% → 0.02–0.07 s  (быстрые клиенты, небольшой разброс)
 *   - 40% → 0.07–0.15 s  (средние)
 *   - 15% → 0.15–0.30 s  (медленные)
 *   - 5%  → 0.00–0.01 s  (случайный «нулевой» слот)
 */
export function rateLimitJitterSec(): number {
  const r = Math.random();
  if (r < 0.05) return Math.random() * 0.01;
  if (r < 0.45) return 0.02 + Math.random() * 0.05;
  if (r < 0.85) return 0.07 + Math.random() * 0.08;
  return 0.15 + Math.random() * 0.15;
}

export const searchTracks = async (
  query: string,
  signal?: AbortSignal,
  opts: { limit?: number; offset?: number; artistCatalog?: boolean } = {},
): Promise<Track[]> => {
  const trimmed = query.trim();
  const artistCatalog = !!opts.artistCatalog;
  const minLen = artistCatalog ? 2 : 3;
  if (!trimmed || trimmed.length < minLen) return [];
  const limit = opts.limit ?? (artistCatalog ? 600 : SEARCH_FIRST_PAGE);
  const offset = opts.offset ?? 0;

  const params = new URLSearchParams({
    q: trimmed,
    limit: String(limit),
    offset: String(offset),
  });
  if (artistCatalog) params.set("artist_catalog", "1");

  const response = await fetchWithTimeout(
    `${API_BASE}/api/music/search?${params}`,
    { method: "GET", headers: authHeaders(), signal },
    18000,
    2,
  );

  if (!response.ok) {
    if (response.status === 429) {
      let retryAfter: number | null = null;
      let detail: string | null = null;
      try {
        const data = await response.json();
        const anyData = data as any;
        const v = anyData?.retry_after_sec;
        if (typeof v === "number" && Number.isFinite(v) && v > 0) retryAfter = v;
        if (typeof anyData?.detail === "string" && anyData.detail.length > 0) detail = anyData.detail;
      } catch {
        // ignore JSON parse error, fallback to null
      }
      // Если сервер не передал retry_after_sec, значит это "жёсткий" 429 (например дневной лимит) — показываем как обычную ошибку без кулдауна.
      if (retryAfter == null) {
        throw new Error(detail ?? "Search failed: 429");
      }
      // Добавляем взвешенный jitter (0–300ms), чтобы клиенты не ломились одновременно после одного и того же окна.
      throw new SearchRateLimitedError(detail ?? "Search rate limited", retryAfter + rateLimitJitterSec());
    }
    throw new Error(`Search failed: ${response.status}`);
  }

  const data = await response.json();
  const items = Array.isArray(data) ? data : (data.items ?? data.tracks ?? data.results ?? []);
  return (items as Record<string, unknown>[])
    .map(normalizeTrack)
    .filter((t) => t.id.length > 0);
};

export { SEARCH_FIRST_PAGE, SEARCH_PAGE_SIZE };

/** Рекомендации VK: один seed или до 3 через запятую на бэкенде (merge round-robin). */
export async function fetchRecommendations(
  seedOrSeeds: string | readonly string[],
  signal?: AbortSignal,
  limit = 40,
): Promise<Track[]> {
  const arr = (Array.isArray(seedOrSeeds) ? [...seedOrSeeds] : [seedOrSeeds])
    .map((s) => String(s).trim())
    .filter(Boolean);
  if (!arr.length) return [];
  const params = new URLSearchParams({ limit: String(limit) });
  if (arr.length === 1) {
    params.set("seed", arr[0]);
  } else {
    params.set("seeds", arr.slice(0, 3).join(","));
  }
  const response = await fetchWithTimeout(
    `${API_BASE}/api/music/recommendations?${params}`,
    { method: "GET", headers: authHeaders(), signal },
    18000,
    2,
  );
  if (!response.ok) return [];
  const data = (await response.json()) as { items?: unknown[] };
  const items = Array.isArray(data.items) ? data.items : [];
  return (items as Record<string, unknown>[])
    .map(normalizeTrack)
    .filter((t) => t.id.length > 0);
}

export type PersonalRecommendationsOpts = {
  limit?: number;
  /** Другая выборка seed по избранному (и для волны — всегда новый микс). */
  refresh?: boolean;
  /** Длинная лента для «Моя волна» (сервер поднимает минимум ~80 треков). */
  wave?: boolean;
};

/** Персональные рекомендации (избранное + аналитика). Нужен `tma` (Mini App) или Bearer (сайт/PWA). */
export async function fetchPersonalRecommendations(
  signal?: AbortSignal,
  limitOrOpts: number | PersonalRecommendationsOpts = 100,
): Promise<Track[]> {
  if (!getAuthorizationHeaderValue()) return [];
  let limit = 100;
  let refresh = false;
  let wave = false;
  if (typeof limitOrOpts === "number") {
    limit = limitOrOpts;
  } else {
    wave = !!limitOrOpts.wave;
    refresh = !!limitOrOpts.refresh;
    limit = limitOrOpts.limit ?? 100;
  }
  const params = new URLSearchParams({ limit: String(limit) });
  if (refresh) params.set("refresh", "1");
  if (wave) params.set("wave", "1");
  const response = await fetchWithTimeout(
    `${API_BASE}/api/music/recommendations/personal?${params}`,
    { method: "GET", headers: authHeaders(), signal },
    22000,
    2,
  );
  if (response.status === 401) return [];
  if (!response.ok) {
    if (import.meta.env.DEV) {
      console.warn("[tgplay] GET /api/music/recommendations/personal", response.status);
    }
    return [];
  }
  const data = (await response.json()) as { items?: unknown[] };
  const items = Array.isArray(data.items) ? data.items : [];
  return (items as Record<string, unknown>[])
    .map(normalizeTrack)
    .filter((t) => t.id.length > 0);
}

// ─── Audio URL resolution ─────────────────────────────────────

/** Кеш resolved URL (track_id → { url, ts }) — в памяти */
const _urlCache = new Map<string, { url: string; ts: number }>();
const _URL_TTL = 20 * 60_000; // 20 мин
const _URL_CACHE_LS_PREFIX = "tgplay_audio_url_";

function _extractYouTubeVideoId(raw: string): string | null {
  const s = (raw || "").trim();
  if (!s) return null;
  if (/^[\w-]{11}$/.test(s)) return s;
  const m = s.match(/(?:v=|youtu\.be\/|shorts\/)([\w-]{11})/i);
  return m?.[1] ?? null;
}

function _isSoundCloudPreviewStreamUrl(url: string): boolean {
  const u = (url || "").toLowerCase();
  return u.includes("cf-preview-media") || u.includes("/preview/") || u.includes("preview_mp3");
}

function _audioCacheKey(trackId: string): string {
  const tid = (trackId || "").trim();
  if (/^sc:\d+$/.test(tid)) return `sc-v3:${tid}`;
  const yt = _extractYouTubeVideoId(tid);
  if (yt) return `yt:${yt}`;
  return `id:${tid}`;
}

function _readUrlCacheFromStorage(cacheKey: string, legacyTrackId?: string): { url: string; ts: number } | null {
  if (typeof window === "undefined") return null;
  const keys = [`${_URL_CACHE_LS_PREFIX}${cacheKey}`];
  if (legacyTrackId && legacyTrackId !== cacheKey) {
    keys.push(`${_URL_CACHE_LS_PREFIX}${legacyTrackId}`);
  }
  try {
    for (const lsKey of keys) {
      const raw = window.localStorage.getItem(lsKey);
      if (!raw) continue;
      const parsed = JSON.parse(raw) as { url?: string; ts?: number };
      if (!parsed || typeof parsed.url !== "string" || !parsed.url.trim() || typeof parsed.ts !== "number") {
        continue;
      }
      if (Date.now() - parsed.ts > _URL_TTL) {
        window.localStorage.removeItem(lsKey);
        continue;
      }
      if (_isSoundCloudPreviewStreamUrl(parsed.url)) {
        window.localStorage.removeItem(lsKey);
        continue;
      }
      if (lsKey !== `${_URL_CACHE_LS_PREFIX}${cacheKey}`) {
        // Миграция старых ключей (track_id/url) в канонический формат.
        window.localStorage.setItem(
          `${_URL_CACHE_LS_PREFIX}${cacheKey}`,
          JSON.stringify({ url: parsed.url, ts: parsed.ts }),
        );
      }
      return { url: parsed.url, ts: parsed.ts };
    }
    return null;
  } catch {
    return null;
  }
}

function _writeUrlCacheToStorage(cacheKey: string, url: string, ts: number): void {
  if (typeof window === "undefined") return;
  try {
    const payload = JSON.stringify({ url, ts });
    window.localStorage.setItem(`${_URL_CACHE_LS_PREFIX}${cacheKey}`, payload);
  } catch {
    // Игнорируем quota / private mode
  }
}

function _deleteUrlCacheFromStorage(cacheKey: string, legacyTrackId?: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(`${_URL_CACHE_LS_PREFIX}${cacheKey}`);
    if (legacyTrackId && legacyTrackId !== cacheKey) {
      window.localStorage.removeItem(`${_URL_CACHE_LS_PREFIX}${legacyTrackId}`);
    }
  } catch {
    // ignore
  }
}

/** Метаданные трека для резолва старых VK id (избранное) — fallback через YouTube Music. */
export type ResolveTrackMeta = { title?: string; artist?: string };
type ResolveTrackOptions = { nextTrackId?: string };

/** Совпадает с бэкендом `_VK_TRACK_ID_RE`. */
export function looksLikeVkTrackId(id: string): boolean {
  return /^-?\d+_\d+(?:_[a-zA-Z0-9_-]+)?$/.test((id || "").trim());
}

/**
 * title/artist для VK id — как раньше: и избранное без поля, и поиск (fallback YTM на бэке при мёртвом VK).
 * Исключение: явное `vk_legacy: true` — только VK, без query (редкие записи).
 */
export function youtubeResolveMetaForTrack(track: Track): ResolveTrackMeta | undefined {
  if (!looksLikeVkTrackId(track.id)) return undefined;
  if (track.vk_legacy === true) return undefined;
  return { title: track.title, artist: track.artist };
}

/** После загрузки избранного: один батч VK resolve в фоне — первый тап по треку не ждёт холодный getById. */
export function prewarmVkTrackUrlsFromPlaylist(tracks: Track[], maxIds = 35): void {
  // Исторически грели только VK id, но для PWA это оставляло YouTube-треки "холодными"
  // и первый play мог ждать yt-dlp/direct-url извлечение. Греем обе группы.
  const ids = tracks
    .slice(0, maxIds)
    .map((t) => t.id)
    .filter((id): id is string => typeof id === "string" && id.trim().length > 0);
  if (ids.length) void preloadBatchUrls(ids);
}

function _resolveQueryString(meta?: ResolveTrackMeta, extra?: Record<string, string>): string {
  const params = new URLSearchParams();
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v) params.set(k, v);
    }
  }
  if (meta?.title?.trim()) params.set("title", meta.title.trim());
  if (meta?.artist?.trim()) params.set("artist", meta.artist.trim());
  const s = params.toString();
  return s ? `?${s}` : "";
}

/**
 * Получает прямой VK CDN URL через бэкенд /resolve.
 * Клиент потом грузит аудио напрямую с VK — без прокси через туннель.
 *
 * Для максимальной скорости старта:
 * - Даже если VK отдаёт HLS (.m3u8), для Mini App используем ПРЯМОЙ
 *   VK CDN URL (HTMLAudioElement в мобильных WebView умеет HLS).
 * - Прокси `/api/music/download` используем только как Fallback
 *   (см. catch в `App.tsx`), чтобы не тянуть весь аудиопоток через туннель.
 */
export const resolveAudioUrl = async (
  trackId: string,
  meta?: ResolveTrackMeta,
  opts?: ResolveTrackOptions,
): Promise<string> => {
  const now = Date.now();
  const cacheKey = _audioCacheKey(trackId);
  // Проверяем кеш в памяти (битые записи с пустым url не считаем — иначе Promise<string> даёт null без fetch)
  const cachedMem = _urlCache.get(cacheKey);
  if (cachedMem && now - cachedMem.ts < _URL_TTL) {
    const u = typeof cachedMem.url === "string" ? cachedMem.url.trim() : "";
    if (u && !_isSoundCloudPreviewStreamUrl(u)) return cachedMem.url;
    _urlCache.delete(cacheKey);
  }

  // Проверяем персистентный кеш (localStorage) — гидратируем в память при попадании
  const cachedStored = _readUrlCacheFromStorage(cacheKey, trackId);
  if (cachedStored && now - cachedStored.ts < _URL_TTL) {
    if (!_isSoundCloudPreviewStreamUrl(cachedStored.url)) {
      _urlCache.set(cacheKey, cachedStored);
      return cachedStored.url;
    }
    _deleteUrlCacheFromStorage(cacheKey, trackId);
  }

  // Запрос к бэкенду (маленький JSON, ~200 байт через туннель)
  // Не цепляем title/artist к URL с youtube.com — длинный GET ломает nginx/прокси и не нужен бэкенду.
  const qs = _resolveQueryString(looksLikeVkTrackId(trackId) ? meta : undefined, {
    next_id: (opts?.nextTrackId || "").trim(),
  });
  const resp = await fetchWithTimeout(
    `${API_BASE}/api/music/resolve/${encodeURIComponent(trackId)}${qs}`,
    { method: "GET", headers: authHeaders() },
    14000,
    1,
  );

  if (!resp.ok) {
    if (resp.status === 429) {
      let retryAfterSec: number | null = null;
      let detail = "Too Many Requests";
      try {
        const data = await resp.json();
        const anyData = data as Record<string, unknown>;
        const v = anyData.retry_after_sec;
        if (typeof v === "number" && Number.isFinite(v) && v > 0) retryAfterSec = v;
        if (typeof anyData.detail === "string" && anyData.detail.trim()) detail = anyData.detail;
      } catch {
        // ignore parse errors
      }
      throw new AudioResolveRateLimitedError(detail, retryAfterSec);
    }
    throw new Error(`Resolve failed: ${resp.status}`);
  }
  const data = await resp.json();
  const url = typeof data.url === "string" ? data.url.trim() : "";
  if (!url) throw new Error("Resolve failed: empty url");

  // Для Mini App всегда предпочитаем прямой VK CDN URL —
  // так старт воспроизведения максимально быстрый и не грузим туннель.
  const ts = Date.now();
  _urlCache.set(cacheKey, { url, ts });
  _writeUrlCacheToStorage(cacheKey, url, ts);
  return url;
};

/** Синхронно возвращает URL из кеша — мгновенный старт без запроса. */
export const getCachedAudioUrl = (trackId: string): string | null => {
  const now = Date.now();
  const cacheKey = _audioCacheKey(trackId);
  const cachedMem = _urlCache.get(cacheKey);
  if (cachedMem && now - cachedMem.ts < _URL_TTL) {
    const u = typeof cachedMem.url === "string" ? cachedMem.url.trim() : "";
    if (u && !_isSoundCloudPreviewStreamUrl(u)) return cachedMem.url;
    _urlCache.delete(cacheKey);
  }

  const stored = _readUrlCacheFromStorage(cacheKey, trackId);
  if (stored && now - stored.ts < _URL_TTL && !_isSoundCloudPreviewStreamUrl(stored.url)) {
    _urlCache.set(cacheKey, stored);
    return stored.url;
  }
  return null;
};

/** Сбросить кеш URL трека (после ошибки загрузки — повтор с новым resolve). */
export const invalidateAudioUrlCache = (trackId: string): void => {
  const cacheKey = _audioCacheKey(trackId);
  _urlCache.delete(cacheKey);
  _deleteUrlCacheFromStorage(cacheKey, trackId);
};

/**
 * Получить свежую ссылку: бэкенд сбрасывает кэш в Redis и тянет новый URL из VK.
 * Использовать при ошибке загрузки аудио (мёртвая ссылка) — незаметный retry для пользователя.
 */
export const resolveAudioUrlWithRefresh = async (
  trackId: string,
  meta?: ResolveTrackMeta,
  opts?: ResolveTrackOptions,
): Promise<string> => {
  const cacheKey = _audioCacheKey(trackId);
  _urlCache.delete(cacheKey);
  const qs = _resolveQueryString(looksLikeVkTrackId(trackId) ? meta : undefined, {
    refresh: "1",
    next_id: (opts?.nextTrackId || "").trim(),
  });
  const resp = await fetchWithTimeout(
    `${API_BASE}/api/music/resolve/${encodeURIComponent(trackId)}${qs}`,
    { method: "GET", headers: authHeaders() },
    14000,
    1,
  );
  if (!resp.ok) {
    if (resp.status === 429) {
      let retryAfterSec: number | null = null;
      let detail = "Too Many Requests";
      try {
        const data = await resp.json();
        const anyData = data as Record<string, unknown>;
        const v = anyData.retry_after_sec;
        if (typeof v === "number" && Number.isFinite(v) && v > 0) retryAfterSec = v;
        if (typeof anyData.detail === "string" && anyData.detail.trim()) detail = anyData.detail;
      } catch {
        // ignore parse errors
      }
      throw new AudioResolveRateLimitedError(detail, retryAfterSec);
    }
    throw new Error(`Resolve failed: ${resp.status}`);
  }
  const data = await resp.json();
  const url = typeof data.url === "string" ? data.url.trim() : "";
  if (!url) throw new Error("Resolve failed: empty url");
  const ts = Date.now();
  _urlCache.set(cacheKey, { url, ts });
  _writeUrlCacheToStorage(cacheKey, url, ts);
  return url;
};

/**
 * Предзагружает URL трека в кеш (fire & forget).
 */
export const preloadTrackUrl = (trackId: string, meta?: ResolveTrackMeta) => {
  const cacheKey = _audioCacheKey(trackId);
  const hit = _urlCache.get(cacheKey);
  if (hit && typeof hit.url === "string" && hit.url.trim() && Date.now() - hit.ts < _URL_TTL) return;
  resolveAudioUrl(trackId, meta)
    .then((url) => {
      _preloadAudioBytes(url);
    })
    .catch(() => {});
};

export class AudioResolveRateLimitedError extends Error {
  retryAfterSec: number | null;
  constructor(message: string, retryAfterSec: number | null) {
    super(message);
    this.name = "AudioResolveRateLimitedError";
    this.retryAfterSec = retryAfterSec;
  }
}

const _looksLikeYoutubeTrackId = (id: string) => Boolean(_extractYouTubeVideoId(id));

/**
 * Предзагружает URLs пачки треков через batch endpoint (1 API-вызов на до 25 треков).
 * YouTube URL не проходят VK batch — для них параллельный resolve + prefetch первых байтов.
 */
export const preloadBatchUrls = async (trackIds: string[]) => {
  const uncached = trackIds.filter((id) => {
    const key = _audioCacheKey(id);
    const hit = _urlCache.get(key);
    if (!hit) {
      const stored = _readUrlCacheFromStorage(key, id);
      if (stored && Date.now() - stored.ts < _URL_TTL) {
        _urlCache.set(key, stored);
        return false;
      }
      return true;
    }
    const fresh = Date.now() - hit.ts < _URL_TTL;
    const u = typeof hit.url === "string" ? hit.url.trim() : "";
    if (fresh && u) return false;
    if (hit) _urlCache.delete(key);
    return true;
  });
  if (uncached.length === 0) return;
  const ytIds = uncached.filter(_looksLikeYoutubeTrackId);
  const vkIds = uncached.filter((id) => !_looksLikeYoutubeTrackId(id));

  if (vkIds.length > 0) {
    try {
      const resp = await fetchWithTimeout(
        `${API_BASE}/api/music/resolve-batch`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ ids: vkIds }),
        },
        20000,
        1,
      );
      if (resp.ok) {
        const data: Record<string, { url: string; hls: boolean }> = await resp.json();
        const ts = Date.now();
        for (const [tid, info] of Object.entries(data)) {
          const u = typeof info?.url === "string" ? info.url.trim() : "";
          if (!u) continue;
          const key = _audioCacheKey(tid);
          _urlCache.set(key, { url: u, ts });
          _writeUrlCacheToStorage(key, u, ts);
          _preloadAudioBytes(u);
        }
      }
    } catch (err) {
      console.error("preloadBatchUrls (VK) failed:", err);
      for (const id of vkIds) {
        resolveAudioUrl(id)
          .then((url) => {
            _preloadAudioBytes(url);
          })
          .catch(() => {});
      }
    }
  }

  if (ytIds.length > 0) {
    // Параллельный resolve десятков YouTube-треков бьёт бэкенд (yt-dlp / youtube-direct) и даёт таймауты
    // основному play(); фоном — небольшими пачками, без блокировки вызывающего кода.
    const slice = ytIds.slice(0, 12);
    void (async () => {
      for (let i = 0; i < slice.length; i += 2) {
        const chunk = slice.slice(i, i + 2);
        await Promise.all(
          chunk.map((id) =>
            resolveAudioUrl(id)
              .then((url) => {
                _preloadAudioBytes(url);
              })
              .catch(() => {}),
          ),
        );
      }
    })();
  }
};

/**
 * Предзагрузка только для наших /api/* URL (youtube-direct, download и т.д.).
 * Внешние VK CDN не трогаем — любой Range/fetch к ним легко ломает последующий <audio src>.
 */
/** Range-prefetch к youtube-direct конкурирует с <audio> по тому же URL и замедляет старт — не делаем. */
function _shouldRangePrefetchOurAudioUrl(proxyUrl: string): boolean {
  if (!proxyUrl) return false;
  const p = proxyUrl.includes("://") ? (() => {
    try {
      return new URL(proxyUrl).pathname;
    } catch {
      return proxyUrl;
    }
  })() : proxyUrl;
  return !p.includes("youtube-direct");
}

const _preloadAudioBytes = (proxyUrl: string) => {
  if (!proxyUrl || !_shouldRangePrefetchOurAudioUrl(proxyUrl)) return;
  const fullUrl = proxyUrl.startsWith("/api") ? `${API_BASE}${proxyUrl}` : proxyUrl;

  let isOurApi = proxyUrl.startsWith("/api");
  if (!isOurApi && typeof window !== "undefined" && proxyUrl.startsWith("http")) {
    try {
      const u = new URL(fullUrl);
      isOurApi = u.origin === window.location.origin && u.pathname.startsWith("/api/");
    } catch {
      isOurApi = false;
    }
  }
  if (!isOurApi) return;

  const headers: Record<string, string> = { Range: "bytes=0-400000" };
  const auth = getAuthorizationHeaderValue();
  if (auth) headers.Authorization = auth;

  fetch(fullUrl, {
    method: "GET",
    headers,
    priority: "low" as RequestPriority,
  }).catch(() => {});
};

/** Fallback URL через прокси (для обратной совместимости) */
export const getDownloadUrl = (id: string, meta?: ResolveTrackMeta) =>
  `${API_BASE}/api/music/download/${encodeURIComponent(id)}${_resolveQueryString(looksLikeVkTrackId(id) ? meta : undefined)}`;

// ─── Auth ───────────────────────────────────────────────────────

export const loginTelegram = async () => {
  const initData = getInitData();
  if (!initData) return null;
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initData }),
    }, 8000, 1);
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.user as { id: number; first_name: string; username?: string } | null;
  } catch (err) {
    console.error("loginTelegram failed:", err);
    return null;
  }
};

/**
 * Обмен OIDC id_token (Telegram.Login) на сессионный Bearer для API.
 * Без глобального onSessionExpired при 401 — ошибка только вызывающему коду.
 */
/** Серверный logout (JWT stateless — смысл в единой точке и будущем revoke). Клиент всё равно чистит storage. */
export async function postAuthLogout(): Promise<void> {
  if (typeof window === "undefined") return;
  try {
    await fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
  } catch {
    /* ignore */
  }
}

export async function exchangeTelegramWebIdToken(
  idToken: string,
  user?: Record<string, unknown>,
): Promise<{ user: WebSessionUser; access_token: string; expires_in: number } | null> {
  const trimmed = idToken.trim();
  if (!trimmed) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  const nonce = peekTelegramOidcNonceForAuth();
  try {
    const resp = await fetch(`${API_BASE}/api/auth/telegram`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        id_token: trimmed,
        ...(user && Object.keys(user).length ? { user } : {}),
        ...(nonce ? { nonce } : {}),
      }),
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) {
      clearTelegramOidcNonce();
      return null;
    }
    const data = (await resp.json()) as {
      access_token?: string;
      expires_in?: number;
      user?: { id: number; first_name?: string; username?: string };
    };
    if (!data.access_token || data.user?.id == null) {
      clearTelegramOidcNonce();
      return null;
    }
    clearTelegramOidcNonce();
    const u: WebSessionUser = {
      id: data.user.id,
      first_name: typeof data.user.first_name === "string" ? data.user.first_name : "",
      username: data.user.username,
    };
    return { user: u, access_token: data.access_token, expires_in: typeof data.expires_in === "number" ? data.expires_in : 0 };
  } catch (err) {
    clearTimeout(timer);
    console.error("exchangeTelegramWebIdToken failed:", err);
    return null;
  }
}

export type TelegramOAuthRedirectResult =
  | { kind: "noop" }
  | { kind: "error"; message: string }
  | { kind: "success"; access_token: string; user: WebSessionUser; expires_in: number };

/** Один общий Promise на колбэк-URL — иначе React StrictMode теряет PKCE при двойном mount. */
let _tgOauthCallbackPromise: Promise<TelegramOAuthRedirectResult> | null = null;

export async function exchangeTelegramOAuthCode(
  code: string,
  redirectUri: string,
  codeVerifier: string,
): Promise<{ user: WebSessionUser; access_token: string; expires_in: number } | null> {
  const trimmed = code.trim();
  if (!trimmed || !redirectUri.trim() || !codeVerifier.trim()) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  try {
    const resp = await fetch(`${API_BASE}/api/auth/telegram/code`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        code: trimmed,
        redirect_uri: redirectUri.trim(),
        code_verifier: codeVerifier.trim(),
      }),
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) return null;
    const data = (await resp.json()) as {
      access_token?: string;
      expires_in?: number;
      user?: { id: number; first_name?: string; username?: string };
    };
    if (!data.access_token || data.user?.id == null) return null;
    const u: WebSessionUser = {
      id: data.user.id,
      first_name: typeof data.user.first_name === "string" ? data.user.first_name : "",
      username: data.user.username,
    };
    return {
      user: u,
      access_token: data.access_token,
      expires_in: typeof data.expires_in === "number" ? data.expires_in : 0,
    };
  } catch (err) {
    clearTimeout(timer);
    console.error("exchangeTelegramOAuthCode failed:", err);
    return null;
  }
}

/**
 * Если открыт /auth/telegram/callback с code или error — обменять code на сессию TGPlay.
 * Singleton Promise — React StrictMode не должен запускать второй обмен и сбрасывать PKCE.
 */
export function tryFinishTelegramOAuthRedirect(): Promise<TelegramOAuthRedirectResult> {
  if (typeof window === "undefined") return Promise.resolve({ kind: "noop" });
  let pathname = window.location.pathname;
  if (pathname.length > 1 && pathname.endsWith("/")) pathname = pathname.slice(0, -1);
  if (pathname !== TELEGRAM_OAUTH_CALLBACK_PATH) return Promise.resolve({ kind: "noop" });
  if (_tgOauthCallbackPromise) return _tgOauthCallbackPromise;

  const clearPkceStorage = () => {
    try {
      sessionStorage.removeItem(TG_OAUTH_STATE_KEY);
      sessionStorage.removeItem(TG_OAUTH_VERIFIER_KEY);
    } catch {
      /* ignore */
    }
  };

  _tgOauthCallbackPromise = (async (): Promise<TelegramOAuthRedirectResult> => {
    try {
      const sp = new URLSearchParams(window.location.search);
      const oauthErr = sp.get("error");
      if (oauthErr) {
        let desc = sp.get("error_description") || oauthErr;
        try {
          desc = decodeURIComponent(desc.replace(/\+/g, " "));
        } catch {
          /* keep */
        }
        clearPkceStorage();
        return {
          kind: "error",
          message: desc.length > 220 ? "Вход через Telegram отменён" : desc,
        };
      }

      const code = sp.get("code") || "";
      const state = sp.get("state") || "";
      let expected: string | null = null;
      let verifier: string | null = null;
      try {
        expected = sessionStorage.getItem(TG_OAUTH_STATE_KEY);
        verifier = sessionStorage.getItem(TG_OAUTH_VERIFIER_KEY);
      } catch {
        /* ignore */
      }

      if (!code || !state || !expected || !verifier || state !== expected) {
        clearPkceStorage();
        return {
          kind: "error",
          message: "Сессия входа устарела или неверна. Нажмите «Войти через Telegram» снова.",
        };
      }

      const redirectUri = getTelegramOAuthRedirectUri();
      const out = await exchangeTelegramOAuthCode(code, redirectUri, verifier);
      clearPkceStorage();
      if (!out) {
        return { kind: "error", message: "Не удалось завершить вход через Telegram" };
      }
      return {
        kind: "success",
        access_token: out.access_token,
        user: out.user,
        expires_in: out.expires_in,
      };
    } finally {
      _tgOauthCallbackPromise = null;
    }
  })();

  return _tgOauthCallbackPromise;
}

/** Дизлайк в персональной подборке: трек исключается; артист/жанр — штраф показа в рекомендациях (по шагам на сервере). */
export async function dislikeTrack(trackId: string, artist?: string, genreId?: number): Promise<boolean> {
  const tid = trackId.trim();
  if (!tid) return false;
  const a = typeof artist === "string" ? artist.trim() : "";
  const gid = typeof genreId === "number" && Number.isFinite(genreId) ? Math.floor(genreId) : undefined;
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/me/dislike`,
      {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          track_id: tid,
          ...(a ? { artist: a } : {}),
          ...(gid !== undefined ? { genre_id: gid } : {}),
        }),
      },
      8000,
      1,
    );
    return resp.ok;
  } catch (err) {
    console.error("dislikeTrack failed:", err);
    return false;
  }
}

// ─── Playlist ───────────────────────────────────────────────────

export type FetchPlaylistResult = { list: Track[]; authFailed: boolean };

export const fetchPlaylist = async (): Promise<FetchPlaylistResult> => {
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/playlist`, { headers: authHeaders() }, 8000, 1);
    if (resp.status === 401) return { list: [], authFailed: true };
    if (!resp.ok) return { list: [], authFailed: false };
    const data = await resp.json();
    const list = ((data.items ?? []) as Record<string, unknown>[])
      .map(normalizeTrack)
      .filter((t) => t.id.length > 0);
    return { list, authFailed: false };
  } catch (err) {
    console.error("fetchPlaylist failed:", err);
    return { list: [], authFailed: false };
  }
};

/** Удобная обёртка: только список треков (для мест, где authFailed не нужен). */
export const fetchPlaylistTracks = async (): Promise<Track[]> => {
  const { list } = await fetchPlaylist();
  return list;
};

export type AddToPlaylistResult =
  | { ok: true; status: "saved" }
  | { ok: true; status: "already_exists" }
  | { ok: false };

export const addToPlaylist = async (track: Track): Promise<AddToPlaylistResult> => {
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/playlist`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        id: track.id,
        title: track.title,
        artist: track.artist,
        duration: track.duration ?? 0,
        cover_url: track.artwork ?? null,
        ...(track.vk_legacy !== undefined ? { vk_legacy: track.vk_legacy } : {}),
      }),
    }, 25000, 3);
    if (!resp.ok) return { ok: false };
    const data = await resp.json().catch(() => ({}));
    return { ok: true, status: data.status === "already_exists" ? "already_exists" : "saved" };
  } catch (err) {
    console.error("addToPlaylist failed:", err);
    return { ok: false };
  }
};

export const removeFromPlaylist = async (trackId: string): Promise<boolean> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/playlist/${encodeURIComponent(trackId)}`,
      { method: "DELETE", headers: authHeaders() }, 8000,
    );
    return resp.ok;
  } catch (err) {
    console.error("removeFromPlaylist failed:", err);
    return false;
  }
};

// ─── Playlists (Избранное + кастомные, лимит) ─────────────────────

export const fetchPlaylists = async (): Promise<PlaylistsResponse | null> => {
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/playlists`, { headers: authHeaders() }, 8000);
    if (!resp.ok) return null;
    const data = await resp.json();
    const items = (data.favorites ?? []) as Record<string, unknown>[];
    return {
      favorites: items.map(normalizeTrack).filter((t) => t.id.length > 0),
      playlists: (data.playlists ?? []) as PlaylistMeta[],
      max_free_playlists: data.max_free_playlists ?? 3,
    };
  } catch (err) {
    console.error("fetchPlaylists failed:", err);
    return null;
  }
};

export const createPlaylist = async (name: string): Promise<{ id: string; share_id: string; name: string } | null> => {
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/playlists`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ name }),
    }, 8000);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.error("createPlaylist failed:", err);
    return null;
  }
};

export const updatePlaylist = async (
  playlistId: string,
  updates: { name?: string; is_public?: boolean },
): Promise<boolean> => {
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/playlists/${encodeURIComponent(playlistId)}`, {
      method: "PATCH",
      headers: authHeaders(),
      body: JSON.stringify(updates),
    }, 8000);
    return resp.ok;
  } catch (err) {
    console.error("updatePlaylist failed:", err);
    return false;
  }
};

export const deletePlaylist = async (playlistId: string): Promise<boolean> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/playlists/${encodeURIComponent(playlistId)}`,
      { method: "DELETE", headers: authHeaders() }, 8000,
    );
    return resp.ok;
  } catch (err) {
    console.error("deletePlaylist failed:", err);
    return false;
  }
};

export const getPlaylistTracks = async (playlistId: string): Promise<Track[]> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/playlists/${encodeURIComponent(playlistId)}`,
      { headers: authHeaders() }, 8000,
    );
    if (!resp.ok) return [];
    const data = await resp.json();
    return ((data.items ?? []) as Record<string, unknown>[]).map(normalizeTrack).filter((t) => t.id.length > 0);
  } catch (err) {
    console.error("getPlaylistTracks failed:", err);
    return [];
  }
};

export const addTrackToPlaylist = async (playlistId: string, track: Track): Promise<AddToPlaylistResult> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/playlists/${encodeURIComponent(playlistId)}/tracks`,
      {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          id: track.id,
          title: track.title,
          artist: track.artist,
          duration: track.duration ?? 0,
          cover_url: track.artwork ?? null,
          ...(track.vk_legacy !== undefined ? { vk_legacy: track.vk_legacy } : {}),
        }),
      },
      8000,
    );
    if (!resp.ok) return { ok: false };
    const data = await resp.json().catch(() => ({}));
    return { ok: true, status: data.status === "already_exists" ? "already_exists" : "saved" };
  } catch (err) {
    console.error("addTrackToPlaylist failed:", err);
    return { ok: false };
  }
};

export const removeTrackFromPlaylist = async (playlistId: string, trackId: string): Promise<boolean> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/playlists/${encodeURIComponent(playlistId)}/tracks/${encodeURIComponent(trackId)}`,
      { method: "DELETE", headers: authHeaders() }, 8000,
    );
    return resp.ok;
  } catch (err) {
    console.error("removeTrackFromPlaylist failed:", err);
    return false;
  }
};

export const getSharedPlaylist = async (shareId: string): Promise<SharedPlaylistResponse | null> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/playlist/shared/${encodeURIComponent(shareId)}`,
      { headers: authHeaders() }, 9000, 1,
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    const items = (data.items ?? []) as Record<string, unknown>[];
    return {
      name: data.name ?? "Плейлист",
      items: items.map(normalizeTrack).filter((t) => t.id.length > 0),
    };
  } catch (err) {
    console.error("getSharedPlaylist failed:", err);
    return null;
  }
};

export const getTrackInfo = async (trackId: string): Promise<Track | null> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/track/${encodeURIComponent(trackId)}`,
      { headers: authHeaders() }, 9000, 1,
    );
    if (!resp.ok) return null;
    const raw = await resp.json();
    return normalizeTrack(raw as Record<string, unknown>);
  } catch (err) {
    console.error("getTrackInfo failed:", err);
    return null;
  }
};

export const createPlaylistShare = async (playlistId: string): Promise<{ share_id: string; url: string } | null> => {
  try {
    const resp = await fetchWithTimeout(`${API_BASE}/api/playlist/share`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ playlist_id: playlistId }),
    }, 8000);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.error("createPlaylistShare failed:", err);
    return null;
  }
};

/** Deep link для шеринга плейлиста по share_id (как трек, но pl_). */
export const getPlaylistShareUrl = (shareId: string): string =>
  `https://t.me/${BOT_USERNAME}?startapp=pl_${shareId}`;

/** Ссылка на трек в боте (deep link tr_*). Открывается у всех, в т.ч. не авторизованных. */
export const getTrackShareUrl = (trackId: string): string =>
  `https://t.me/${BOT_USERNAME}?startapp=tr_${getStartParamTrackId(trackId)}`;

/** Короткая ссылка для шеринга: превью = карточка (og:image), в сообщении видна только /s/xxx, не api/track-card. */
export function getShortShareUrl(trackId: string): string {
  const id = encodeURIComponent(getShareableTrackId(trackId));
  if (typeof window !== "undefined" && window.location?.origin) {
    return `${window.location.origin}/s/${id}`;
  }
  return `${import.meta.env.VITE_API_BASE || "https://tgplay.fun"}/s/${id}`;
}

/** URL PNG-карточки трека (для превью в историю). */
export function getTrackCardUrl(trackId: string): string {
  const id = encodeURIComponent(getShareableTrackId(trackId));
  if (typeof window !== "undefined" && window.location?.origin) {
    return `${window.location.origin}/api/track-card/${id}`;
  }
  return `${import.meta.env.VITE_API_BASE || "https://tgplay.fun"}/api/track-card/${id}`;
}

/** URL страницы шеринга трека с OG-тегами: Telegram подтягивает превью (картинка + заголовок), по клику — редирект в бота. */
export function getShareTrackPageUrl(trackId: string): string {
  const id = encodeURIComponent(getShareableTrackId(trackId));
  if (typeof window !== "undefined" && window.location?.origin) {
    return `${window.location.origin}/share/track/${id}`;
  }
  return `${import.meta.env.VITE_API_BASE || "https://tgplay.fun"}/share/track/${id}`;
}

/** Публичный JPEG для сторис: путь …/api/story-card/sc_123.jpg (без ?format= — Telegram не путает с видео). */
export function getStoryCardJpegUrl(trackId: string): string {
  const token = encodeURIComponent(getStartParamTrackId(trackId));
  return `${API_BASE}/api/story-card/${token}.jpg`;
}

/** Проверяет, что JPEG доступен, и возвращает URL для shareToStory. */
export const prepareStoryMedia = async (trackId: string): Promise<string | null> => {
  const base = getStoryCardJpegUrl(trackId);
  // Уникальный query на каждый шеринг — иначе Telegram показывает старую карточку из кэша.
  const url = `${base}?v=${Date.now()}`;
  try {
    const head = await fetchWithTimeout(url, { method: "HEAD", cache: "no-store" }, 12000, 1);
    if (head.ok) {
      const ct = (head.headers.get("content-type") || "").toLowerCase();
      if (ct.includes("image/jpeg")) return url;
    }
    const resp = await fetchWithTimeout(url, { method: "GET", cache: "no-store" }, 20000, 1);
    if (!resp.ok) return null;
    const buf = new Uint8Array(await resp.arrayBuffer());
    if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xd8) return url;
    return null;
  } catch (err) {
    console.error("prepareStoryMedia failed:", err);
    return null;
  }
};

/** Сбросить кеш карточки трека на сервере (после «В историю» или отмена — следующая генерация будет новой). */
export async function invalidateTrackCard(trackId: string): Promise<void> {
  try {
    const id = encodeURIComponent(getShareableTrackId(trackId));
    await fetchWithTimeout(
      `${API_BASE}/api/track-card/${id}/invalidate`,
      { method: "POST", headers: authHeaders() },
      5000,
    );
  } catch (err) {
    console.error("invalidateTrackCard failed:", err);
  }
}

/** Запросить нативный выбор пользователя: бот пришлёт кнопку «Выбрать друга» → выбранному уйдёт одно сообщение (картинка + ссылка). */
export const requestShareUserPicker = async (trackId: string): Promise<boolean> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/share/request-user-picker`,
      {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ track_id: getShareableTrackId(trackId) }),
      },
      15000,
    );
    return resp.ok;
  } catch (err) {
    console.error("requestShareUserPicker failed:", err);
    return false;
  }
};

/** Подготовить сообщение для shareMessage (Bot API 8.0+). Возвращает prepared_message_id или null при 501/ошибке. */
export const prepareShareMessage = async (trackId: string): Promise<string | null> => {
  try {
    const body = JSON.stringify({ track_id: getShareableTrackId(trackId) });
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/share/prepare-message`,
      {
        method: "POST",
        headers: authHeaders(),
        body,
      },
      10000,
    );
    if (!resp.ok) return null;
    const data = (await resp.json()) as { ok?: boolean; prepared_message_id?: string };
    return data?.prepared_message_id ?? null;
  } catch (err) {
    console.error("prepareShareMessage failed:", err);
    return null;
  }
};

// ─── Send to Telegram bot ───────────────────────────────────────

export const sendToBot = async (trackId: string): Promise<boolean> => {
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/send-to-bot/${encodeURIComponent(trackId)}`,
      { method: "POST", headers: authHeaders() },
      120000,
    );
    return resp.ok;
  } catch (err) {
    console.error("sendToBot failed:", err);
    return false;
  }
};

export type BotAudioDeliveredPayload = {
  trackIds: string[];
  /** Реальная доставка после учёта verified_live на сервере — UI не даёт повторно «скачать». */
  verifiedLiveTrackIds: string[];
};

/** Состояние «в чате с ботом»: все id + подмножество с подтверждённой доставкой (sendAudio). */
export const fetchBotAudioDelivered = async (): Promise<BotAudioDeliveredPayload> => {
  const empty: BotAudioDeliveredPayload = { trackIds: [], verifiedLiveTrackIds: [] };
  try {
    const resp = await fetchWithTimeout(
      `${API_BASE}/api/me/bot-audio-delivered`,
      { method: "GET", headers: authHeaders() },
      20000,
    );
    if (!resp.ok) return empty;
    const data = (await resp.json()) as { track_ids?: unknown; verified_live_track_ids?: unknown };
    const raw = data.track_ids;
    const rawV = data.verified_live_track_ids;
    const trackIds = Array.isArray(raw) ? raw.map((x) => String(x).trim()).filter(Boolean) : [];
    const verifiedLiveTrackIds = Array.isArray(rawV)
      ? rawV.map((x) => String(x).trim()).filter(Boolean)
      : [];
    return { trackIds, verifiedLiveTrackIds };
  } catch (err) {
    console.error("fetchBotAudioDelivered failed:", err);
    return empty;
  }
};

/** @deprecated Используйте fetchBotAudioDelivered для verified_live_track_ids */
export const fetchBotAudioDeliveredTrackIds = async (): Promise<string[]> => {
  const r = await fetchBotAudioDelivered();
  return r.trackIds;
};
