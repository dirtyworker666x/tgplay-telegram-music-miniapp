import { useEffect, useRef, useState } from "react";

type MetricPoint = { date: string; count: number };

type ErrorRow = {
  time: string;
  telegram_user_id: number | null;
  username: string;
  user_display: string;
  error_key: string;
  message: string;
  country: string;
  region: string;
};

type UserRow = {
  telegram_user_id: number | null;
  username: string;
  last_seen_utc?: number | null;
  registered_utc?: number | null;
  ordinal: number;
  country_code?: string | null;
  city_region?: string | null;
  /** Есть приватный чат с ботом (нужен для рассылки в ЛС); только мини‑апп без /start — false */
  bot_private_chat_ok?: boolean;
};

type Overview = {
  unique_users: number;
  unique_users_today: number;
  unique_users_month: number;
  users_online?: number;
  visits: number;
  track_plays: number;
  track_finishes: number;
  downloads_total: number;
  search_count: number;
  errors_count: number;
  retention_pct: number;
  by_button: Record<string, number>;
  errors_by_key: Record<string, number>;
  recent_errors: ErrorRow[];
  captcha_stats: Record<string, number>;
  captcha_total: number;
  /** За последние 24 ч — чтобы видеть текущую картину (ошибка 9 vs капча) */
  captcha_stats_24h?: Record<string, number>;
  captcha_total_24h?: number;
  users_list: UserRow[];
  /** VK token pool (round-robin): число токенов и пиковая ёмкость */
  vk_tokens_total?: number;
  vk_tokens_healthy?: number;
  peak_capacity_users?: number;
  cache?: {
    version?: string;
    in_flight?: number;
    negative_hit?: number;
    search?: { hit?: number; miss?: number; ratio?: number | null; avg_ttl_age_sec?: number | null };
    source?: { hit?: number; miss?: number; ratio?: number | null };
    meta?: { hit?: number; miss?: number; ratio?: number | null };
  };
  /** Сколько записей в bot_subscribers с private_chat_ok=1 */
  bot_private_chat_users_total?: number;
  /** Уникальные id из аналитики, у которых открыт ЛС с ботом */
  analytics_users_with_bot_dm?: number;
};

type PlaylistEvent = {
  time: string;
  telegram_user_id: number | null;
  username: string;
  playlist_id: string | null;
  action: string;
  extra?: Record<string, unknown>;
};

const API_KEY_PARAM = "key";

/**
 * Единый список кнопок без дубликатов: одна строка на действие.
 * ids — ключи в API (суммируем, если несколько, например старый и новый «Скачать»).
 */
/** Ключ API → русская подпись для отображения */
const BUTTON_KEY_LABELS: Record<string, string> = {
  button_profile_open: "Профиль",
  button_profile_from_player: "Переход в плейлист (из большого плеера)",
  button_share_channel: "Переход в канал",
  button_share_chat: "Переход в чат",
  button_share_to_users: "Шеринг трека пользователям",
  button_share_story: "Шеринг в историю (сторис)",
  button_add_to_favorites: "Добавить в избранное",
  button_add_playlist: "Добавить в избранное (legacy)",
  button_add_to_custom_playlist: "Добавить в кастомный плейлист",
  button_download: "Скачать",
  button_add_send: "Скачать (legacy)",
  button_remove: "Удалить из плейлиста",
  button_create_playlist: "Создать плейлист",
  button_reset_search: "Сброс поиска (иконка TGPlay)",
  button_share_playlist: "Шеринг плейлиста",
  share_card_to_self: "Карточка себе (Избранное)",
  share_request_user_picker: "Выбор пользователя для шеринга",
};

function getButtonEntries(byButton: Record<string, number> | undefined): [string, number][] {
  if (!byButton) return [];
  return Object.entries(byButton).sort((a, b) => b[1] - a[1]);
}

function getAdminKeyFromUrl(): string | null {
  const url = new URL(window.location.href);
  const fromQuery = url.searchParams.get(API_KEY_PARAM);
  if (fromQuery) return fromQuery;
  const hash = url.hash.replace(/^#/, "");
  const params = new URLSearchParams(hash);
  return params.get(API_KEY_PARAM);
}

async function fetchJson<T>(path: string, key: string, options?: RequestInit): Promise<T> {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("key", key);
  const res = await fetch(url.toString(), { credentials: "include", ...options });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

type TabId = "overview" | "cache" | "users" | "errors" | "captcha" | "buttons" | "tracks" | "playlists";

function pct(x: number | null | undefined): number {
  if (x == null || !Number.isFinite(x)) return 0;
  return Math.round(x * 1000) / 10; // 87.5%
}

export const AdminStatsApp = () => {
  const [apiKey] = useState<string | null>(() => getAdminKeyFromUrl());
  const [activeTab, setActiveTab] = useState<TabId>("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [userSortMode, setUserSortMode] = useState<"last_seen" | "registered">("last_seen");
  const [captchaClearLoading, setCaptchaClearLoading] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [metric, setMetric] = useState<MetricPoint[]>([]);
  const [metricName, setMetricName] = useState<string>("visits");
  const [cacheInfo, setCacheInfo] = useState<{ title: string; description: string } | null>(null);
  const [playlistEvents, setPlaylistEvents] = useState<PlaylistEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRequestUrl, setLastRequestUrl] = useState<string | null>(null);
  const metricNameRef = useRef(metricName);
  metricNameRef.current = metricName;

  // Снимаем стартовый boot-оверлей (его обычно убирает App.tsx, но админка рендерится отдельно).
  useEffect(() => {
    const boot = document.getElementById("tgplay-boot");
    if (boot) {
      boot.setAttribute("data-tgplay-dismissed", "1");
      boot.remove();
    }
  }, []);

  useEffect(() => {
    if (!apiKey) {
      setError("Отсутствует ключ ?key=... в URL.");
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        setLoading(true);
        setError(null);
        setLastRequestUrl(null);
        const [ov, m] = await Promise.all([
          fetchJson<Overview>("/api/admin/stats/overview", apiKey),
          fetchJson<{ points: MetricPoint[] }>("/api/admin/stats/metric?metric=visits&days=30", apiKey),
        ]);
        if (!cancelled) {
          setOverview(ov);
          setMetric(m.points || []);
          setMetricName("visits");
        }
      } catch (e) {
        if (!cancelled) {
          const msg = e instanceof Error ? e.message : "Не удалось загрузить статистику";
          try {
            const u = new URL("/api/admin/stats/overview", window.location.origin);
            u.searchParams.set("key", apiKey);
            setLastRequestUrl(u.toString());
          } catch {
            setLastRequestUrl(null);
          }
          setError(msg.includes("403") ? "Доступ запрещён (403). Проверьте ключ в URL или задайте ANALYTICS_ADMIN_KEY в .env на сервере." : msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    const interval = setInterval(async () => {
      if (!apiKey) return;
      try {
        const [ov, m] = await Promise.all([
          fetchJson<Overview>("/api/admin/stats/overview", apiKey),
          fetchJson<{ points: MetricPoint[] }>(`/api/admin/stats/metric?metric=${metricNameRef.current}&days=30`, apiKey),
        ]);
        setOverview(ov);
        setMetric(m.points || []);
      } catch {
        // не сбрасываем ошибку при фоновом обновлении
      }
    }, 30_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [apiKey]);

  const changeMetric = async (name: string) => {
    if (!apiKey) return;
    try {
      setLoading(true);
      setError(null);
      const data = await fetchJson<{ points: MetricPoint[] }>(`/api/admin/stats/metric?metric=${encodeURIComponent(name)}&days=30`, apiKey);
      setMetric(data.points || []);
      setMetricName(name);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось загрузить график");
    } finally {
      setLoading(false);
    }
  };

  const loadPlaylistsRecent = async (key: string) => {
    try {
      const data = await fetchJson<{ events: PlaylistEvent[] }>("/api/admin/stats/playlists/recent", key);
      setPlaylistEvents(data.events || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось загрузить историю плейлистов");
    }
  };

  const clearCaptchaEvents = async () => {
    if (!apiKey) return;
    if (!window.confirm("Удалить все записи капч/кулдаунов из аналитики? (старые ошибочные 815 можно так сбросить)")) return;
    try {
      setCaptchaClearLoading(true);
      await fetchJson<{ ok: boolean; deleted?: number }>("/api/admin/captcha/clear-events", apiKey, { method: "POST" });
      const ov = await fetchJson<Overview>("/api/admin/stats/overview", apiKey);
      setOverview(ov);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось сбросить капчи");
    } finally {
      setCaptchaClearLoading(false);
    }
  };

  if (!apiKey) {
    return (
      <div className="min-h-screen bg-black text-zinc-100 flex items-center justify-center px-4">
        <div className="max-w-md space-y-4 text-center">
          <h1 className="text-xl font-semibold tracking-tight">TGPlay — Admin Stats</h1>
          <p className="text-sm text-zinc-400">
            Добавь параметр <code>?key=...</code> к URL, чтобы открыть дашборд.
          </p>
        </div>
      </div>
    );
  }

  const tabs: { id: TabId; label: string }[] = [
    { id: "overview", label: "Обзор" },
    { id: "cache", label: "Кэш" },
    { id: "users", label: "Пользователи" },
    { id: "errors", label: "Ошибки" },
    { id: "captcha", label: "Капчи" },
    { id: "buttons", label: "Кнопки" },
    { id: "tracks", label: "Треки" },
    { id: "playlists", label: "Плейлисты" },
  ];

  return (
    <div className="min-h-screen bg-black text-zinc-100 flex flex-col">
      <header className="border-b border-zinc-800 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="md:hidden inline-flex items-center justify-center rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-200 bg-zinc-900"
            onClick={() => setSidebarOpen((v) => !v)}
          >
            Меню
          </button>
          <div>
            <h1 className="text-lg font-semibold tracking-tight">TGPlay — Статистика</h1>
            <p className="text-xs text-zinc-500">UTC · счётчики обновляются каждые 30 сек</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="text-[10px] text-zinc-500 font-mono truncate max-w-[220px]" title={apiKey}>
            key={apiKey}
          </div>
          <button
            type="button"
            className="text-[10px] text-zinc-400 border border-zinc-700 rounded px-2 py-1 bg-zinc-900 hover:bg-zinc-800"
            onClick={() => {
              const url = new URL(window.location.href);
              url.searchParams.set(API_KEY_PARAM, apiKey);
              url.hash = "";
              void navigator.clipboard?.writeText(url.toString());
            }}
          >
            Копировать ссылку
          </button>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden relative">
        {/* Мобильное выезжающее меню */}
        <div className="md:hidden">
          <div
            className={[
              "fixed inset-y-0 left-0 w-52 bg-zinc-950/95 border-r border-zinc-800 z-40 transform transition-transform duration-200",
              sidebarOpen ? "translate-x-0" : "-translate-x-full",
            ].join(" ")}
          >
            <div className="px-2 py-3 space-y-1 text-xs max-h-[70vh] overflow-y-auto">
              {tabs.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => {
                    setActiveTab(t.id);
                    setSidebarOpen(false);
                  }}
                  className={[
                    "w-full text-left px-3 py-1.5 rounded-lg transition-colors",
                    activeTab === t.id ? "bg-zinc-800 text-zinc-50" : "text-zinc-400 hover:bg-zinc-900",
                  ].join(" ")}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Левое меню на десктопе (урезано по высоте) */}
        <nav className="hidden md:block w-40 border-r border-zinc-800 bg-zinc-950/80 backdrop-blur-sm px-2 py-3 space-y-1 text-xs max-h-[80vh] overflow-y-auto">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setActiveTab(t.id)}
              className={[
                "w-full text-left px-3 py-1.5 rounded-lg transition-colors",
                activeTab === t.id ? "bg-zinc-800 text-zinc-50" : "text-zinc-400 hover:bg-zinc-900",
              ].join(" ")}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <main className="flex-1 px-2 md:px-4 py-4 overflow-auto">
          {loading && (
            <div className="text-xs text-zinc-400 mb-3">Загрузка...</div>
          )}
          {error && (
            <div className="text-xs text-red-400 mb-3">
              Ошибка: {error}
              {lastRequestUrl ? (
                <div className="mt-1 text-[11px] text-zinc-400 font-mono break-all">
                  last_request: {lastRequestUrl}
                </div>
              ) : null}
            </div>
          )}

          {activeTab === "overview" && overview && (
            <section className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Card title="Сейчас онлайн (15 мин)" value={overview.users_online ?? 0} />
                <Card title="Пользователи всего" value={overview.unique_users} />
                <Card title="Пользователи 24ч" value={overview.unique_users_today} />
                <Card title="Пользователи месяц" value={overview.unique_users_month} />
                {overview.vk_tokens_total != null && (
                  <>
                    <Card title={`VK токенов (robin ${overview.vk_tokens_healthy ?? 0}/${overview.vk_tokens_total})`} value={overview.vk_tokens_total} />
                    <Card title="Пик пользователей" value={overview.peak_capacity_users ?? 0} />
                  </>
                )}
                <Card title="Визиты (open_app)" value={overview.visits} />
                <Card title="Прослушиваний" value={overview.track_plays} />
                <Card title="Доиграно" value={overview.track_finishes} />
                <Card title="Скачано в бота" value={overview.downloads_total} />
                <Card title="Ошибок всего" value={overview.errors_count} />
                <Card title="Капч/кулдаунов" value={overview.captcha_total ?? 0} />
                <Card title="Поисков" value={overview.search_count} />
                <Card title="Удержание D1, %" value={overview.retention_pct} />
                {overview.bot_private_chat_users_total != null && (
                  <Card
                    title="ЛС с ботом (рассылка)"
                    value={overview.bot_private_chat_users_total}
                    description="Открыли диалог с ботом (/start и т.д.). Без этого Bot API не шлёт сообщения."
                  />
                )}
                {overview.analytics_users_with_bot_dm != null && (
                  <Card
                    title="В аналитике — с ЛС ботом"
                    value={overview.analytics_users_with_bot_dm}
                    description="Уникальные user id из событий, у кого есть приватный чат с ботом."
                  />
                )}
              </div>
              <div className="rounded-xl bg-zinc-950/70 border border-zinc-800 p-3">
                <h3 className="text-xs font-medium text-zinc-400 mb-2">Клики по кнопкам</h3>
                <div className="flex flex-wrap gap-2">
                  {getButtonEntries(overview.by_button).map(([key, count]) => (
                    <span key={key} className="px-2 py-1 rounded bg-zinc-800 text-zinc-200 text-xs" title={key}>
                      {BUTTON_KEY_LABELS[key] ?? key}: <strong>{count}</strong>
                    </span>
                  ))}
                </div>
              </div>
              <section className="space-y-2">
                <div className="flex items-center justify-between">
                  <h2 className="text-sm font-medium text-zinc-100">График за 30 дней</h2>
                  <select
                    className="bg-zinc-900 border border-zinc-700 text-xs rounded-md px-2 py-1"
                    value={metricName}
                    onChange={(e) => changeMetric(e.target.value)}
                  >
                    <option value="visits">Визиты</option>
                    <option value="search_count">Поиски</option>
                    <option value="track_plays">Прослушивания</option>
                    <option value="track_finishes">Доигрывания</option>
                    <option value="downloads">Скачивания</option>
                    <option value="errors_count">Ошибки</option>
                  </select>
                </div>
                <SimpleLine points={metric} />
              </section>
            </section>
          )}

          {activeTab === "cache" && overview && (
            <section className="space-y-4">
              {(() => {
                const search = overview.cache?.search;
                const source = overview.cache?.source;
                const meta = overview.cache?.meta;
                const totalHits =
                  (search?.hit ?? 0) + (source?.hit ?? 0) + (meta?.hit ?? 0);
                const totalMisses =
                  (search?.miss ?? 0) + (source?.miss ?? 0) + (meta?.miss ?? 0);
                const total = totalHits + totalMisses;
                const overallRatio = total > 0 ? totalHits / total : null;
                const overallPct = pct(overallRatio);

                let borderClass = "border-zinc-700";
                let bgClass = "bg-zinc-950/70";
                if (overallRatio != null) {
                  if (overallRatio < 0.7) {
                    borderClass = "border-red-500";
                    bgClass = "bg-red-500/10";
                  } else if (overallRatio < 0.9) {
                    borderClass = "border-amber-500";
                    bgClass = "bg-amber-500/10";
                  } else {
                    borderClass = "border-emerald-500";
                    bgClass = "bg-emerald-500/10";
                  }
                }

                return (
                  <div className={`rounded-xl ${bgClass} border-2 ${borderClass} p-3`}>
                    <h2 className="text-sm font-medium text-zinc-100 mb-1">
                      Общий hit rate кэша
                    </h2>
                    <p className="text-2xl font-semibold text-zinc-50">
                      {overallRatio != null ? `${overallPct}%` : "—"}
                    </p>
                    <p className="text-xs text-zinc-500 mt-1">
                      Метрики считаются на бэкенде с момента последнего рестарта (per-process). Версия ключей:{" "}
                      <span className="font-mono text-zinc-300">{overview.cache?.version ?? "—"}</span>
                    </p>
                  </div>
                );
              })()}

              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Card
                  title="In-flight (singleflight)"
                  value={overview.cache?.in_flight ?? 0}
                  description="Сколько запросов к VK/кэшу сейчас объединены через singleflight (один реальный запрос, остальные ждут результат)."
                  onClick={() =>
                    setCacheInfo({
                      title: "In-flight (singleflight)",
                      description:
                        "Сколько запросов к VK/кэшу сейчас объединены через singleflight (один реальный запрос, остальные ждут результат).",
                    })
                  }
                />
                <Card
                  title="Negative cache hits"
                  value={overview.cache?.negative_hit ?? 0}
                  description="Сколько раз отдали ответ из негативного кэша (трек удалён/недоступен), без повторного похода к VK."
                  onClick={() =>
                    setCacheInfo({
                      title: "Negative cache hits",
                      description:
                        "Сколько раз отдали ответ из негативного кэша (трек удалён/недоступен), без повторного похода к VK.",
                    })
                  }
                />
                <Card
                  title="Search hit rate, %"
                  value={pct(overview.cache?.search?.ratio)}
                  description="Доля поисковых запросов, обслуженных из кэша (без живого запроса к VK)."
                  onClick={() =>
                    setCacheInfo({
                      title: "Search hit rate, %",
                      description:
                        "Доля поисковых запросов, обслуженных из кэша (без живого запроса к VK). Чем выше, тем меньше нагрузка на VK API.",
                    })
                  }
                />
                <Card
                  title="Search avg age, sec"
                  value={Math.round(overview.cache?.search?.avg_ttl_age_sec ?? 0)}
                  description="Средний возраст записей поискового кэша в секундах (чем меньше, тем свежее)."
                  onClick={() =>
                    setCacheInfo({
                      title: "Search avg age, sec",
                      description:
                        "Средний возраст записей поискового кэша поиска в секундах. Помогает понять, насколько свежие данные выдаёт поиск.",
                    })
                  }
                />
                <Card
                  title="Search hits"
                  value={overview.cache?.search?.hit ?? 0}
                  description="Количество попаданий в кэш поиска (сколько раз запрос сразу нашёл данные по ключу)."
                  onClick={() =>
                    setCacheInfo({
                      title: "Search hits",
                      description:
                        "Количество попаданий (hits) по кэшу поиска: сколько раз результаты были отданы сразу из памяти/Redis без запроса к VK.",
                    })
                  }
                />
                <Card
                  title="Search misses"
                  value={overview.cache?.search?.miss ?? 0}
                  description="Количество промахов по кэшу поиска (пришлось делать живой запрос к VK)."
                  onClick={() =>
                    setCacheInfo({
                      title: "Search misses",
                      description:
                        "Количество промахов (misses) по кэшу поиска: сколько раз не нашли данные по ключу и пришлось идти в VK.",
                    })
                  }
                />
                <Card
                  title="Source hit rate, %"
                  value={pct(overview.cache?.source?.ratio)}
                  description="Hit rate кэша прямых URL источника (VK CDN) — чем выше, тем реже запрашиваем ссылки у VK."
                  onClick={() =>
                    setCacheInfo({
                      title: "Source hit rate, %",
                      description:
                        "Доля запросов к прямым URL (источник, VK CDN), обслуженных из кэша. Чем выше, тем реже обновляем ссылки у VK.",
                    })
                  }
                />
                <Card
                  title="Meta hit rate, %"
                  value={pct(overview.cache?.meta?.ratio)}
                  description="Hit rate кэша метаданных треков (getById) — заголовки, артисты и т.п."
                  onClick={() =>
                    setCacheInfo({
                      title: "Meta hit rate, %",
                      description:
                        "Hit rate кэша метаданных треков (getById): насколько часто заголовок/артист и прочие поля берутся из кэша.",
                    })
                  }
                />
              </div>

              {cacheInfo && (
                <div className="rounded-xl bg-zinc-950/70 border border-zinc-800 p-3 text-xs text-zinc-200 space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                      {cacheInfo.title}
                    </div>
                    <button
                      type="button"
                      onClick={() => setCacheInfo(null)}
                      className="text-[11px] text-zinc-500 hover:text-zinc-300"
                    >
                      Закрыть
                    </button>
                  </div>
                  <p className="leading-snug text-[12px] text-zinc-300">{cacheInfo.description}</p>
                  <p className="text-[11px] text-zinc-500">
                    Если общий hit rate стабильно ниже 70%, обычно проблема в нормализации ключей, слишком коротком TTL или
                    отсутствии прогрева кэша.
                  </p>
                </div>
              )}
            </section>
          )}

          {activeTab === "errors" && overview?.recent_errors && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-zinc-100">Последние ошибки (UTC)</h2>
              <div className="overflow-x-auto rounded-xl border border-zinc-800">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-zinc-900 text-zinc-400 text-left">
                      <th className="p-2">Время</th>
                      <th className="p-2">User ID / hash</th>
                      <th className="p-2">Username</th>
                      <th className="p-2">Тип</th>
                      <th className="p-2">Сообщение</th>
                      <th className="p-2">Регион</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.recent_errors.map((e, i) => (
                      <tr key={i} className="border-t border-zinc-800">
                        <td className="p-2 text-zinc-300">{e.time}</td>
                        <td className="p-2 font-mono text-cyan-400">
                          {(() => {
                            const byId = e.telegram_user_id;
                            const byName = e.username;
                            const label = e.user_display ?? (byId ?? "");
                            const href =
                              byName && byName.length > 0
                                ? `https://t.me/${byName}`
                                : byId != null
                                ? `tg://user?id=${byId}`
                                : undefined;
                            return href ? (
                              <a href={href} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2">
                                {label}
                              </a>
                            ) : (
                              <span>{label || "—"}</span>
                            );
                          })()}
                        </td>
                        <td className="p-2 text-zinc-400">{e.username || "—"}</td>
                        <td className="p-2 text-amber-400">{e.error_key}</td>
                        <td className="p-2 text-zinc-400 max-w-[200px] truncate" title={e.message}>{e.message}</td>
                        <td className="p-2 text-zinc-500">{e.country || e.region || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {overview.errors_by_key && Object.keys(overview.errors_by_key).length > 0 && (
                <div className="rounded-xl bg-zinc-950/70 border border-zinc-800 p-3">
                  <h3 className="text-xs font-medium text-zinc-400 mb-2">По типам ошибок</h3>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(overview.errors_by_key).map(([k, v]) => (
                      <span key={k} className="px-2 py-1 rounded bg-zinc-800 text-amber-400/90 text-xs">{k}: <strong>{v}</strong></span>
                    ))}
                  </div>
                </div>
              )}
            </section>
          )}

          {activeTab === "buttons" && overview && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-zinc-100">Клики по кнопкам</h2>
              <div className="overflow-x-auto rounded-xl border border-zinc-800">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-zinc-900 text-zinc-400 text-left">
                      <th className="p-2">Кнопка</th>
                      <th className="p-2">Ключ</th>
                      <th className="p-2">Кликов</th>
                    </tr>
                  </thead>
                  <tbody>
                    {getButtonEntries(overview.by_button).map(([key, count]) => (
                      <tr key={key} className="border-t border-zinc-800">
                        <td className="p-2 text-zinc-300" title={key}>{BUTTON_KEY_LABELS[key] ?? key}</td>
                        <td className="p-2 font-mono text-zinc-500 text-[10px]">{key}</td>
                        <td className="p-2 text-zinc-50 font-semibold">{count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {activeTab === "captcha" && overview && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-zinc-100">Капчи и кулдауны</h2>
              <p className="text-xs text-zinc-500">
                captcha_shown = только когда VK вернул ошибку 14 (капча). Ошибка 9 (flood) логируется как cooldown_start. Если сервис решения капчи не получал запросов — старые 815 могли быть ошибочно посчитаны (можно сбросить кнопкой ниже).
              </p>
              {(overview.captcha_stats_24h != null && Object.keys(overview.captcha_stats_24h).length > 0) || (overview.captcha_total_24h != null && overview.captcha_total_24h > 0) ? (
                <>
                  <h3 className="text-xs font-medium text-zinc-400">За последние 24 ч</h3>
                  <Card title="Событий за 24 ч" value={overview.captcha_total_24h ?? 0} />
                  <div className="rounded-xl border border-zinc-800 overflow-hidden">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="bg-zinc-900 text-zinc-400 text-left">
                          <th className="p-2">Тип события</th>
                          <th className="p-2">Количество</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(overview.captcha_stats_24h ?? {}).map(([k, v]) => (
                          <tr key={k} className="border-t border-zinc-800">
                            <td className="p-2 font-mono text-zinc-300">{k}</td>
                            <td className="p-2 text-zinc-50 font-semibold">{v}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <p className="text-xs text-zinc-500">За последние 24 ч событий нет (или только cooldown_start при ошибке 9).</p>
              )}
              <h3 className="text-xs font-medium text-zinc-400">За всё время</h3>
              <Card title="Всего событий" value={overview.captcha_total ?? 0} />
              {overview.captcha_stats && Object.keys(overview.captcha_stats).length > 0 ? (
                <div className="rounded-xl border border-zinc-800 overflow-hidden">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="bg-zinc-900 text-zinc-400 text-left">
                        <th className="p-2">Тип события</th>
                        <th className="p-2">Количество</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(overview.captcha_stats).map(([k, v]) => (
                        <tr key={k} className="border-t border-zinc-800">
                          <td className="p-2 font-mono text-zinc-300">{k}</td>
                          <td className="p-2 text-zinc-50 font-semibold">{v}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-zinc-500">Нет данных по капчам за всё время.</p>
              )}
              <button
                type="button"
                disabled={captchaClearLoading || !apiKey}
                className="mt-2 px-3 py-1.5 rounded-lg bg-amber-900/50 border border-amber-700 text-xs text-amber-200 hover:bg-amber-800/50 disabled:opacity-50"
                onClick={clearCaptchaEvents}
              >
                {captchaClearLoading ? "Сброс…" : "Сбросить счётчики капч/кулдаунов"}
              </button>
            </section>
          )}

          {activeTab === "tracks" && overview && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-zinc-100">Треки</h2>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                <Card title="Прослушиваний (play)" value={overview.track_plays} />
                <Card title="Доиграно (complete)" value={overview.track_finishes} />
                <Card title="Скачано в бота" value={overview.downloads_total} />
              </div>
            </section>
          )}

          {activeTab === "playlists" && overview && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-zinc-100">Плейлисты</h2>
              <p className="text-xs text-zinc-500">
                Ниже — последние операции с плейлистами (создание, переименование, удаление, добавление и удаление треков).
              </p>
              {apiKey && playlistEvents.length === 0 && (
                <button
                  type="button"
                  className="px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-700 text-xs text-zinc-200 hover:bg-zinc-800"
                  onClick={() => loadPlaylistsRecent(apiKey)}
                >
                  Загрузить историю плейлистов
                </button>
              )}
              {playlistEvents.length > 0 && (
                <div className="overflow-x-auto rounded-xl border border-zinc-800 max-h-[60vh] overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-zinc-900 z-10 text-zinc-400 text-left">
                      <tr>
                        <th className="p-2">Время</th>
                        <th className="p-2">User ID</th>
                        <th className="p-2">Username</th>
                        <th className="p-2">Действие</th>
                        <th className="p-2">Playlist ID</th>
                        <th className="p-2">Детали</th>
                      </tr>
                    </thead>
                    <tbody>
                      {playlistEvents.map((ev, i) => (
                        <tr key={`${ev.time}-${ev.playlist_id}-${i}`} className="border-t border-zinc-800">
                          <td className="p-2 text-zinc-300">{ev.time}</td>
                          <td className="p-2 font-mono text-cyan-400">{ev.telegram_user_id ?? "—"}</td>
                          <td className="p-2 text-zinc-300">{ev.username || "—"}</td>
                          <td className="p-2 text-emerald-400">{ev.action}</td>
                          <td className="p-2 font-mono text-zinc-500">{ev.playlist_id ?? "—"}</td>
                          <td className="p-2 text-zinc-400 max-w-[200px] truncate" title={JSON.stringify(ev.extra ?? {})}>
                            {ev.extra && (ev.extra.name as string)
                              ? (ev.extra.name as string)
                              : ev.extra && (ev.extra.track_id as string)
                              ? `track_id=${ev.extra.track_id as string}`
                              : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}

          {activeTab === "users" && overview?.users_list && (
            <section className="space-y-3">
              <h2 className="text-sm font-medium text-zinc-100">Пользователи</h2>
              <div className="flex flex-wrap gap-2 text-xs">
                <button
                  type="button"
                  className={`px-2 py-1 rounded-md border text-xs ${
                    userSortMode === "last_seen"
                      ? "border-emerald-500 text-emerald-300 bg-emerald-500/10"
                      : "border-zinc-700 text-zinc-300 hover:border-zinc-500"
                  }`}
                  onClick={() => setUserSortMode("last_seen")}
                >
                  по последнему заходу
                </button>
                <button
                  type="button"
                  className={`px-2 py-1 rounded-md border text-xs ${
                    userSortMode === "registered"
                      ? "border-emerald-500 text-emerald-300 bg-emerald-500/10"
                      : "border-zinc-700 text-zinc-300 hover:border-zinc-500"
                  }`}
                  onClick={() => setUserSortMode("registered")}
                >
                  по дате регистрации
                </button>
              </div>
              <div className="overflow-x-auto rounded-xl border border-zinc-800 max-h-[60vh] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-zinc-900 z-10">
                    <tr className="text-zinc-400 text-left">
                      <th className="p-2">№</th>
                      <th className="p-2">Пользователь</th>
                      <th className="p-2">User ID</th>
                      <th className="p-2">Регион</th>
                      <th className="p-2">Регистрация (UTC)</th>
                      <th className="p-2">Последний визит (UTC)</th>
                      <th className="p-2">ЛС ботом</th>
                    </tr>
                  </thead>
                  <tbody>
                    {([...overview.users_list] as UserRow[])
                      .sort((a, b) => {
                        if (userSortMode === "registered") {
                          const ar = a.registered_utc ?? 0;
                          const br = b.registered_utc ?? 0;
                          return br - ar;
                        }
                        const al = a.last_seen_utc ?? 0;
                        const bl = b.last_seen_utc ?? 0;
                        return bl - al;
                      })
                      .map((u) => (
                      <tr key={u.telegram_user_id ?? u.ordinal} className="border-t border-zinc-800">
                        <td className="p-2 font-mono text-zinc-500">{u.ordinal}</td>
                        <td className="p-2 text-zinc-300">
                          {(() => {
                            const byId = u.telegram_user_id;
                            const byName = u.username;
                            const label = byName && byName.length > 0 ? `@${byName}` : byId ?? "—";
                            const href =
                              byName && byName.length > 0
                                ? `https://t.me/${byName}`
                                : byId != null
                                ? `tg://user?id=${byId}`
                                : undefined;
                            return href ? (
                              <a href={href} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2">
                                {label}
                              </a>
                            ) : (
                              <span>{label}</span>
                            );
                          })()}
                        </td>
                        <td className="p-2 font-mono text-cyan-400">{u.telegram_user_id ?? "—"}</td>
                        <td className="p-2 text-zinc-300">
                          {u.city_region || u.country_code || "—"}
                        </td>
                        <td className="p-2 text-zinc-400">
                          {u.registered_utc
                            ? new Date(u.registered_utc * 1000).toISOString().replace("T", " ").slice(0, 19)
                            : "—"}
                        </td>
                        <td className="p-2 text-zinc-400">
                          {u.last_seen_utc
                            ? new Date(u.last_seen_utc * 1000).toISOString().replace("T", " ").slice(0, 19)
                            : "—"}
                        </td>
                        <td className="p-2">
                          {u.bot_private_chat_ok ? (
                            <span className="text-emerald-400" title="Можно слать сообщения от бота в ЛС">
                              да
                            </span>
                          ) : (
                            <span className="text-zinc-500" title="Только мини‑апп или нет диалога с ботом — рассылка в ЛС недоступна">
                              нет
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-zinc-500">Всего: {overview.users_list.length}</p>
            </section>
          )}
        </main>
      </div>
    </div>
  );
};

const Card = ({
  title,
  value,
  description,
  onClick,
}: {
  title: string;
  value: number;
  description?: string;
  onClick?: () => void;
}) => {
  const clickable = !!description || !!onClick;
  const handleClick = () => {
    if (onClick) {
      onClick();
      return;
    }
    if (!description) return;
    window.alert(description);
  };
  return (
    <div
      className={`rounded-xl bg-zinc-950/70 border border-zinc-800 px-3 py-2.5 shadow-sm min-h-[56px] flex flex-col justify-between min-w-0 overflow-hidden ${
        clickable ? "cursor-pointer hover:border-zinc-600 transition-colors" : ""
      }`}
      onClick={handleClick}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <div className="flex items-start justify-between gap-1">
        <div
          className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1 leading-tight break-words line-clamp-2 overflow-hidden"
          title={description || title}
        >
          {title}
        </div>
        {clickable && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              handleClick();
            }}
            className="ml-1 mt-0.5 inline-flex items-center justify-center rounded-full border border-zinc-700 bg-zinc-900 text-[9px] text-zinc-300 w-4 h-4 flex-shrink-0"
            title="Показать описание метрики"
          >
            i
          </button>
        )}
      </div>
      <div className="text-sm font-semibold text-zinc-50">{value}</div>
    </div>
  );
};

const SimpleLine = ({ points }: { points: MetricPoint[] }) => {
  if (!points.length) {
    return <div className="text-xs text-zinc-500">Нет данных за выбранный период.</div>;
  }
  const max = Math.max(...points.map((p) => p.count));
  const min = Math.min(...points.map((p) => p.count));
  const range = Math.max(1, max - min);
  const normalized = points.map((p) => (p.count - min) / range);

  return (
    <div className="w-full h-36 bg-zinc-950/70 border border-zinc-800 rounded-xl px-3 py-2 flex items-end gap-1 overflow-hidden">
      {normalized.map((v, i) => (
        <div
          key={points[i].date}
          className="flex-1 bg-gradient-to-t from-emerald-500/70 to-cyan-400/80 rounded-t-full"
          style={{ height: `${10 + v * 90}%` }}
          title={`${points[i].date}: ${points[i].count}`}
        />
      ))}
    </div>
  );
};

