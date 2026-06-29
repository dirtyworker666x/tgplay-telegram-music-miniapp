import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { toast } from "./lib/toast";
import { AddToPlaylistSheet } from "./components/AddToPlaylistSheet";
import { ErrorState } from "./components/ErrorState";
import { FullPlayer } from "./components/FullPlayer";
import { ShareTrackSheet } from "./components/ShareTrackSheet";
import { MiniPlayer } from "./components/MiniPlayer";
import { ProfilePage } from "./components/ProfilePage";
import { TelegramWebLoginRow } from "./components/TelegramWebLoginRow";
import { SearchBar } from "./components/SearchBar";
import { TrackList } from "./components/TrackList";
import { useDebouncedValue } from "./hooks/useDebouncedValue";
import { useHlsAudio } from "./hooks/useHlsAudio";
import { useMediaSession } from "./hooks/useMediaSession";
import { useTelegramTheme } from "./hooks/useTelegramTheme";
import { Megaphone, MessageCircle, User, BookmarkPlus } from "lucide-react";
import {
  addToPlaylist,
  addTrackToPlaylist,
  createPlaylist,
  deletePlaylist,
  fetchPlaylists,
  fetchPlaylist,
  fetchPlaylistTracks,
  getCachedAudioUrl,
  getDownloadUrl,
  getSharedPlaylist,
  getTrackInfo,
  getPlaylistTracks,
  removeTrackFromPlaylist,
  loginTelegram,
  postAuthLogout,
  setWebSession,
  clearWebSession,
  clearTelegramWebAuthStorage,
  clearWebAuthPreferred,
  getStoredWebSessionUser,
  getWebAccessToken,
  getAuthorizationHeaderValue,
  WEB_SESSION_STORAGE_KEY,
  registerMiniAppIdentity,
  dislikeTrack,
  preloadBatchUrls,
  preloadTrackUrl,
  prewarmVkTrackUrlsFromPlaylist,
  removeFromPlaylist,
  resolveAudioUrl,
  resolveAudioUrlWithRefresh,
  youtubeResolveMetaForTrack,
  searchTracks,
  mergeLibraryArtworkIntoTracks,
  fetchRecommendations,
  fetchPersonalRecommendations,
  SEARCH_FIRST_PAGE,
  SEARCH_PAGE_SIZE,
  sendToBot,
  fetchBotAudioDelivered,
  AudioResolveRateLimitedError,
  SearchRateLimitedError,
  onSessionExpired,
  type WebSessionUser,
} from "./lib/api";
import { shouldClearRecommendationsLoading } from "./lib/recommendationsRequest";
import { addAddedPlaylistId, getAddedPlaylistIds, SHARED_SAVED_PREFIX } from "./lib/playlistLocal";
import { trackEvent } from "./lib/analytics";
import { perfMark, perfAudioPlaying } from "./lib/perf";
import {
  getTelegramUser,
  getStartParam,
  isTelegramWebDesktop,
  getInitData,
  initTelegram,
  isAndroid,
  clearTelegramOidcNonce,
  openTelegramDeepLink,
} from "./lib/telegram";
import { canonicalPlaylistTrackId, parseStartParamTrackId } from "./lib/shareTrackId";

const PLAYLIST_CACHE_KEY = "tgplay_favorites";

/**
 * Кэш избранного: Mini App (initData) или сохранённая веб-сессия OAuth на том же устройстве.
 * Без токена/контекста кэш не читаем — риск чужих данных.
 */
function readPlaylistCache(): Track[] {
  if (typeof window === "undefined") return [];
  let userId: number | null = null;
  if (getInitData()) {
    userId = getTelegramUser()?.id ?? null;
  } else if (getWebAccessToken()) {
    userId = getStoredWebSessionUser()?.id ?? null;
  }
  if (userId == null) return [];
  try {
    const raw = localStorage.getItem(`${PLAYLIST_CACHE_KEY}_${userId}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writePlaylistCache(list: Track[], userId: number): void {
  try {
    localStorage.setItem(`${PLAYLIST_CACHE_KEY}_${userId}`, JSON.stringify(list));
  } catch {
    /* ignore */
  }
}

function clearPlaylistCache(userId: number | null): void {
  if (typeof window === "undefined" || !userId) return;
  try {
    localStorage.removeItem(`${PLAYLIST_CACHE_KEY}_${userId}`);
  } catch {
    /* ignore */
  }
}
import { useFullscreenLaunch } from "./hooks/useFullscreenLaunch";
import {
  buildQueue,
  dedupeTracksById,
  getRandomNextIndexAvoidingRecent,
  SHUFFLE_RECENT_EXCLUDE_MAX,
  type PlaybackRepeatMode,
} from "./lib/playerQueue";
import type { Track } from "./types";

/** Сколько верхних треков из избранного участвуют в случайном выборе seed для рекомендаций VK */
const RECOMMENDATIONS_SEED_POOL_SIZE = 20;
/** Сколько разных треков из пула передаём в API (merge на бэкенде) */
const RECOMMENDATIONS_SEED_COUNT = 3;

/** Опрос /api/me/bot-audio-delivered после «Скачать», пока фоновая отправка в Telegram не завершится */
const BOT_DELIVER_POLL_MS = 1500;
const BOT_DELIVER_POLL_MAX_MS = 180_000;

function pickDistinctRandomTrackIds(tracks: Track[], poolSize: number, want: number): string[] {
  const n = Math.min(poolSize, tracks.length);
  if (n === 0) return [];
  const order = Array.from({ length: n }, (_, i) => i);
  for (let i = n - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  const out: string[] = [];
  const seen = new Set<string>();
  for (let i = 0; i < n && out.length < want; i++) {
    const id = tracks[order[i]]?.id;
    if (id && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

type TgUser = { id: number; first_name: string; username?: string } | null;
type View = "main" | "profile" | "shared" | "playlist";

/** Поздний start_param (прокси/WebView) + защита от StrictMode double-mount */
let tgplayDeepLinkConsumed: string | null = null;
let tgplayDeepLinkNonTrackParam = false;
const App = () => {
  useEffect(() => {
    const boot = document.getElementById("tgplay-boot");
    if (boot) {
      boot.setAttribute("data-tgplay-dismissed", "1");
      boot.remove();
    }
  }, []);

  useTelegramTheme();
  const compactSpacing = useFullscreenLaunch();
  /** В Telegram Web (десктоп) в полной версии используем пропорции сжатого плеера; смартфоны не трогаем */
  const useCompressedProportions = compactSpacing && isTelegramWebDesktop();
  const audioRef = useRef<HTMLAudioElement>(null);
  /** Якорь под поиском: результаты поиска или рекомендации — для «списка» из полного плеера */
  const mainBelowSearchRef = useRef<HTMLDivElement | null>(null);
  const searchLoadMoreSentinelRef = useRef<HTMLDivElement | null>(null);
  const loadMoreSearchInFlightRef = useRef(false);
  const playlistSearchLoadMoreSentinelRef = useRef<HTMLDivElement | null>(null);
  const loadMorePlaylistSearchInFlightRef = useRef(false);

  const [tgUser, setTgUser] = useState<TgUser>(() => getTelegramUser() ?? getStoredWebSessionUser());
  /** Mini App: initData подписан и уходит в API раньше, чем в state попадёт tgUser из initDataUnsafe — кнопки плеера/листов не должны быть «гостевыми». */
  const isLoggedIn = tgUser !== null || Boolean(getInitData());
  const [sessionExpired, setSessionExpired] = useState(false);
  const [sessionExpiredHadWebToken, setSessionExpiredHadWebToken] = useState(false);

  useEffect(() => {
    const unsub = onSessionExpired(() => {
      setSessionExpiredHadWebToken(Boolean(getWebAccessToken()));
      clearWebSession();
      setSessionExpired(true);
    });
    return unsub;
  }, []);

  const [query, setQuery] = useState("");
  const [tracks, setTracks] = useState<Track[]>([]);
  const [playlist, setPlaylist] = useState<Track[]>(() => readPlaylistCache());
  const [playlistLoading, setPlaylistLoading] = useState(() => readPlaylistCache().length === 0);
  const [error, setError] = useState("");
  const [currentTrack, setCurrentTrack] = useState<Track | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  /** Сброс <audio> при повторном тапе с тем же URL из кеша (React иначе не переэффектит). */
  const [audioPlaybackEpoch, setAudioPlaybackEpoch] = useState(0);
  const [isPlayerOpen, setIsPlayerOpen] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isBuffering, setIsBuffering] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  /** Есть ли ещё результаты поиска (подгрузка по 20) */
  const [searchHasMore, setSearchHasMore] = useState(false);
  const [loadMoreSearchLoading, setLoadMoreSearchLoading] = useState(false);
  /** Треки, по которым аудио уже доставлено в чат с ботом (сервер + опрос после «Скачать») */
  const [botDeliveredIds, setBotDeliveredIds] = useState<Set<string>>(() => new Set());
  /** Подтверждённая доставка (verified_live на сервере) — без повторной кнопки «скачать» */
  const [botDeliveredVerifiedLiveIds, setBotDeliveredVerifiedLiveIds] = useState<Set<string>>(() => new Set());
  /** Пока ждём фактическую доставку после POST /api/send-to-bot */
  const [sendToBotPendingIds, setSendToBotPendingIds] = useState<Set<string>>(() => new Set());
  const [view, setView] = useState<View>("main");
  const [sharedPlaylist, setSharedPlaylist] = useState<{ name: string; items: Track[] } | null>(null);
  const [addToPlaylistSheetTrack, setAddToPlaylistSheetTrack] = useState<Track | null>(null);
  const [addToPlaylistSheetFromFavorites, setAddToPlaylistSheetFromFavorites] = useState(false);
  const [shareMenuTrack, setShareMenuTrack] = useState<Track | null>(null);
  /** Инкремент при добавлении трека в плейлист через шит — профиль перезагружает список */
  const [profileRefreshTrigger, setProfileRefreshTrigger] = useState(0);
  /** Активный плейлист для queue (треки из кастомного плейлиста или шарингового) */
  const [activePlaylistTracks, setActivePlaylistTracks] = useState<Track[] | null>(null);
  const [sharedSaving, setSharedSaving] = useState(false);
  const [sharedSaved, setSharedSaved] = useState(false);
  const [sharedSavedPlaylistId, setSharedSavedPlaylistId] = useState<string | null>(null);
  const [sharedShareId, setSharedShareId] = useState<string | null>(null);
  const [isShuffle, setIsShuffle] = useState(false);
  const [repeatMode, setRepeatMode] = useState<PlaybackRepeatMode>("off");
  const [openedPlaylist, setOpenedPlaylist] = useState<{
    id: string;
    name: string;
    isFavorites: boolean;
    isAdded?: boolean;
  } | null>(null);
  const [openedPlaylistTracks, setOpenedPlaylistTracks] = useState<Track[]>([]);
  /** Актуальные треки библиотеки для подстановки обложек в поиск (тот же VK id). */
  const searchLibraryArtworkRef = useRef<{ pl: Track[]; opened: Track[] }>({ pl: [], opened: [] });
  searchLibraryArtworkRef.current = { pl: playlist, opened: openedPlaylistTracks };
  const [playlistSearchQuery, setPlaylistSearchQuery] = useState("");
  const [playlistSearchResults, setPlaylistSearchResults] = useState<Track[]>([]);
  const [playlistSearchLoading, setPlaylistSearchLoading] = useState(false);
  const [playlistSearchHasMore, setPlaylistSearchHasMore] = useState(false);
  const [playlistSearchLoadMoreLoading, setPlaylistSearchLoadMoreLoading] = useState(false);
  const [sharedLimitReached, setSharedLimitReached] = useState(false);

  // ─── Refs для доступа из audio event handlers (useEffect []) ────
  const bufferingRef = useRef(false);
  const userPausedRef = useRef(false);
  const seekingRef = useRef(false);
  /** Автопереход (конец трека / synthetic / ошибка) — не через дебаунс кнопки «вперёд». */
  const handleNextAutoRef = useRef<() => void>(() => {});
  /** Дополнительные «вперёд» после первого мгновенного шага в окне дебаунса (см. handleNext). */
  const userNextBurstExtraRef = useRef(0);
  const userNextSkipsTimerRef = useRef<number | null>(null);
  const prevTapTimeoutRef = useRef<number | null>(null);
  const currentTrackIdRef = useRef<string | null>(null);
  /** Активный трек плеера — для retry resolve с title/artist (старые VK id в избранном). */
  const currentPlayingTrackRef = useRef<Track | null>(null);
  /** Монотонно растёт в playTrack — отбрасываем устаревший resolveAudioUrl после быстрых переключений. */
  const audioLoadRequestGenRef = useRef(0);
  const lastTimeUpdateRef = useRef(0);
  const audioErrorRetryDoneRef = useRef(false);
  const audioErrorReportedRef = useRef(false);
  /** Сколько треков подряд не удалось воспроизвести — защита от бесконечного авто-проскока. */
  const consecutiveAudioSkipRef = useRef(0);
  /** Длительность из метаданных трека (поиск); не даём audio.duration «удваивать» шкалу на DASH/fMP4. */
  const playbackMetaDurationRef = useRef(0);
  /** Досрочное окончание при завышенном internal duration (тишина в хвосте). */
  const syntheticEndedRef = useRef(false);
  /** true только после onAudioReady для текущего src — иначе при быстром next мета нового трека + старый поток дают ложный synthetic ended и лавину handleNext. */
  const playbackArmedRef = useRef(false);
  const repeatModeRef = useRef<PlaybackRepeatMode>("off");
  const sharedEntryTrackIdRef = useRef<string | null>(null);

  const debouncedQuery = useDebouncedValue(query, 500);
  const debouncedPlaylistSearchQuery = useDebouncedValue(playlistSearchQuery, 500);
  /** Один прогон login+fetchPlaylist на монтирование (повтор после появления WebApp). */
  const authBootstrapOnceRef = useRef(false);

  const searchAbortRef = useRef<AbortController | null>(null);
  const playlistSearchAbortRef = useRef<AbortController | null>(null);
  const lastSubmittedQueryRef = useRef<string>("");
  const lastSubmittedPlaylistSearchRef = useRef<string>("");
  /** Режим «все треки исполнителя» — не подмешивать обычный поиск и не грузить «ещё» постранично */
  const artistCatalogModeRef = useRef(false);
  const skipNextDebouncedSearchRef = useRef(false);
  const [searchCooldownUntil, setSearchCooldownUntil] = useState<number | null>(null);
  const [searchStatus, setSearchStatus] = useState<string | null>(null);
  const pendingRetryRef = useRef<"initial" | null>(null);
  const [searchHardBlocked, setSearchHardBlocked] = useState<string | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  /** Подпись списка при открытии каталога исполнителя */
  const [artistCatalogTitle, setArtistCatalogTitle] = useState<string | null>(null);
  const [recommendationTracks, setRecommendationTracks] = useState<Track[]>([]);
  const [recommendationsLoading, setRecommendationsLoading] = useState(false);
  const recLoadAbortRef = useRef<AbortController | null>(null);
  /** На главной открыта «Моя волна» — автообновление не подменяет её лентой после «Обновить» */
  const recommendationsWaveModeRef = useRef(false);
  /** «Моя волна» как радио: id треков, для которых уже подгрузили похожие (чтобы не дёргать повторно). */
  const waveRadioSeededRef = useRef<Set<string>>(new Set());
  const waveRadioFetchingRef = useRef(false);
  /** Последние проигранные id при shuffle — чтобы реже повторять треки подряд */
  const shuffleRecentTrackIdsRef = useRef<string[]>([]);
  /** Ключ пользователя/гостя для однократной подгрузки рекомендаций (не сбрасываем при возврате с профиля) */
  const recommendationsBootstrapKeyRef = useRef<string>("");
  const recommendationsBootstrappedRef = useRef(false);
  /** Лента устарела (добавлен/удалён трек) — следующий возврат/возобновление форсит обновление. */
  const recommendationsStaleRef = useRef(false);
  const isShuffleRef = useRef(false);
  const playlistRecRef = useRef(playlist);
  playlistRecRef.current = playlist;

  const now = Date.now();
  const searchCooldownActive = searchCooldownUntil != null && searchCooldownUntil > now;

  const mainSearchActive = query.trim().length > 0;
  /** Для повторного fetch рекомендаций после закрытия поиска (abort при открытии поиска). */
  const prevMainSearchActiveForRecsRef = useRef(false);

  /** Кнопка входа в карточке пустых рекомендаций (гость без избранного); под поиском не дублируем */
  const showTelegramLoginInRecsEmptyCard =
    view === "main" &&
    !mainSearchActive &&
    !getInitData() &&
    !isLoggedIn &&
    playlist.length === 0 &&
    recommendationTracks.length === 0 &&
    !recommendationsLoading;

  useEffect(() => {
    if (!isLoggedIn) {
      setBotDeliveredIds(new Set());
      setBotDeliveredVerifiedLiveIds(new Set());
      return;
    }
    let cancelled = false;
    void fetchBotAudioDelivered().then((p) => {
      if (!cancelled) {
        setBotDeliveredIds(new Set(p.trackIds.map((id) => canonicalPlaylistTrackId(id))));
        setBotDeliveredVerifiedLiveIds(new Set(p.verifiedLiveTrackIds.map((id) => canonicalPlaylistTrackId(id))));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [isLoggedIn, tgUser?.id]);

  /**
   * Seed(ы) для VK recommendations: до 3 случайных из верхних N избранного при старте, или один трек с главной.
   */
  const [recApiSeedTrackIds, setRecApiSeedTrackIds] = useState<string[]>([]);
  const recApiSeedTrackIdsRef = useRef(recApiSeedTrackIds);
  recApiSeedTrackIdsRef.current = recApiSeedTrackIds;

  useEffect(() => {
    if (recApiSeedTrackIds.length > 0) return;
    if (!playlist.length) return;
    if (getAuthorizationHeaderValue()) return;
    const ids = pickDistinctRandomTrackIds(playlist, RECOMMENDATIONS_SEED_POOL_SIZE, RECOMMENDATIONS_SEED_COUNT);
    if (ids.length) setRecApiSeedTrackIds(ids);
  }, [playlist, recApiSeedTrackIds.length]);

  useEffect(() => {
    if (view !== "playlist") {
      setPlaylistSearchQuery("");
      setPlaylistSearchResults([]);
      setPlaylistSearchLoading(false);
      setPlaylistSearchHasMore(false);
      lastSubmittedPlaylistSearchRef.current = "";
    }
  }, [view]);

  // Низкоуровневый запуск поиска (без проверки кулдауна) — проверка делается в вызывающем коде.
  const runSearch = useCallback((trimmed: string): (() => void) | void => {
    if (trimmed.length < 3) return;
    if (searchHardBlocked) return;
    const isArtistLike = (() => {
      const q = trimmed.normalize("NFKC").toLowerCase().trim();
      if (!q) return false;
      const toks = q.split(/\s+/g).filter(Boolean);
      if (toks.length > 2) return false;
      return toks.every((t) => t.length >= 2) && q.length <= 40;
    })();
    artistCatalogModeRef.current = false;
    setArtistCatalogTitle(null);
    searchAbortRef.current?.abort();
    const controller = new AbortController();
    searchAbortRef.current = controller;
    let active = true;
    let didArtistFallback = false;
    setError("");
    setSearchStatus(null);
    setSearchLoading(true);
    searchTracks(trimmed, controller.signal, { limit: SEARCH_FIRST_PAGE, offset: 0 })
      .then((results) => {
        if (!active || controller.signal.aborted) return;
        // Авто-fallback: если запрос похож на артиста и результатов мало — попробуем «каталог артиста».
        if (!didArtistFallback && isArtistLike && results.length > 0 && results.length < 18) {
          didArtistFallback = true;
          setSearchStatus("Подбираем больше треков исполнителя…");
          searchTracks(trimmed, controller.signal, { artistCatalog: true, limit: 600 })
            .then((catalog) => {
              if (!active || controller.signal.aborted) return;
              if (catalog.length > results.length) {
                artistCatalogModeRef.current = true;
                setArtistCatalogTitle(trimmed);
                const { pl, opened } = searchLibraryArtworkRef.current;
                setTracks(mergeLibraryArtworkIntoTracks(catalog, [pl, opened]));
                setSearchHasMore(false);
                setError(catalog.length === 0 ? "Треков этого исполнителя не найдено." : "");
                if (catalog.length > 0) {
                  perfMark("search-results");
                  preloadBatchUrls(catalog.slice(0, 15).map((t) => t.id));
                }
              }
            })
            .catch(() => {
              // ignore fallback errors — оставляем базовую выдачу
            })
            .finally(() => {
              if (!controller.signal.aborted) setSearchStatus(null);
            });
        }
        const { pl, opened } = searchLibraryArtworkRef.current;
        setTracks(mergeLibraryArtworkIntoTracks(results, [pl, opened]));
        setSearchHasMore(results.length >= SEARCH_FIRST_PAGE);
        if (results.length === 0) setError("Ничего не найдено.");
        trackEvent("search", {
          has_results: results.length > 0,
          query_length: trimmed.length,
          extra: {
            q_norm: trimmed.normalize("NFKC").toLowerCase().trim().slice(0, 100),
          },
        });
        if (results.length > 0) {
          perfMark("search-results");
          preloadBatchUrls(results.slice(0, 10).map((t) => t.id));
        }
        lastSubmittedQueryRef.current = trimmed;
      })
      .catch((err) => {
        if (!active || controller.signal.aborted) return;
        if (err instanceof SearchRateLimitedError) {
          const retrySec = err.retryAfterSec && err.retryAfterSec > 0 ? err.retryAfterSec : 20;
          const until = Date.now() + retrySec * 1000;
          setSearchCooldownUntil(until);
          const msg =
            retrySec <= 10
              ? "Подбираем лучшие совпадения…"
              : retrySec <= 20
              ? "Оптимизируем поиск…"
              : "Ускоряем алгоритмы…";
          setSearchStatus(msg);
          setError("");
          // Запланировать единичный авто-повтор этого поиска после кулдауна
          pendingRetryRef.current = "initial";
          trackEvent("error", { place: "search", message: "rate_limited", retry_after_sec: retrySec });
          return;
        }
        if (err instanceof Error && err.message.includes("Превышен дневной лимит поисковых запросов")) {
          setSearchHardBlocked(err.message);
          setError("");
          trackEvent("error", { place: "search", message: "daily_limit", detail: err.message });
          return;
        }
        setError(err instanceof Error ? err.message : "Ошибка поиска");
        toast.error("Ошибка поиска треков");
        trackEvent("error", { place: "search", message: err instanceof Error ? err.message : "search_failed" });
      })
      .finally(() => {
        active = false;
        setSearchLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      setSearchLoading(false);
    };
  }, []);

  const onSearchSubmit = useCallback(() => {
    const trimmed = query.trim();
    if (trimmed.length < 3) return;
    if (searchHardBlocked) return;
    if (searchCooldownActive) {
      setSearchStatus((prev) => prev ?? "Подбираем лучшие совпадения…");
      return;
    }
    artistCatalogModeRef.current = false;
    setArtistCatalogTitle(null);
    runSearch(trimmed);
  }, [query, runSearch, searchCooldownActive]);

  const handleMainSearchQueryChange = useCallback((v: string) => {
    artistCatalogModeRef.current = false;
    setArtistCatalogTitle(null);
    setQuery(v);
  }, []);

  const openArtistCatalog = useCallback(
    (rawArtist: string) => {
      const name = rawArtist.trim();
      if (name.length < 2 || searchHardBlocked) return;
      setView("main");
      setIsPlayerOpen(false);
      artistCatalogModeRef.current = true;
      skipNextDebouncedSearchRef.current = true;
      setArtistCatalogTitle(name);
      setQuery(name);
      lastSubmittedQueryRef.current = name;
      searchAbortRef.current?.abort();
      const controller = new AbortController();
      searchAbortRef.current = controller;
      setError("");
      setSearchStatus(null);
      setSearchLoading(true);
      setSearchHasMore(false);
      searchTracks(name, controller.signal, { artistCatalog: true, limit: 600 })
        .then((results) => {
          if (controller.signal.aborted) return;
          const { pl, opened } = searchLibraryArtworkRef.current;
          setTracks(mergeLibraryArtworkIntoTracks(results, [pl, opened]));
          if (results.length === 0) setError("Треков этого исполнителя не найдено.");
          trackEvent("search", {
            has_results: results.length > 0,
            query_length: name.length,
            extra: {
              q_norm: name.normalize("NFKC").toLowerCase().trim().slice(0, 100),
              artist_catalog: true,
            },
          });
          if (results.length > 0) {
            perfMark("search-results");
            preloadBatchUrls(results.slice(0, 15).map((t) => t.id));
          }
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          if (err instanceof SearchRateLimitedError) {
            const retrySec = err.retryAfterSec && err.retryAfterSec > 0 ? err.retryAfterSec : 20;
            setSearchCooldownUntil(Date.now() + retrySec * 1000);
            setSearchStatus("Подбираем лучшие совпадения…");
            setError("");
            pendingRetryRef.current = "initial";
            trackEvent("error", { place: "search", message: "rate_limited", retry_after_sec: retrySec });
            return;
          }
          if (err instanceof Error && err.message.includes("Превышен дневной лимит поисковых запросов")) {
            setSearchHardBlocked(err.message);
            setError("");
            trackEvent("error", { place: "search", message: "daily_limit", detail: err.message });
            return;
          }
          setError(err instanceof Error ? err.message : "Ошибка загрузки");
          toast.error("Не удалось загрузить треки исполнителя");
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearchLoading(false);
        });
    },
    [searchHardBlocked],
  );

  const loadMoreSearch = useCallback(() => {
    if (artistCatalogModeRef.current) return;
    const trimmed = lastSubmittedQueryRef.current;
    if (!trimmed || !searchHasMore || loadMoreSearchLoading || searchCooldownActive || searchHardBlocked) return;
    if (loadMoreSearchInFlightRef.current) return;
    loadMoreSearchInFlightRef.current = true;
    setLoadMoreSearchLoading(true);
    const requestQuery = trimmed;
    const startOffset = tracks.length;
    searchTracks(requestQuery, undefined, { offset: startOffset, limit: SEARCH_PAGE_SIZE })
      .then((chunk) => {
        // Если за время запроса пользователь сменил поиск — игнорируем этот ответ
        if (lastSubmittedQueryRef.current !== requestQuery) return;
        const { pl, opened } = searchLibraryArtworkRef.current;
        const mergedChunk = mergeLibraryArtworkIntoTracks(chunk, [pl, opened]);
        setTracks((prev) => {
          const seen = new Set(prev.map((t) => t.id));
          const added = mergedChunk.filter((t) => !seen.has(t.id));
          if (added.length === 0) return prev;
          return [...prev, ...added];
        });
        setSearchHasMore(chunk.length >= SEARCH_PAGE_SIZE);
        if (chunk.length > 0) preloadBatchUrls(chunk.slice(0, 10).map((t) => t.id));
      })
      .catch((err) => {
        if (lastSubmittedQueryRef.current !== requestQuery) return;
        if (err instanceof SearchRateLimitedError) {
          const retrySec = err.retryAfterSec && err.retryAfterSec > 0 ? err.retryAfterSec : 20;
          const until = Date.now() + retrySec * 1000;
          setSearchCooldownUntil(until);
          const msg =
            retrySec <= 10
              ? "Подбираем лучшие совпадения…"
              : retrySec <= 20
              ? "Оптимизируем поиск…"
              : "Ускоряем алгоритмы…";
          setSearchStatus(msg);
          setError("");
          trackEvent("error", { place: "search_load_more", message: "rate_limited", retry_after_sec: retrySec });
          return;
        }
        if (err instanceof Error && err.message.includes("Превышен дневной лимит поисковых запросов")) {
          setSearchHardBlocked(err.message);
          setError("");
          trackEvent("error", { place: "search_load_more", message: "daily_limit", detail: err.message });
          return;
        }
        toast.error("Не удалось подгрузить треки");
      })
      .finally(() => {
        loadMoreSearchInFlightRef.current = false;
        setLoadMoreSearchLoading(false);
      });
  }, [searchHasMore, loadMoreSearchLoading, tracks.length, searchCooldownActive]);

  // Автоподгрузка результатов поиска при прокрутке до конца списка
  useEffect(() => {
    const sentinel = searchLoadMoreSentinelRef.current;
    const scrollEl = typeof document !== "undefined" ? document.querySelector(".app-scroll") : null;
    if (!sentinel || !scrollEl) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const [entry] = entries;
        if (!entry?.isIntersecting) return;
        if (view !== "main") return;
        if (!searchHasMore || loadMoreSearchLoading || searchCooldownActive || searchHardBlocked) return;
        if (tracks.length === 0) return;
        loadMoreSearch();
      },
      { root: scrollEl, rootMargin: "200px", threshold: 0 }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [view, searchHasMore, loadMoreSearchLoading, tracks.length, searchCooldownActive, searchHardBlocked, loadMoreSearch]);

  // Обновление статуса во время мягкого кулдауна и его снятие по таймеру
  useEffect(() => {
    if (!searchCooldownUntil) return;
    const timerId = window.setInterval(() => {
      const until = searchCooldownUntil;
      if (!until) return;
      const nowTs = Date.now();
      const remainingMs = until - nowTs;
      if (remainingMs <= 0) {
        window.clearInterval(timerId);
        setSearchCooldownUntil(null);
        setSearchStatus(null);
        // Однократный авто-повтор последнего поиска после мягкого кулдауна
        const action = pendingRetryRef.current;
        pendingRetryRef.current = null;
        const trimmed = lastSubmittedQueryRef.current || debouncedQuery.trim();
        if (action === "initial" && trimmed.length >= 3) {
          runSearch(trimmed);
        }
        return;
      }
      const sec = Math.max(1, Math.ceil(remainingMs / 1000));
      const msg =
        sec <= 10
          ? "Подбираем лучшие совпадения…"
          : sec <= 20
          ? "Оптимизируем поиск…"
          : "Ускоряем алгоритмы…";
      setSearchStatus(msg);
    }, 1000);
    return () => {
      window.clearInterval(timerId);
    };
  }, [searchCooldownUntil, debouncedQuery, runSearch]);

  // ─── Search (минимум 3 символа, отмена предыдущего запроса, без дубля по Enter) ─────
  useEffect(() => {
    const trimmed = debouncedQuery.trim();
    if (trimmed.length < 3) {
      if (
        artistCatalogModeRef.current &&
        trimmed.length >= 2 &&
        trimmed === lastSubmittedQueryRef.current.trim()
      ) {
        return;
      }
      setTracks([]);
      setError("");
      setSearchHasMore(false);
      return;
    }
    if (searchHardBlocked) {
      return;
    }
    if (searchCooldownActive) {
      setSearchStatus((prev) => prev ?? "Подбираем лучшие совпадения…");
      return;
    }
    if (skipNextDebouncedSearchRef.current) {
      skipNextDebouncedSearchRef.current = false;
      return;
    }
    if (trimmed === lastSubmittedQueryRef.current) return;
    if (artistCatalogModeRef.current && trimmed === lastSubmittedQueryRef.current.trim()) {
      return;
    }
    const cancel = runSearch(trimmed);
    return () => cancel?.();
  }, [debouncedQuery, runSearch, searchCooldownActive]);

  // ─── Метка готовности UI (для замеров производительности) ───
  useEffect(() => {
    perfMark("app-ready");
  }, []);

  // При открытии из списка чатов WebApp может появиться с задержкой — повторная инициализация убирает бесконечную загрузку
  useEffect(() => {
    initTelegram();
  }, []);

  // ─── Auth + плейлист: app_open, ждём WebApp (часто с задержкой), таймаут — без вечной загрузки ───
  useEffect(() => {
    trackEvent("app_open");
    let active = true;
    let pollIv: ReturnType<typeof setInterval> | null = null;
    let authDeadlineTimer: ReturnType<typeof setTimeout> | null = null;
    const POLL_MS = 250;
    const MAX_WAIT_USER_MS = 10_000;
    const MAX_AUTH_MS = 14_000;
    const pollStart = Date.now();

    const clearPoll = () => {
      if (pollIv != null) {
        clearInterval(pollIv);
        pollIv = null;
      }
    };

    const startAuthTelegram = (u: NonNullable<ReturnType<typeof getTelegramUser>>) => {
      if (!active || authBootstrapOnceRef.current) return;
      clearWebAuthPreferred();
      authBootstrapOnceRef.current = true;
      clearPoll();

      authDeadlineTimer = setTimeout(() => {
        if (!active) return;
        setTgUser((prev) => prev ?? u);
        setPlaylistLoading(false);
        authDeadlineTimer = null;
      }, MAX_AUTH_MS);

      Promise.all([
        loginTelegram().then((v) => (active ? (v ?? u) : null)).catch(() => (active ? u : null)),
        fetchPlaylist().then((r) => (active ? r : { list: [] as Track[], authFailed: false })).catch(() =>
          active ? { list: [] as Track[], authFailed: false } : { list: [], authFailed: false },
        ),
      ])
        .then(([user, result]) => {
          if (!active) return;
          if (authDeadlineTimer != null) {
            clearTimeout(authDeadlineTimer);
            authDeadlineTimer = null;
          }
          if (user) setTgUser(user);
          if (result.authFailed) {
            clearPlaylistCache(u?.id ?? user?.id ?? null);
            setPlaylist([]);
          } else {
            const nextList = Array.isArray(result.list) ? result.list : [];
            setPlaylist(nextList);
            if (user?.id) writePlaylistCache(nextList, user.id);
            prewarmVkTrackUrlsFromPlaylist(nextList);
          }
          setPlaylistLoading(false);
        })
        .catch(() => {
          if (!active) return;
          if (authDeadlineTimer != null) {
            clearTimeout(authDeadlineTimer);
            authDeadlineTimer = null;
          }
          setTgUser((prev) => prev ?? u);
          setPlaylistLoading(false);
        });
    };

    const startAuthWeb = (u: NonNullable<ReturnType<typeof getStoredWebSessionUser>>) => {
      if (!active || authBootstrapOnceRef.current) return;
      authBootstrapOnceRef.current = true;
      clearPoll();

      authDeadlineTimer = setTimeout(() => {
        if (!active) return;
        setTgUser((prev) => prev ?? u);
        setPlaylistLoading(false);
        authDeadlineTimer = null;
      }, MAX_AUTH_MS);

      fetchPlaylist()
        .then((result) => {
          if (!active) return;
          if (authDeadlineTimer != null) {
            clearTimeout(authDeadlineTimer);
            authDeadlineTimer = null;
          }
          setTgUser(u);
          if (result.authFailed) {
            clearPlaylistCache(u.id);
            clearWebSession();
            setTgUser(null);
            authBootstrapOnceRef.current = false;
            setPlaylist([]);
          } else {
            const nextList = Array.isArray(result.list) ? result.list : [];
            setPlaylist(nextList);
            writePlaylistCache(nextList, u.id);
            prewarmVkTrackUrlsFromPlaylist(nextList);
            void registerMiniAppIdentity();
          }
          setPlaylistLoading(false);
        })
        .catch(() => {
          if (!active) return;
          if (authDeadlineTimer != null) {
            clearTimeout(authDeadlineTimer);
            authDeadlineTimer = null;
          }
          setTgUser((prev) => prev ?? u);
          setPlaylistLoading(false);
        });
    };

    const tryPickUser = () => {
      const u = getTelegramUser();
      if (!u) return false;
      startAuthTelegram(u);
      return true;
    };

    const tryWebSession = () => {
      const wu = getStoredWebSessionUser();
      const tok = getWebAccessToken();
      if (!wu?.id || !tok) return false;
      startAuthWeb(wu);
      return true;
    };

    const onWebAppSignal = () => {
      if (!active) return;
      const u = getTelegramUser();
      if (!u || !getInitData()) return;
      if (authBootstrapOnceRef.current) {
        clearWebSession();
        authBootstrapOnceRef.current = false;
      }
      tryPickUser();
    };

    window.addEventListener("tgplay-webapp-ready", onWebAppSignal);

    if (!tryPickUser()) {
      // При открытии Mini App initData часто есть раньше, чем initDataUnsafe.user — не вешаемся на веб-сессию.
      const inTelegramWebApp = Boolean(getInitData());
      const startedWeb = !inTelegramWebApp && tryWebSession();
      if (!startedWeb) {
        pollIv = setInterval(() => {
          if (!active || authBootstrapOnceRef.current) {
            clearPoll();
            return;
          }
          if (tryPickUser()) return;
          if (Date.now() - pollStart > MAX_WAIT_USER_MS) {
            clearPoll();
            setPlaylistLoading(false);
          }
        }, POLL_MS);
      }
    }

    return () => {
      active = false;
      clearPoll();
      window.removeEventListener("tgplay-webapp-ready", onWebAppSignal);
      if (authDeadlineTimer != null) clearTimeout(authDeadlineTimer);
    };
  }, []);

  /** Ответ /api/auth/telegram после успешного fetch в index.html (tgLoginOnAuth). Полная замена сессии. */
  const applyTelegramWebSessionRef = useRef<
    (out: { access_token: string; user: WebSessionUser; expires_in?: number }) => void
  >(() => {});

  applyTelegramWebSessionRef.current = (out) => {
    void (async () => {
      trackEvent("button_telegram_oauth_open");
      // Пока в storage старый Bearer — снимаем кэш избранного другого аккаунта.
      const prevWebId = getStoredWebSessionUser()?.id;
      if (prevWebId != null && prevWebId !== out.user.id) {
        clearPlaylistCache(prevWebId);
      }
      // Сброс ленты рекомендаций и seed (иначе остаётся гость/прошлый пользователь до смены ключа).
      recLoadAbortRef.current?.abort();
      recLoadAbortRef.current = null;
      setRecommendationTracks([]);
      setRecApiSeedTrackIds([]);
      setRecommendationsLoading(false);
      recommendationsWaveModeRef.current = false;
      recommendationsBootstrappedRef.current = false;
      setPlaylist([]);
      setPlaylistLoading(true);

      setWebSession(out.access_token, out.user);
      setTgUser(out.user);
      setSessionExpired(false);
      const pl = await fetchPlaylist();
      if (pl.authFailed) {
        clearWebSession();
        clearTelegramOidcNonce();
        setTgUser(null);
        toast.error("Сессия отклонена сервером");
        return;
      }
      const list = Array.isArray(pl.list) ? pl.list : [];
      setPlaylist(list);
      writePlaylistCache(list, out.user.id);
      prewarmVkTrackUrlsFromPlaylist(list);
      setPlaylistLoading(false);
      authBootstrapOnceRef.current = true;
      void registerMiniAppIdentity();
    })();
  };

  useLayoutEffect(() => {
    window.tgplayApplyTelegramWebSession = (raw: unknown) => {
      if (!raw || typeof raw !== "object") return;
      const o = raw as Record<string, unknown>;
      const tok = o.access_token;
      const u = o.user;
      if (typeof tok !== "string" || !u || typeof u !== "object") return;
      const ur = u as Record<string, unknown>;
      const id = ur.id;
      if (typeof id !== "number") return;
      const user: WebSessionUser = {
        id,
        first_name: typeof ur.first_name === "string" ? ur.first_name : "",
        ...(typeof ur.username === "string" && ur.username.trim() ? { username: ur.username } : {}),
      };
      const expires_in = typeof o.expires_in === "number" ? o.expires_in : undefined;
      applyTelegramWebSessionRef.current({ access_token: tok, user, expires_in });
    };
    window.tgplayOnTelegramWebAuthError = (code, detail) => {
      clearTelegramOidcNonce();
      if (code === "telegram") {
        toast.error("Вход через Telegram не выполнен (ошибка от Telegram). Откройте tgplay.fun в Safari и повторите.");
        return;
      }
      if (code === "missing_token") {
        toast.error(
          "Telegram подтвердил вход, но сайт не получил id_token. Откройте https://tgplay.fun в Safari и войдите снова.",
        );
        return;
      }
      console.warn("[TGPlay] Telegram web auth:", code, detail);
      toast.error("Не удалось войти через Telegram");
    };
    return () => {
      delete window.tgplayApplyTelegramWebSession;
      delete window.tgplayOnTelegramWebAuthError;
    };
  }, []);

  // Возврат из OAuth / всплывающего окна: в storage уже токен, а колбэк в WebView иногда не успевает — подхватываем сессию
  useEffect(() => {
    let lastPlaylistSync = 0;
    const apply = () => {
      if (getInitData()) return;
      const u = getStoredWebSessionUser();
      const tok = getWebAccessToken();
      if (!u?.id || !tok) return;
      setTgUser((prev) => getTelegramUser() ?? prev ?? u);
      const now = Date.now();
      if (now - lastPlaylistSync < 2500) return;
      lastPlaylistSync = now;
      void fetchPlaylist().then((pl) => {
        if (pl.authFailed) return;
        const synced = Array.isArray(pl.list) ? pl.list : [];
        setPlaylist(synced);
        writePlaylistCache(pl.list ?? [], u.id);
        prewarmVkTrackUrlsFromPlaylist(synced);
        setPlaylistLoading(false);
        authBootstrapOnceRef.current = true;
      });
      void registerMiniAppIdentity();
    };
    const onVis = () => {
      if (document.visibilityState === "visible") apply();
    };
    const onStorage = (e: StorageEvent) => {
      if (e.key !== WEB_SESSION_STORAGE_KEY) return;
      apply();
    };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("pageshow", apply);
    window.addEventListener("focus", apply);
    window.addEventListener("storage", onStorage);
    const t = window.setTimeout(apply, 0);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("pageshow", apply);
      window.removeEventListener("focus", apply);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  // Сохраняем плейлист в кэш при любом изменении (добавление/удаление), чтобы при следующем открытии сразу показывать актуальный список
  useEffect(() => {
    if (!tgUser?.id || playlistLoading) return;
    writePlaylistCache(playlist, tgUser.id);
  }, [tgUser?.id, playlist, playlistLoading]);

  // ─── Queue ───────────────────────────────────────────────────────
  // Приоритет: активный плейлист (кастомный/шаринговый) > результаты поиска > избранное
  const queue = useMemo(
    () => buildQueue(activePlaylistTracks, tracks, playlist),
    [activePlaylistTracks, tracks, playlist],
  );

  const tracksRef = useRef(tracks);
  tracksRef.current = tracks;
  const activePlaylistTracksRef = useRef(activePlaylistTracks);
  activePlaylistTracksRef.current = activePlaylistTracks;
  const queueRef = useRef(queue);
  queueRef.current = queue;

  const currentIndex = useMemo(() => currentTrack ? queue.findIndex((t) => t.id === currentTrack.id) : -1, [queue, currentTrack]);

  useEffect(() => {
    isShuffleRef.current = isShuffle;
  }, [isShuffle]);

  // ─── Play track ──────────────────────────────────────────────────
  const playTrack = useCallback((track: Track, sourceTracks?: Track[]) => {
    const sameTrackReplay =
      currentTrackIdRef.current === track.id &&
      currentPlayingTrackRef.current?.id === track.id &&
      !!audioRef.current?.src;
    if (sameTrackReplay) {
      const audioEl = audioRef.current;
      if (audioEl) {
        try {
          audioEl.currentTime = 0;
        } catch {
          // ignore
        }
        userPausedRef.current = false;
        bufferingRef.current = false;
        setCurrentTrack(track);
        setCurrentTime(0);
        setIsBuffering(false);
        audioEl.play().then(() => setIsPlaying(true)).catch(() => setIsPlaying(false));
        return;
      }
    }

    const requestGen = ++audioLoadRequestGenRef.current;
    userPausedRef.current = false;
    bufferingRef.current = true;
    if (userNextSkipsTimerRef.current != null) {
      window.clearTimeout(userNextSkipsTimerRef.current);
      userNextSkipsTimerRef.current = null;
    }
    userNextBurstExtraRef.current = 0;

    if (isShuffleRef.current && sourceTracks != null && sourceTracks.length > 0) {
      const tid = track.id?.trim() ?? "";
      if (tid) {
        const h = shuffleRecentTrackIdsRef.current;
        if (h[0] !== tid) {
          h.unshift(tid);
          shuffleRecentTrackIdsRef.current = h.slice(0, SHUFFLE_RECENT_EXCLUDE_MAX);
        }
      }
    }

    // Пустой массив [] не сбрасывает очередь (иначе next/prev после сброса recommendationTracks в UI давали null).
    if (sourceTracks != null && sourceTracks.length > 0) {
      setActivePlaylistTracks(sourceTracks);
    } else if (sourceTracks === undefined) {
      setActivePlaylistTracks(null);
    }

    currentTrackIdRef.current = track.id;
    currentPlayingTrackRef.current = track;
    audioErrorRetryDoneRef.current = false;
    audioErrorReportedRef.current = false;
    syntheticEndedRef.current = false;
    playbackArmedRef.current = false;
    playbackMetaDurationRef.current = track.duration && track.duration > 0 ? track.duration : 0;
    setAudioUrl(null);
    setCurrentTrack(track);
    setIsPlaying(true);
    setIsBuffering(true);
    setCurrentTime(0);
    setDuration(track.duration && track.duration > 0 ? track.duration : 0);
    const urlFromCache = getCachedAudioUrl(track.id);
    trackEvent("track_play", {
      track_id: track.id,
      from_cache: !!urlFromCache,
    });

    // Сразу обновляем системный пуш (без ожидания React effect)
    if ("mediaSession" in navigator) {
      navigator.mediaSession.playbackState = "playing";
      const artwork: MediaImage[] = [];
      const artSrc = track.artwork || (typeof window !== "undefined" ? `${window.location.origin}/icon-track.png` : "");
      if (artSrc) artwork.push({ src: artSrc, sizes: "256x256", type: track.artwork ? "image/jpeg" : "image/png" });
      navigator.mediaSession.metadata = new MediaMetadata({ title: track.title, artist: track.artist, album: "TGPlay", artwork });
    }

    // Кеш → мгновенно. Иначе resolve → прямой VK, Fallback → proxy.
    if (urlFromCache) {
      setAudioPlaybackEpoch((e) => e + 1);
      setAudioUrl(urlFromCache);
      return;
    }
    const pickNextTrackIdForWarm = (): string | undefined => {
      const source = sourceTracks && sourceTracks.length > 1 ? sourceTracks : queueRef.current;
      if (!source || source.length <= 1) return undefined;
      const idx = source.findIndex((t) => t.id === track.id);
      if (idx < 0) return undefined;
      const next = source[(idx + 1) % source.length];
      const nextId = next?.id?.trim();
      return nextId && nextId !== track.id ? nextId : undefined;
    };

    const nextTrackIdForWarm = pickNextTrackIdForWarm();
    resolveAudioUrl(track.id, youtubeResolveMetaForTrack(track), { nextTrackId: nextTrackIdForWarm })
      .then((directUrl) => {
        if (audioLoadRequestGenRef.current !== requestGen) return;
        if (currentTrackIdRef.current !== track.id) return;
        setAudioPlaybackEpoch((e) => e + 1);
        setAudioUrl(directUrl);
      })
      .catch((err: unknown) => {
        if (audioLoadRequestGenRef.current !== requestGen) return;
        if (currentTrackIdRef.current !== track.id) return;
        if (err instanceof AudioResolveRateLimitedError) {
          bufferingRef.current = false;
          setIsBuffering(false);
          setIsPlaying(false);
          const retry = err.retryAfterSec != null ? ` Попробуйте через ${Math.ceil(err.retryAfterSec)} с.` : "";
          toast.error(`Сервер временно ограничил запуск треков.${retry}`);
          trackEvent("error", { place: "play", message: "resolve_rate_limited" });
          return;
        }
        setAudioPlaybackEpoch((e) => e + 1);
        setAudioUrl(getDownloadUrl(track.id, youtubeResolveMetaForTrack(track)));
      });
  }, []);

  const loadMainRecommendations = useCallback(
    (opts?: { refresh?: boolean; wave?: boolean; force?: boolean }) => {
      if (view !== "main") return;
      if (!opts?.force && mainSearchActive) return;
      recLoadAbortRef.current?.abort();
      const ac = new AbortController();
      recLoadAbortRef.current = ac;
      const signal = ac.signal;
      setRecommendationsLoading(true);
      const isWave = !!opts?.wave;
      const effLimit = 100;

      const onFail = () => {
        if (!signal.aborted) {
          setRecommendationTracks([]);
          recommendationsWaveModeRef.current = false;
        }
      };

      if (getAuthorizationHeaderValue()) {
        // «Моя волна» должна формироваться как новая выдача при каждом нажатии,
        // не зависеть от предыдущего списка рекомендаций на странице.
        fetchPersonalRecommendations(signal, { limit: effLimit, refresh: isWave ? true : !!opts?.refresh, wave: isWave })
          .then((items) => {
            if (signal.aborted) return;
            const out = dedupeTracksById(items);
            recommendationsWaveModeRef.current = isWave;
            setRecommendationTracks(out);
            trackEvent("recommendations_load", {
              count: out.length,
              refresh: !!opts?.refresh,
              wave: isWave,
              personal: true,
            });
            if (out.length > 0) {
              void preloadBatchUrls(out.slice(0, 12).map((t) => t.id));
            }
            if (isWave && out.length > 0) {
              shuffleRecentTrackIdsRef.current = [];
              waveRadioSeededRef.current = new Set();
              isShuffleRef.current = true;
              setIsShuffle(true);
              setIsPlayerOpen(true);
              const i = Math.floor(Math.random() * out.length);
              playTrack(out[i]!, out);
            }
          })
          .catch(onFail)
          .finally(() => {
            if (shouldClearRecommendationsLoading(ac, recLoadAbortRef)) setRecommendationsLoading(false);
          });
        return;
      }

      let seeds = recApiSeedTrackIdsRef.current;
      const pl = playlistRecRef.current;
      if (opts?.refresh && pl.length) {
        const picked = pickDistinctRandomTrackIds(pl, RECOMMENDATIONS_SEED_POOL_SIZE, RECOMMENDATIONS_SEED_COUNT);
        if (picked.length) seeds = picked;
      }
      if (!seeds.length) {
        setRecommendationTracks([]);
        setRecommendationsLoading(false);
        return;
      }
      fetchRecommendations(seeds, signal, effLimit)
        .then((items) => {
          if (signal.aborted) return;
          const out = dedupeTracksById(items);
          recommendationsWaveModeRef.current = isWave;
          setRecommendationTracks(out);
          trackEvent("recommendations_load", {
            count: out.length,
            refresh: !!opts?.refresh,
            wave: isWave,
            personal: false,
          });
          if (out.length > 0) {
            void preloadBatchUrls(out.slice(0, 12).map((t) => t.id));
          }
          if (isWave && out.length > 0) {
            shuffleRecentTrackIdsRef.current = [];
            waveRadioSeededRef.current = new Set();
            isShuffleRef.current = true;
            setIsShuffle(true);
            setIsPlayerOpen(true);
            const i = Math.floor(Math.random() * out.length);
            playTrack(out[i]!, out);
          }
        })
        .catch(onFail)
        .finally(() => {
          if (shouldClearRecommendationsLoading(ac, recLoadAbortRef)) setRecommendationsLoading(false);
        });
    },
    [view, mainSearchActive, playTrack],
  );

  const loadMainRecommendationsRef = useRef(loadMainRecommendations);
  loadMainRecommendationsRef.current = loadMainRecommendations;

  const handleDislikeRecommendation = useCallback(async (track: Track) => {
    if (!getAuthorizationHeaderValue()) {
      toast.error("Войдите через Telegram, чтобы сохранять дизлайки");
      return;
    }
    const ok = await dislikeTrack(track.id, track.artist, track.genreId);
    if (!ok) {
      toast.error("Не удалось сохранить");
      return;
    }
    const tid = track.id;
    trackEvent("button_dislike_track", { track_id: tid });
    setRecommendationTracks((prev) => prev.filter((t) => t.id !== tid));
    setTracks((prev) => prev.filter((t) => t.id !== tid));
    setPlaylistSearchResults((prev) => prev.filter((t) => t.id !== tid));
    setActivePlaylistTracks((prev) => {
      if (!prev?.some((t) => t.id === tid)) return prev;
      const next = prev.filter((t) => t.id !== tid);
      return next.length > 0 ? next : null;
    });

    if (currentTrackIdRef.current !== tid) {
      toast.success("Убрали из подборки");
      return;
    }

    const ap0 = activePlaylistTracksRef.current;
    const tr = tracksRef.current;
    const pl = playlistRecRef.current;
    const oldQueue = buildQueue(ap0, tr, pl);
    const idx = oldQueue.findIndex((t) => t.id === tid);
    let newActive: Track[] | null = ap0;
    if (ap0?.some((t) => t.id === tid)) {
      const f = ap0.filter((t) => t.id !== tid);
      newActive = f.length > 0 ? f : null;
    }
    const newQueue = buildQueue(newActive, tr, pl);

    const stopPlayback = () => {
      const a = audioRef.current;
      if (a) {
        a.pause();
        a.removeAttribute("src");
        a.load();
      }
      playbackArmedRef.current = false;
      currentTrackIdRef.current = null;
      currentPlayingTrackRef.current = null;
      setCurrentTrack(null);
      setAudioUrl(null);
      setIsPlaying(false);
      setIsBuffering(false);
      setCurrentTime(0);
      setDuration(0);
    };

    if (newQueue.length === 0) {
      stopPlayback();
      toast.success("Убрали из подборки");
      return;
    }

    let nextTrack: Track;
    if (idx < 0) {
      nextTrack = newQueue[0]!;
    } else if (isShuffleRef.current) {
      const ni = getRandomNextIndexAvoidingRecent(idx, oldQueue, shuffleRecentTrackIdsRef.current);
      const candidate = ni >= 0 ? oldQueue[ni] : newQueue[0];
      nextTrack = candidate!;
      if (nextTrack.id === tid) {
        nextTrack = newQueue.find((t) => t.id !== tid) ?? newQueue[0]!;
      }
    } else {
      nextTrack = idx < oldQueue.length - 1 ? oldQueue[idx + 1]! : newQueue[0]!;
      if (nextTrack.id === tid) {
        nextTrack = newQueue.find((t) => t.id !== tid) ?? newQueue[0]!;
      }
    }

    const playSource =
      newActive && newActive.length > 0 ? newActive : newQueue.length > 0 ? newQueue : undefined;
    if (playSource?.length) {
      playTrack(nextTrack, playSource);
    } else {
      stopPlayback();
    }
    toast.success("Убрали из подборки");
  }, [playTrack]);

  // Смена аккаунта / гостевых seed — снова разрешаем автозагрузку один раз.
  useEffect(() => {
    const aid = tgUser?.id ?? getStoredWebSessionUser()?.id ?? null;
    const key = getAuthorizationHeaderValue()
      ? `u:${aid ?? ""}`
      : `g:${[...recApiSeedTrackIds].sort().join(",")}`;
    if (key !== recommendationsBootstrapKeyRef.current) {
      recommendationsBootstrapKeyRef.current = key;
      recommendationsBootstrappedRef.current = false;
    }
  }, [tgUser?.id, recApiSeedTrackIds]);

  // Первая подгрузка рекомендаций на главной (один раз на ключ выше). Возврат с профиля не триггерит.
  useEffect(() => {
    if (view !== "main" || mainSearchActive) return;
    if (recommendationsBootstrappedRef.current) return;
    // Персональные реки: user id на сервере из tma/Bearer — не ждём tgUser из initDataUnsafe (на части клиентов он позже).
    if (!getAuthorizationHeaderValue() && !recApiSeedTrackIds.length) return;
    recommendationsBootstrappedRef.current = true;
    // Каждый новый вход (cold-open) — свежий ПОЛНЫЙ список рекомендаций (refresh форсирует новую выдачу
    // на сервере). Возврат с профиля сюда не попадает (bootstrap ref уже true).
    loadMainRecommendationsRef.current(
      recommendationsWaveModeRef.current ? { wave: true } : { refresh: true },
    );
    return () => {
      recLoadAbortRef.current?.abort();
      recommendationsBootstrappedRef.current = false;
    };
    // Не зависим от loadMainRecommendations: иначе смена ссылки → cleanup abort → ref bootstrapped=true → повторный fetch не стартует (вечный спиннер / пустая лента).
  }, [view, mainSearchActive, tgUser?.id, recApiSeedTrackIds]);

  // Возврат в Mini App / на вкладку после фона — новая выдача (refresh на сервере для персональных).
  // В Telegram WebView webview переиспользуется и document.visibilitychange может не сработать,
  // поэтому слушаем несколько сигналов: visibility, focus, pageshow и нативный Telegram 'activated'.
  useEffect(() => {
    let lastRefreshAt = 0;
    let wasInactive = false;

    const markInactive = () => {
      wasInactive = true;
    };

    const resumeRefresh = (force: boolean) => {
      if (view !== "main" || mainSearchActive) return;
      if (!getAuthorizationHeaderValue() && !recApiSeedTrackIds.length) return;
      // Обновляем только при реальном возобновлении (был фон) либо форс-сигнале/устаревшей ленте.
      if (!force && !wasInactive && !recommendationsStaleRef.current) return;
      const now = Date.now();
      // Троттл: несколько сигналов подряд (focus+visibility+activated) не должны слать дубли запросов.
      if (now - lastRefreshAt < 4000) return;
      lastRefreshAt = now;
      wasInactive = false;
      recommendationsStaleRef.current = false;
      loadMainRecommendationsRef.current({
        refresh: true,
        wave: recommendationsWaveModeRef.current ? true : false,
      });
    };

    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        markInactive();
        return;
      }
      resumeRefresh(false);
    };
    const onFocus = () => resumeRefresh(false);
    const onBlur = () => markInactive();
    const onPageShow = () => resumeRefresh(false);
    const onPageHide = () => markInactive();
    // Telegram шлёт 'activated' именно при повторной активации Mini App — форсим обновление.
    const onTgResumed = () => resumeRefresh(true);

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("tgplay-app-resumed", onTgResumed);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("pageshow", onPageShow);
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("tgplay-app-resumed", onTgResumed);
    };
  }, [view, mainSearchActive, recApiSeedTrackIds]);

  useEffect(() => {
    const prev = prevMainSearchActiveForRecsRef.current;
    prevMainSearchActiveForRecsRef.current = mainSearchActive;
    if (view !== "main" || mainSearchActive) return;
    if (!prev) return;
    if (recommendationTracks.length > 0) return;
    loadMainRecommendationsRef.current({});
  }, [view, mainSearchActive, recommendationTracks.length]);

  // «Моя волна» как радио: по мере проигрывания подмешиваем в очередь похожие на ТЕКУЩИЙ трек,
  // чтобы волна звучала как бесконечная станция (рандом + похожие), а не крутила фиксированный список.
  const currentTrackId = currentTrack?.id;
  useEffect(() => {
    if (!recommendationsWaveModeRef.current) return;
    if (!currentTrackId) return;
    const seedId = currentTrackId;
    if (waveRadioSeededRef.current.has(seedId)) return;
    if (waveRadioFetchingRef.current) return;
    // Предохранитель: не раздуваем очередь бесконечно.
    if (queueRef.current.length > 600) {
      waveRadioSeededRef.current.add(seedId);
      return;
    }
    waveRadioFetchingRef.current = true;
    let cancelled = false;
    const ac = new AbortController();
    fetchRecommendations([seedId], ac.signal, 25)
      .then((items) => {
        if (cancelled) return;
        waveRadioSeededRef.current.add(seedId);
        const existing = new Set(queueRef.current.map((t) => t.id));
        const fresh = dedupeTracksById(items).filter((t) => t.id && !existing.has(t.id));
        if (fresh.length === 0) return;
        setActivePlaylistTracks((prevTracks) => {
          const base = prevTracks && prevTracks.length > 0 ? prevTracks : queueRef.current;
          const seen = new Set(base.map((t) => t.id));
          const merged = [...base];
          for (const t of fresh) {
            if (t.id && !seen.has(t.id)) {
              seen.add(t.id);
              merged.push(t);
            }
          }
          return merged;
        });
        void preloadBatchUrls(fresh.slice(0, 6).map((t) => t.id));
      })
      .catch(() => {
        /* радио best-effort: не мешаем воспроизведению при сбое */
      })
      .finally(() => {
        waveRadioFetchingRef.current = false;
      });
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [currentTrackId]);

  const handleNextImmediate = useCallback(() => {
    const qNow = queueRef.current;
    const idxNow = currentTrackIdRef.current
      ? qNow.findIndex((t) => t.id === currentTrackIdRef.current)
      : -1;
    // Специальный кейс: трек запущен по шеринг‑ссылке (одиночный трек в activePlaylistTracks),
    // а у пользователя есть основной плейлист. При нажатии «вперёд» переходим к плейлисту:
    // первый трек или рандомный, если включён shuffle.
    if (
      sharedEntryTrackIdRef.current &&
      activePlaylistTracksRef.current &&
      activePlaylistTracksRef.current.length === 1 &&
      activePlaylistTracksRef.current[0].id === sharedEntryTrackIdRef.current &&
      playlistRecRef.current.length > 0
    ) {
      const base = playlistRecRef.current;
      const nextTrack = isShuffleRef.current
        ? base[Math.floor(Math.random() * base.length)]
        : base[0];
      sharedEntryTrackIdRef.current = null;
      setActivePlaylistTracks(base);
      playTrack(nextTrack, base);
      return;
    }

    if (qNow.length === 0 || idxNow === -1) return;
    const nextIndex = isShuffleRef.current
      ? getRandomNextIndexAvoidingRecent(idxNow, qNow, shuffleRecentTrackIdsRef.current)
      : (idxNow + 1) % qNow.length;
    if (nextIndex === -1) return;
    playTrack(qNow[nextIndex]!, qNow);
  }, [playTrack]);

  const handleNextAuto = useCallback(() => {
    if (userNextSkipsTimerRef.current != null) {
      window.clearTimeout(userNextSkipsTimerRef.current);
      userNextSkipsTimerRef.current = null;
    }
    userNextBurstExtraRef.current = 0;
    handleNextImmediate();
  }, [handleNextImmediate]);

  const flushUserNextBurstExtra = useCallback(() => {
    const extra = userNextBurstExtraRef.current;
    userNextBurstExtraRef.current = 0;
    if (extra <= 0) return;
    const q = queueRef.current;
    if (q.length === 0) return;
    const curId = currentTrackIdRef.current;
    if (!curId) return;
    let cur = q.findIndex((t) => t.id === curId);
    if (cur < 0) return;

    if (!isShuffleRef.current) {
      const targetIdx = (cur + extra) % q.length;
      playTrack(q[targetIdx]!, q);
      return;
    }

    for (let s = 0; s < extra; s++) {
      const ni = getRandomNextIndexAvoidingRecent(cur, q, shuffleRecentTrackIdsRef.current);
      if (ni === -1) break;
      cur = ni;
      const stepId = q[cur]?.id?.trim() ?? "";
      if (stepId) {
        const h = shuffleRecentTrackIdsRef.current;
        if (h[0] !== stepId) {
          h.unshift(stepId);
          shuffleRecentTrackIdsRef.current = h.slice(0, SHUFFLE_RECENT_EXCLUDE_MAX);
        }
      }
    }
    playTrack(q[cur]!, q);
  }, [playTrack]);

  const handleNext = useCallback(() => {
    if (
      sharedEntryTrackIdRef.current &&
      activePlaylistTracksRef.current &&
      activePlaylistTracksRef.current.length === 1 &&
      activePlaylistTracksRef.current[0].id === sharedEntryTrackIdRef.current &&
      playlistRecRef.current.length > 0
    ) {
      if (userNextSkipsTimerRef.current != null) {
        window.clearTimeout(userNextSkipsTimerRef.current);
        userNextSkipsTimerRef.current = null;
      }
      userNextBurstExtraRef.current = 0;
      handleNextImmediate();
      return;
    }

    if (userNextSkipsTimerRef.current == null) {
      handleNextImmediate();
      userNextBurstExtraRef.current = 0;
    } else {
      userNextBurstExtraRef.current += 1;
    }
    if (userNextSkipsTimerRef.current != null) {
      window.clearTimeout(userNextSkipsTimerRef.current);
    }
    userNextSkipsTimerRef.current = window.setTimeout(() => {
      userNextSkipsTimerRef.current = null;
      flushUserNextBurstExtra();
    }, 140);
  }, [handleNextImmediate, flushUserNextBurstExtra]);

  const handlePrev = useCallback(() => {
    // Специальный кейс для трека из шеринг‑ссылки: переключаемся в основной плейлист.
    if (
      sharedEntryTrackIdRef.current &&
      activePlaylistTracks &&
      activePlaylistTracks.length === 1 &&
      activePlaylistTracks[0].id === sharedEntryTrackIdRef.current &&
      playlist.length > 0
    ) {
      const base = playlist;
      const prevTrack = isShuffle
        ? base[Math.floor(Math.random() * base.length)]
        : base[0];
      sharedEntryTrackIdRef.current = null;
      setActivePlaylistTracks(base);
      playTrack(prevTrack, base);
      return;
    }

    if (queue.length === 0 || currentIndex === -1) return;
    // 1 тап — в начало трека; 2 тапа — shuffle: рандомный трек, без shuffle: предыдущий трек
    if (prevTapTimeoutRef.current != null) {
      window.clearTimeout(prevTapTimeoutRef.current);
      prevTapTimeoutRef.current = null;
      if (isShuffle) {
        const randomIndex = getRandomNextIndexAvoidingRecent(currentIndex, queue, shuffleRecentTrackIdsRef.current);
        if (randomIndex !== -1) playTrack(queue[randomIndex], queue);
      } else {
        const prevIndex = (currentIndex - 1 + queue.length) % queue.length;
        playTrack(queue[prevIndex], queue);
      }
      return;
    }
    prevTapTimeoutRef.current = window.setTimeout(() => {
      prevTapTimeoutRef.current = null;
      const audio = audioRef.current;
      if (audio) {
        audio.currentTime = 0;
        setCurrentTime(0);
      }
    }, 220);
  }, [queue, currentIndex, playTrack, isShuffle]);

  useEffect(() => {
    handleNextAutoRef.current = handleNextAuto;
  }, [handleNextAuto]);
  useEffect(() => { repeatModeRef.current = repeatMode; }, [repeatMode]);

  // ─── Deep link: tr_* → воспроизвести трек, pl_* → открыть шаринговый плейлист ───
  const playTrackRef = useRef(playTrack);
  playTrackRef.current = playTrack;
  useEffect(() => {
    const DEEP_LINK_POLL_MS = 250;
    const DEEP_LINK_POLL_MAX = 48;

    const tryDeepLink = () => {
      const param = getStartParam();
      if (!param) return;

      if (!param.startsWith("tr_") && !param.startsWith("pl_")) {
        tgplayDeepLinkNonTrackParam = true;
        return;
      }

      if (tgplayDeepLinkConsumed === param) return;
      tgplayDeepLinkConsumed = param;

      if (param.startsWith("tr_")) {
        const trackId = parseStartParamTrackId(param.slice(3));
        if (!trackId) {
          toast.error("Некорректная ссылка");
          return;
        }
        preloadTrackUrl(trackId);
        getTrackInfo(trackId)
          .then((track) => {
            if (track) {
              setIsPlayerOpen(true);
              sharedEntryTrackIdRef.current = track.id;
              playTrackRef.current(track, [track]);
            } else {
              toast.error("Трек не найден");
            }
          })
          .catch(() => toast.error("Трек не найден"));
        return;
      }

      const shareId = param.slice(3);
      if (!shareId) {
        toast.error("Некорректная ссылка");
        return;
      }
      setSharedShareId(shareId);
      getSharedPlaylist(shareId)
        .then((data) => {
          if (!data) {
            toast.error("Плейлист не найден");
            return;
          }
          setSharedPlaylist({ name: data.name, items: data.items });
          if (typeof window !== "undefined") {
            const key = `tgplay_shared_saved_${shareId}`;
            const savedId = window.localStorage.getItem(key);
            if (savedId) {
              setSharedSaved(true);
              setSharedSavedPlaylistId(savedId);
            } else {
              setSharedSaved(false);
              setSharedSavedPlaylistId(null);
            }
          }
          setView("shared");
        })
        .catch(() => toast.error("Плейлист не найден"));
    };

    tryDeepLink();

    const onSignal = () => tryDeepLink();
    window.addEventListener("tgplay-webapp-ready", onSignal);
    window.addEventListener("hashchange", onSignal);

    let n = 0;
    const pollIv = window.setInterval(() => {
      if (tgplayDeepLinkConsumed || tgplayDeepLinkNonTrackParam) {
        window.clearInterval(pollIv);
        return;
      }
      tryDeepLink();
      if (++n >= DEEP_LINK_POLL_MAX) window.clearInterval(pollIv);
    }, DEEP_LINK_POLL_MS);

    return () => {
      window.removeEventListener("tgplay-webapp-ready", onSignal);
      window.removeEventListener("hashchange", onSignal);
      window.clearInterval(pollIv);
    };
  }, []);

  // ─── Preload соседних треков (resolve URL в кеш; не резолвим текущий — уже в playTrack) ─────────────────
  useEffect(() => {
    if (queue.length === 0 || currentIndex === -1) return;
    const currentId = currentTrackIdRef.current;
    const nextIdx = (currentIndex + 1) % queue.length;
    const prevIdx = (currentIndex - 1 + queue.length) % queue.length;
    if (queue[nextIdx].id !== currentId) preloadTrackUrl(queue[nextIdx].id, youtubeResolveMetaForTrack(queue[nextIdx]));
    if (prevIdx !== nextIdx && queue[prevIdx].id !== currentId)
      preloadTrackUrl(queue[prevIdx].id, youtubeResolveMetaForTrack(queue[prevIdx]));
    const aheadIds: string[] = [];
    for (let step = 1; step <= Math.min(4, queue.length - 1); step += 1) {
      const idx = (currentIndex + step) % queue.length;
      const tid = queue[idx]?.id;
      if (tid && tid !== currentId) aheadIds.push(tid);
    }
    if (aheadIds.length > 0) void preloadBatchUrls(aheadIds);
    if (isShuffle && queue.length > 1) {
      const picks: string[] = [];
      const seen = new Set<string>(currentId ? [currentId] : []);
      let guard = 0;
      while (picks.length < 3 && guard++ < 24) {
        const j = Math.floor(Math.random() * queue.length);
        const t = queue[j]!;
        if (j !== currentIndex && !seen.has(t.id)) {
          seen.add(t.id);
          picks.push(t.id);
        }
      }
      if (picks.length) void preloadBatchUrls(picks);
    }
  }, [queue, currentIndex, isShuffle]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      userPausedRef.current = false;
      audio.play().then(() => setIsPlaying(true)).catch(() => setIsPlaying(false));
    } else {
      userPausedRef.current = true;
      audio.pause();
      setIsPlaying(false);
    }
  }, []);

  const handleSeek = useCallback((value: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    seekingRef.current = true;
    setCurrentTime(value);
    audio.currentTime = value;
    const unlock = () => { seekingRef.current = false; };
    audio.addEventListener("seeked", unlock, { once: true });
    setTimeout(unlock, 500);
  }, []);

  // ─── Playlist actions ────────────────────────────────────────────
  const handleRemove = useCallback(async (track: Track) => {
    try {
      const pid = canonicalPlaylistTrackId(track.id);
      if (await removeFromPlaylist(pid)) {
        trackEvent("button_remove");
        setPlaylist((prev) => prev.filter((t) => canonicalPlaylistTrackId(t.id) !== pid));
        toast.success("Удалено");
      }
    } catch {
      toast.error("Не удалось удалить");
    }
  }, []);

  const pollBotDeliveredUntil = useCallback(async (trackId: string): Promise<boolean> => {
    const want = canonicalPlaylistTrackId(trackId);
    const deadline = Date.now() + BOT_DELIVER_POLL_MAX_MS;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, BOT_DELIVER_POLL_MS));
      const p = await fetchBotAudioDelivered();
      setBotDeliveredIds(new Set(p.trackIds.map((id) => canonicalPlaylistTrackId(id))));
      setBotDeliveredVerifiedLiveIds(new Set(p.verifiedLiveTrackIds.map((id) => canonicalPlaylistTrackId(id))));
      if (p.trackIds.some((id) => canonicalPlaylistTrackId(id) === want)) return true;
    }
    return false;
  }, []);

  /** Только отправка аудио в чат с ботом (без избранного — избранное только через сердечко). */
  const handleSendToBotOnly = useCallback(
    async (track: Track) => {
      if (!isLoggedIn) {
        toast.error("Войдите через Telegram");
        return;
      }
      const botKey = canonicalPlaylistTrackId(track.id);
      trackEvent("button_download", { extra: { track_id: botKey } });
      setSendToBotPendingIds((prev) => new Set(prev).add(botKey));
      const t = toast.loading("Отправляем в чат с ботом…");
      try {
        const queued = await sendToBot(botKey);
        if (!queued) {
          toast.dismiss(t);
          toast.error("Не удалось поставить в очередь. Проверьте сеть.");
          return;
        }
        const ok = await pollBotDeliveredUntil(botKey);
        toast.dismiss(t);
        if (ok) {
          toast.success("Трек в чате с ботом");
        } else {
          toast.error("Доставка задерживается — проверьте чат с ботом. При необходимости нажмите снова позже.");
        }
      } finally {
        setSendToBotPendingIds((prev) => {
          const next = new Set(prev);
          next.delete(botKey);
          return next;
        });
      }
    },
    [isLoggedIn, pollBotDeliveredUntil],
  );

  const handleAddToPlaylist = useCallback(async (track: Track) => {
    if (!isLoggedIn) { toast.error("Войдите через Telegram"); return; }
    trackEvent("button_add_to_favorites");
    const t = toast.loading("Добавляем...");
    try {
      const addResult = await addToPlaylist(track);
      const list = await fetchPlaylistTracks();
      setPlaylist(list);
      prewarmVkTrackUrlsFromPlaylist(list);
      toast.dismiss(t);
      if (addResult.ok && addResult.status === "already_exists") {
        toast.success("Трек уже в плейлисте");
      } else if (addResult.ok) {
        // Новый трек влияет на персональные рекомендации (seed = последние добавленные):
        // помечаем ленту устаревшей — обновится при следующем возврате/возобновлении.
        recommendationsStaleRef.current = true;
        toast.success("Трек добавлен в плейлист");
      } else {
        toast.error("Не удалось добавить");
      }
    } catch {
      toast.dismiss(t);
      toast.error("Ошибка");
    }
  }, [isLoggedIn]);

  const handleCloseMiniPlayer = useCallback(() => {
    const audio = audioRef.current;
    if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
    bufferingRef.current = false;
    userPausedRef.current = false;
    playbackArmedRef.current = false;
    setCurrentTrack(null); setAudioUrl(null);
    setIsPlaying(false); setIsBuffering(false);
    setCurrentTime(0); setDuration(0); setIsPlayerOpen(false);
    setActivePlaylistTracks(null); // Сбрасываем активный плейлист при закрытии плеера
  }, []);

  // ─── Audio events (ОДИН раз, refs для актуального состояния) ─────
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onTimeUpdate = () => {
      if (bufferingRef.current || seekingRef.current) return;
      const now = Date.now();
      if (now - lastTimeUpdateRef.current < 200) return;
      lastTimeUpdateRef.current = now;
      setCurrentTime(audio.currentTime);

      const meta = playbackMetaDurationRef.current;
      const ad = audio.duration;
      // Preview / усечённый поток: длительность в плеере заметно короче метаданных — переходим к следующему по факту конца потока.
      if (
        playbackArmedRef.current &&
        meta > 15 &&
        ad > 0 &&
        Number.isFinite(ad) &&
        ad < meta * 0.85 &&
        audio.currentTime >= ad - 0.35 &&
        !syntheticEndedRef.current &&
        !userPausedRef.current
      ) {
        syntheticEndedRef.current = true;
        trackEvent("track_finish", { track_id: currentTrackIdRef.current ?? undefined, synthetic: true });
        if (repeatModeRef.current === "one") {
          syntheticEndedRef.current = false;
          audio.currentTime = 0;
          userPausedRef.current = false;
          audio.play().catch(() => {
            setIsPlaying(false);
          });
        } else {
          handleNextAutoRef.current();
        }
      }
    };

    const onDurationChange = () => {
      const ad = audio.duration;
      if (!ad || !Number.isFinite(ad) || ad <= 0) return;
      const meta = playbackMetaDurationRef.current;
      if (meta > 0 && ad < meta * 0.85) {
        setDuration(ad);
        return;
      }
      if (meta > 0) {
        if (ad >= meta * 1.75 && ad <= meta * 2.2) {
          setDuration(meta);
          return;
        }
        if (ad > meta * 1.45) {
          setDuration(meta);
          return;
        }
      }
      setDuration(ad);
    };

    const onPlaying = () => {
      // Трек РЕАЛЬНО играет — снимаем буферизацию
      bufferingRef.current = false;
      setIsBuffering(false);
      setIsPlaying(true);
      // Успешное воспроизведение сбрасывает счётчик подряд недоступных треков.
      consecutiveAudioSkipRef.current = 0;
      // Обновляем пуш
      if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
    };

    const onPause = () => {
      // Игнорируем pause при буферизации (смена src вызывает pause)
      if (bufferingRef.current) return;
      // Игнорируем если пользователь не нажимал паузу
      // (браузер может вызвать pause при seeking и т.д.)
      if (!userPausedRef.current) return;
      setIsPlaying(false);
    };

    const onWaiting = () => {
      bufferingRef.current = true;
      setIsBuffering(true);
    };

    const onCanPlay = () => {
      // Данные загружены — если мы не на паузе, буферизация окончена
      if (!userPausedRef.current) {
        bufferingRef.current = false;
        setIsBuffering(false);
      }
    };

    const onEnded = () => {
      if (!playbackArmedRef.current) return;
      trackEvent("track_finish", { track_id: currentTrackIdRef.current ?? undefined });
      if (repeatModeRef.current === "one") {
        const audioEl = audioRef.current;
        if (!audioEl) {
          handleNextAutoRef.current();
          return;
        }
        audioEl.currentTime = 0;
        userPausedRef.current = false;
        audioEl.play().catch(() => {
          setIsPlaying(false);
        });
        return;
      }
      handleNextAutoRef.current();
    };

    const onError = () => {
      if (bufferingRef.current && audio.error?.code === MediaError.MEDIA_ERR_ABORTED) return;
      if (audioErrorReportedRef.current) return;
      bufferingRef.current = false;
      setIsBuffering(false);
      setIsPlaying(false);
      toast.error("Не удалось воспроизвести трек");
      trackEvent("error", { place: "play", message: "play_failed" });
    };

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("loadedmetadata", onDurationChange);
    audio.addEventListener("durationchange", onDurationChange);
    audio.addEventListener("playing", onPlaying);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("waiting", onWaiting);
    audio.addEventListener("canplay", onCanPlay);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("error", onError);

    return () => {
      if (prevTapTimeoutRef.current != null) {
        window.clearTimeout(prevTapTimeoutRef.current);
        prevTapTimeoutRef.current = null;
      }
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("loadedmetadata", onDurationChange);
      audio.removeEventListener("durationchange", onDurationChange);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("waiting", onWaiting);
      audio.removeEventListener("canplay", onCanPlay);
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("error", onError);
    };
  }, []);

  // ─── useHlsAudio ─────────────────────────────────────────────────
  const onAudioReady = useCallback(() => {
    playbackArmedRef.current = true;
    bufferingRef.current = false;
    setIsPlaying(true);
    setIsBuffering(false);
    perfAudioPlaying();
  }, []);

  const onAudioError = useCallback((msg: string) => {
    playbackArmedRef.current = false;
    bufferingRef.current = false;
    setIsBuffering(false);
    const trackId = currentTrackIdRef.current;
    const tr = currentPlayingTrackRef.current;
    const resolveMeta =
      trackId && tr && tr.id === trackId ? youtubeResolveMetaForTrack(tr) : undefined;

    // Мягкая обработка сетевых ошибок аудио:
    // 1) первый раз пробуем обновить прямой URL;
    // 2) если не удалось — пробуем proxy URL;
    // 3) если снова ошибка для этого трека — тихо переходим к следующему.
    if (msg === "Ошибка загрузки аудио" && trackId) {
      if (!audioErrorRetryDoneRef.current) {
        audioErrorRetryDoneRef.current = true;
        resolveAudioUrlWithRefresh(trackId, resolveMeta)
          .then((newUrl) => {
            if (currentTrackIdRef.current === trackId) {
              setAudioPlaybackEpoch((e) => e + 1);
              setAudioUrl(newUrl);
            }
          })
          .catch((err: unknown) => {
            if (currentTrackIdRef.current !== trackId) return;
            if (err instanceof AudioResolveRateLimitedError) {
              setIsPlaying(false);
              const retry = err.retryAfterSec != null ? ` Попробуйте через ${Math.ceil(err.retryAfterSec)} с.` : "";
              toast.error(`Сервер временно ограничил запуск треков.${retry}`);
              trackEvent("error", { place: "audio", message: "resolve_refresh_rate_limited" });
              return;
            }
            setAudioPlaybackEpoch((e) => e + 1);
            setAudioUrl(getDownloadUrl(trackId, resolveMeta));
          });
        return;
      }

      // Повторная ошибка на этом же треке — тихо переходим к следующему (если есть куда),
      // чтобы плеер не «застывал» на битом треке.
      audioErrorReportedRef.current = true;
      setIsPlaying(false);
      trackEvent("error", { place: "audio", message: "track_unavailable_after_retry" });
      // Защита от бесконечного проскока, если подряд недоступны несколько треков (например, сеть упала).
      if (consecutiveAudioSkipRef.current >= 4) {
        consecutiveAudioSkipRef.current = 0;
        toast.error("Не удалось воспроизвести несколько треков подряд. Проверьте соединение.");
        return;
      }
      const unavailableLabel = tr
        ? [tr.artist, tr.title].filter(Boolean).join(" — ").trim()
        : "";
      if (queueRef.current.length > 1) {
        consecutiveAudioSkipRef.current += 1;
        // Понятный статус вместо «молчаливого» проскока битого трека.
        toast(
          unavailableLabel
            ? `«${unavailableLabel}» сейчас недоступен — пропускаю`
            : "Трек недоступен — пропускаю",
        );
        handleNextAutoRef.current();
      } else {
        toast.error(
          unavailableLabel
            ? `«${unavailableLabel}» сейчас недоступен.`
            : "Не удалось воспроизвести трек. Попробуйте другой.",
        );
      }
      return;
    }

    // Прочие ошибки аудио — показываем аккуратный тост и логируем
    audioErrorReportedRef.current = true;
    setIsPlaying(false);
    toast.error(msg);
    trackEvent("error", { place: "audio", message: msg });
  }, []);

  const onPlayRejected = useCallback(() => {
    // Автоплей отклонён (политика WebView) — снимаем спиннер везде, иначе «вечная» буферизация.
    bufferingRef.current = false;
    setIsBuffering(false);
    if (isAndroid()) {
      setIsPlaying(false);
      return;
    }
    toast("Нажмите ▶ для воспроизведения");
  }, []);

  useHlsAudio(audioRef, audioUrl, audioPlaybackEpoch, onAudioReady, onAudioError, onPlayRejected);

  useMediaSession(currentTrack, isPlaying, togglePlay, handleNext, handlePrev, handleSeek, duration, currentTime);

  const handleBackFromProfile = useCallback(() => {
    setView("main");
    fetchPlaylistTracks()
      .then((list) => {
        setPlaylist(list);
        prewarmVkTrackUrlsFromPlaylist(list);
      })
      .catch(() => {});
    // Если трек не играет, сбрасываем активный плейлист
    if (!currentTrack) setActivePlaylistTracks(null);
  }, [currentTrack]);

  /** Выход из веб-сессии OAuth (Bearer). В Mini App без веб-токена кнопка не показывается. */
  const handleProfileLogout = useCallback(() => {
    if (!getWebAccessToken()) return;
    void postAuthLogout();
    const uid = tgUser?.id ?? null;
    // Иначе лента рекомендаций и seed остаются от прошлой сессии (гость «под кнопкой» как залогиненный).
    recLoadAbortRef.current?.abort();
    recLoadAbortRef.current = null;
    setRecommendationTracks([]);
    setRecApiSeedTrackIds([]);
    setRecommendationsLoading(false);
    recommendationsWaveModeRef.current = false;
    recommendationsBootstrappedRef.current = false;
    clearTelegramWebAuthStorage();
    const miniUser = getTelegramUser();
    const next = miniUser ?? null;

    if (!next) {
      if (uid != null) clearPlaylistCache(uid);
      setPlaylist([]);
      authBootstrapOnceRef.current = false;
      setPlaylistLoading(false);
    } else {
      void fetchPlaylist().then((result) => {
        if (result.authFailed) {
          clearPlaylistCache(miniUser?.id ?? null);
          setPlaylist([]);
        } else {
          const nextList = Array.isArray(result.list) ? result.list : [];
          setPlaylist(nextList);
          if (miniUser?.id) writePlaylistCache(nextList, miniUser.id);
          prewarmVkTrackUrlsFromPlaylist(nextList);
        }
      });
    }
    setTgUser(next);
    setSessionExpired(false);
    setView("main");
    trackEvent("button_profile_logout_web");
    toast.success("Вы вышли из аккаунта");
  }, [tgUser?.id]);

  const handleSelectTrack = useCallback((track: Track, sourceTracks?: Track[]) => {
    // Первый запуск плеера по треку — открываем большой плеер.
    // Если плеер уже играет (есть currentTrack), трек меняется только в мини-плеере.
    if (!currentTrack) {
      setIsPlayerOpen(true);
    }
    shuffleRecentTrackIdsRef.current = [];
    playTrack(track, sourceTracks);
  }, [currentTrack, playTrack]);

  const handlePreloadTrack = useCallback((track: Track) => {
    void preloadTrackUrl(track.id, youtubeResolveMetaForTrack(track));
  }, []);

  const handleBackFromShared = useCallback(() => {
    setView("profile");
    setSharedPlaylist(null);
    // Если трек не играет, сбрасываем активный плейлист
    if (!currentTrack) setActivePlaylistTracks(null);
  }, [currentTrack]);

  const handleAddToPlaylistSheetAdded = useCallback(() => {
    fetchPlaylistTracks()
      .then((list) => {
        setPlaylist(list);
        prewarmVkTrackUrlsFromPlaylist(list);
      })
      .catch(() => {});
  }, []);

  const handleCloseAddToPlaylistSheet = useCallback(() => {
    setAddToPlaylistSheetTrack(null);
    setAddToPlaylistSheetFromFavorites(false);
    fetchPlaylistTracks()
      .then((list) => {
        setPlaylist(list);
        prewarmVkTrackUrlsFromPlaylist(list);
      })
      .catch(() => {});
  }, []);

  const handleProfileRefresh = useCallback(() => {
    setProfileRefreshTrigger((t) => t + 1);
  }, []);

  const handleOpenAddToPlaylist = useCallback((track: Track, alreadyInFavorites?: boolean) => {
    setAddToPlaylistSheetTrack(track);
    setAddToPlaylistSheetFromFavorites(!!alreadyInFavorites);
  }, []);

  const handleOpenShareMenu = useCallback((track: Track) => {
    setShareMenuTrack(track);
  }, []);

  const handleToggleShuffle = useCallback(() => {
    setIsShuffle((prev) => {
      const next = !prev;
      if (!next) {
        shuffleRecentTrackIdsRef.current = [];
        isShuffleRef.current = false;
      } else {
        isShuffleRef.current = true;
      }
      return next;
    });
  }, []);

  const handleCycleRepeatMode = useCallback(() => {
    setRepeatMode((prev) => (prev === "off" ? "one" : "off"));
  }, []);

  const handleOpenPlaylistScreen = useCallback(
    async (opts: { id: string; name: string; isFavorites: boolean; isAdded?: boolean }) => {
      setOpenedPlaylist(opts);
      setView("playlist");
      if (opts.isFavorites) {
        setOpenedPlaylistTracks(playlist);
        return;
      }
      try {
        const tracks = await getPlaylistTracks(opts.id);
        setOpenedPlaylistTracks(tracks);
      } catch {
        setOpenedPlaylistTracks([]);
      }
    },
    [playlist],
  );

  const handleBackFromPlaylistView = useCallback(() => {
    setView("profile");
    setOpenedPlaylist(null);
    setOpenedPlaylistTracks([]);
    setPlaylistSearchQuery("");
    setPlaylistSearchResults([]);
    setPlaylistSearchHasMore(false);
    setProfileRefreshTrigger((prev) => prev + 1);
  }, []);

  // Прогреваем URL первых треков в открытом плейлисте заранее — первый тап не ждёт cold resolve.
  useEffect(() => {
    if (openedPlaylistTracks.length === 0) return;
    const ids = openedPlaylistTracks.slice(0, 12).map((t) => t.id);
    if (ids.length) void preloadBatchUrls(ids);
  }, [openedPlaylistTracks]);

  // На главной заранее резолвим первые рекомендации, чтобы запуск стартовал быстрее.
  useEffect(() => {
    if (recommendationTracks.length === 0) return;
    const ids = recommendationTracks.slice(0, 12).map((t) => t.id);
    if (ids.length) void preloadBatchUrls(ids);
  }, [recommendationTracks]);

  // Низкоуровневый запуск поиска в плейлисте — та же логика и лимиты, что на главной.
  const runPlaylistSearch = useCallback((trimmed: string): (() => void) | void => {
    if (trimmed.length < 3 || !openedPlaylist || openedPlaylist.isAdded) return;
    if (searchHardBlocked) return;
    playlistSearchAbortRef.current?.abort();
    const controller = new AbortController();
    playlistSearchAbortRef.current = controller;
    let active = true;
    setPlaylistSearchLoading(true);
    searchTracks(trimmed, controller.signal, { limit: SEARCH_FIRST_PAGE, offset: 0 })
      .then((results) => {
        if (!active || controller.signal.aborted) return;
        const { pl, opened } = searchLibraryArtworkRef.current;
        setPlaylistSearchResults(mergeLibraryArtworkIntoTracks(results, [pl, opened]));
        setPlaylistSearchHasMore(results.length >= SEARCH_FIRST_PAGE);
        if (results.length > 0) preloadBatchUrls(results.slice(0, 10).map((t) => t.id));
        lastSubmittedPlaylistSearchRef.current = trimmed;
      })
      .catch((err) => {
        if (!active || controller.signal.aborted) return;
        if (err instanceof SearchRateLimitedError) {
          const retrySec = err.retryAfterSec && err.retryAfterSec > 0 ? err.retryAfterSec : 20;
          setSearchCooldownUntil(Date.now() + retrySec * 1000);
          setSearchStatus(
            retrySec <= 10 ? "Подбираем лучшие совпадения…" : retrySec <= 20 ? "Оптимизируем поиск…" : "Ускоряем алгоритмы…"
          );
          return;
        }
        if (err instanceof Error && err.message.includes("Превышен дневной лимит поисковых запросов")) {
          setSearchHardBlocked(err.message);
          return;
        }
        setPlaylistSearchResults([]);
        setPlaylistSearchHasMore(false);
        toast.error("Ошибка поиска треков");
      })
      .finally(() => {
        active = false;
        setPlaylistSearchLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      setPlaylistSearchLoading(false);
    };
  }, [openedPlaylist]);

  const handlePlaylistSearchSubmit = useCallback(() => {
    const trimmed = playlistSearchQuery.trim();
    if (trimmed.length < 3 || !openedPlaylist || openedPlaylist.isAdded) return;
    if (searchHardBlocked) return;
    if (searchCooldownActive) return;
    runPlaylistSearch(trimmed);
  }, [playlistSearchQuery, openedPlaylist, searchHardBlocked, searchCooldownActive, runPlaylistSearch]);

  // Запуск поиска в плейлисте по дебаунсу (как на главной).
  useEffect(() => {
    const trimmed = debouncedPlaylistSearchQuery.trim();
    if (!openedPlaylist || openedPlaylist.isAdded) return;
    if (trimmed.length < 3) {
      setPlaylistSearchResults([]);
      setPlaylistSearchHasMore(false);
      lastSubmittedPlaylistSearchRef.current = "";
      return;
    }
    if (searchHardBlocked) return;
    if (searchCooldownActive) return;
    if (trimmed === lastSubmittedPlaylistSearchRef.current) return;
    const cancel = runPlaylistSearch(trimmed);
    return () => cancel?.();
  }, [debouncedPlaylistSearchQuery, openedPlaylist, runPlaylistSearch, searchCooldownActive, searchHardBlocked]);

  const loadMorePlaylistSearch = useCallback(() => {
    const trimmed = lastSubmittedPlaylistSearchRef.current;
    if (!trimmed || !openedPlaylist || openedPlaylist.isAdded || !playlistSearchHasMore || playlistSearchLoadMoreLoading) return;
    if (searchCooldownActive || searchHardBlocked) return;
    if (loadMorePlaylistSearchInFlightRef.current) return;
    loadMorePlaylistSearchInFlightRef.current = true;
    setPlaylistSearchLoadMoreLoading(true);
    const requestQuery = trimmed;
    const startOffset = playlistSearchResults.length;
    searchTracks(requestQuery, undefined, { offset: startOffset, limit: SEARCH_PAGE_SIZE })
      .then((chunk) => {
        if (lastSubmittedPlaylistSearchRef.current !== requestQuery) return;
        const { pl, opened } = searchLibraryArtworkRef.current;
        const mergedChunk = mergeLibraryArtworkIntoTracks(chunk, [pl, opened]);
        setPlaylistSearchResults((prev) => {
          const seen = new Set(prev.map((t) => t.id));
          const added = mergedChunk.filter((t) => !seen.has(t.id));
          if (added.length === 0) return prev;
          return [...prev, ...added];
        });
        setPlaylistSearchHasMore(chunk.length >= SEARCH_PAGE_SIZE);
        if (chunk.length > 0) preloadBatchUrls(chunk.slice(0, 10).map((t) => t.id));
      })
      .catch((err) => {
        if (lastSubmittedPlaylistSearchRef.current !== requestQuery) return;
        if (err instanceof SearchRateLimitedError) {
          const retrySec = err.retryAfterSec && err.retryAfterSec > 0 ? err.retryAfterSec : 20;
          setSearchCooldownUntil(Date.now() + retrySec * 1000);
          setSearchStatus(
            retrySec <= 10 ? "Подбираем лучшие совпадения…" : retrySec <= 20 ? "Оптимизируем поиск…" : "Ускоряем алгоритмы…"
          );
          return;
        }
        if (err instanceof Error && err.message.includes("Превышен дневной лимит поисковых запросов")) {
          setSearchHardBlocked(err.message);
          return;
        }
        toast.error("Не удалось подгрузить треки");
      })
      .finally(() => {
        loadMorePlaylistSearchInFlightRef.current = false;
        setPlaylistSearchLoadMoreLoading(false);
      });
  }, [openedPlaylist, playlistSearchHasMore, playlistSearchLoadMoreLoading, playlistSearchResults.length, searchCooldownActive, searchHardBlocked]);

  // Автоподгрузка результатов поиска в плейлисте при прокрутке (та же логика, что на главной)
  useEffect(() => {
    const sentinel = playlistSearchLoadMoreSentinelRef.current;
    const scrollEl = typeof document !== "undefined" ? document.querySelector(".app-scroll") : null;
    if (!sentinel || !scrollEl) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const [entry] = entries;
        if (!entry?.isIntersecting) return;
        if (view !== "playlist" || !openedPlaylist || openedPlaylist.isAdded) return;
        if (!playlistSearchHasMore || playlistSearchLoadMoreLoading || searchCooldownActive || searchHardBlocked) return;
        if (playlistSearchResults.length === 0) return;
        loadMorePlaylistSearch();
      },
      { root: scrollEl, rootMargin: "200px", threshold: 0 }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [view, openedPlaylist, playlistSearchHasMore, playlistSearchLoadMoreLoading, playlistSearchResults.length, searchCooldownActive, searchHardBlocked, loadMorePlaylistSearch]);

  /** Добавить трек в текущий открытый плейлист (кнопка «В плейлист» в результатах поиска). */
  const handlePlaylistSearchAdd = useCallback(
    async (track: Track) => {
      if (!openedPlaylist || !tgUser?.id) return;
      if (openedPlaylist.isFavorites) {
        await handleAddToPlaylist(track);
        fetchPlaylistTracks()
          .then((list) => {
            setPlaylist(list);
            prewarmVkTrackUrlsFromPlaylist(list);
          })
          .catch(() => {});
        setOpenedPlaylistTracks((prev) => {
          if (prev.some((t) => t.id === track.id)) return prev;
          return [track, ...prev];
        });
        return;
      }
      await addTrackToPlaylist(openedPlaylist.id, track);
      try {
        const tracks = await getPlaylistTracks(openedPlaylist.id);
        setOpenedPlaylistTracks(tracks);
      } catch {
        // ignore
      }
    },
    [openedPlaylist, tgUser?.id, handleAddToPlaylist],
  );

  /** Удалить трек из открытого плейлиста (в экране плейлиста; для результатов поиска — показываем мусорку, если трек уже в плейлисте). */
  const handleRemoveFromOpenedPlaylist = useCallback(
    async (track: Track) => {
      if (!openedPlaylist) return;
      // Сразу убираем из UI, иначе при быстром «добавил → удалил» трек не исчезает (рефетч может вернуть устаревшие данные)
      setOpenedPlaylistTracks((prev) => prev.filter((t) => t.id !== track.id));
      if (openedPlaylist.isFavorites) {
        setPlaylist((prev) => prev.filter((t) => t.id !== track.id));
        try {
          await removeFromPlaylist(track.id);
          trackEvent("button_remove");
        } catch {
          // Откат при ошибке
          const list = await fetchPlaylistTracks().catch(() => []);
          setPlaylist(list);
          prewarmVkTrackUrlsFromPlaylist(list);
          setOpenedPlaylistTracks(list);
        }
        return;
      }
      try {
        const ok = await removeTrackFromPlaylist(openedPlaylist.id, track.id);
        if (!ok) {
          const tracks = await getPlaylistTracks(openedPlaylist.id).catch(() => []);
          setOpenedPlaylistTracks(tracks);
        }
      } catch {
        const tracks = await getPlaylistTracks(openedPlaylist.id).catch(() => []);
        setOpenedPlaylistTracks(tracks);
      }
    },
    [openedPlaylist],
  );

  const handleSaveSharedPlaylist = useCallback(async () => {
    // Минимальный guard: только наличие данных и отсутствие активного запроса.
    if (!sharedPlaylist || sharedSaving) return;
    setSharedLimitReached(false);

    const storageKey =
      typeof window !== "undefined" && sharedShareId
        ? `${SHARED_SAVED_PREFIX}${sharedShareId}`
        : null;

    // Если уже сохранён — удаляем сохранённый плейлист
    if (sharedSaved && sharedSavedPlaylistId) {
      const ok = await deletePlaylist(sharedSavedPlaylistId);
      if (ok) {
        setSharedSaved(false);
        setSharedSavedPlaylistId(null);
        if (storageKey) {
          window.localStorage.removeItem(storageKey);
        }
      }
      return;
    }

    // Иначе пытаемся сохранить новый плейлист из шаринга.
    if (tgUser?.id) {
      try {
        const data = await fetchPlaylists();
        if (data) {
          const addedIds = new Set(getAddedPlaylistIds(tgUser.id));
          const addedCount = data.playlists.filter((pl) => addedIds.has(pl.id)).length;
          if (addedCount >= 5) {
            setSharedLimitReached(true);
            return;
          }
        }
      } catch {
        // при ошибке проверки лимита не блокируем сохранение
      }
    }
    setSharedSaving(true);
    try {
      const created = await createPlaylist(sharedPlaylist.name);
      if (!created) return;
      await Promise.all(
        sharedPlaylist.items.map((track) => addTrackToPlaylist(created.id, track)),
      );
      if (tgUser?.id) {
        addAddedPlaylistId(tgUser.id, created.id);
      }
      setSharedSaved(true);
      setSharedSavedPlaylistId(created.id);
      if (storageKey) {
        window.localStorage.setItem(storageKey, created.id);
      }
    } finally {
      setSharedSaving(false);
    }
  }, [
    sharedPlaylist,
    isLoggedIn,
    sharedSaving,
    sharedSaved,
    sharedSavedPlaylistId,
    sharedShareId,
    tgUser?.id,
  ]);

  if (sessionExpired) {
    return (
      <div
        style={{
          position: "fixed",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: "32px 24px",
          textAlign: "center",
          background: "var(--tg-theme-bg-color, #f1f5f9)",
          color: "var(--tg-theme-text-color, #0f172a)",
          fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif",
        }}
      >
        <p style={{ margin: "0 0 8px", fontSize: 18, fontWeight: 600 }}>Сессия устарела</p>
        <p style={{ margin: "0 0 20px", fontSize: 14, opacity: 0.72, lineHeight: 1.45, maxWidth: 280 }}>
          {sessionExpiredHadWebToken
            ? "Нажмите «Обновить» и войдите снова кнопкой «Войти через Telegram» под поиском или откройте приложение из бота в Telegram."
            : "Пожалуйста, закройте и снова откройте приложение из Telegram."}
        </p>
        <button
          onClick={() => window.location.reload()}
          style={{
            padding: "10px 24px",
            borderRadius: 12,
            border: "none",
            background: "var(--tg-theme-button-color, #0ea5e9)",
            color: "var(--tg-theme-button-text-color, #fff)",
            fontSize: 15,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          Обновить
        </button>
      </div>
    );
  }

  return (
    <div className="min-h-full min-h-[100dvh] px-4 pt-5 pb-32 space-y-8 relative">
      {view === "main" && (
        <div className="absolute top-0 left-0 z-30 pl-1 text-[10px] font-semibold tracking-[0.18em] uppercase text-text-muted/60 pointer-events-none">
          tgplay beta v0.20.1
        </div>
      )}
      {(view === "profile" || view === "playlist") && (
        <div
          className={view === "playlist" ? "absolute inset-0 pointer-events-none invisible" : undefined}
          aria-hidden={view === "playlist"}
        >
          <ProfilePage
            onBack={handleBackFromProfile}
            onOpenPlaylistScreen={handleOpenPlaylistScreen}
            isLoggedIn={isLoggedIn}
            telegramUser={tgUser}
            profileRefreshTrigger={profileRefreshTrigger}
            onLogout={getWebAccessToken() ? handleProfileLogout : undefined}
          />
        </div>
      )}

      {view === "shared" && sharedPlaylist && (
        <section className="space-y-4 pt-2">
          <header
            className="app-screen-header"
            style={{ paddingTop: "max(8px, env(safe-area-inset-top, 0px))" }}
          >
            <button
              type="button"
              className="app-screen-header__back p-1 text-text touch-manipulation active:opacity-80"
              onClick={handleBackFromShared}
              aria-label="Назад"
            >
              <svg
                className="w-6 h-6"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                aria-hidden
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15 19l-7-7 7-7"
                />
              </svg>
            </button>
            <div className="app-screen-header__title-zone">
              <h1 className="text-lg font-semibold text-text truncate">
                Плейлист: {sharedPlaylist.name}
              </h1>
            </div>
          </header>
          {isLoggedIn && (
            <div className="px-1 flex justify-center">
              <button
                type="button"
                disabled={sharedSaving || sharedLimitReached}
                className={`inline-flex items-center justify-center gap-2 mt-1 px-6 py-2 text-[13px] font-medium rounded-full glass-dark border border-white/10 text-text shadow-card touch-manipulation ${
                  sharedSaving
                    ? "opacity-80 animate-pulse"
                    : sharedLimitReached
                    ? "opacity-60 cursor-default"
                    : "active:opacity-80"
                }`}
                onClick={handleSaveSharedPlaylist}
              >
                <BookmarkPlus className="w-4 h-4" />
                <span>
                  {sharedSaving
                    ? "Сохранение…"
                    : sharedLimitReached
                    ? "Лимит добавленных достигнут"
                    : sharedSaved
                    ? "Удалить"
                    : "Сохранить плейлист"}
                </span>
              </button>
            </div>
          )}
          <TrackList
            title={sharedPlaylist.name}
            tracks={sharedPlaylist.items}
            playlist={sharedPlaylist.items}
            onArtistClick={openArtistCatalog}
            onSelect={(track) => handleSelectTrack(track, sharedPlaylist.items)}
            onOpenAddToPlaylist={isLoggedIn ? handleOpenAddToPlaylist : undefined}
            onAddAndSend={isLoggedIn ? handleSendToBotOnly : undefined}
            onOpenShareMenu={handleOpenShareMenu}
            onPreloadTrack={handlePreloadTrack}
            isLoggedIn={isLoggedIn}
            onDislike={handleDislikeRecommendation}
            deliveredToBotIds={botDeliveredIds}
            repeatSendLockedIds={botDeliveredVerifiedLiveIds}
            sendToBotPendingIds={sendToBotPendingIds}
          />
        </section>
      )}

      {view === "playlist" && openedPlaylist && (
        <section className="space-y-4 pt-2">
          <header
            className="app-screen-header"
            style={{ paddingTop: "max(8px, env(safe-area-inset-top, 0px))" }}
          >
            <button
              type="button"
              className="app-screen-header__back p-1 text-text touch-manipulation active:opacity-80"
              onClick={handleBackFromPlaylistView}
              aria-label="Назад"
            >
              <svg
                className="w-6 h-6"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                aria-hidden
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15 19l-7-7 7-7"
                />
              </svg>
            </button>
            <div className="app-screen-header__title-zone">
              <h1 className="text-lg font-semibold text-text truncate">
                {openedPlaylist.isFavorites
                  ? "Избранное"
                  : `Плейлист: ${openedPlaylist.name}`}
              </h1>
            </div>
          </header>

          {/* Поиск по трекам для добавления в этот плейлист (только для избранного и своих плейлистов) */}
          {!openedPlaylist.isAdded && (
            <div className="space-y-2 px-1">
              <SearchBar
                value={playlistSearchQuery}
                onChange={setPlaylistSearchQuery}
                onSubmit={handlePlaylistSearchSubmit}
                disabled={playlistSearchLoading || searchCooldownActive || !!searchHardBlocked}
                loading={playlistSearchLoading}
              />
              {searchCooldownActive && searchStatus && (
                <p className="text-[13px] text-text-muted text-center py-1">{searchStatus}</p>
              )}
              {playlistSearchResults.length > 0 && (
                <>
                  <TrackList
                    title="Результаты поиска"
                    tracks={playlistSearchResults}
                    playlist={openedPlaylistTracks}
                    onArtistClick={openArtistCatalog}
                    onSelect={(track) => handleSelectTrack(track, playlistSearchResults)}
                    onOpenAddToPlaylist={handlePlaylistSearchAdd}
                    onAddAndSend={undefined}
                    onOpenShareMenu={undefined}
                    onRemove={handleRemoveFromOpenedPlaylist}
                    onPreloadTrack={handlePreloadTrack}
                    isLoggedIn={isLoggedIn}
                    onDislike={handleDislikeRecommendation}
                    deliveredToBotIds={botDeliveredIds}
                    repeatSendLockedIds={botDeliveredVerifiedLiveIds}
                    sendToBotPendingIds={sendToBotPendingIds}
                  />
                  {playlistSearchResults.length > 0 && (
                    <div ref={playlistSearchLoadMoreSentinelRef} className="h-px w-full" aria-hidden />
                  )}
                  {playlistSearchHasMore && (
                    <button
                      type="button"
                      className="w-full py-3 text-[13px] font-medium text-accent rounded-2xl glass shadow-card active:opacity-80 touch-manipulation"
                      onClick={loadMorePlaylistSearch}
                      disabled={playlistSearchLoadMoreLoading}
                    >
                      {playlistSearchLoadMoreLoading ? (
                        <span className="inline-flex items-center gap-2">
                          <span className="inline-block w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                          Загрузка…
                        </span>
                      ) : (
                        "Показать ещё"
                      )}
                    </button>
                  )}
                </>
              )}
            </div>
          )}

          {/* Треки текущего плейлиста */}
          <TrackList
            title={
              openedPlaylist.isFavorites ? "Избранное" : openedPlaylist.name
            }
            tracks={openedPlaylistTracks}
            playlist={openedPlaylistTracks}
            onArtistClick={openArtistCatalog}
            onSelect={(track) => handleSelectTrack(track, openedPlaylistTracks)}
            onRemove={handleRemoveFromOpenedPlaylist}
            onOpenAddToPlaylist={
              openedPlaylist.isFavorites
                ? (track) => handleOpenAddToPlaylist(track, true)
                : handleOpenAddToPlaylist
            }
            onAddAndSend={handleSendToBotOnly}
            onOpenShareMenu={handleOpenShareMenu}
            onPreloadTrack={handlePreloadTrack}
            isLoggedIn={isLoggedIn}
            allowAddToPlaylistInList={openedPlaylist.isFavorites}
            onDislike={handleDislikeRecommendation}
            deliveredToBotIds={botDeliveredIds}
            repeatSendLockedIds={botDeliveredVerifiedLiveIds}
            sendToBotPendingIds={sendToBotPendingIds}
          />
        </section>
      )}

      {view === "main" && (
        <>
          <div className="flex flex-col gap-2 w-full">
          <header className="space-y-3 w-full -mx-4 px-4 app-fade-in -mt-2">
            <div className="flex items-center justify-between gap-2 w-full">
              <button
                type="button"
                className="flex flex-1 min-w-0 items-center text-left border-0 bg-transparent p-0 pl-0 touch-manipulation active:opacity-80 -ml-4 pl-4"
                onClick={() => {
                  if (query.trim()) trackEvent("button_reset_search");
                  handleMainSearchQueryChange("");
                }}
                aria-label="TGPlay — сброс поиска"
              >
                <img
                  src="/icon-header.png"
                  alt=""
                  className="shrink-0 object-contain object-right"
                  style={{
                    width: 116,
                    height: 116,
                    marginRight: 0,
                    objectPosition: "right center",
                    filter: "brightness(0)",
                  }}
                  fetchPriority="high"
                  decoding="async"
                  aria-hidden
                />
                <div className="flex flex-col items-start justify-center min-w-0" style={{ marginLeft: -20 }}>
                  <h1 className="text-xl font-semibold text-text tracking-tight leading-tight">TGPlay</h1>
                  <p className="text-[11px] uppercase text-text-muted tracking-[0.12em] font-medium m-0 mt-1 leading-tight truncate">
                    {isLoggedIn ? `Привет, ${tgUser?.first_name ?? ""}` : "Telegram Mini App"}
                  </p>
                </div>
              </button>
              <button
                type="button"
                className="shrink-0 w-11 h-11 rounded-full glass flex items-center justify-center text-text touch-manipulation active:opacity-80"
                onClick={() => {
                  trackEvent("button_profile_open");
                  setView("profile");
                }}
                aria-label="Профиль"
              >
                <User className="h-6 w-6" strokeWidth={2} />
              </button>
            </div>
            {/* Sub-header: канал и чат — серый полупрозрачный фон на всём окошке, узкие кнопки */}
            <div className="flex items-center justify-center gap-1 w-full ml-[1.75ch]">
              <button
                type="button"
                className="sub-header-link flex items-center gap-1 px-1.5 py-1 rounded-lg text-[11px] font-medium touch-manipulation active:opacity-70 border"
                onClick={() => {
                  const url = "https://t.me/tgplayapp";
                  openTelegramDeepLink(url);
                  trackEvent("button_share_channel"); // переход в канал
                }}
                aria-label="Наш канал"
              >
                <span className="sub-header-icon flex items-center justify-center w-6 h-6 rounded-full shrink-0">
                  <Megaphone className="h-3 w-3 text-[rgb(var(--text-muted))]" strokeWidth={2} />
                </span>
                Канал
              </button>
              <button
                type="button"
                className="sub-header-link flex items-center gap-1 px-1.5 py-1 rounded-lg text-[11px] font-medium touch-manipulation active:opacity-70 border"
                onClick={() => {
                  const url = "https://t.me/tgplaychat";
                  openTelegramDeepLink(url);
                  trackEvent("button_share_chat"); // переход в чат
                }}
                aria-label="Чат"
              >
                <span className="sub-header-icon flex items-center justify-center w-6 h-6 rounded-full shrink-0">
                  <MessageCircle className="h-3 w-3 text-[rgb(var(--text-muted))]" strokeWidth={2} />
                </span>
                Чат
              </button>
            </div>
            <div className="flex items-center justify-center gap-1 w-full ml-[1.75ch] px-0">
              <button
                type="button"
                disabled={
                  recommendationsLoading ||
                  (!getAuthorizationHeaderValue() && playlist.length === 0)
                }
                className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-[12px] font-semibold touch-manipulation active:opacity-90 border border-white/15 shadow-card disabled:opacity-40 tgplay-shine-dark"
                onClick={() => {
                  trackEvent("button_my_wave");
                  loadMainRecommendations({ wave: true });
                }}
                aria-label="Моя волна"
              >
                Моя волна
              </button>
            </div>
            <div className="search-bar-wrap block w-full space-y-1" style={{ paddingLeft: "24px" }}>
              <SearchBar
                value={query}
                onChange={handleMainSearchQueryChange}
                onSubmit={onSearchSubmit}
                disabled={searchCooldownActive || !!searchHardBlocked}
                loading={searchLoading && !searchHardBlocked}
              />
              <TelegramWebLoginRow show={!getInitData() && !isLoggedIn && !showTelegramLoginInRecsEmptyCard} />
            </div>
            {searchHardBlocked ? (
              <div className="w-full pt-1 px-2">
                <div className="w-full flex items-center justify-center gap-2 rounded-xl bg-zinc-900/80 border border-zinc-700 px-4 py-2">
                  <span className="text-[13px] text-text-muted leading-snug text-center">
                    Дневной лимит поиска превышен. Продолжайте слушать музыку из плейлистов и избранного — поиск обновится в 00:00 UTC.
                  </span>
                </div>
              </div>
            ) : (
              searchStatus && (
                <div className="w-full pt-1">
                  <div className="flex items-center justify-center gap-2 transition-opacity duration-300 ease-out opacity-80">
                    <span className="inline-block w-3 h-3 border-[2px] border-accent border-t-transparent rounded-full animate-spin" />
                    <span className="text-[13px] text-text-muted leading-snug">{searchStatus}</span>
                  </div>
                </div>
              )
            )}
          </header>

          <div ref={mainBelowSearchRef} id="main-below-search" className="space-y-3 min-h-[200px]">
            {mainSearchActive ? (
              <section className="space-y-4 app-fade-in">
                {error ? <ErrorState message={error} /> : null}
                <TrackList
                  title={artistCatalogTitle ? `${artistCatalogTitle} · треки` : "Результаты поиска"}
                  tracks={tracks}
                  playlist={playlist}
                  onArtistClick={openArtistCatalog}
                  onSelect={(track) => {
                    setRecApiSeedTrackIds([track.id]);
                    handleSelectTrack(track, tracks);
                  }}
                  onOpenAddToPlaylist={handleOpenAddToPlaylist}
                  onAddAndSend={handleSendToBotOnly}
                  onOpenShareMenu={handleOpenShareMenu}
                  onRemove={handleRemove}
                  onPreloadTrack={handlePreloadTrack}
                  isLoggedIn={isLoggedIn}
                  onDislike={handleDislikeRecommendation}
                  deliveredToBotIds={botDeliveredIds}
                  repeatSendLockedIds={botDeliveredVerifiedLiveIds}
                  sendToBotPendingIds={sendToBotPendingIds}
                />
                {tracks.length > 0 && <div ref={searchLoadMoreSentinelRef} className="h-px w-full" aria-hidden />}
                {searchHasMore && tracks.length > 0 && !loadMoreSearchLoading && (
                  <button
                    className="w-full py-3 text-[13px] font-medium text-accent rounded-2xl glass shadow-card active:opacity-80 touch-manipulation select-none"
                    onClick={loadMoreSearch}
                    type="button"
                  >
                    Показать ещё
                  </button>
                )}
                {loadMoreSearchLoading && (
                  <div className="w-full flex items-center justify-center py-4">
                    <span className="inline-block w-5 h-5 border-[2px] border-accent border-t-transparent rounded-full animate-spin" />
                  </div>
                )}
              </section>
            ) : (
              <section className="space-y-3 app-fade-in" aria-label="Рекомендации">
                {recommendationTracks.length > 0 ? (
                  <TrackList
                    title=""
                    onArtistClick={openArtistCatalog}
                    actions={
                      <div className="flex items-center justify-center gap-2 w-full">
                        <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">Рекомендации</span>
                        <button
                          type="button"
                          disabled={
                            recommendationsLoading ||
                            (!getAuthorizationHeaderValue() && playlist.length === 0)
                          }
                          className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-accent rounded-lg bg-accent/15 active:opacity-80 disabled:opacity-40 touch-manipulation select-none"
                          onClick={() => {
                            trackEvent("button_recommendations_refresh");
                            loadMainRecommendations({ refresh: true });
                          }}
                        >
                          Обновить
                        </button>
                      </div>
                    }
                    tracks={recommendationTracks}
                    playlist={playlist}
                    onSelect={(track) => {
                      handleSelectTrack(track, recommendationTracks);
                    }}
                    onOpenAddToPlaylist={handleOpenAddToPlaylist}
                    onAddAndSend={handleSendToBotOnly}
                    onOpenShareMenu={handleOpenShareMenu}
                    onRemove={handleRemove}
                    onPreloadTrack={handlePreloadTrack}
                    isLoggedIn={isLoggedIn}
                    deliveredToBotIds={botDeliveredIds}
                    repeatSendLockedIds={botDeliveredVerifiedLiveIds}
                    sendToBotPendingIds={sendToBotPendingIds}
                    /* Всегда передаём обработчик: при первом кадре getInitData() часто ещё пустой — иначе onDislike=null навсегда без лишнего setState. Внутри — проверка initData перед API. */
                    onDislike={handleDislikeRecommendation}
                  />
                ) : recommendationsLoading ? (
                  <div className="w-full flex flex-col items-center justify-center gap-2 py-10">
                    <span
                      className="inline-block w-6 h-6 border-[2px] border-accent border-t-transparent rounded-full animate-spin"
                      aria-hidden
                    />
                    <p className="text-[12px] text-text-muted text-center px-4">Подбираем треки…</p>
                  </div>
                ) : getAuthorizationHeaderValue() ? (
                  <div className="space-y-3">
                    <div className="flex items-center justify-center gap-2 px-0.5 relative flex-wrap">
                      <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted text-center">
                        Рекомендации
                      </h2>
                      <button
                        type="button"
                        disabled={recommendationsLoading}
                        className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-accent rounded-lg bg-accent/15 active:opacity-80 disabled:opacity-40 touch-manipulation select-none"
                        onClick={() => {
                          trackEvent("button_recommendations_refresh");
                          loadMainRecommendations({ refresh: true });
                        }}
                      >
                        Обновить
                      </button>
                    </div>
                    <div className="glass rounded-3xl p-2 space-y-1.5 shadow-card">
                      <p className="py-6 px-2 text-center text-[13px] text-text-muted leading-snug">
                        Нажмите «Обновить», чтобы загрузить персональную подборку. Добавьте треки в избранное — так
                        рекомендации станут точнее.
                      </p>
                    </div>
                  </div>
                ) : playlist.length > 0 ? (
                  <div className="space-y-3">
                    <div className="flex items-center justify-center gap-2 px-0.5 relative flex-wrap">
                      <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted text-center">
                        Рекомендации
                      </h2>
                      <button
                        type="button"
                        disabled={recommendationsLoading || !recApiSeedTrackIds.length}
                        className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-accent rounded-lg bg-accent/15 active:opacity-80 disabled:opacity-40 touch-manipulation select-none"
                        onClick={() => {
                          trackEvent("button_recommendations_refresh");
                          loadMainRecommendations({ refresh: true });
                        }}
                      >
                        Обновить
                      </button>
                    </div>
                    <div className="glass rounded-3xl p-2 space-y-1.5 shadow-card">
                      <p className="py-4 px-2 text-center text-[13px] text-text-muted leading-snug">
                        Подборка по вашему избранному. Войдите через Telegram — тогда лента станет персональной.
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <div className="flex items-center justify-center gap-2 px-0.5 relative">
                      <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted text-center">
                        Рекомендации
                      </h2>
                    </div>
                    <div className="glass rounded-3xl p-2 space-y-1.5 shadow-card flex flex-col items-center">
                      <p className="py-4 px-2 text-center text-[13px] text-text-muted leading-snug">
                        Войдите через Telegram и добавьте треки в избранное — здесь появятся рекомендации.
                      </p>
                      <TelegramWebLoginRow show={showTelegramLoginInRecsEmptyCard} />
                    </div>
                  </div>
                )}
              </section>
            )}
          </div>
          </div>
        </>
      )}

      <MiniPlayer
        track={currentTrack}
        isPlaying={isPlaying}
        isBuffering={isBuffering}
        isShuffle={isShuffle}
        repeatMode={repeatMode}
        onToggle={togglePlay}
        onNext={handleNext}
        onPrev={handlePrev}
        onToggleShuffle={handleToggleShuffle}
        onCycleRepeatMode={handleCycleRepeatMode}
        onOpen={() => setIsPlayerOpen(true)}
        onClose={handleCloseMiniPlayer}
      />

      {isPlayerOpen && (
        <div className="fixed inset-0 z-40 bg-black/50 pointer-events-none" aria-hidden />
      )}
      <AddToPlaylistSheet track={addToPlaylistSheetTrack} isOpen={addToPlaylistSheetTrack !== null} onClose={handleCloseAddToPlaylistSheet} onAdded={handleAddToPlaylistSheetAdded} onProfileRefresh={handleProfileRefresh} hideFavorites={addToPlaylistSheetFromFavorites} />
      <ShareTrackSheet track={shareMenuTrack} isOpen={shareMenuTrack !== null} onClose={() => setShareMenuTrack(null)} />
      <FullPlayer
        isOpen={isPlayerOpen}
        track={currentTrack}
        isPlaying={isPlaying}
        isBuffering={isBuffering}
        isShuffle={isShuffle}
        repeatMode={repeatMode}
        currentTime={currentTime}
        duration={duration}
        onClose={() => setIsPlayerOpen(false)}
        onToggle={togglePlay}
        onNext={handleNext}
        onPrev={handlePrev}
        onSeek={handleSeek}
        onToggleShuffle={handleToggleShuffle}
        onCycleRepeatMode={handleCycleRepeatMode}
        onOpenAddToPlaylist={handleOpenAddToPlaylist}
        onAddToPlaylist={handleAddToPlaylist}
        onAddAndSend={handleSendToBotOnly}
        onRemove={handleRemove}
        onOpenShareMenu={handleOpenShareMenu}
        onArtistClick={openArtistCatalog}
        onDislike={isLoggedIn ? handleDislikeRecommendation : undefined}
        isLoggedIn={isLoggedIn}
        isInPlaylist={
          currentTrack
            ? playlist.some((t) => canonicalPlaylistTrackId(t.id) === canonicalPlaylistTrackId(currentTrack.id))
            : false
        }
        addedToCache={
          currentTrack
            ? botDeliveredIds.has(canonicalPlaylistTrackId(currentTrack.id)) ||
              botDeliveredVerifiedLiveIds.has(canonicalPlaylistTrackId(currentTrack.id))
            : false
        }
        downloadRepeatLocked={
          currentTrack ? botDeliveredVerifiedLiveIds.has(canonicalPlaylistTrackId(currentTrack.id)) : false
        }
        sendToBotPending={
          currentTrack ? sendToBotPendingIds.has(canonicalPlaylistTrackId(currentTrack.id)) : false
        }
        onGoToPlaylist={
          isLoggedIn
            ? () => {
                trackEvent("button_profile_from_player");
                setIsPlayerOpen(false);
                requestAnimationFrame(() =>
                  mainBelowSearchRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
                );
              }
            : undefined
        }
        compactSpacing={compactSpacing}
        useCompressedProportions={useCompressedProportions}
      />

      <audio ref={audioRef} preload="auto" playsInline />
    </div>
  );
};

export default App;
