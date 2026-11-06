"use client";

import { useState, type FormEvent } from "react";
import { Button, Card, ErrorBox, Field, inputClass } from "@/components/ui";
import { supabase } from "@/lib/supabase";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  async function signIn(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { error: err } = await supabase().auth.signInWithPassword({ email: email.trim(), password });
    if (err) setError("Email or password is wrong, or this account is not active.");
    setBusy(false);
  }

  async function magicLink() {
    if (!email.trim()) {
      setError("Enter your email first.");
      return;
    }
    setBusy(true);
    setError(null);
    const { error: err } = await supabase().auth.signInWithOtp({
      email: email.trim(),
      options: { shouldCreateUser: false, emailRedirectTo: window.location.origin },
    });
    if (err) setError("Could not send a sign-in link. Check the address, or ask the administrator to invite you.");
    else setSent(true);
    setBusy(false);
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4">
      <div className="mb-6 text-center">
        <div className="font-mono text-2xl font-bold tracking-[0.35em] text-accent">G-AEGISBOT</div>
        <div className="mt-1 font-mono text-[11px] uppercase tracking-[0.2em] text-mute">private trading terminal</div>
      </div>
      <Card title="Sign in">
        <form onSubmit={signIn} className="space-y-4">
          <Field label="Email">
            <input className={inputClass} type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Password">
            <input className={inputClass} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          {error && <ErrorBox message={error} />}
          {sent && <p className="text-sm text-up">Check your email for a sign-in link.</p>}
          <div className="flex gap-2">
            <Button type="submit" variant="primary" disabled={busy || !password} className="flex-1">
              Sign in
            </Button>
            <Button type="button" onClick={magicLink} disabled={busy}>
              Email link
            </Button>
          </div>
        </form>
      </Card>
      <p className="mt-4 text-center text-xs text-mute">Access is by invitation only.</p>
    </main>
  );
}
