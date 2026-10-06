import { LogIn, LogOut } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  Link,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { Button } from "@/components/motion/button/base";
import { SharedLayoutBg } from "@/components/motion/shared-layout-bg";
import { BrandMark, ErrorBox, Loading, ToastProvider } from "@/components/ui";
import { SPRING_LAYOUT } from "@/lib/ease";
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
        setBootstrapError(
          cause instanceof Error ? cause.message : String(cause),
        );
      }
    }
    void initialize();
  }, []);
  if (bootstrapError) {
    return (
      <div className="grid min-h-[70vh] place-items-center px-6">
        <div className="grid w-full max-w-md justify-items-start gap-4">
          <ErrorBox title="Could not open ModelShelf">
            {bootstrapError}
          </ErrorBox>
          <Button onClick={() => window.location.reload()}>Retry</Button>
        </div>
      </div>
    );
  }
  if (authenticated === null || publicArtifacts === null)
    return <Loading className="min-h-[70vh]">Opening the shelf…</Loading>;
  const defaultPath = authenticated
    ? "/tasks"
    : publicArtifacts
      ? "/artifacts"
      : "/login";
  return (
    <ToastProvider>
      <Routes>
        <Route
          path="/login"
          element={<LoginPage onLogin={() => setAuthenticated(true)} />}
        />
        <Route
          element={
            <Shell
              authenticated={authenticated}
              publicArtifacts={publicArtifacts}
              onLogout={() => setAuthenticated(false)}
            />
          }
        >
          <Route index element={<Navigate to={defaultPath} replace />} />
          <Route
            path="/integration"
            element={
              <Suspense
                fallback={
                  <Loading className="min-h-[50vh]">
                    Loading integration guide…
                  </Loading>
                }
              >
                <IntegrationPage />
              </Suspense>
            }
          />
          <Route
            path="/artifacts"
            element={
              <ArtifactAccess
                authenticated={authenticated}
                publicArtifacts={publicArtifacts}
              />
            }
          />
          <Route
            path="/artifacts/:artifactId"
            element={
              <ArtifactAccess
                authenticated={authenticated}
                publicArtifacts={publicArtifacts}
              />
            }
          />
          <Route element={<Protected authenticated={authenticated} />}>
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/tasks/new" element={<NewTaskPage />} />
            <Route path="/tasks/:id" element={<TasksPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to={defaultPath} replace />} />
      </Routes>
    </ToastProvider>
  );
}
function Protected({ authenticated }: { authenticated: boolean }) {
  const location = useLocation();
  return authenticated ? (
    <Outlet />
  ) : (
    <Navigate to="/login" state={{ from: location }} replace />
  );
}

function ArtifactAccess({
  authenticated,
  publicArtifacts,
}: {
  authenticated: boolean;
  publicArtifacts: boolean;
}) {
  const location = useLocation();
  return authenticated || publicArtifacts ? (
    <ArtifactsPage canManage={authenticated} />
  ) : (
    <Navigate to="/login" state={{ from: location }} replace />
  );
}

function Shell({
  authenticated,
  publicArtifacts,
  onLogout,
}: {
  authenticated: boolean;
  publicArtifacts: boolean;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  // Detail URLs keep the list underneath them at its current scroll position.
  const screen = location.pathname.startsWith("/artifacts")
    ? "/artifacts"
    : location.pathname === "/tasks/new"
      ? "/tasks/new"
      : location.pathname.startsWith("/tasks")
        ? "/tasks"
        : location.pathname;
  useEffect(() => {
    if (!window.location.hash) window.scrollTo({ top: 0, behavior: "instant" });
  }, [screen]);
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
  const links = [
    ...(authenticated ? [{ to: "/tasks", label: "Downloads" }] : []),
    { to: "/artifacts", label: "Artifacts" },
    { to: "/integration", label: "Integration" },
  ];
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 w-[min(1180px,calc(100%-32px))] items-center gap-3 sm:w-[min(1180px,calc(100%-48px))] sm:gap-8">
          <Link
            aria-label="ModelShelf home"
            className="flex items-center gap-2.5 font-semibold tracking-tight"
            to={
              authenticated
                ? "/tasks"
                : publicArtifacts
                  ? "/artifacts"
                  : "/integration"
            }
          >
            <BrandMark />
            <span className="hidden sm:inline">ModelShelf</span>
          </Link>
          <MainNav links={links} />
          {authenticated ? (
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0 px-2 sm:px-3"
              aria-label="Sign out"
              onClick={() => void logout()}
            >
              <LogOut className="size-3.5" aria-hidden />
              <span className="hidden sm:inline">Sign out</span>
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0 px-2 sm:px-3"
              aria-label="Sign in"
              onClick={() => navigate("/login")}
            >
              <LogIn className="size-3.5" aria-hidden />
              <span className="hidden sm:inline">Sign in</span>
            </Button>
          )}
        </div>
      </header>
      <main className="mx-auto w-[min(1180px,calc(100%-32px))] sm:w-[min(1180px,calc(100%-48px))]">
        {error && (
          <div className="pt-6">
            <ErrorBox>Sign out failed: {error}</ErrorBox>
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}

function MainNav({ links }: { links: Array<{ to: string; label: string }> }) {
  const location = useLocation();
  const reduce = useReducedMotion();
  return (
    <nav
      className="min-w-0 flex-1 overflow-x-auto py-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      aria-label="Primary"
    >
      <SharedLayoutBg className="flex-row gap-0.5 sm:gap-1" inset={0}>
        {links.map((link) => {
          const active =
            location.pathname === link.to ||
            location.pathname.startsWith(`${link.to}/`);
          return (
            <NavLink
              key={link.to}
              to={link.to}
              className="block shrink-0 rounded-full text-sm font-medium text-muted-foreground transition-colors hover:text-foreground aria-[current=page]:text-foreground"
            >
              <span className="block px-2 py-1.5 sm:px-3.5">{link.label}</span>
              {active && (
                <motion.span
                  layoutId="main-nav-active"
                  transition={reduce ? { duration: 0 } : SPRING_LAYOUT}
                  className="absolute inset-x-3 -bottom-4 h-0.5 rounded-full bg-foreground"
                />
              )}
            </NavLink>
          );
        })}
      </SharedLayoutBg>
    </nav>
  );
}
