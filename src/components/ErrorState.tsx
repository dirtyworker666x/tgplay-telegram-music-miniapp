type ErrorStateProps = {
  message: string;
};

export const ErrorState = ({ message }: ErrorStateProps) => {
  return (
    <div className="glass rounded-2xl p-4 text-sm text-text-muted shadow-card">
      {message}
    </div>
  );
};
