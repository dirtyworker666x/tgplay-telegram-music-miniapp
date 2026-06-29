import { useEffect } from "react";
import type { Track } from "../types";

/**
 * Media Session API — системные медиа-контроли
 * (экран блокировки, пуш-уведомление плеера, шторка).
 */
export const useMediaSession = (
  track: Track | null,
  isPlaying: boolean,
  onToggle: () => void,
  onNext: () => void,
  onPrev: () => void,
  onSeek?: (time: number) => void,
  duration?: number,
  currentTime?: number,
) => {
  // Метаданные трека (обложка или иконка приложения на заблокированном экране)
  useEffect(() => {
    if (!("mediaSession" in navigator) || !track) return;
    const artwork: MediaImage[] = [];
    const artSrc = track.artwork || (typeof window !== "undefined" ? `${window.location.origin}/icon-track.png` : "");
    if (artSrc) {
      const type = track.artwork ? "image/jpeg" : "image/png";
      artwork.push(
        { src: artSrc, sizes: "96x96", type },
        { src: artSrc, sizes: "256x256", type },
        { src: artSrc, sizes: "512x512", type },
      );
    }
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title,
      artist: track.artist,
      album: "TGPlay",
      artwork,
    });
  }, [track]);

  // Статус — всегда "playing" если есть трек и мы не на паузе
  // (isPlaying остаётся true во время буферизации)
  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.playbackState = isPlaying ? "playing" : "paused";
  }, [isPlaying]);

  // Обработчики кнопок
  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    const handlers: [MediaSessionAction, MediaSessionActionHandler][] = [
      ["play", onToggle],
      ["pause", onToggle],
      ["previoustrack", onPrev],
      ["nexttrack", onNext],
    ];
    if (onSeek) {
      handlers.push(["seekto", (d) => { if (d.seekTime != null) onSeek(d.seekTime); }]);
    }
    for (const [action, handler] of handlers) {
      try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* unsupported */ }
    }
    return () => {
      for (const [action] of handlers) {
        try { navigator.mediaSession.setActionHandler(action, null); } catch { /* ignore */ }
      }
    };
  }, [onToggle, onNext, onPrev, onSeek]);

  // Позиция воспроизведения
  useEffect(() => {
    if (!("mediaSession" in navigator) || !track) return;
    if (!("setPositionState" in navigator.mediaSession)) return;
    if (!duration || duration <= 0) return;
    try {
      navigator.mediaSession.setPositionState({
        duration,
        playbackRate: 1,
        position: Math.min(Math.max(currentTime ?? 0, 0), duration),
      });
    } catch { /* ignore */ }
  }, [track, duration, currentTime]);
};
