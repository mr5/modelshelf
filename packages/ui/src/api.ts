export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string; error?: string };
    throw new ApiError(body.detail ?? body.error ?? `HTTP ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}

export function formatBytes(value: number): string {
  let size = value;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = units[0]!;
  for (const candidate of units) {
    unit = candidate;
    if (size < 1024 || candidate === "TiB") break;
    size /= 1024;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${unit}`;
}

export function formatDownloadSize(value: number): string {
  const decimalGigabytes = value / 1_000_000_000;
  const digits = decimalGigabytes >= 100 ? 1 : decimalGigabytes >= 10 ? 2 : 3;
  return `${formatBytes(value)} · ${decimalGigabytes.toFixed(digits)} GB`;
}

export function formatRate(value?: number): string {
  return value && value > 0 ? `${formatBytes(value)}/s` : "—";
}

export function formatDuration(seconds?: number): string {
  if (seconds === undefined) return "Calculating…";
  if (seconds <= 0) return "Done";
  const rounded = Math.ceil(seconds);
  const days = Math.floor(rounded / 86_400);
  const hours = Math.floor((rounded % 86_400) / 3_600);
  const minutes = Math.floor((rounded % 3_600) / 60);
  const remainingSeconds = rounded % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}
