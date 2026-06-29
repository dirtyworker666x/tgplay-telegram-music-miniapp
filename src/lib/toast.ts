// Централизованный "тихий" toast: весь UI-опыт с тостами отключён.
// API совместим с sonner для уже существующего кода.

type ToastId = string | number;

type ToastFn = ((message: string) => ToastId) & {
  success: (message: string) => ToastId;
  error: (message: string) => ToastId;
  loading: (message: string) => ToastId;
  dismiss: (id?: ToastId) => void;
};

const noopId: ToastId = "";

export const toast: ToastFn = Object.assign(
  ((_message: string) => noopId) as ToastFn,
  {
    success: (_msg: string) => noopId,
    error: (_msg: string) => noopId,
    loading: (_msg: string) => noopId,
    dismiss: (_id?: ToastId) => {
      // no-op: тосты не показываем вообще
    },
  },
);

