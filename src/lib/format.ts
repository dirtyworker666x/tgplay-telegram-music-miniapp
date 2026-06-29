export const formatTime = (value: number) => {
  if (!Number.isFinite(value)) return "0:00";
  const minutes = Math.floor(value / 60);
  const seconds = Math.floor(value % 60)
    .toString()
    .padStart(2, "0");
  return `${minutes}:${seconds}`;
};

/** Длительность в секундах → строка M:SS или H:MM:SS */
export const formatDuration = (totalSeconds: number) => formatTime(totalSeconds);
