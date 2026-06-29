import { Loader2, Pause, Play, Shuffle, SkipBack, SkipForward, Repeat } from "lucide-react";
import type { PlaybackRepeatMode } from "../lib/playerQueue";
import { isAndroid } from "../lib/telegram";

type PlayerControlsProps = {
  variant: "mini" | "full";
  isPlaying: boolean;
  isBuffering?: boolean;
  isShuffle: boolean;
  repeatMode: PlaybackRepeatMode;
  onTogglePlay: () => void;
  onNext: () => void;
  onPrev: () => void;
  onToggleShuffle: () => void;
  onCycleRepeatMode: () => void;
};

const ACCENT_STYLE = { color: "rgb(var(--accent))" } as const;

export const PlayerControls = ({
  variant,
  isPlaying,
  isBuffering,
  isShuffle,
  repeatMode,
  onTogglePlay,
  onNext,
  onPrev,
  onToggleShuffle,
  onCycleRepeatMode,
}: PlayerControlsProps) => {
  const isMini = variant === "mini";
  const isAndroidPlatform = isAndroid();

  const renderPlayIcon = () => {
    if (isBuffering) {
      const size = isMini ? "h-4 w-4" : "h-12 w-12";
      return <Loader2 className={`${size} animate-spin`} color={isMini ? undefined : "rgb(var(--accent))"} />;
    }
    if (isPlaying) {
      const size = isMini ? "h-4 w-4" : "h-12 w-12";
      return <Pause className={size} color={isMini ? undefined : "rgb(var(--accent))"} />;
    }
    const size = isMini ? "h-4 w-4" : "h-12 w-12";
    return <Play className={`${size} ${isMini ? "" : "ml-0.5"}`} color={isMini ? undefined : "rgb(var(--accent))"} />;
  };

  const renderRepeatIcon = () => {
    const size = isMini ? "h-4 w-4" : "h-5 w-5";
    return <Repeat className={size} />;
  };

  if (isMini) {
    return (
      <div className="flex items-center shrink-0 gap-0">
        <button
          type="button"
          className={`p-1.5 rounded-full active:opacity-80 touch-manipulation select-none ${
            isShuffle ? "text-accent" : "text-text-muted"
          }`}
          onClick={onToggleShuffle}
          aria-label="Перемешать треки"
        >
          <Shuffle className="h-4 w-4" />
        </button>
        <button
          className="p-1 rounded-full active:opacity-80 text-accent touch-manipulation select-none"
          onClick={onPrev}
          type="button"
          aria-label="Предыдущий трек"
        >
          <SkipBack className="h-4 w-4" />
        </button>
        <button
          className="h-8 w-8 rounded-full bg-transparent text-accent flex items-center justify-center active:opacity-80 touch-manipulation select-none"
          onClick={onTogglePlay}
          type="button"
          aria-label={isPlaying ? "Пауза" : "Воспроизведение"}
        >
          {renderPlayIcon()}
        </button>
        <button
          className="p-1 rounded-full active:opacity-80 text-accent touch-manipulation select-none"
          onClick={onNext}
          type="button"
          aria-label="Следующий трек"
        >
          <SkipForward className="h-4 w-4" />
        </button>
        <button
          type="button"
          className={`p-1.5 rounded-full active:opacity-80 touch-manipulation select-none ${
            repeatMode === "one" ? "text-accent" : "text-text-muted"
          }`}
          onClick={onCycleRepeatMode}
          aria-label={repeatMode === "one" ? "Отключить повтор трека" : "Зациклить трек"}
        >
          {renderRepeatIcon()}
        </button>
      </div>
    );
  }

  const fullMarginTopClass =
    variant === "full" && isAndroidPlatform ? "mt-0" : "mt-[1.5em]";

  return (
    <div className={`flex items-center justify-center gap-4 ${fullMarginTopClass}`}>
      <button
        type="button"
        className={`p-3 rounded-full bg-transparent active:opacity-80 border-0 touch-manipulation select-none ${
          isShuffle ? "text-accent" : "text-white/70"
        }`}
        onClick={onToggleShuffle}
        aria-label="Перемешать треки"
      >
        <Shuffle className="h-4 w-4" />
      </button>
      <button
        className="p-4 rounded-full bg-transparent active:opacity-80 border-0 touch-manipulation select-none"
        style={ACCENT_STYLE}
        onClick={onPrev}
        type="button"
        aria-label="Предыдущий трек"
      >
        <SkipBack className="h-8 w-8" color="rgb(var(--accent))" />
      </button>
      <button
        className="h-20 w-20 rounded-full bg-transparent flex items-center justify-center active:opacity-80 border-0 touch-manipulation select-none"
        style={ACCENT_STYLE}
        onClick={onTogglePlay}
        type="button"
        aria-label={isPlaying ? "Пауза" : "Воспроизведение"}
      >
        {renderPlayIcon()}
      </button>
      <button
        className="p-4 rounded-full bg-transparent active:opacity-80 border-0 touch-manipulation select-none"
        style={ACCENT_STYLE}
        onClick={onNext}
        type="button"
        aria-label="Следующий трек"
      >
        <SkipForward className="h-8 w-8" color="rgb(var(--accent))" />
      </button>
      <button
        type="button"
        className={`p-3 rounded-full bg-transparent active:opacity-80 border-0 touch-manipulation select-none ${
          repeatMode === "one" ? "text-accent" : "text-white/70"
        }`}
        onClick={onCycleRepeatMode}
        aria-label={repeatMode === "one" ? "Отключить повтор трека" : "Зациклить трек"}
      >
        {renderRepeatIcon()}
      </button>
    </div>
  );
}

