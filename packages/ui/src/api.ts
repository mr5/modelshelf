export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...init?.headers },
      ...init,
    });
  } catch (cause) {
    if (cause instanceof DOMException && ["AbortError", "TimeoutError"].includes(cause.name)) {
      throw cause;
    }
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new Error(`${method} ${path} could not reach ModelShelf: ${detail}`, { cause });
  }
  if (!response.ok) {
    const raw = await response.text();
    let detail = "";
    if (raw) {
      try {
        const body = JSON.parse(raw) as { detail?: unknown; error?: unknown };
        const candidate = body.detail ?? body.error;
        detail = typeof candidate === "string" ? candidate : JSON.stringify(candidate);
      } catch {
        detail = raw.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
      }
    }
    const reason = detail || response.statusText || "request failed without a response body";
    throw new ApiError(`${method} ${path} returned HTTP ${response.status}: ${reason.slice(0, 4_000)}`, response.status);
  }
  if (response.status === 204) return undefined as T;
  try {
    return await response.json() as T;
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new Error(`${method} ${path} returned invalid JSON: ${detail}`, { cause });
  }
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
