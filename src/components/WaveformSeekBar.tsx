import { useCallback, useEffect, useRef, useState, memo } from "react";
import { formatTime } from "../lib/format";

/**
 * SoundCloud-style: одна непрерывная дорожка на всю ширину, проходит сзади таймера.
 * Таймер по центру поверх дорожки. Фиксированное число баров — без «больших кусков».
 */

type WaveformSeekBarProps = {
  trackId: string;
  currentTime: number;
  duration: number;
  onSeekStart: () => void;
  onSeekMove: (time: number) => void;
  onSeekEnd: (time: number) => void;
};

const BAR_COUNT = 80;
const BAR_MIN = 0.08;
const BAR_MAX = 1.0;
const CANVAS_HEIGHT = 116;
const BAR_WIDTH_RATIO = 0.6; // ширина бара от слота (остальное — зазор)
// Наши цвета: верх баров
const PLAYED_COLOR = "rgba(0, 136, 204, 1)";
const PLAYED_COLOR_TOP = "rgba(84, 169, 235, 0.9)";
const UNPLAYED_COLOR = "rgba(255, 255, 255, 0.25)";
const UNPLAYED_COLOR_TOP = "rgba(255, 255, 255, 0.15)";
// Затемнённая нижняя половина (как у SoundCloud)
const PLAYED_COLOR_BOTTOM = "rgba(0, 85, 150, 0.95)";
const UNPLAYED_COLOR_BOTTOM = "rgba(255, 255, 255, 0.08)";

// ─── Deterministic waveform generation ────────────────────────

function hashCode(str: string): number {
  let hash = 5381;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) + hash + str.charCodeAt(i)) | 0;
  }
  return hash >>> 0;
}

/** Seeded pseudo-random (mulberry32) */
function seededRandom(seed: number) {
  let t = seed + 0x6d2b79f5;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}

function generateWaveformData(trackId: string): Float32Array {
  const data = new Float32Array(BAR_COUNT);
  const seed = hashCode(trackId);

  // Generate raw values
  for (let i = 0; i < BAR_COUNT; i++) {
    data[i] = seededRandom(seed + i * 7 + 13);
  }

  // Apply envelope: louder in the middle, quieter at edges (like real music)
  for (let i = 0; i < BAR_COUNT; i++) {
    const position = i / BAR_COUNT;
    // Smooth rise in first 10%, sustain in middle, slight fall at end
    let envelope = 1.0;
    if (position < 0.08) envelope = 0.3 + 0.7 * (position / 0.08);
    else if (position > 0.92) envelope = 0.4 + 0.6 * ((1 - position) / 0.08);
    data[i] *= envelope;
  }

  // Smooth with neighbors (moving average window = 3)
  const smoothed = new Float32Array(BAR_COUNT);
  for (let i = 0; i < BAR_COUNT; i++) {
    const prev = i > 0 ? data[i - 1] : data[i];
    const next = i < BAR_COUNT - 1 ? data[i + 1] : data[i];
    smoothed[i] = prev * 0.2 + data[i] * 0.6 + next * 0.2;
  }

  // Normalize to [BAR_MIN, BAR_MAX]
  let max = 0;
  for (let i = 0; i < BAR_COUNT; i++) if (smoothed[i] > max) max = smoothed[i];
  if (max === 0) max = 1;
  for (let i = 0; i < BAR_COUNT; i++) {
    smoothed[i] = BAR_MIN + (smoothed[i] / max) * (BAR_MAX - BAR_MIN);
  }

  return smoothed;
}

// ─── Waveform cache (per trackId) ─────────────────────────────
const _waveformCache = new Map<string, Float32Array>();

function getWaveform(trackId: string): Float32Array {
  let wf = _waveformCache.get(trackId);
  if (!wf) {
    wf = generateWaveformData(trackId);
    _waveformCache.set(trackId, wf);
    // Keep cache small
    if (_waveformCache.size > 30) {
      const first = _waveformCache.keys().next().value;
      if (first) _waveformCache.delete(first);
    }
  }
  return wf;
}

// ─── Drawing: одна дорожка на всю ширину, линия прогресса — граница цветов, таймер поверх ───

function drawWaveform(
  ctx: CanvasRenderingContext2D,
  waveform: Float32Array,
  progress: number, // 0..1
  width: number,
  height: number,
  dpr: number,
) {
  const W = Math.round(width * dpr);
  const H = Math.round(height * dpr);
  ctx.clearRect(0, 0, W, H);

  const slotW = W / BAR_COUNT;
  const barW = Math.max(1, Math.floor(slotW * BAR_WIDTH_RATIO));
  const progressX = progress * W;

  const drawBar = (
    fromX: number, fromW: number, amp: number,
    topColor: "played" | "unplayed", bottomColor: "played" | "unplayed",
  ) => {
    const barH = Math.max(4, Math.round(amp * (H - 6)));
    const y = Math.floor((H - barH) / 2);
    const topHalfH = Math.floor(barH / 2);
    const bottomHalfH = barH - topHalfH;
    const ix = Math.round(fromX);
    const iw = Math.max(1, Math.round(fromW));
    const gradTop = ctx.createLinearGradient(ix, y, ix, y + topHalfH);
    gradTop.addColorStop(0, topColor === "played" ? PLAYED_COLOR_TOP : UNPLAYED_COLOR_TOP);
    gradTop.addColorStop(1, topColor === "played" ? PLAYED_COLOR : UNPLAYED_COLOR);
    ctx.fillStyle = gradTop;
    ctx.fillRect(ix, y, iw, topHalfH);
    ctx.fillStyle = bottomColor === "played" ? PLAYED_COLOR_BOTTOM : UNPLAYED_COLOR_BOTTOM;
    ctx.fillRect(ix, y + topHalfH, iw, bottomHalfH);
  };

  // Все бары по всей ширине (включая под таймер) — линия прогресса делит на проиграно/не проиграно
  for (let i = 0; i < BAR_COUNT; i++) {
    const x = i * slotW;
    const barRight = x + barW;
    const amp = waveform[i];
    if (barRight <= progressX) {
      drawBar(x, barW, amp, "played", "played");
    } else if (x >= progressX) {
      drawBar(x, barW, amp, "unplayed", "unplayed");
    } else {
      const playedW = progressX - x;
      if (playedW >= 1) drawBar(x, playedW, amp, "played", "played");
      const unplayedW = barRight - progressX;
      if (unplayedW >= 1) drawBar(progressX, unplayedW, amp, "unplayed", "unplayed");
    }
  }
}

// ─── Component ────────────────────────────────────────────────

export const WaveformSeekBar = memo(({
  trackId,
  currentTime,
  duration,
  onSeekStart,
  onSeekMove,
  onSeekEnd,
}: WaveformSeekBarProps) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const intentRef = useRef<"none" | "horizontal" | "vertical">("none");
  const startRef = useRef({ x: 0, y: 0 });
  const lastProgressRef = useRef(0);
  const sizeRef = useRef({ w: 0, h: 0, cw: 0, ch: 0 });
  const [localProgress, setLocalProgress] = useState<number | null>(null);
  const waveformRef = useRef<Float32Array>(getWaveform(trackId));
  const rafRef = useRef<number>(0);

  // Update waveform when track changes
  useEffect(() => {
    waveformRef.current = getWaveform(trackId);
    setLocalProgress(null);
  }, [trackId]);

  // Draw on every frame
  const progress = localProgress ?? (duration > 0 ? currentTime / duration : 0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const container = containerRef.current;
    if (!container) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = container.getBoundingClientRect();
    const w = rect.width;
    const h = CANVAS_HEIGHT;
    const cw = Math.round(w * dpr);
    const ch = Math.round(h * dpr);

    const prev = sizeRef.current;
    if (prev.w !== w || prev.h !== h || prev.cw !== cw || prev.ch !== ch) {
      sizeRef.current = { w, h, cw, ch };
      canvas.width = cw;
      canvas.height = ch;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
    }

    drawWaveform(ctx, waveformRef.current, Math.min(Math.max(progress, 0), 1), w, h, dpr);
  }, [progress, trackId]);

  // ─── Touch / Mouse handlers ──────────────────────────────

  const getProgressFromEvent = useCallback((clientX: number): number => {
    const container = containerRef.current;
    if (!container) return 0;
    const rect = container.getBoundingClientRect();
    const x = clientX - rect.left;
    return Math.min(Math.max(x / rect.width, 0), 1);
  }, []);

  const handleStart = useCallback((clientX: number) => {
    draggingRef.current = true;
    onSeekStart();
    const p = getProgressFromEvent(clientX);
    lastProgressRef.current = p;
    setLocalProgress(p);
    if (duration > 0) onSeekMove(p * duration);
  }, [getProgressFromEvent, onSeekStart, onSeekMove, duration]);

  const handleMove = useCallback((clientX: number) => {
    if (!draggingRef.current) return;
    const p = getProgressFromEvent(clientX);
    lastProgressRef.current = p;
    setLocalProgress(p);
    if (duration > 0) onSeekMove(p * duration);
  }, [getProgressFromEvent, onSeekMove, duration]);

  const handleEnd = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    if (duration > 0) {
      onSeekEnd(lastProgressRef.current * duration);
    }
    setLocalProgress(null);
  }, [duration, onSeekEnd]);

  // Touch events — вертикальный свайп отдаём родителю (FullPlayer), горизонтальный — для seek
  const onTouchStart = useCallback((e: React.TouchEvent) => {
    const t = e.touches[0];
    startRef.current = { x: t.clientX, y: t.clientY };
    intentRef.current = "none";
    // Не preventDefault — решаем на первом move
  }, []);

  const onTouchMove = useCallback((e: React.TouchEvent) => {
    const t = e.touches[0];
    const dx = Math.abs(t.clientX - startRef.current.x);
    const dy = Math.abs(t.clientY - startRef.current.y);

    if (intentRef.current === "none" && (dx > 8 || dy > 8)) {
      intentRef.current = dy > dx * 1.2 ? "vertical" : "horizontal";
      if (intentRef.current === "horizontal") {
        handleStart(startRef.current.x);
      }
    }

    if (intentRef.current === "vertical") {
      e.preventDefault(); // вертикальный свайп — блокируем, не закрываем плеер
      return;
    }
    if (intentRef.current === "horizontal") {
      e.preventDefault();
      handleMove(t.clientX);
    }
  }, [handleStart, handleMove]);

  const onTouchEnd = useCallback((e: React.TouchEvent) => {
    if (intentRef.current === "horizontal") {
      e.preventDefault();
      handleEnd();
    }
    intentRef.current = "none";
  }, [handleEnd]);

  // Mouse events
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    handleStart(e.clientX);
  }, [handleStart]);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => handleMove(e.clientX);
    const onMouseUp = () => handleEnd();

    if (draggingRef.current) {
      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp);
    }

    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  });

  // Cleanup RAF
  useEffect(() => {
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  const timeAtCursor = duration > 0 ? Math.min(progress * duration, duration) : 0;

  return (
    <div
      ref={containerRef}
      className="w-full relative cursor-pointer select-none touch-manipulation"
      style={{ height: CANVAS_HEIGHT, touchAction: "pan-x", contain: "paint", backfaceVisibility: "hidden" }}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
      onMouseDown={onMouseDown}
    >
      <canvas
        ref={canvasRef}
        className="absolute inset-0 block"
        style={{ width: "100%", height: CANVAS_HEIGHT }}
      />
      {/* Прямоугольник времени по центру (как в SoundCloud) */}
      <div
        className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 pointer-events-none flex items-center gap-1.5 px-2.5 py-1.5 rounded bg-black/90 text-[11px] font-semibold"
      >
        <span style={{ color: "rgb(84, 169, 235)" }}>{formatTime(timeAtCursor)}</span>
        <span className="text-white/50">/</span>
        <span className="text-white/90">{formatTime(duration)}</span>
      </div>
    </div>
  );
});

WaveformSeekBar.displayName = "WaveformSeekBar";
