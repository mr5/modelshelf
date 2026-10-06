import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App.tsx";
import { ThemeProvider } from "./components/ThemePicker.tsx";
import { applyTheme, readThemePreference } from "./theme.ts";
import "./styles.css";

applyTheme(readThemePreference());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>,
);
