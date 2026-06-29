import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "framer-motion";
import { Heart, ListPlus, Loader2, X } from "lucide-react";
import { addToPlaylist, addTrackToPlaylist, createPlaylist, fetchPlaylists } from "../lib/api";
import type { PlaylistMeta, Track } from "../types";
import { trackEvent } from "../lib/analytics";
import { getTelegramUser } from "../lib/telegram";
import { getAddedPlaylistIds } from "../lib/playlistLocal";

type AddToPlaylistSheetProps = {
  track: Track | null;
  isOpen: boolean;
  onClose: () => void;
  onAdded?: () => void;
  /** Вызвать после добавления трека, чтобы обновить список плейлистов в профиле */
  onProfileRefresh?: () => void;
  /** Скрыть пункт «Избранное» (трек уже в избранном, например открыто из профиля) */
  hideFavorites?: boolean;
};

export function AddToPlaylistSheet({
  track,
  isOpen,
  onClose,
  onAdded,
  onProfileRefresh,
  hideFavorites = false,
}: AddToPlaylistSheetProps) {
  const [data, setData] = useState<{
    favorites: Track[];
    playlists: PlaylistMeta[];
    max_free_playlists: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [addingTo, setAddingTo] = useState<string | "favorites" | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const tgUser = getTelegramUser();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchPlaylists();
      if (res) setData(res);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen && track) {
      setData(null);
      load();
      setCreating(false);
      setNewName("");
    }
  }, [isOpen, track, load]);

  const handleAddToFavorites = useCallback(async () => {
    if (!track) return;
    setAddingTo("favorites");
    trackEvent("button_add_to_favorites");
    try {
      await addToPlaylist(track);
      onAdded?.();
      onProfileRefresh?.();
      onClose();
    } finally {
      setAddingTo(null);
    }
  }, [track, onAdded, onProfileRefresh, onClose]);

  const handleAddToPlaylist = useCallback(
    async (playlistId: string) => {
      if (!track) return;
      setAddingTo(playlistId);
      trackEvent("button_add_to_custom_playlist", { playlist_id: playlistId });
      try {
        await addTrackToPlaylist(playlistId, track);
        onAdded?.();
        onProfileRefresh?.();
        onClose();
      } finally {
        setAddingTo(null);
      }
    },
    [track, onAdded, onProfileRefresh, onClose],
  );

  const handleCreateAndAdd = useCallback(async () => {
    if (!track || !newName.trim()) return;
    setAddingTo("create");
    try {
      const pl = await createPlaylist(newName.trim());
      if (pl) {
        await addTrackToPlaylist(pl.id, track);
        onAdded?.();
        onProfileRefresh?.();
        onClose();
      }
    } finally {
      setAddingTo(null);
    }
  }, [track, newName, onAdded, onProfileRefresh, onClose]);

  if (!track) return null;

  const addedIds =
    tgUser?.id && data
      ? new Set(getAddedPlaylistIds(tgUser.id))
      : new Set<string>();
  const ownPlaylists =
    data?.playlists.filter((pl) => !addedIds.has(pl.id)) ?? [];
  const canCreateNew = data != null && ownPlaylists.length < 5;
  const content = (
    <AnimatePresence>
      {isOpen ? (
        <>
          <motion.div
            className="fixed inset-0 z-[99998] bg-black/50"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            aria-hidden
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Добавить в плейлист"
            className="fixed left-0 right-0 bottom-0 z-[99999] rounded-t-3xl overflow-hidden"
            style={{
              paddingBottom: "env(safe-area-inset-bottom)",
              background: "rgb(var(--surface))",
              boxShadow: "0 -8px 32px rgba(0,0,0,0.3)",
            }}
            initial={{ y: "100%" }}
            animate={{ y: 0 }}
            exit={{ y: "100%" }}
            transition={{ type: "spring", damping: 28, stiffness: 300 }}
          >
            <div className="w-full h-1.5 rounded-full bg-white/20 mx-auto mt-2.5 shrink-0" />
            <div className="px-4 py-3">
              <div className="flex items-start justify-between gap-3 mb-3">
                <h2 className="text-lg font-semibold text-text">Добавить в плейлист</h2>
                <button
                  type="button"
                  onClick={onClose}
                  className="p-2 -mr-2 rounded-full text-text-muted hover:text-text active:bg-white/10 touch-manipulation"
                  aria-label="Закрыть"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>
              <p className="text-[13px] text-text-muted truncate mb-4">{track.title} · {track.artist}</p>

              {loading || !data ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-8 w-8 animate-spin text-text-muted" />
                </div>
              ) : (
                <ul className="space-y-1">
                  {!hideFavorites && (
                    <li>
                      <button
                        type="button"
                        className="w-full flex items-center gap-3 p-3 rounded-2xl active:bg-white/10 text-left touch-manipulation"
                        onClick={handleAddToFavorites}
                        disabled={addingTo !== null}
                      >
                        <Heart className="h-5 w-5 text-accent shrink-0" style={{ color: "rgb(var(--accent))" }} />
                        <span className="font-medium text-text">Избранное</span>
                        {addingTo === "favorites" && <Loader2 className="h-4 w-4 animate-spin ml-auto text-text-muted" />}
                      </button>
                    </li>
                  )}
                  {data.playlists.map((pl) => (
                    <li key={pl.id}>
                      <button
                        type="button"
                        className="w-full flex items-center gap-3 p-3 rounded-2xl active:bg-white/10 text-left touch-manipulation"
                        onClick={() => handleAddToPlaylist(pl.id)}
                        disabled={addingTo !== null}
                      >
                        <ListPlus className="h-5 w-5 text-text-muted shrink-0" />
                        <span className="font-medium text-text truncate">{pl.name}</span>
                        {addingTo === pl.id && <Loader2 className="h-4 w-4 animate-spin ml-auto text-text-muted" />}
                      </button>
                    </li>
                  ))}
                  {canCreateNew && !creating && (
                    <li>
                      <button
                        type="button"
                        className="w-full flex items-center gap-3 p-3 rounded-2xl active:bg-white/10 text-left touch-manipulation border border-dashed border-white/20"
                        onClick={() => setCreating(true)}
                        disabled={addingTo !== null}
                      >
                        <ListPlus className="h-5 w-5 text-text-muted shrink-0" />
                        <span className="font-medium text-text">Создать новый</span>
                      </button>
                    </li>
                  )}
                  {creating && (
                    <li className="flex items-center gap-2 p-2">
                      <input
                        type="text"
                        value={newName}
                        onChange={(e) => setNewName(e.target.value)}
                        placeholder="Название плейлиста"
                        className="flex-1 px-3 py-2 rounded-xl bg-white/10 text-text placeholder:text-text-muted border-0 text-[14px]"
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleCreateAndAdd();
                          if (e.key === "Escape") setCreating(false);
                        }}
                      />
                      <button
                        type="button"
                        className="px-4 py-2 rounded-xl font-medium text-text bg-white/20 active:opacity-80 touch-manipulation disabled:opacity-50"
                        onClick={handleCreateAndAdd}
                        disabled={!newName.trim() || addingTo !== null}
                      >
                        {addingTo === "create" ? <Loader2 className="h-4 w-4 animate-spin" /> : "Готово"}
                      </button>
                    </li>
                  )}
                </ul>
              )}
            </div>
          </motion.div>
        </>
      ) : null}
    </AnimatePresence>
  );

  return createPortal(content, document.body);
}
