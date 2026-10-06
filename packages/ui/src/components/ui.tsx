import { AlertCircle } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type AnimatedBadgeStatus,
  AnimatedBadge,
} from "@/components/motion/animated-badge";
import {
  AnimatedToastStack,
  type ToastInput,
  useAnimatedToastStack,
} from "@/components/motion/animated-toast-stack";
import { type ButtonProps } from "@/components/motion/button/base";
import {
  type ButtonState,
  StatefulButton,
} from "@/components/motion/button/stateful";
import { Checkbox } from "@/components/motion/checkbox";
import { Loader } from "@/components/motion/loader";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/motion/select";
import { Switch } from "@/components/motion/switch";
import { TextShimmer } from "@/components/motion/text-shimmer";
import { EASE_OUT } from "@/lib/ease";
import { cn } from "@/lib/utils";

/* Layout ------------------------------------------------------------------ */

export function PageShell({
  narrow = false,
  className,
  children,
}: {
  narrow?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "min-w-0 pb-20 pt-10 sm:pt-14",
        narrow && "mx-auto w-full max-w-[760px]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function BrandMark({ large = false }: { large?: boolean }) {
  return (
    <span
      className={cn(
        "grid place-items-center bg-accent font-black text-accent-foreground",
        large
          ? "size-12 rounded-2xl text-[22px]"
          : "size-8 rounded-[10px] text-sm",
      )}
      aria-hidden
    >
      M
    </span>
  );
}

export function Eyebrow({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <p
      className={cn(
        "mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-primary",
        className,
      )}
    >
      {children}
    </p>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-6">
      <div className="min-w-0 max-w-3xl">
        <Eyebrow>{eyebrow}</Eyebrow>
        <h1 className="text-balance text-3xl font-semibold tracking-tight sm:text-4xl">
          {title}
        </h1>
        {description && (
          <p className="mt-2 text-pretty leading-relaxed text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </div>
  );
}

/** The primary surface: beUI's card token on the page background. */
export function Panel({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <section
      className={cn(
        "min-w-0 rounded-[28px] border border-border bg-card",
        className,
      )}
    >
      {children}
    </section>
  );
}

/** A nested surface inside a panel or modal. */
export function Inset({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-border bg-background",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center sm:gap-4">
      <div className="grid min-w-0 gap-1">
        <span className="flex items-center gap-2 text-sm font-semibold [&_svg]:size-4 [&_svg]:text-muted-foreground">
          {icon}
          {title}
        </span>
        {description && (
          <small className="text-xs leading-relaxed text-muted-foreground">
            {description}
          </small>
        )}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="px-6 py-16 text-center">
      <h2 className="mb-1.5 text-lg font-semibold">{title}</h2>
      <p className="text-sm text-muted-foreground">{children}</p>
    </div>
  );
}

/* Feedback ---------------------------------------------------------------- */

export function ErrorBox({
  title,
  children,
  className,
}: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      role="alert"
      initial={reduce ? false : { opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: EASE_OUT }}
      className={cn(
        "flex items-start gap-2.5 rounded-2xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm leading-normal text-destructive",
        className,
      )}
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="grid min-w-0 gap-0.5 [overflow-wrap:anywhere]">
        {title && <strong className="font-semibold">{title}</strong>}
        <span>{children}</span>
      </div>
    </motion.div>
  );
}

export function Notice({
  tone = "warning",
  className,
  children,
}: {
  tone?: "warning" | "success";
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "grid gap-1 rounded-2xl border px-4 py-3 text-[13px] leading-relaxed text-foreground",
        tone === "warning" && "border-amber-500/30 bg-amber-500/10",
        tone === "success" && "border-emerald-500/30 bg-emerald-500/10",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function Loading({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-center justify-center gap-3 text-sm text-muted-foreground",
        className,
      )}
      role="status"
    >
      <Loader variant="dots" size={18} />
      <TextShimmer>{children}</TextShimmer>
    </div>
  );
}

type ToastApi = { notify: (toast: ToastInput) => void };
const ToastContext = createContext<ToastApi>({ notify: () => undefined });

export function ToastProvider({ children }: { children: ReactNode }) {
  const { toasts, showToast, dismissToast } = useAnimatedToastStack({
    defaultDuration: 3600,
    limit: 4,
  });
  const notify = useCallback(
    (toast: ToastInput) => {
      showToast(toast);
    },
    [showToast],
  );
  const api = useMemo(() => ({ notify }), [notify]);
  return (
    <ToastContext.Provider value={api}>
      {children}
      <AnimatedToastStack
        toasts={toasts}
        onDismiss={dismissToast}
        position="bottom-right"
        placement="fixed"
        portal
      />
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

/* Forms ------------------------------------------------------------------- */

export function FieldHelp({
  tone = "muted",
  className,
  children,
}: {
  tone?: "muted" | "error";
  className?: string;
  children: ReactNode;
}) {
  return (
    <p
      className={cn(
        "px-1 text-xs leading-relaxed",
        tone === "error" ? "text-destructive" : "text-muted-foreground",
        className,
      )}
    >
      {children}
    </p>
  );
}

/** A beUI checkbox whose title and description are part of its label. */
export function CheckRow({
  checked,
  onCheckedChange,
  title,
  description,
  disabled,
  className,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("flex items-start gap-3", className)}>
      <Checkbox
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
      />
      <label
        htmlFor={id}
        className={cn(
          "grid min-w-0 gap-0.5 text-sm",
          disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
        )}
      >
        <span className="font-medium leading-5 text-foreground">{title}</span>
        {description && (
          <small className="text-xs leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
            {description}
          </small>
        )}
      </label>
    </div>
  );
}

/** A beUI switch beside a title and description. */
export function SwitchRow({
  checked,
  onCheckedChange,
  title,
  description,
  disabled,
  className,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  title: string;
  description?: ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center justify-between gap-4", className)}>
      <div className="grid min-w-0 gap-0.5 text-sm">
        <span className="font-medium text-foreground">{title}</span>
        {description && (
          <small className="text-xs leading-relaxed text-muted-foreground">
            {description}
          </small>
        )}
      </div>
      <Switch
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        ariaLabel={title}
      />
    </div>
  );
}

/** A labelled beUI select. Long option lists scroll inside the unfolded panel. */
export function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
  className,
}: {
  label: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (value: T) => void;
  className?: string;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  function trigger() {
    return rootRef.current?.querySelector<HTMLButtonElement>(
      'button[aria-haspopup="listbox"]',
    );
  }
  return (
    <div
      ref={rootRef}
      role="group"
      aria-label={label}
      className={cn("grid gap-1.5", className)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDownCapture={(event) => {
        const items = Array.from(
          rootRef.current?.querySelectorAll<HTMLButtonElement>(
            '[role="option"]:not(:disabled)',
          ) ?? [],
        );
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          setOpen(true);
          const current = items.indexOf(event.target as HTMLButtonElement);
          const selected = items.findIndex(
            (item) => item.getAttribute("aria-selected") === "true",
          );
          const index =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? items.length - 1
                : current === -1
                  ? Math.max(0, selected)
                  : (current +
                      (event.key === "ArrowDown" ? 1 : -1) +
                      items.length) %
                    items.length;
          requestAnimationFrame(() => items[index]?.focus());
        } else if (open && event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          trigger()?.focus();
        } else if (open && event.key === "Tab") {
          // Resume the page's tab order from the trigger instead of walking
          // every option in a closed list.
          trigger()?.focus();
          setOpen(false);
        }
      }}
    >
      <span className="px-1 text-sm font-medium">{label}</span>
      <Select
        value={value}
        open={open}
        onOpenChange={setOpen}
        className={open ? "z-30" : undefined}
        onValueChange={(next) => {
          onChange(next as T);
          requestAnimationFrame(() => trigger()?.focus());
        }}
      >
        <SelectTrigger>
          <span className="sr-only">{label}:</span>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <div className="max-h-72 overflow-y-auto overscroll-contain">
            {options.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </div>
        </SelectContent>
      </Select>
    </div>
  );
}

export async function writeClipboard(value: string) {
  if (navigator.clipboard) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  const succeeded = document.execCommand("copy");
  input.remove();
  if (!succeeded) throw new Error("clipboard API unavailable");
}

/** Copies a value and confirms it with beUI's stateful button transition. */
export function CopyButton({
  value,
  children = "Copy",
  successText = "Copied",
  onCopyError,
  ...rest
}: Omit<ButtonProps, "children" | "onClick" | "value"> & {
  value: string;
  children?: ReactNode;
  successText?: ReactNode;
  icon?: ReactNode;
  onCopyError?: (message: string) => void;
}) {
  const [state, setState] = useState<ButtonState>("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    window.clearTimeout(timer.current);
    try {
      await writeClipboard(value);
      setState("success");
    } catch (cause) {
      setState("error");
      onCopyError?.(cause instanceof Error ? cause.message : String(cause));
    }
    timer.current = window.setTimeout(() => setState("idle"), 1800);
  }
  return (
    <StatefulButton
      state={state}
      successText={successText}
      errorText="Copy failed"
      onClick={() => void copy()}
      {...rest}
    >
      {children}
    </StatefulButton>
  );
}

/* Data display ------------------------------------------------------------ */

const statusTone: Record<string, AnimatedBadgeStatus> = {
  completed: "success",
  failed: "danger",
  cancelled: "neutral",
  paused: "warning",
  awaiting_confirmation: "warning",
  scheduled: "info",
  queued: "neutral",
  resolving: "loading",
  downloading: "loading",
  verifying: "loading",
  publishing: "loading",
};

export function statusText(status: string): string {
  const text = status.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function StatusBadge({
  status,
  label,
  size = "sm",
}: {
  status: string;
  label?: string;
  size?: "sm" | "md";
}) {
  const text = label ?? statusText(status);
  return (
    <AnimatedBadge
      status={statusTone[status] ?? "neutral"}
      size={size}
      contentKey={text}
    >
      {text}
    </AnimatedBadge>
  );
}

export function ProgressBar({
  percent,
  indeterminate = false,
  label,
  size = "sm",
  className,
}: {
  percent?: number;
  indeterminate?: boolean;
  label: string;
  size?: "sm" | "lg";
  className?: string;
}) {
  const reduce = useReducedMotion();
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : percent}
      className={cn(
        "relative w-full overflow-hidden rounded-full bg-foreground/[0.07]",
        size === "lg" ? "h-2.5" : "h-1.5",
        className,
      )}
    >
      {indeterminate ? (
        <span
          className={cn(
            "absolute inset-y-0 left-0 w-[32%] rounded-full bg-primary",
            !reduce && "animate-indeterminate",
          )}
        />
      ) : (
        <motion.span
          className="absolute inset-0 origin-left rounded-full bg-primary"
          initial={false}
          animate={{ scaleX: Math.min(100, Math.max(0, percent ?? 0)) / 100 }}
          transition={
            reduce
              ? { duration: 0 }
              : { type: "spring", stiffness: 120, damping: 24 }
          }
        />
      )}
    </div>
  );
}

export const labelText = "text-xs text-muted-foreground";

export function MetaTile({
  label,
  children,
  mono = false,
  wide = false,
  className,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
  wide?: boolean;
  className?: string;
}) {
  return (
    <Inset
      className={cn(
        "grid min-w-0 content-start gap-1 px-4 py-3",
        wide && "sm:col-span-2",
        className,
      )}
    >
      <span className={labelText}>{label}</span>
      <strong
        className={cn(
          "min-w-0 text-sm font-medium leading-relaxed [overflow-wrap:anywhere]",
          mono && "break-all font-mono text-xs",
        )}
      >
        {children}
      </strong>
    </Inset>
  );
}

export function Stat({
  label,
  value,
  detail,
  className,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  className?: string;
}) {
  return (
    <Inset className={cn("grid min-w-0 gap-1 p-3.5", className)}>
      <span className={labelText}>{label}</span>
      <strong className="text-lg font-semibold tabular-nums tracking-tight">
        {value}
      </strong>
      {detail && (
        <small className="text-xs leading-relaxed text-muted-foreground">
          {detail}
        </small>
      )}
    </Inset>
  );
}
