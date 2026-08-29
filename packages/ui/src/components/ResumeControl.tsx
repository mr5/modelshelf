import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

function localDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function ResumeControl({
  disabled = false,
  onResume,
}: {
  disabled?: boolean;
  onResume: (scheduledAt?: string) => Promise<boolean>;
}) {
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [delayed, setDelayed] = useState(false);
  const [scheduledAt, setScheduledAt] = useState("");
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
  }, [open, delayed]);

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

  function resetAndClose() {
    setOpen(false);
    setDelayed(false);
    setScheduledAt("");
    setValidationError("");
    triggerRef.current?.focus();
  }

  async function resume() {
    let schedule: Date | undefined;
    if (delayed) {
      schedule = new Date(scheduledAt);
      if (!scheduledAt || Number.isNaN(schedule.getTime()) || schedule <= new Date()) {
        setValidationError("Choose a resume time in the future.");
        return;
      }
    }
    setBusy(true);
    setValidationError("");
    try {
      if (await onResume(schedule?.toISOString())) resetAndClose();
    } finally {
      setBusy(false);
    }
  }

  return <span className="resume-control">
    <button
      ref={triggerRef}
      disabled={disabled || busy}
      aria-haspopup="dialog"
      aria-expanded={open}
      onClick={() => setOpen((current) => !current)}
    >Resume</button>
    {open && createPortal(<div
      ref={popoverRef}
      className="resume-popover"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      style={{ left: position.left, top: position.top }}
    >
      <div className="resume-popover-copy">
        <strong id={titleId}>Resume task</strong>
        <p>Continue from the downloaded staging data now, or at a specific time.</p>
      </div>
      <label className="resume-popover-option">
        <input type="checkbox" checked={delayed} disabled={busy} onChange={(event) => {
          const checked = event.target.checked;
          setDelayed(checked);
          setValidationError("");
          if (checked && !scheduledAt) {
            setScheduledAt(localDateTimeValue(new Date(Date.now() + 60 * 60_000)));
          }
        }} />
        <span><strong>Resume later</strong><small>No source polling occurs while this task waits.</small></span>
      </label>
      {delayed && <label className="resume-popover-time">Resume time
        <input type="datetime-local" value={scheduledAt} min={minimumScheduledAt} disabled={busy} onChange={(event) => {
          setScheduledAt(event.target.value);
          setValidationError("");
        }} />
        <small>Uses your browser's local timezone.</small>
      </label>}
      {validationError && <p className="resume-popover-error">{validationError}</p>}
      <div className="resume-popover-actions">
        <button className="ghost small" disabled={busy} onClick={resetAndClose}>Cancel</button>
        <button className="small" disabled={busy} onClick={() => void resume()}>{busy ? "Saving…" : delayed ? "Schedule resume" : "Resume now"}</button>
      </div>
    </div>, document.body)}
  </span>;
}
