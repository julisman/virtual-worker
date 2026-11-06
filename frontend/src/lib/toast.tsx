// Small global toast/notification system. Fixed top-center stack, auto-dismiss
// (success/info: 2.5s, error: 5s so it can be read). Every screen with a user
// save should route through this rather than owning its own success chip.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

export type ToastKind = "success" | "error" | "info";

interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  // Wall-clock ms at which the toast should be removed. Compared against
  // Date.now() by the per-toast timeout — no polling.
  expiresAt: number;
}

interface ToastCtx {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
  /** Programmatic dismiss (e.g. on route change). Rarely needed. */
  dismiss: (id: number) => void;
}

const Ctx = createContext<ToastCtx | null>(null);

const DURATION: Record<ToastKind, number> = { success: 2500, info: 2500, error: 5000 };

function CheckIcon() {
  return <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>;
}
function AlertIcon() {
  return <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" /></svg>;
}
function InfoIcon() {
  return <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><path d="M12 16v-4M12 8h.01" /></svg>;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((kind: ToastKind, message: string) => {
    const id = nextId.current++;
    const expiresAt = Date.now() + DURATION[kind];
    setToasts((ts) => [...ts, { id, kind, message, expiresAt }]);
  }, []);

  const value: ToastCtx = {
    success: useCallback((m: string) => push("success", m), [push]),
    error: useCallback((m: string) => push("error", m), [push]),
    info: useCallback((m: string) => push("info", m), [push]),
    dismiss,
  };

  return (
    <Ctx.Provider value={value}>
      {children}
      <ToastLayer toasts={toasts} onDismiss={dismiss} />
    </Ctx.Provider>
  );
}

function ToastLayer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div className="pointer-events-none fixed inset-x-0 top-4 z-[9999] flex flex-col items-center gap-2 px-4">
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} onDismiss={() => onDismiss(t.id)} />
      ))}
    </div>
  );
}

function ToastCard({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  useEffect(() => {
    const ms = Math.max(0, toast.expiresAt - Date.now());
    const t = window.setTimeout(onDismiss, ms);
    return () => window.clearTimeout(t);
  }, [toast.expiresAt, onDismiss]);

  const palette =
    toast.kind === "success" ? "border-success-soft bg-success-soft text-success"
      : toast.kind === "error" ? "border-danger-soft bg-danger-soft text-danger"
      : "border-primary-line bg-primary-soft text-primary-deep";
  const Icon = toast.kind === "success" ? CheckIcon : toast.kind === "error" ? AlertIcon : InfoIcon;
  const role = toast.kind === "error" ? "alert" : "status";

  return (
    <div role={role} aria-live={toast.kind === "error" ? "assertive" : "polite"}
      onClick={onDismiss}
      className={`vw-toast pointer-events-auto flex max-w-[420px] cursor-pointer items-center gap-2 rounded-full border px-4 py-2 text-sm font-medium shadow-[0_10px_30px_-12px_rgba(0,0,0,0.25)] ${palette}`}>
      <Icon />
      <span className="truncate">{toast.message}</span>
    </div>
  );
}

export function useToast(): ToastCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useToast must be used within <ToastProvider>");
  return v;
}
