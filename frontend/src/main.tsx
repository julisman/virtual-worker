import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthProvider } from "@/lib/auth";
import { ThemeProvider, applyTheme, initialTheme } from "@/lib/theme";
import { ToastProvider } from "@/lib/toast";
import "./index.css";

// Apply the theme before first paint to avoid a light-mode flash on reload.
applyTheme(initialTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  </StrictMode>,
);
