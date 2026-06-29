import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Check, ChevronLeft, X } from "lucide-react";
import { getTelegramUser, getWebApp, openTelegramDeepLink } from "../lib/telegram";
import {
  createPlaylist,
  deletePlaylist,
  fetchMyProfilePhotoBlob,
  fetchPlaylists,
  updatePlaylist,
  createPlaylistShare,
  getPlaylistShareUrl,
} from "../lib/api";
import { clearSharedMappingForPlaylist, getAddedPlaylistIds } from "../lib/playlistLocal";
import type { PlaylistMeta, Track } from "../types";
import { trackEvent } from "../lib/analytics";
import { PlaylistCard } from "./playlists/PlaylistCard";

const FAVORITES_ID = "favorites";

type ProfilePageProps = {
  onBack: () => void;
  onOpenPlaylistScreen?: (opts: { id: string; name: string; isFavorites: boolean; isAdded?: boolean }) => void;
  isLoggedIn: boolean;
  /** Пользователь из состояния App (в т.ч. веб OAuth); иначе Mini App initDataUnsafe */
  telegramUser?: { id: number; first_name: string; username?: string } | null;
  /** Инкрементируется при добавлении трека в плейлист через шит — перезагружаем список */
  profileRefreshTrigger?: number;
  /** Веб OAuth: выход из Bearer-сессии (в Mini App без веб-токена не передаётся). */
  onLogout?: () => void;
};

export function ProfilePage({
  onBack,
  onOpenPlaylistScreen,
  isLoggedIn,
  telegramUser,
  profileRefreshTrigger,
  onLogout,
}: ProfilePageProps) {
  const tgUser = telegramUser ?? getTelegramUser();
  const tgUserWithPhoto = getWebApp()?.initDataUnsafe?.user as
    | { id?: number; photo_url?: string }
    | undefined;
  const canUseInitDataPhoto = Boolean(
    tgUserWithPhoto?.photo_url &&
      typeof tgUserWithPhoto?.id === "number" &&
      typeof tgUser?.id === "number" &&
      tgUserWithPhoto.id === tgUser.id,
  );
  const [proxiedAvatarUrl, setProxiedAvatarUrl] = useState<string | null>(null);
  const [playlistsData, setPlaylistsData] = useState<{
    favorites: Track[];
    playlists: PlaylistMeta[];
    max_free_playlists: number;
  } | null>(null);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [selectedPlaylistId, setSelectedPlaylistId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [creating, setCreating] = useState(false);
  const [creatingNew, setCreatingNew] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [activeFolder, setActiveFolder] = useState<"own" | "added" | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);
  const newPlaylistInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const data = await fetchPlaylists();
    if (data) setPlaylistsData(data);
  }, []);

  useEffect(() => {
    if (!isLoggedIn) {
      setPlaylistsData(null);
      return;
    }
    // Смена Telegram user id (в т.ч. веб: выход → другой аккаунт) — isLoggedIn остаётся true, без id не обновим данные.
    setPlaylistsData(null);
    void load();
  }, [load, profileRefreshTrigger, isLoggedIn, telegramUser?.id]);

  useEffect(() => {
    if (!isLoggedIn) {
      setProxiedAvatarUrl(null);
      return;
    }
    let cancelled = false;
    let toRevoke: string | null = null;
    void (async () => {
      const blob = await fetchMyProfilePhotoBlob(tgUser?.id);
      if (cancelled || !blob || blob.size < 16) return;
      const u = URL.createObjectURL(blob);
      if (cancelled) {
        URL.revokeObjectURL(u);
        return;
      }
      toRevoke = u;
      setProxiedAvatarUrl(u);
    })();
    return () => {
      cancelled = true;
      setProxiedAvatarUrl(null);
      if (toRevoke) URL.revokeObjectURL(toRevoke);
    };
  }, [isLoggedIn, telegramUser?.id]);

  const handleRename = useCallback(
    async (pl: PlaylistMeta) => {
      if (!editName.trim()) {
        setEditingId(null);
        return;
      }
      const ok = await updatePlaylist(pl.id, { name: editName.trim() });
      setEditingId(null);
      if (ok) load();
    },
    [editName, load],
  );

  const handleStartCreate = useCallback(() => {
    flushSync(() => {
      setSelectedPlaylistId(null);
      setCreatingNew(true);
      setEditName("");
    });
    if (typeof window !== "undefined") {
      try {
        window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
      } catch {
        // ignore
      }
    }
    const input = newPlaylistInputRef.current;
    if (input) {
      // Сначала фокус, затем скроллим ближе к центру, чтобы поле не пряталось под клавиатуру.
      input.focus();
      requestAnimationFrame(() => {
        try {
          input.scrollIntoView({ block: "center", behavior: "smooth" });
        } catch {
          // ignore
        }
      });
    }
  }, []);

  const handleConfirmNewPlaylist = useCallback(async () => {
    const name = editName.trim() || "Новый плейлист";
    const addedIds = tgUser?.id ? new Set(getAddedPlaylistIds(tgUser.id)) : new Set<string>();
    const ownCount =
      playlistsData?.playlists.filter((pl) => !addedIds.has(pl.id)).length ?? 0;
    const maxOwn = 5;
    if (ownCount >= maxOwn) {
      setCreateError("Лимит собственных плейлистов достигнут");
      return;
    }
    setCreateError(null);
    flushSync(() => {
      setSelectedPlaylistId(null);
      setCreatingNew(false);
      setEditName("");
      setCreating(true);
    });
    const result = await createPlaylist(name);
    if (!result) {
      setCreateError("Лимит собственных плейлистов достигнут");
      setCreating(false);
      return;
    }
    trackEvent("button_create_playlist");
    await load();
    setSelectedPlaylistId(result.id);
    setCreating(false);
  }, [editName, load, playlistsData, tgUser?.id]);

  const handleCancelNewPlaylist = useCallback(() => {
    setCreatingNew(false);
    setEditName("");
  }, []);

  useEffect(() => {
    if (!editingId) return;
    const focusInput = () => editInputRef.current?.focus({ preventScroll: false });
    const tId = setTimeout(focusInput, 100);
    return () => clearTimeout(tId);
  }, [editingId]);

  if (!isLoggedIn) {
    return (
      <div className="min-h-full px-4 pt-5 pb-32">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-2 text-text-muted hover:text-text mb-6 touch-manipulation"
        >
          <ChevronLeft className="h-5 w-5" /> Назад
        </button>
        <p className="text-text-muted">Войдите через Telegram, чтобы видеть профиль и плейлисты.</p>
      </div>
    );
  }

  const addedIds = tgUser?.id ? new Set(getAddedPlaylistIds(tgUser.id)) : new Set<string>();
  const ownPlaylists = playlistsData?.playlists.filter((pl) => !addedIds.has(pl.id)) ?? [];
  const addedPlaylists = playlistsData?.playlists.filter((pl) => addedIds.has(pl.id)) ?? [];

  const inFolder = activeFolder !== null;

  return (
    <div className="min-h-full px-3 pt-2 pb-32 space-y-6">
      {/* Хедер: название по центру между кнопками Telegram, высота не выше текущей (4rem) */}
      <header
        className="app-screen-header"
        style={{ paddingTop: "max(0px, env(safe-area-inset-top, 0px))" }}
      >
        <button
          type="button"
          className="app-screen-header__back p-1 text-text-muted/70 hover:text-text touch-manipulation active:opacity-80"
          onClick={inFolder ? () => setActiveFolder(null) : onBack}
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
        {!inFolder && (
          <div className="app-screen-header__title-zone flex flex-col gap-0">
            <div
              className="w-16 h-16 rounded-full bg-accent/20 flex items-center justify-center text-2xl font-bold text-accent shrink-0 overflow-hidden"
              aria-hidden
            >
              {proxiedAvatarUrl ? (
                <img
                  key={`proxied-${tgUser?.id ?? "unknown"}`}
                  src={proxiedAvatarUrl}
                  alt=""
                  className="w-full h-full object-cover"
                />
              ) : canUseInitDataPhoto ? (
                <img
                  key={`initdata-${tgUser?.id ?? "unknown"}`}
                  src={tgUserWithPhoto?.photo_url}
                  alt=""
                  className="w-full h-full object-cover"
                  referrerPolicy="no-referrer"
                />
              ) : (
                tgUser?.first_name?.charAt(0)?.toUpperCase() ?? "?"
              )}
            </div>
            <h1 className="text-xl font-semibold text-text mt-2">
              {tgUser?.username ? `@${tgUser.username}` : tgUser?.first_name ?? "Пользователь"}
            </h1>
            <p className="text-sm text-text-muted">Мои плейлисты</p>
            {onLogout && (
              <button
                type="button"
                onClick={onLogout}
                className="mt-2.5 self-center text-[13px] font-medium text-text-muted/75 hover:text-rose-500/90 active:opacity-70 touch-manipulation bg-transparent border-0 p-0 cursor-pointer"
              >
                Выйти
              </button>
            )}
          </div>
        )}
        {inFolder && (
          <div className="app-screen-header__title-zone">
            <h1 className="text-xl font-semibold text-text">
              {activeFolder === "own" ? "Мои плейлисты" : "Добавленные"}
            </h1>
          </div>
        )}
      </header>

      <section aria-label="Плейлисты" className="relative">
        {openMenuId && (
          <button
            type="button"
            className="fixed inset-0 z-10 cursor-default"
            onClick={() => setOpenMenuId(null)}
            aria-label="Закрыть меню плейлиста"
          />
        )}
        {playlistsData && (
          <div className="space-y-4 relative z-20" onClick={() => setOpenMenuId(null)}>
            {!inFolder && (
              <PlaylistCard
                playlist={null}
                trackCount={playlistsData.favorites.length}
                isSelected={selectedPlaylistId === FAVORITES_ID}
                isFavorites
                onOpen={() =>
                  onOpenPlaylistScreen?.({
                    id: FAVORITES_ID,
                    name: "Избранное",
                    isFavorites: true,
                  })
                }
              />
            )}

            {/* Папки-карточки (видны только в корне профиля) */}
            {!inFolder && (
              <div className="grid grid-cols-2 gap-3">
                <button
                  type="button"
                  className="rounded-3xl glass-dark border border-sky-300/35 bg-white/8 px-4 py-4 flex flex-col items-start justify-between text-left shadow-card touch-manipulation"
                  onClick={(e) => {
                    e.stopPropagation();
                    setActiveFolder("own");
                  }}
                >
                  <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
                    Мои плейлисты
                  </span>
                  <span className="mt-2 inline-flex items-center justify-center rounded-full border border-white/70 bg-black/15 px-2.5 py-1 text-xs font-semibold text-white">
                    {ownPlaylists.length}
                  </span>
                </button>
                <button
                  type="button"
                  className="rounded-3xl glass-dark border border-sky-300/35 bg-white/8 px-4 py-4 flex flex-col items-start justify-between text-left shadow-card touch-manipulation"
                  onClick={(e) => {
                    e.stopPropagation();
                    setActiveFolder("added");
                  }}
                >
                  <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
                    Добавленные
                  </span>
                  <span className="mt-2 inline-flex items-center justify-center rounded-full border border-white/70 bg-black/15 px-2.5 py-1 text-xs font-semibold text-white">
                    {addedPlaylists.length}
                  </span>
                </button>
              </div>
            )}

            {/* Окно папки "Мои плейлисты" */}
            {activeFolder === "own" && (
              <div className="rounded-3xl glass-dark border border-white/10 px-3 py-3 space-y-2">
                <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted text-center">
                  Мои плейлисты
                </h2>
                <div className="grid grid-cols-1 gap-2">
                  {createError && (
                    <p className="text-[12px] text-red-400 px-1">{createError}</p>
                  )}
                  {!creatingNew && !creating && ownPlaylists.length < 5 && (
                    <button
                      type="button"
                      className="w-full py-3 px-4 rounded-2xl glass-dark text-text-muted hover:text-text shadow-card border border-white/10 transition-colors touch-manipulation"
                      onClick={handleStartCreate}
                    >
                      {"+ Создать плейлист"}
                    </button>
                  )}
                  {creatingNew && (
                  <div
                    role="button"
                    tabIndex={0}
                    className="flex items-center gap-2 py-2.5 px-4 rounded-2xl bg-white/[0.08] backdrop-blur-xl border border-white/[0.06] shadow-sm cursor-text"
                    onClick={(e) => {
                      e.stopPropagation();
                      newPlaylistInputRef.current?.focus();
                    }}
                    onKeyDown={(e) => e.key === "Enter" && newPlaylistInputRef.current?.focus()}
                  >
                    <input
                      ref={newPlaylistInputRef}
                      type="text"
                      value={editName}
                      onFocus={(e) => {
                        try {
                          e.currentTarget.scrollIntoView({ block: "center", behavior: "smooth" });
                        } catch {
                          // ignore
                        }
                      }}
                      onChange={(e) => setEditName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleConfirmNewPlaylist();
                        if (e.key === "Escape") handleCancelNewPlaylist();
                      }}
                      placeholder="Новый плейлист"
                      className="flex-1 min-w-0 py-2.5 px-0 bg-transparent border-0 outline-none text-text font-medium text-[16px] placeholder:text-white/25 focus:ring-0 caret-[rgb(var(--accent))]"
                      autoComplete="off"
                      enterKeyHint="done"
                      inputMode="text"
                      aria-label="Название плейлиста"
                    />
                    <button
                      type="button"
                      onClick={() => handleConfirmNewPlaylist()}
                      disabled={creating}
                      className="shrink-0 p-2 rounded-full text-text-muted hover:text-text active:opacity-80 touch-manipulation disabled:opacity-50"
                      aria-label="Подтвердить"
                    >
                      <Check className="h-5 w-5" strokeWidth={2.5} />
                    </button>
                    <button
                      type="button"
                      onClick={handleCancelNewPlaylist}
                      className="shrink-0 p-2 rounded-full text-text-muted hover:text-text active:opacity-80 touch-manipulation"
                      aria-label="Отменить"
                    >
                      <X className="h-5 w-5" strokeWidth={2.5} />
                    </button>
                  </div>
                )}
                  {creating && (
                    <div className="w-full py-3 px-4 rounded-2xl glass-dark text-text-muted text-center text-sm shadow-card">
                      Создание…
                    </div>
                  )}
                  {ownPlaylists.map((pl) =>
                  editingId === pl.id ? (
                    <div
                      key={pl.id}
                      className="flex items-center gap-2 py-2.5 px-4 rounded-2xl bg-white/[0.08] backdrop-blur-xl border border-white/[0.06] shadow-sm"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <input
                        ref={editInputRef}
                        type="text"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleRename(pl);
                          if (e.key === "Escape") {
                            setEditingId(null);
                            setEditName("");
                          }
                        }}
                        placeholder="Новый плейлист"
                        className="flex-1 min-w-0 py-2.5 px-0 bg-transparent border-0 outline-none text-text font-medium text-[16px] placeholder:text-white/25 focus:ring-0 caret-[rgb(var(--accent))]"
                      />
                      <button
                        type="button"
                        onClick={() => handleRename(pl)}
                        className="shrink-0 p-2 rounded-full text-text-muted hover:text-text active:opacity-80 touch-manipulation"
                        aria-label="Подтвердить"
                      >
                        <Check className="h-5 w-5" strokeWidth={2.5} />
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setEditingId(null);
                          setEditName("");
                        }}
                        className="shrink-0 p-2 rounded-full text-text-muted hover:text-text active:opacity-80 touch-manipulation"
                        aria-label="Отменить"
                      >
                        <X className="h-5 w-5" strokeWidth={2.5} />
                      </button>
                    </div>
                  ) : (
                    <PlaylistCard
                      key={pl.id}
                      playlist={pl}
                      trackCount={pl.track_count}
                      isSelected={selectedPlaylistId === pl.id}
                      isMenuOpen={openMenuId === pl.id}
                      onOpen={() =>
                        onOpenPlaylistScreen?.({
                          id: pl.id,
                          name: pl.name,
                          isFavorites: false,
                        })
                      }
                      onStartRename={() => {
                        setEditingId(pl.id);
                        setEditName(pl.name);
                      }}
                      onTogglePublic={async () => {
                        const nextPublic = !pl.is_public;
                        let ok = true;
                        if (nextPublic) {
                          const res = await createPlaylistShare(pl.id);
                          ok = !!res;
                        } else {
                          ok = await updatePlaylist(pl.id, { is_public: false });
                        }
                        if (ok) {
                          await load();
                        }
                      }}
                      onShare={async () => {
                        let shareId = pl.share_id ?? "";
                        if (!shareId) {
                          const res = await createPlaylistShare(pl.id);
                          if (!res) return;
                          shareId = res.share_id;
                          await load();
                        }
                        const url = getPlaylistShareUrl(shareId);
                        const text = `Я слушаю плейлист «${pl.name}» в TGPlay\n\n▶️ PLAYLIST: ${url}`;
                        const shareUrl = `https://t.me/share/url?url=&text=${encodeURIComponent(
                          text,
                        )}`;
                        openTelegramDeepLink(shareUrl);
                        setOpenMenuId(null);
                      }}
                      onDelete={async () => {
                        if (
                          !confirm(
                            "Удалить плейлист? Треки останутся в «Избранном», если были там.",
                          )
                        )
                          return;
                        const ok = await deletePlaylist(pl.id);
                        if (ok) {
                          if (selectedPlaylistId === pl.id) setSelectedPlaylistId(null);
                          setOpenMenuId(null);
                          load();
                        }
                      }}
                      onToggleMenu={() => {
                        setOpenMenuId((prev) => (prev === pl.id ? null : pl.id));
                      }}
                    />
                    ),
                  )}
                </div>
              </div>
            )}

            {activeFolder === "added" && (
              <div className="rounded-3xl glass-dark border border-white/10 px-3 py-3 space-y-2">
                <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted text-center">
                  Добавленные
                </h2>
                <div className="grid grid-cols-1 gap-2">
                  {addedPlaylists.map((pl) => (
                    <PlaylistCard
                      key={pl.id}
                      playlist={pl}
                      trackCount={pl.track_count}
                      isSelected={selectedPlaylistId === pl.id}
                      isMenuOpen={openMenuId === pl.id}
                      onOpen={() =>
                        onOpenPlaylistScreen?.({
                          id: pl.id,
                          name: pl.name,
                          isFavorites: false,
                          isAdded: true,
                        })
                      }
                      onStartRename={undefined}
                      onTogglePublic={undefined}
                      onShare={undefined}
                      onDelete={async () => {
                        if (
                          !confirm(
                            "Удалить плейлист? Треки останутся в «Избранном», если были там.",
                          )
                        )
                          return;
                        const ok = await deletePlaylist(pl.id);
                        if (ok) {
                          clearSharedMappingForPlaylist(pl.id);
                          if (selectedPlaylistId === pl.id) setSelectedPlaylistId(null);
                          setOpenMenuId(null);
                          load();
                        }
                      }}
                      onToggleMenu={() => {
                        setOpenMenuId((prev) => (prev === pl.id ? null : pl.id));
                      }}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
