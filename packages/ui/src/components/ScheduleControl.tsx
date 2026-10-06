import { useId, useState } from "react";
import { Button } from "@/components/motion/button/base";
import { StatefulButton } from "@/components/motion/button/stateful";
import { Input } from "@/components/motion/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/motion/popover";
import { localDateTimeValue } from "@/datetime";

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
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [value, setValue] = useState(() =>
    localDateTimeValue(new Date(scheduledAt)),
  );
  const [validationError, setValidationError] = useState("");
  const minimumScheduledAt = localDateTimeValue(new Date(Date.now() + 60_000));

  function changeOpen(next: boolean) {
    if (busy) return;
    if (next) setValue(localDateTimeValue(new Date(scheduledAt)));
    setValidationError("");
    setOpen(next);
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
      if (await onSave(schedule.toISOString())) setOpen(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Popover
      open={open}
      onOpenChange={changeOpen}
      side="top"
      align="end"
      sideOffset={10}
    >
      <PopoverTrigger>
        <Button variant="outline" disabled={disabled || busy}>
          Change time
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[min(370px,calc(100vw-24px))]">
        <div
          role="group"
          aria-labelledby={titleId}
          className="grid gap-3.5 p-4"
        >
          <div>
            <strong id={titleId} className="mb-1 block text-sm font-semibold">
              Change scheduled start
            </strong>
            <p className="text-xs leading-normal text-muted-foreground">
              The existing timer will be replaced. No source polling occurs
              while the task waits.
            </p>
          </div>
          <div className="grid gap-1.5">
            <Input
              label="Start time"
              type="datetime-local"
              value={value}
              min={minimumScheduledAt}
              disabled={busy}
              error={validationError || undefined}
              onChange={(next) => {
                setValue(next);
                setValidationError("");
              }}
            />
            <small className="px-1 text-xs text-muted-foreground">
              Uses your browser's local timezone.
            </small>
          </div>
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => changeOpen(false)}
            >
              Cancel
            </Button>
            <StatefulButton
              size="sm"
              state={busy ? "loading" : "idle"}
              loadingText="Saving…"
              onClick={() => void save()}
            >
              Save time
            </StatefulButton>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
