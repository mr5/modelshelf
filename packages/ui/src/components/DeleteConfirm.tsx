import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export function DeleteConfirm({
  triggerLabel,
  title,
  description,
  confirmLabel,
  onConfirm,
  optionLabel,
  disabled = false,
  triggerClassName = "ghost danger-text small",
}: {
  triggerLabel: string;
  title: string;
  description: string | ((optionChecked: boolean) => string);
  confirmLabel: string;
  onConfirm: (optionChecked: boolean) => Promise<boolean>;
  optionLabel?: string;
  disabled?: boolean;
  triggerClassName?: string;
}) {
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [optionChecked, setOptionChecked] = useState(false);
  const [position, setPosition] = useState({ left: 12, top: 12 });

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const trigger = triggerRef.current?.getBoundingClientRect();
      const popover = popoverRef.current;
      if (!trigger || !popover) return;
      const gap = 8;
      const edge = 12;
      const width = popover.offsetWidth;
      const height = popover.offsetHeight;
      const left = Math.min(
        Math.max(edge, trigger.right - width),
        window.innerWidth - width - edge,
      );
      const below = trigger.bottom + gap;
      const top = below + height <= window.innerHeight - edge
        ? below
        : Math.max(edge, trigger.top - height - gap);
      setPosition({ left, top });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !popoverRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", close);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  async function confirm() {
    setBusy(true);
    try {
      if (await onConfirm(optionChecked)) {
        setOpen(false);
        setOptionChecked(false);
      }
    } finally {
      setBusy(false);
    }
  }

  return <span className="delete-confirm">
    <button
      ref={triggerRef}
      className={triggerClassName}
      disabled={disabled || busy}
      aria-haspopup="dialog"
      aria-expanded={open}
      onClick={() => setOpen((current) => {
        if (current) setOptionChecked(false);
        return !current;
      })}
    >{triggerLabel}</button>
    {open && createPortal(<div
      ref={popoverRef}
      className="delete-popover"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      style={{ left: position.left, top: position.top }}
    >
      <div className="delete-popover-copy">
        <span className="delete-popover-icon" aria-hidden="true">!</span>
        <div><strong id={titleId}>{title}</strong><p>{typeof description === "function" ? description(optionChecked) : description}</p></div>
      </div>
      {optionLabel && <label className="delete-popover-option"><input type="checkbox" checked={optionChecked} disabled={busy} onChange={(event) => setOptionChecked(event.target.checked)} /><span>{optionLabel}</span></label>}
      <div className="delete-popover-actions">
        <button className="ghost small" disabled={busy} onClick={() => { setOpen(false); setOptionChecked(false); triggerRef.current?.focus(); }}>Cancel</button>
        <button className="danger small" disabled={busy} onClick={() => void confirm()}>{busy ? "Deleting…" : confirmLabel}</button>
      </div>
    </div>, document.body)}
  </span>;
}
