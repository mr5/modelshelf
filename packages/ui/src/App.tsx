import { useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api.ts";
import { ArtifactsPage } from "./pages/ArtifactsPage.tsx";
import { IntegrationPage } from "./pages/IntegrationPage.tsx";
import { LoginPage } from "./pages/LoginPage.tsx";
import { NewTaskPage } from "./pages/NewTaskPage.tsx";
import { TasksPage } from "./pages/TasksPage.tsx";
import type { ServerInfo } from "./types.ts";

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [publicArtifacts, setPublicArtifacts] = useState<boolean | null>(null);
  useEffect(() => {
    void Promise.all([
      api<{ authenticated: boolean }>("/auth/session").catch(() => ({ authenticated: false })),
      api<ServerInfo>("/info").catch(() => null),
    ]).then(([session, info]) => {
      setAuthenticated(session.authenticated);
      setPublicArtifacts(info?.publicArtifacts ?? false);
    });
  }, []);
  if (authenticated === null || publicArtifacts === null) return <div className="boot">Opening the shelf…</div>;
  const defaultPath = authenticated ? "/tasks" : publicArtifacts ? "/artifacts" : "/login";
  return (
    <Routes>
      <Route path="/login" element={<LoginPage onLogin={() => setAuthenticated(true)} />} />
      <Route element={<Shell authenticated={authenticated} publicArtifacts={publicArtifacts} onLogout={() => setAuthenticated(false)} />}>
        <Route index element={<Navigate to={defaultPath} replace />} />
        <Route path="/integration" element={<IntegrationPage />} />
        <Route path="/artifacts" element={<ArtifactAccess authenticated={authenticated} publicArtifacts={publicArtifacts} />} />
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
    ? <ArtifactsPage />
    : <Navigate to="/login" state={{ from: location }} replace />;
}

function Shell({ authenticated, publicArtifacts, onLogout }: { authenticated: boolean; publicArtifacts: boolean; onLogout: () => void }) {
  const navigate = useNavigate();
  async function logout() {
    await api("/auth/logout", { method: "POST" });
    onLogout();
    navigate(publicArtifacts ? "/artifacts" : "/login");
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
      <main><Outlet /></main>
    </div>
  );
}
