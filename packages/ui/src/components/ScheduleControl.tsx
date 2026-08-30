import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

function localDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function ScheduleControl({
  scheduledAt,
  disabled = false,
  onSave,
}: {
  scheduledAt: string;
  disabled?: boolean;
  onSave: (scheduledAt: string) => Promise<boolean>;
}) {
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [value, setValue] = useState(() => localDateTimeValue(new Date(scheduledAt)));
  const [validationError, setValidationError] = useState("");
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const minimumScheduledAt = localDateTimeValue(new Date(Date.now() + 60_000));

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
      const left = Math.min(Math.max(edge, trigger.right - width), window.innerWidth - width - edge);
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
      if (!triggerRef.current?.contains(target) && !popoverRef.current?.contains(target)) setOpen(false);
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

  function close() {
    setOpen(false);
    setValidationError("");
    triggerRef.current?.focus();
  }

  async function save() {
    const schedule = new Date(value);
    if (!value || Number.isNaN(schedule.getTime()) || schedule <= new Date()) {
      setValidationError("Choose a scheduled start time in the future.");
      return;
    }
    setBusy(true);
    setValidationError("");
    try {
      if (await onSave(schedule.toISOString())) close();
    } finally {
      setBusy(false);
    }
  }

  return <span className="resume-control">
    <button
      ref={triggerRef}
      className="ghost"
      disabled={disabled || busy}
      aria-haspopup="dialog"
      aria-expanded={open}
      onClick={() => {
        if (!open) setValue(localDateTimeValue(new Date(scheduledAt)));
        setOpen((current) => !current);
      }}
    >Change time</button>
    {open && createPortal(<div
      ref={popoverRef}
      className="resume-popover"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      style={{ left: position.left, top: position.top }}
    >
      <div className="resume-popover-copy">
        <strong id={titleId}>Change scheduled start</strong>
        <p>The existing timer will be replaced. No source polling occurs while the task waits.</p>
      </div>
      <label className="resume-popover-time">Start time
        <input type="datetime-local" value={value} min={minimumScheduledAt} disabled={busy} onChange={(event) => {
          setValue(event.target.value);
          setValidationError("");
        }} />
        <small>Uses your browser's local timezone.</small>
      </label>
      {validationError && <p className="resume-popover-error">{validationError}</p>}
      <div className="resume-popover-actions">
        <button className="ghost small" disabled={busy} onClick={close}>Cancel</button>
        <button className="small" disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : "Save time"}</button>
      </div>
    </div>, document.body)}
  </span>;
}
