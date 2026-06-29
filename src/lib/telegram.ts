/** Платформы десктоп/веб: не смартфон */
const DESKTOP_WEB_PLATFORMS = ["tdesktop", "weba", "webk", "macos", "windows", "linux"];

export type TelegramWebApp = {
  ready: () => void;
  expand: () => void;
  colorScheme?: "light" | "dark";
  themeParams?: Record<string, string>;
  /** Платформа: ios, android, tdesktop, weba, webk и т.д. */
  platform?: string;
  initData?: string;
  initDataUnsafe?: {
    user?: { id: number; first_name: string; last_name?: string; username?: string; photo_url?: string };
    start_param?: string;
  };
  /** Bot API 8.0+: true, если мини-приложение в полноэкранном режиме (без шапки/подвала Telegram) */
  isFullscreen?: boolean;
  /** Текущая высота видимой области (обновляется в реальном времени) */
  viewportHeight?: number;
  /** Высота видимой области в последнем стабильном состоянии (BottomSheet меньше, main app — почти весь экран) */
  viewportStableHeight?: number;
  /** true, если приложение развёрнуто на максимальную высоту */
  isExpanded?: boolean;
  onEvent: (event: string, handler: () => void) => void;
  offEvent: (event: string, handler: () => void) => void;
  /** Проверка версии Bot API (например "6.7"). Без этого — тихий сбой при вызове switchInlineQuery на старых клиентах. */
  isVersionAtLeast?: (version: string) => boolean;
  /** Переключение в inline-режим бота с запросом (Bot API 6.7+). Карточка уйдёт в выбранный чат как фото. */
  switchInlineQuery?: (query: string, choose_chat_types?: ("users" | "bots" | "groups" | "channels")[]) => void;
  /** Поделиться подготовленным сообщением — открывает выбор чатов (Bot API 7.10+). ID с бэкенда (savePreparedInlineMessage). */
  shareMessage?: (preparedMessageId: string, callback?: (shared: boolean) => void) => void;
  /** Открыть ссылку t.me/... в клиенте (fallback для шеринга). */
  openTelegramLink?: (url: string) => void;
  /** Блокировка ориентации (Bot API 8.0+) — только портрет на мобильном, чтобы обложка не обрезалась в ландшафте */
  lockOrientation?: (orientation: "portrait" | "landscape" | "portrait_primary" | "landscape_primary" | "portrait_secondary" | "landscape_secondary") => void;
  unlockOrientation?: () => void;
  /** Bot API 8.0+: системные отступы (вырез, статус-бар). Суммировать с contentSafeAreaInset. */
  safeAreaInset?: { top: number; right: number; bottom: number; left: number };
  /** Bot API 8.0+: отступ под шапку Telegram (Закрыть и др.). Суммировать с safeAreaInset. */
  contentSafeAreaInset?: { top: number; right: number; bottom: number; left: number };
};

export function getWebApp(): TelegramWebApp | null {
  if (typeof window === "undefined") return null;
  const w = window as Window & { Telegram?: { WebApp?: TelegramWebApp } };
  return w.Telegram?.WebApp ?? null;
}

/** initData строка для отправки на бэкенд (trim — пустой/пробельный контекст не должен блокировать Bearer) */
export function getInitData(): string {
  const s = getWebApp()?.initData;
  return typeof s === "string" ? s.trim() : "";
}

/** Данные текущего пользователя (без верификации, только для UI) */
export function getTelegramUser() {
  return getWebApp()?.initDataUnsafe?.user ?? null;
}

/** Bot ID (client_id) из BotFather; дефолт — боевой OAuth-клиент TGPlay */
const DEFAULT_TELEGRAM_OAUTH_CLIENT_ID = "8575565887";

/** Bot ID (client_id) из BotFather для `Telegram.Login` на сайте/PWA */
export function getTelegramOAuthClientId(): string {
  const fromEnv = (import.meta.env.VITE_TELEGRAM_OAUTH_CLIENT_ID || "").trim();
  return fromEnv || DEFAULT_TELEGRAM_OAUTH_CLIENT_ID;
}

/** Ответ callback после Telegram.Login (OIDC id_token или ошибка) */
export type TelegramLoginCallbackPayload =
  | { error: string; error_description?: string }
  | { id_token: string; user?: Record<string, unknown> };

/** OIDC nonce для сопоставления с клеймом в id_token (см. InitOptions.nonce в доке Telegram). */
export const TGPLAY_TELEGRAM_OIDC_NONCE_STORAGE_KEY = "tgplay.telegram.oidc.nonce.v1";

export const TELEGRAM_OIDC_LOGIN_SCRIPT_SRC = "https://oauth.telegram.org/js/telegram-login.js?3";

function makeOidcNonce(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const a = new Uint8Array(16);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(a);
  } else {
    for (let i = 0; i < a.length; i += 1) a[i] = Math.floor(Math.random() * 256);
  }
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Значение nonce, отправленное на /api/auth/telegram вместе с id_token. */
export function peekTelegramOidcNonceForAuth(): string | null {
  try {
    const v = sessionStorage.getItem(TGPLAY_TELEGRAM_OIDC_NONCE_STORAGE_KEY);
    return v && v.trim() ? v.trim() : null;
  } catch {
    return null;
  }
}

export function clearTelegramOidcNonce(): void {
  try {
    sessionStorage.removeItem(TGPLAY_TELEGRAM_OIDC_NONCE_STORAGE_KEY);
  } catch {
    /* private mode */
  }
}

/** Ждём появления `window.Telegram.Login` после подключения скрипта. */
export function waitForTelegramLoginScript(timeoutMs = 12000): Promise<boolean> {
  if (typeof window === "undefined") return Promise.resolve(false);
  const w = window as Window & {
    Telegram?: { Login?: { init: (...args: unknown[]) => void; open: () => void } };
  };
  if (w.Telegram?.Login) return Promise.resolve(true);
  const started = Date.now();
  return new Promise((resolve) => {
    const tick = () => {
      if (w.Telegram?.Login) {
        resolve(true);
        return;
      }
      if (Date.now() - started >= timeoutMs) {
        resolve(false);
        return;
      }
      window.setTimeout(tick, 50);
    };
    tick();
  });
}

/**
 * Подключает официальный `telegram-login.js` без data-client-id (иначе auto-init без nonce).
 * После вызова доступен `Telegram.Login.init` / `open`.
 */
export function loadTelegramOidcLoginSdk(): Promise<void> {
  if (typeof document === "undefined" || typeof window === "undefined") return Promise.resolve();
  const w = window as Window & { Telegram?: { Login?: unknown } };
  if (w.Telegram?.Login) return Promise.resolve();

  const existing = document.querySelector(`script[src="${TELEGRAM_OIDC_LOGIN_SCRIPT_SRC}"]`);
  if (existing) {
    return waitForTelegramLoginScript(15000).then((ok) => {
      if (!ok) throw new Error("telegram-login.js: timeout waiting for Telegram.Login");
    });
  }

  return new Promise<void>((resolve, reject) => {
    const s = document.createElement("script");
    s.async = true;
    s.src = TELEGRAM_OIDC_LOGIN_SCRIPT_SRC;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("telegram-login.js failed to load"));
    document.head.appendChild(s);
  }).then(() =>
    waitForTelegramLoginScript(15000).then((ok) => {
      if (!ok) throw new Error("telegram-login.js: timeout waiting for Telegram.Login");
    }),
  );
}

/**
 * `Telegram.Login.init` с client_id, request_access и nonce (рекомендация доки).
 * nonce сохраняется в sessionStorage для проверки на сервере вместе с id_token.
 * @returns false если нет client_id или SDK не загружен
 */
export function initTelegramWebLogin(onResult: (data: TelegramLoginCallbackPayload) => void): boolean {
  const id = getTelegramOAuthClientId();
  const w = window as Window & {
    Telegram?: {
      Login?: {
        init: (
          opts: { client_id: string; request_access?: string[]; lang?: string; nonce?: string },
          cb: (d: Record<string, unknown>) => void,
        ) => void;
        open: () => void;
      };
    };
  };
  const Login = w.Telegram?.Login;
  if (!id || !Login) return false;

  let nonce: string;
  try {
    const existing = sessionStorage.getItem(TGPLAY_TELEGRAM_OIDC_NONCE_STORAGE_KEY);
    if (existing && existing.trim()) {
      nonce = existing.trim();
    } else {
      nonce = makeOidcNonce();
      sessionStorage.setItem(TGPLAY_TELEGRAM_OIDC_NONCE_STORAGE_KEY, nonce);
    }
  } catch {
    nonce = makeOidcNonce();
  }

  Login.init(
    { client_id: id, request_access: ["write"], lang: "ru", nonce },
    (data: Record<string, unknown>) => {
      onResult(data as TelegramLoginCallbackPayload);
    },
  );
  return true;
}

export function openTelegramWebLogin(): void {
  const w = window as Window & { Telegram?: { Login?: { open: () => void } } };
  w.Telegram?.Login?.open();
}

/**
 * Полная версия только при запуске по ссылке с ?startapp или в режиме isFullscreen.
 * Не используем viewport — иначе развёрнутый BottomSheet из меню ошибочно получает стили полной версии.
 * Чтобы при открытии по кнопке «Открыть» в списке чатов был полный дизайн: в BotFather у Main Mini App
 * укажи URL с параметром, например https://tgplay.fun?startapp=full — тогда start_param будет «full».
 */
export function isFullscreenLaunch(): boolean {
  const w = getWebApp();
  if (!w) return false;
  const startParam = w.initDataUnsafe?.start_param;
  if (startParam != null && startParam !== "") return true;
  if (Boolean(w.isFullscreen)) return true;
  return false;
}

/** start_param из ссылки (например ?startapp=tr_123 или ?start=pl_abc). Для deep link. */
export function getStartParam(): string | undefined {
  const w = getWebApp();
  const param = w?.initDataUnsafe?.start_param;
  if (typeof param === "string" && param.length > 0) return param;

  if (typeof window === "undefined") return undefined;

  // Telegram Web передаёт start_param также в hash (#tgWebAppStartParam=...)
  if (window.location.hash) {
    const hashParams = new URLSearchParams(window.location.hash.slice(1));
    const fromHash = hashParams.get("tgWebAppStartParam");
    if (fromHash && fromHash.length > 0) return fromHash;
  }

  // Fallback для прямого открытия в браузере: ?startapp=... или ?start=...
  if (window.location.search) {
    const searchParams = new URLSearchParams(window.location.search);
    const fromQuery = searchParams.get("startapp") ?? searchParams.get("start");
    if (fromQuery && fromQuery.length > 0) return fromQuery;
  }

  return undefined;
}

/** Ссылки t.me из браузера / PWA: openTelegramLink в Mini App; иначе переход тем же окном (window.open часто блочится в standalone). */
export function openTelegramDeepLink(url: string): void {
  const u = (url || "").trim();
  if (!u || typeof window === "undefined") return;
  const tg = getWebApp();
  try {
    if (tg?.openTelegramLink) {
      tg.openTelegramLink(u);
      return;
    }
  } catch {
    /* fall through */
  }
  try {
    window.location.assign(u);
  } catch {
    window.open(u, "_blank", "noopener,noreferrer");
  }
}

/**
 * true, если Mini App запущен из инлайн-режима (кнопка «Switch to Mini App» в результатах инлайна).
 * Только в этом случае switchInlineQuery() работает; при запуске из профиля бота вызов закрывает приложение.
 */
export function isLaunchedInInlineMode(): boolean {
  if (typeof window === "undefined" || !window.location?.hash) return false;
  const params = new URLSearchParams(window.location.hash.slice(1));
  return params.get("tgWebAppBotInline") === "1" || params.get("tgWebAppBotInline") === "true";
}

/** True, если мини-приложение открыто в Telegram Web (десктоп) или десктоп-клиенте. Смартфоны (ios, android) — false. */
export function isTelegramWebDesktop(): boolean {
  if (typeof window === "undefined") return false;
  const w = getWebApp();
  let platform: string | undefined;
  if (w && "platform" in w && typeof (w as { platform?: string }).platform === "string") {
    platform = (w as { platform: string }).platform;
  }
  if (!platform && typeof window.location?.hash === "string") {
    const params = new URLSearchParams(window.location.hash.slice(1));
    platform = params.get("tgWebAppPlatform") ?? undefined;
  }
  if (!platform) return false;
  return DESKTOP_WEB_PLATFORMS.includes(platform.toLowerCase());
}

/** True, если мини-приложение запущено на Android‑клиенте Telegram. */
export function isAndroid(): boolean {
  const w = getWebApp();
  const platform = (w?.platform ?? "").toLowerCase();
  if (platform) return platform === "android";
  if (typeof window !== "undefined" && typeof window.location?.hash === "string") {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const fromHash = (params.get("tgWebAppPlatform") ?? "").toLowerCase();
    if (fromHash) return fromHash === "android";
  }
  return false;
}

export const applyTelegramTheme = () => {
  const WebApp = getWebApp();
  // Всегда светлая тема — не подстраиваемся под пользовательскую
  document.documentElement.classList.remove("dark");
  if (!WebApp) return;

  const params = WebApp.themeParams ?? {};
  const root = document.documentElement;

  const setVar = (name: string, value?: string) => {
    if (!value) return;
    root.style.setProperty(name, value);
  };

  setVar("--tg-bg", params.bg_color);
  setVar("--tg-text", params.text_color);
  setVar("--tg-hint", params.hint_color);
  setVar("--tg-accent", params.button_color);
  setVar("--tg-accent-text", params.button_text_color);
};

/** Вызывать при смене вьюпорта — выставляет data-fullscreen-launch для стилей полной версии */
export function applyFullscreenLaunchFlagToDocument(): void {
  if (isFullscreenLaunch()) document.documentElement.dataset.fullscreenLaunch = "true";
}

const TG_HEADER_SAFE_MIN_PX = 52;

/** Синхронизирует верхний safe area с document (Bot API 8.0+). Android: safe+content с минимумом 52px. iOS: только системный safe (вырез), без content — иначе интерфейс уезжает вниз. */
export function applySafeAreaToDocument(): void {
  if (typeof document === "undefined" || !document.documentElement) return;
  const w = getWebApp();
  if (!w) return;
  const safeTop = w.safeAreaInset?.top ?? 0;
  const contentTop = w.contentSafeAreaInset?.top ?? 0;
  const safeBottom = w.safeAreaInset?.bottom ?? 0;
  const contentBottom = w.contentSafeAreaInset?.bottom ?? 0;
  const platform = (w.platform ?? "").toLowerCase();
  const totalTop =
    platform === "android"
      ? Math.max(TG_HEADER_SAFE_MIN_PX, safeTop + contentTop)
      : platform === "ios"
        ? safeTop
        : safeTop + contentTop;
  document.documentElement.style.setProperty("--tg-header-safe", `${totalTop}px`);

  // Нижний safe area: чтобы элементы управления плеера не упирались в системные кнопки Android.
  const totalBottom = isAndroid()
    ? Math.max(96, safeBottom + contentBottom)
    : safeBottom + contentBottom;
  document.documentElement.style.setProperty("--tg-bottom-safe", `${totalBottom}px`);
}

let initTelegramDone = false;

type WindowWithTgplayFlag = Window & { __tgplayEarlyReady?: boolean };

function runTelegramInit(WebApp: TelegramWebApp): void {
  try {
    const root = document.documentElement;
    let platform = (WebApp.platform ?? "").toLowerCase();
    if (!platform && typeof window !== "undefined" && typeof window.location?.hash === "string") {
      const params = new URLSearchParams(window.location.hash.slice(1));
      platform = (params.get("tgWebAppPlatform") ?? "").toLowerCase();
    }
    root.classList.remove("platform-android", "platform-ios", "platform-desktop");
    if (platform === "android") {
      root.classList.add("platform-android");
    } else if (platform === "ios") {
      root.classList.add("platform-ios");
    } else if (platform) {
      root.classList.add("platform-desktop");
    }

    const win = typeof window !== "undefined" ? (window as WindowWithTgplayFlag) : null;
    // index.html уже мог вызвать ready/expand в onload у telegram-web-app.js — не дублировать (редкие глюки WebView).
    if (!win?.__tgplayEarlyReady) {
      WebApp.ready();
      WebApp.expand();
    }
    if (win) win.__tgplayEarlyReady = true;
    applyTelegramTheme();
    applyFullscreenLaunchFlagToDocument();
    applySafeAreaToDocument();
    WebApp.onEvent("themeChanged", applyTelegramTheme);
    WebApp.onEvent("viewportChanged", () => {
      applyFullscreenLaunchFlagToDocument();
      // Поздняя инъекция user/initData в WebView — даём App.tsx повторно подхватить профиль
      if (typeof window !== "undefined") {
        window.dispatchEvent(new Event("tgplay-webapp-ready"));
      }
    });
    WebApp.onEvent("safeAreaChanged", applySafeAreaToDocument);
    WebApp.onEvent("contentSafeAreaChanged", applySafeAreaToDocument);
    // Повторная активация Mini App (Bot API 8.0+). В переиспользуемом WebView
    // document.visibilitychange ненадёжен, поэтому пробрасываем нативный сигнал —
    // App.tsx по нему обновляет ленту рекомендаций при каждом заходе.
    WebApp.onEvent("activated", () => {
      if (typeof window !== "undefined") {
        window.dispatchEvent(new Event("tgplay-app-resumed"));
      }
    });
    // Блокировка ориентации только внутри Telegram — в ярлыке Safari/PWA без initData давала сбои на iOS.
    if (getInitData() && typeof WebApp.lockOrientation === "function") {
      try {
        WebApp.lockOrientation("portrait");
      } catch {
        // Игнорируем, если платформа не поддерживает
      }
    }
    if (typeof window !== "undefined") {
      window.dispatchEvent(new Event("tgplay-webapp-ready"));
    }
  } catch {
    // Safe fallback for non-Telegram environment.
  }
}

/**
 * Инициализация Telegram WebApp. Вызывать при загрузке приложения.
 * При открытии по кнопке «Открыть» в списке чатов клиент может инжектировать WebApp
 * с задержкой — поэтому повторяем попытку через 100 и 400 мс, чтобы убрать бесконечную загрузку.
 */
/** Ярлык «На экран Домой» / установленное PWA (вне браузерной вкладки). */
export function isStandaloneDisplayMode(): boolean {
  if (typeof window === "undefined") return false;
  const nav = window.navigator as Navigator & { standalone?: boolean };
  if (nav.standalone === true) return true;
  try {
    return window.matchMedia("(display-mode: standalone)").matches === true;
  } catch {
    return false;
  }
}

/** Задержки до появления Telegram.WebApp: медленные сети и прокси в клиенте дают инъекцию позже сотен ms. */
const WEBAPP_RETRY_MS = [50, 150, 400, 900, 2000, 4500, 8000, 12000];

export const initTelegram = () => {
  if (typeof window === "undefined") return;

  const tryBind = (): void => {
    if (initTelegramDone) return;
    const w = getWebApp();
    if (w) {
      initTelegramDone = true;
      runTelegramInit(w);
    }
  };

  tryBind();
  if (initTelegramDone) return;

  for (const ms of WEBAPP_RETRY_MS) {
    window.setTimeout(tryBind, ms);
  }

  if (document.readyState === "complete") {
    tryBind();
  } else {
    window.addEventListener("load", tryBind, { once: true });
  }
};
