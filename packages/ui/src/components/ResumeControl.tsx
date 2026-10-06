import { useId, useState } from "react";
import { Button } from "@/components/motion/button/base";
import { StatefulButton } from "@/components/motion/button/stateful";
import { Input } from "@/components/motion/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/motion/popover";
import { Inset, SwitchRow } from "@/components/ui";
import { localDateTimeValue } from "@/datetime";

export function ResumeControl({
  disabled = false,
  onResume,
  retry = false,
}: {
  disabled?: boolean;
  onResume: (scheduledAt?: string) => Promise<boolean>;
  retry?: boolean;
}) {
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [delayed, setDelayed] = useState(false);
  const [scheduledAt, setScheduledAt] = useState("");
  const [validationError, setValidationError] = useState("");
  const minimumScheduledAt = localDateTimeValue(new Date(Date.now() + 60_000));

  function changeOpen(next: boolean) {
    if (busy) return;
    if (!next) {
      setDelayed(false);
      setScheduledAt("");
      setValidationError("");
    }
    setOpen(next);
  }

  async function resume() {
    let schedule: Date | undefined;
    if (delayed) {
      schedule = new Date(scheduledAt);
      if (
        !scheduledAt ||
        Number.isNaN(schedule.getTime()) ||
        schedule <= new Date()
      ) {
        setValidationError("Choose a resume time in the future.");
        return;
      }
    }
    setBusy(true);
    setValidationError("");
    try {
      if (await onResume(schedule?.toISOString())) {
        setOpen(false);
        setDelayed(false);
        setScheduledAt("");
      }
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
        <Button disabled={disabled || busy}>
          {retry ? "Retry" : "Resume"}
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
              {retry ? "Retry task" : "Resume task"}
            </strong>
            <p className="text-xs leading-normal text-muted-foreground">
              Continue from retained data using the same locked revision, now or
              at a specific time.
            </p>
          </div>
          <Inset className="p-3">
            <SwitchRow
              title="Resume later"
              description="No source polling occurs while this task waits."
              checked={delayed}
              disabled={busy}
              onCheckedChange={(checked) => {
                setDelayed(checked);
                setValidationError("");
                if (checked && !scheduledAt)
                  setScheduledAt(
                    localDateTimeValue(new Date(Date.now() + 60 * 60_000)),
                  );
              }}
            />
          </Inset>
          {delayed && (
            <div className="grid gap-1.5">
              <Input
                label="Resume time"
                type="datetime-local"
                value={scheduledAt}
                min={minimumScheduledAt}
                disabled={busy}
                error={validationError || undefined}
                onChange={(value) => {
                  setScheduledAt(value);
                  setValidationError("");
                }}
              />
              <small className="px-1 text-xs text-muted-foreground">
                Uses your browser's local timezone.
              </small>
            </div>
          )}
          {validationError && !delayed && (
            <p className="text-xs text-destructive">{validationError}</p>
          )}
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
              onClick={() => void resume()}
            >
              {delayed ? "Schedule resume" : "Resume now"}
            </StatefulButton>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
