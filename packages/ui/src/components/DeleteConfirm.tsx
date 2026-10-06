import { TriangleAlert } from "lucide-react";
import { type ReactNode, useId, useState } from "react";
import {
  Button,
  type ButtonSize,
  type ButtonVariant,
} from "@/components/motion/button/base";
import { StatefulButton } from "@/components/motion/button/stateful";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/motion/popover";
import { CheckRow, Inset } from "@/components/ui";

type Side = "top" | "bottom";

/** The one deliberate restyle: irreversible confirmations use the destructive token. */
export const destructiveButton =
  "bg-destructive text-primary-foreground hover:bg-destructive/90";
type Align = "start" | "center" | "end";

export function DeleteConfirm({
  triggerLabel,
  triggerIcon,
  title,
  description,
  confirmLabel,
  onConfirm,
  optionLabel,
  busyLabel = "Deleting…",
  disabled = false,
  triggerVariant = "ghost",
  triggerSize = "sm",
  side = "bottom",
  align = "end",
}: {
  triggerLabel: string;
  triggerIcon?: ReactNode;
  title: string;
  description: string | ((optionChecked: boolean) => string);
  confirmLabel: string;
  onConfirm: (optionChecked: boolean) => Promise<boolean>;
  optionLabel?: string;
  busyLabel?: string;
  disabled?: boolean;
  triggerVariant?: ButtonVariant;
  triggerSize?: ButtonSize;
  side?: Side;
  align?: Align;
}) {
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [optionChecked, setOptionChecked] = useState(false);

  function changeOpen(next: boolean) {
    if (busy) return;
    if (!next) setOptionChecked(false);
    setOpen(next);
  }

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

  return (
    <Popover
      open={open}
      onOpenChange={changeOpen}
      side={side}
      align={align}
      sideOffset={10}
    >
      <PopoverTrigger>
        <Button
          variant={triggerVariant}
          size={triggerSize}
          disabled={disabled || busy}
          aria-label={
            triggerIcon && triggerSize === "icon" ? triggerLabel : undefined
          }
          title={
            triggerIcon && triggerSize === "icon" ? triggerLabel : undefined
          }
        >
          {triggerIcon}
          {!(triggerIcon && triggerSize === "icon") && triggerLabel}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[min(340px,calc(100vw-24px))]">
        <div role="group" aria-labelledby={titleId} className="grid gap-4 p-4">
          <div className="grid grid-cols-[28px_minmax(0,1fr)] items-start gap-2.5">
            <span
              className="grid size-7 place-items-center rounded-full bg-destructive/10 text-destructive"
              aria-hidden
            >
              <TriangleAlert className="size-3.5" />
            </span>
            <div>
              <strong
                id={titleId}
                className="mb-1 mt-px block text-sm leading-tight"
              >
                {title}
              </strong>
              <p className="text-xs leading-normal text-muted-foreground">
                {typeof description === "function"
                  ? description(optionChecked)
                  : description}
              </p>
            </div>
          </div>
          {optionLabel && (
            <Inset className="p-3">
              <CheckRow
                checked={optionChecked}
                disabled={busy}
                onCheckedChange={setOptionChecked}
                title={optionLabel}
              />
            </Inset>
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
              loadingText={busyLabel}
              className={destructiveButton}
              onClick={() => void confirm()}
            >
              {confirmLabel}
            </StatefulButton>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
