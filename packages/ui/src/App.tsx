import { BookOpen, Download, Layers, LogIn, LogOut } from "lucide-react";
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
import { ThemePicker } from "@/components/ThemePicker";
import { SharedLayoutBg } from "@/components/motion/shared-layout-bg";
import { BrandMark, ErrorBox, Loading, ToastProvider } from "@/components/ui";
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
    if (!window.location.hash) {
      window.scrollTo({ top: 0, behavior: "instant" });
      document.getElementById("main-content")?.focus({ preventScroll: true });
    }
  }, [screen]);
  const [error, setError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true);
    setError("");
    try {
      await api("/auth/logout", { method: "POST" });
      onLogout();
      navigate(publicArtifacts ? "/artifacts" : "/login");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoggingOut(false);
    }
  }
  const links = [
    ...(authenticated
      ? [{ to: "/tasks", label: "Downloads", icon: Download }]
      : []),
    { to: "/artifacts", label: "Artifacts", icon: Layers },
    { to: "/integration", label: "Integration", icon: BookOpen },
  ];
  return (
    <div className="min-h-screen">
      <a
        href="#main-content"
        className="fixed left-4 top-3 z-50 -translate-y-24 rounded-xl bg-primary px-4 py-3 text-sm font-medium text-primary-foreground focus:translate-y-0"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur-xl">
        <div className="mx-auto flex min-h-16 w-[min(1180px,calc(100%-32px))] flex-wrap items-center gap-x-2 sm:w-[min(1180px,calc(100%-48px))] sm:flex-nowrap sm:gap-6">
          <Link
            aria-label="ModelShelf home"
            className="mr-auto flex min-h-16 items-center gap-2.5 font-semibold tracking-tight sm:mr-0"
            to={
              authenticated
                ? "/tasks"
                : publicArtifacts
                  ? "/artifacts"
                  : "/integration"
            }
          >
            <BrandMark />
            <span>ModelShelf</span>
          </Link>
          <MainNav links={links} />
          <ThemePicker />
          {authenticated ? (
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0 px-2 sm:px-3"
              aria-label="Sign out"
              disabled={loggingOut}
              aria-busy={loggingOut}
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
      <main
        id="main-content"
        tabIndex={-1}
        className="mx-auto w-[min(1180px,calc(100%-32px))] sm:w-[min(1180px,calc(100%-48px))]"
      >
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

function MainNav({
  links,
}: {
  links: Array<{ to: string; label: string; icon: typeof Download }>;
}) {
  return (
    <nav
      className="order-last w-full min-w-0 border-t border-border py-2 sm:order-none sm:w-auto sm:flex-1 sm:border-0 sm:py-0"
      aria-label="Primary"
    >
      <SharedLayoutBg className="flex-row gap-1" inset={0}>
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            className="block flex-1 rounded-lg text-sm font-medium text-muted-foreground transition-colors hover:text-foreground aria-[current=page]:bg-primary/10 aria-[current=page]:text-primary sm:flex-none"
          >
            <span className="flex min-h-11 items-center justify-center gap-2 px-2 sm:px-3.5">
              <link.icon
                className="hidden size-4 min-[375px]:block"
                aria-hidden
              />
              {link.label}
            </span>
          </NavLink>
        ))}
      </SharedLayoutBg>
    </nav>
  );
}
