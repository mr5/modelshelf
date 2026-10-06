import { type FormEvent, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { StatefulButton } from "@/components/motion/button/stateful";
import { Input } from "@/components/motion/input";
import { ThemePicker } from "@/components/ThemePicker";
import { BrandMark, ErrorBox, Eyebrow, Panel } from "@/components/ui";
import { api } from "../api.ts";

export function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      onLogin();
      const from = (location.state as { from?: { pathname?: string } } | null)
        ?.from?.pathname;
      navigate(from ?? "/tasks", { replace: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="relative grid min-h-screen place-items-center p-4">
      <div className="absolute right-4 top-4 sm:right-6 sm:top-6">
        <ThemePicker />
      </div>
      <Panel className="w-full max-w-[420px] p-7 sm:p-9">
        <div className="mb-7">
          <BrandMark large />
        </div>
        <Eyebrow>Private model storage</Eyebrow>
        <h1 className="mb-2 text-3xl font-semibold tracking-tight">
          Open ModelShelf
        </h1>
        <p className="leading-relaxed text-muted-foreground">
          Use the administrator password configured on the server.
        </p>
        <form
          className="mt-7 grid gap-4"
          onSubmit={(event) => void submit(event)}
        >
          <Input
            label="Password"
            autoFocus
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={setPassword}
          />
          {error && <ErrorBox>{error}</ErrorBox>}
          <StatefulButton
            type="submit"
            size="lg"
            state={busy ? "loading" : "idle"}
            loadingText="Signing in…"
            disabled={!password}
          >
            Sign in
          </StatefulButton>
        </form>
      </Panel>
    </div>
  );
}
