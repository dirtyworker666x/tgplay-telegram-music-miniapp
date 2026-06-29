import { MoreVertical } from "lucide-react";
import type { PlaylistMeta } from "../../types";

type PlaylistCardProps = {
  playlist: PlaylistMeta | null;
  trackCount: number;
  isSelected?: boolean;
  isFavorites?: boolean;
  isMenuOpen?: boolean;
  onOpen: () => void;
  onStartRename?: () => void;
  onTogglePublic?: () => void;
  onShare?: () => void;
  onDelete?: () => void;
  onToggleMenu?: () => void;
};

export const PlaylistCard = ({
  playlist,
  trackCount,
  isSelected,
  isFavorites = false,
  onOpen,
  onStartRename,
  onTogglePublic,
  onShare,
  onDelete,
  isMenuOpen = false,
  onToggleMenu,
}: PlaylistCardProps) => {
  const isPublic = !!playlist?.is_public;
  const canTogglePublic = !isFavorites && !!onTogglePublic;
  const canShare = !isFavorites && !!onShare && isPublic;

  return (
    <div
      className={`relative rounded-2xl px-4 py-3 flex items-center gap-3 transition-colors ${
        isSelected
          ? "glass-dark text-text shadow-card"
          : "bg-surface-strong/70 text-text hover:bg-surface-strong/90 dark:bg-white/10 dark:hover:bg-white/15"
      }`}
    >
      <button
        type="button"
        className="flex-1 min-w-0 text-left flex flex-col gap-1 touch-manipulation"
        onClick={onOpen}
      >
        <span className="font-medium truncate">
          {isFavorites ? "Избранное" : playlist?.name ?? "Плейлист"}
        </span>
        <span className="text-xs text-text-muted flex items-center gap-2">
          <span className="tabular-nums">{trackCount}</span>
          {isFavorites ? (
            <span className="px-2 py-0.5 rounded-full bg-white/5 text-[11px] uppercase tracking-[0.16em]">
              Приватный
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded-full bg-white/5 text-[11px] uppercase tracking-[0.16em]">
              {isPublic ? "Публичный" : "Приватный"}
            </span>
          )}
        </span>
      </button>
      {!isFavorites && (onStartRename || canTogglePublic || canShare || onDelete) && (
        <div className="relative shrink-0" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            className="p-2 -mr-2 rounded-full text-text-muted/70 hover:text-text active:bg-white/10 touch-manipulation"
            aria-label="Действия с плейлистом"
            onClick={(e) => {
              e.stopPropagation();
              onToggleMenu?.();
            }}
          >
            <MoreVertical className="h-4 w-4" />
          </button>
          {isMenuOpen && (
            <div
              className="absolute right-0 bottom-8 z-30 w-48 rounded-2xl bg-surface-strong/95 shadow-card border border-white/10 py-1"
              onClick={(e) => e.stopPropagation()}
            >
              {canShare && (
                <button
                  type="button"
                  className="w-full text-left px-3 py-1.5 text-[13px] hover:bg-white/10 touch-manipulation"
                  onClick={() => {
                    onShare?.();
                  }}
                >
                  Поделиться плейлистом
                </button>
              )}
              {canTogglePublic && (
                <div className="w-full flex items-center justify-between px-3 py-1.5 text-[13px] hover:bg-white/10">
                  <span className="select-none">Публичный доступ</span>
                  <button
                    type="button"
                    className="touch-manipulation"
                    aria-label="Переключить публичный доступ к плейлисту"
                    onClick={() => {
                      onTogglePublic?.();
                    }}
                  >
                    <span
                      className={`w-10 h-5 rounded-full flex items-center px-[3px] transition-colors duration-150 ${
                        isPublic ? "bg-accent shadow-inner" : "bg-zinc-400/60"
                      }`}
                    >
                      <span
                        className={`w-4 h-4 rounded-full bg-white shadow-sm transform transition-transform duration-150 ${
                          isPublic ? "translate-x-4" : ""
                        }`}
                      />
                    </span>
                  </button>
                </div>
              )}
              {onStartRename && (
                <button
                  type="button"
                  className="w-full text-left px-3 py-1.5 text-[13px] hover:bg-white/10 touch-manipulation"
                  onClick={() => {
                    onStartRename();
                  }}
                >
                  Переименовать
                </button>
              )}
              {onDelete && (
                <button
                  type="button"
                  className="w-full text-left px-3 py-1.5 text-[13px] text-red-400 hover:bg-white/10 touch-manipulation"
                  onClick={() => {
                    onDelete();
                  }}
                >
                  Удалить плейлист
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

