export const LoadingState = () => {
  return (
    <div className="space-y-3">
      {[0, 1, 2].map((item) => (
        <div
          key={item}
          className="glass rounded-2xl p-3 flex items-center gap-3 overflow-hidden relative"
        >
          <div className="h-12 w-12 rounded-2xl bg-white/60 dark:bg-white/10 shrink-0" />
          <div className="flex-1 space-y-2">
            <div className="h-3 w-1/2 rounded-full bg-white/60 dark:bg-white/10" />
            <div className="h-3 w-1/3 rounded-full bg-white/40 dark:bg-white/5" />
          </div>
          <div className="absolute inset-0 animate-shimmer bg-gradient-to-r from-transparent via-white/15 to-transparent pointer-events-none" />
        </div>
      ))}
    </div>
  );
};
