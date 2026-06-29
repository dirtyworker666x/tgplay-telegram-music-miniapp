import { useEffect } from "react";

function looksLikeHlsUrl(url: string): boolean {
  return /\.m3u8(\?|$)/i.test(url);
}

function wireProgressive(
  audio: HTMLAudioElement,
  url: string,
  cancelled: () => boolean,
  tryPlayAfterReady: () => void,
  fail: (msg: string) => void,
): () => void {
  audio.currentTime = 0;
  audio.src = url;
  try {
    audio.load();
  } catch {
    /* ignore */
  }
  // Не вызывать play() до canplay: в Telegram WebView ранний play() часто падает (NotSupportedError),
  // ломает цепочку загрузки и даёт «вечную» буферизацию без звука.

  const handleCanPlay = () => {
    if (cancelled()) return;
    tryPlayAfterReady();
  };

  const handleError = () => {
    if (cancelled()) return;
    fail("Ошибка загрузки аудио");
  };

  audio.addEventListener("canplay", handleCanPlay, { once: true });
  audio.addEventListener("error", handleError, { once: true });

  return () => {
    audio.removeEventListener("canplay", handleCanPlay);
    audio.removeEventListener("error", handleError);
    try {
      audio.pause();
    } catch {
      /* ignore */
    }
  };
}

/**
 * Подключает audio и сразу пытается play() — системный пуш видит «воспроизведение».
 * Для .m3u8 (VK HLS) подгружается hls.js; при сбое import — обычный src (Safari / часть WebView).
 */
export const useHlsAudio = (
  audioRef: React.RefObject<HTMLAudioElement>,
  url: string | null,
  /** Меняется на каждый старт трека — чтобы при том же кешированном URL эффект плеера перезапускался */
  playbackEpoch: number,
  onReady: () => void,
  onError: (msg: string) => void,
  onPlayRejected?: () => void
) => {
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !url) return;

    let cancelled = false;
    let hls: import("hls.js").default | null = null;
    let detachProgressive: (() => void) | null = null;
    let settled = false;

    const isCancelled = () => cancelled;

    const finishReady = () => {
      if (cancelled || settled) return;
      settled = true;
      onReady();
    };

    const fail = (msg: string) => {
      if (cancelled || settled) return;
      settled = true;
      onError(msg);
    };

    const tryPlayAfterReady = () => {
      if (cancelled) return;
      // Сброс в paused перед play(): иначе при гонке src элемент мог остаться «playing» без
      // реального старта нового потока; вечный play() подряд давал лавину error/skip на избранном.
      try {
        audio.pause();
      } catch {
        /* ignore */
      }
      audio
        .play()
        .then(() => {
          if (!cancelled) finishReady();
        })
        .catch(() => {
          if (!cancelled) onPlayRejected?.();
        });
    };

    if (!looksLikeHlsUrl(url)) {
      detachProgressive = wireProgressive(audio, url, isCancelled, tryPlayAfterReady, fail);
      return () => {
        cancelled = true;
        detachProgressive?.();
      };
    }

    let fallbackTimer: ReturnType<typeof setTimeout> | null = null;
    let hlsJsStarted = false;

    const startHlsJs = () => {
      if (cancelled || settled || hlsJsStarted) return;
      hlsJsStarted = true;
      if (fallbackTimer != null) {
        clearTimeout(fallbackTimer);
        fallbackTimer = null;
      }
      detachProgressive?.();
      detachProgressive = null;

      void import("hls.js")
        .then(({ default: Hls }) => {
        if (cancelled) return;

        if (Hls.isSupported()) {
          hls = new Hls({
            enableWorker: false,
            lowLatencyMode: false,
          });
          if (cancelled) {
            hls.destroy();
            hls = null;
            return;
          }
          audio.src = "";
          hls.attachMedia(audio);
          hls.on(Hls.Events.MANIFEST_PARSED, () => {
            if (cancelled) return;
            tryPlayAfterReady();
          });
          hls.on(Hls.Events.ERROR, (_e, data) => {
            if (cancelled || !data.fatal) return;
            fail("Ошибка загрузки аудио");
          });
          if (cancelled) {
            hls.destroy();
            hls = null;
            return;
          }
          hls.loadSource(url);
          return;
        }

        if (cancelled) return;
        detachProgressive = wireProgressive(audio, url, isCancelled, tryPlayAfterReady, fail);
      })
      .catch(() => {
        if (cancelled) return;
        detachProgressive = wireProgressive(audio, url, isCancelled, tryPlayAfterReady, fail);
      });
    };

    // Для Telegram WebView часто быстрее и стабильнее сначала дать нативному media-стеку открыть m3u8,
    // и только при подвисании быстро переключаться на hls.js.
    detachProgressive = wireProgressive(
      audio,
      url,
      isCancelled,
      tryPlayAfterReady,
      () => startHlsJs(),
    );
    fallbackTimer = setTimeout(() => {
      if (cancelled || settled || hlsJsStarted) return;
      startHlsJs();
    }, 1200);

    return () => {
      cancelled = true;
      if (fallbackTimer != null) {
        clearTimeout(fallbackTimer);
        fallbackTimer = null;
      }
      detachProgressive?.();
      detachProgressive = null;
      hls?.destroy();
      hls = null;
    };
  }, [audioRef, url, playbackEpoch, onReady, onError, onPlayRejected]);
};
