import { useEffect, useRef, type RefObject } from "react";
import WaveSurfer from "wavesurfer.js";

type WaveformProps = {
  url: string | null;
  audioRef: RefObject<HTMLAudioElement>;
};

export const Waveform = ({ url, audioRef }: WaveformProps) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const waveSurferRef = useRef<WaveSurfer | null>(null);

  useEffect(() => {
    if (!containerRef.current || !audioRef.current || !url) return;

    const styles = getComputedStyle(document.documentElement);
    const waveColor = `rgb(${styles.getPropertyValue("--wave").trim()})`;
    const progressColor = `rgb(${styles
      .getPropertyValue("--wave-progress")
      .trim()})`;

    const waveSurfer = WaveSurfer.create({
      container: containerRef.current,
      height: 80,
      barWidth: 3,
      barGap: 2,
      cursorWidth: 0,
      waveColor,
      progressColor,
      backend: "MediaElement",
      media: audioRef.current,
      normalize: true,
    });

    waveSurfer.load(url);
    waveSurferRef.current = waveSurfer;

    return () => {
      waveSurfer.destroy();
      waveSurferRef.current = null;
    };
  }, [audioRef, url]);

  return <div className="waveform w-full" ref={containerRef} />;
};
