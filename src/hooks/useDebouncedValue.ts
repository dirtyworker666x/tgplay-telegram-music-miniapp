import { useEffect, useState } from "react";

export const useDebouncedValue = <T,>(value: T, delay = 450) => {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedValue(value), delay);
    return () => window.clearTimeout(id);
  }, [value, delay]);

  return debouncedValue;
};
