"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, inputClass } from "./ui";

/** Confirmation for actions that move money or stop trading. Optionally requires typing a word. */
export function ConfirmModal({
  open,
  title,
  children,
  confirmLabel,
  confirmWord,
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  confirmWord?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setTyped("");
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  const ready = !confirmWord || typed.trim() === confirmWord;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onMouseDown={onCancel}>
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        className={`w-full max-w-md rounded-md border bg-panel p-5 shadow-2xl focus:outline-none ${danger ? "border-down/60" : "border-line"}`}
      >
        <h3 className={`font-mono text-sm font-semibold uppercase tracking-wider ${danger ? "text-down" : "text-ink"}`}>{title}</h3>
        <div className="mt-3 space-y-3 text-sm text-ink/90">{children}</div>
        {confirmWord && (
          <div className="mt-4">
            <label className="block font-mono text-[11px] uppercase tracking-[0.14em] text-mute">
              Type <span className="text-ink">{confirmWord}</span> to confirm
              <input className={`${inputClass} mt-1`} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            </label>
          </div>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={!ready || busy}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
