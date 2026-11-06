import { supabase } from "./supabase";

const BASE = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** Turn the API's error body into one readable sentence. */
export function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d)))
      .join("; ");
  }
  if (detail && typeof detail === "object") {
    const d = detail as { message?: string; problems?: string[] };
    return [d.message, ...(d.problems ?? [])].filter(Boolean).join(" ");
  }
  return "Something went wrong.";
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const { data } = await supabase().auth.getSession();
  const token = data.session?.access_token;
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: init.method ?? "GET",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail;
    } catch {
      // the error body was not JSON
    }
    throw new ApiError(res.status, describeDetail(detail));
  }
  return (await res.json()) as T;
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong.";
}
