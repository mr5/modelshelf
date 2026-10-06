import { Monitor, Moon, Sun } from "lucide-react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { SelectField } from "@/components/ui";
import {
  applyTheme,
  readThemePreference,
  themeStorageKey,
  type ThemePreference,
} from "../theme";

const options = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
] as const;

const ThemeContext = createContext<{
  preference: ThemePreference;
  setPreference: (preference: ThemePreference) => void;
} | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreference] = useState(readThemePreference);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => applyTheme(preference);
    const sync = (event: StorageEvent) => {
      if (event.key === themeStorageKey || event.key === null) {
        const next = readThemePreference();
        applyTheme(next);
        setPreference(next);
      }
    };
    update();
    media.addEventListener("change", update);
    window.addEventListener("storage", sync);
    return () => {
      media.removeEventListener("change", update);
      window.removeEventListener("storage", sync);
    };
  }, [preference]);
  const select = useCallback((next: ThemePreference) => {
    applyTheme(next);
    setPreference(next);
    try {
      localStorage.setItem(themeStorageKey, next);
    } catch {
      // Retain the user's selection for this session without browser storage.
    }
  }, []);
  return (
    <ThemeContext.Provider value={{ preference, setPreference: select }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function ThemePicker() {
  const theme = useContext(ThemeContext);
  if (!theme) throw new Error("ThemePicker requires ThemeProvider");
  const Icon =
    theme.preference === "system"
      ? Monitor
      : theme.preference === "dark"
        ? Moon
        : Sun;
  return (
    <SelectField
      label="Theme"
      value={theme.preference}
      options={options}
      onChange={theme.setPreference}
      className="w-8 shrink-0 sm:w-32 [&>span]:sr-only"
      triggerClassName="h-8 justify-center px-2 sm:justify-between sm:px-3 [&>span:last-child]:hidden sm:[&>span:last-child]:inline-flex"
      contentClassName="left-auto min-w-40"
      triggerContent={
        <>
          <Icon className="size-4 shrink-0" aria-hidden />
          <span className="sr-only sm:not-sr-only">
            {options.find((option) => option.value === theme.preference)?.label}
          </span>
        </>
      }
    />
  );
}
