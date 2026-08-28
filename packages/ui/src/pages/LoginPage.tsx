import { type FormEvent, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
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
      await api("/auth/login", { method: "POST", body: JSON.stringify({ password }) });
      onLogin();
      const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
      navigate(from ?? "/tasks", { replace: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <section className="login-card">
        <div className="brand-mark large">M</div>
        <p className="eyebrow">Private model storage</p>
        <h1>Open ModelShelf</h1>
        <p className="muted">Use the administrator password configured on the server.</p>
        <form onSubmit={(event) => void submit(event)}>
          <label>Password<input autoFocus type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          {error && <div className="error-box">{error}</div>}
          <button disabled={busy || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </section>
    </div>
  );
}
