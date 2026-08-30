import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api.ts";
import { ArtifactsPage } from "./pages/ArtifactsPage.tsx";
import { LoginPage } from "./pages/LoginPage.tsx";
import { NewTaskPage } from "./pages/NewTaskPage.tsx";
import { TasksPage } from "./pages/TasksPage.tsx";
import type { ServerInfo } from "./types.ts";

const IntegrationPage = lazy(async () => {
  const module = await import("./pages/IntegrationPage.tsx");
  return { default: module.IntegrationPage };
});

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [publicArtifacts, setPublicArtifacts] = useState<boolean | null>(null);
  const [bootstrapError, setBootstrapError] = useState("");
  useEffect(() => {
    async function initialize() {
      try {
        const [session, info] = await Promise.all([
          api<{ authenticated: boolean }>("/auth/session"),
          api<ServerInfo>("/info"),
        ]);
        setAuthenticated(session.authenticated);
        setPublicArtifacts(info.publicArtifacts);
      } catch (cause) {
        setBootstrapError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void initialize();
  }, []);
  if (bootstrapError) {
    return <div className="boot">
      <div className="error-box"><strong>Could not open ModelShelf</strong><span>{bootstrapError}</span></div>
      <button onClick={() => window.location.reload()}>Retry</button>
    </div>;
  }
  if (authenticated === null || publicArtifacts === null) return <div className="boot">Opening the shelf…</div>;
  const defaultPath = authenticated ? "/tasks" : publicArtifacts ? "/artifacts" : "/login";
  return (
    <Routes>
      <Route path="/login" element={<LoginPage onLogin={() => setAuthenticated(true)} />} />
      <Route element={<Shell authenticated={authenticated} publicArtifacts={publicArtifacts} onLogout={() => setAuthenticated(false)} />}>
        <Route index element={<Navigate to={defaultPath} replace />} />
        <Route path="/integration" element={<Suspense fallback={<div className="boot">Loading integration guide…</div>}><IntegrationPage /></Suspense>} />
        <Route path="/artifacts" element={<ArtifactAccess authenticated={authenticated} publicArtifacts={publicArtifacts} />} />
        <Route path="/artifacts/:artifactId" element={<ArtifactAccess authenticated={authenticated} publicArtifacts={publicArtifacts} />} />
        <Route element={<Protected authenticated={authenticated} />}>
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/tasks/new" element={<NewTaskPage />} />
          <Route path="/tasks/:id" element={<TasksPage />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to={defaultPath} replace />} />
    </Routes>
  );
}
function Protected({ authenticated }: { authenticated: boolean }) {
  const location = useLocation();
  return authenticated ? <Outlet /> : <Navigate to="/login" state={{ from: location }} replace />;
}

function ArtifactAccess({ authenticated, publicArtifacts }: { authenticated: boolean; publicArtifacts: boolean }) {
  const location = useLocation();
  return authenticated || publicArtifacts
    ? <ArtifactsPage canManage={authenticated} />
    : <Navigate to="/login" state={{ from: location }} replace />;
}

function Shell({ authenticated, publicArtifacts, onLogout }: { authenticated: boolean; publicArtifacts: boolean; onLogout: () => void }) {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  async function logout() {
    setError("");
    try {
      await api("/auth/logout", { method: "POST" });
      onLogout();
      navigate(publicArtifacts ? "/artifacts" : "/login");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }
  return (
    <div className="shell">
      <header className="topbar">
        <NavLink className="brand" to={authenticated ? "/tasks" : publicArtifacts ? "/artifacts" : "/integration"}>
          <span className="brand-mark">M</span>
          <span>ModelShelf</span>
        </NavLink>
        <nav>
          {authenticated && <NavLink to="/tasks">Downloads</NavLink>}
          <NavLink to="/artifacts">Artifacts</NavLink>
          <NavLink to="/integration">Integration</NavLink>
        </nav>
        {authenticated
          ? <button className="ghost small" onClick={() => void logout()}>Sign out</button>
          : <NavLink className="ghost small button" to="/login">Sign in</NavLink>}
      </header>
      <main>
        {error && <div className="page"><div className="error-box">Sign out failed: {error}</div></div>}
        <Outlet />
      </main>
    </div>
  );
}
