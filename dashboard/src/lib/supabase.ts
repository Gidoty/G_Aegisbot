import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

export const configured = Boolean(url && key);

let client: SupabaseClient | null = null;

/** Browser-only. Uses the public (anon / publishable) key, which is safe to expose. */
export function supabase(): SupabaseClient {
  if (!url || !key) throw new Error("Supabase is not configured");
  if (!client) client = createClient(url, key, { auth: { persistSession: true, autoRefreshToken: true } });
  return client;
}
