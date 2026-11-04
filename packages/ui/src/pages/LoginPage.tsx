import { Eye, EyeOff, LockKeyhole } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { StatefulButton } from "@/components/motion/button/stateful";
import { Input } from "@/components/motion/input";
import { ThemePicker } from "@/components/ThemePicker";
import { BrandMark, ErrorBox, Eyebrow, Panel } from "@/components/ui";
import { api } from "../api.ts";

export function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);
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
      passwordRef.current?.focus();
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="relative grid min-h-dvh place-items-center px-4 pb-12 pt-24">
      <div className="absolute right-4 top-4 sm:right-6 sm:top-6">
        <ThemePicker />
      </div>
      <Panel className="w-full max-w-[440px] p-7 sm:p-10">
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
            ref={passwordRef}
            label="Password"
            autoFocus
            type={showPassword ? "text" : "password"}
            value={password}
            autoComplete="current-password"
            onChange={setPassword}
            leftIcon={<LockKeyhole />}
            aria-invalid={Boolean(error)}
            aria-describedby={error ? "login-error" : "login-help"}
            rightIcon={
              <button
                type="button"
                aria-label={showPassword ? "Hide password" : "Show password"}
                aria-pressed={showPassword}
                onClick={() => setShowPassword((value) => !value)}
              >
                {showPassword ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
              </button>
            }
          />
          <p
            id="login-help"
            className="text-xs leading-relaxed text-muted-foreground"
          >
            Your session gives you access to downloads and shelf management.
          </p>
          {error && (
            <div id="login-error">
              <ErrorBox>{error}</ErrorBox>
            </div>
          )}
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
        <div className="mt-7 border-t border-border pt-5 text-center">
          <Link
            className="inline-flex min-h-11 items-center text-sm font-medium text-primary hover:underline"
            to="/integration"
          >
            Read the integration guide
          </Link>
        </div>
      </Panel>
    </div>
  );
}
