import { useRef } from "react";
import { Search } from "lucide-react";

type SearchBarProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit?: () => void;
  disabled?: boolean;
  loading?: boolean;
};

export const SearchBar = ({ value, onChange, onSubmit, disabled, loading }: SearchBarProps) => {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      onSubmit?.();
      inputRef.current?.blur();
    }
  };

  return (
    <div
      className={`w-full glass rounded-full py-3.5 flex items-center gap-3 shadow-card pr-5 ${
        disabled ? "opacity-70" : ""
      }`}
      style={{ paddingLeft: "40px" }}
    >
      {loading ? (
        <span className="h-4 w-4 rounded-full border-[2px] border-text-muted border-t-transparent animate-spin shrink-0" />
      ) : (
        <Search className="h-4 w-4 text-text-muted shrink-0" />
      )}
      <input
        ref={inputRef}
        data-testid="search-input"
        className="w-full bg-transparent outline-none text-[15px] font-medium placeholder:text-text-muted focus:ring-0 border-0"
        placeholder="Поиск треков, артистов..."
        value={value}
        onChange={(event) => {
          if (disabled) return;
          onChange(event.target.value);
        }}
        onKeyDown={handleKeyDown}
        enterKeyHint="search"
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
        disabled={disabled}
        readOnly={disabled}
      />
    </div>
  );
};
