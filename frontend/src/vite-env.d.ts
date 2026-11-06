/// <reference types="vite/client" />

// Injected by the desktop shell's preload (desktop/preload.js). Absent when the
// app runs in a plain browser (dev via ./dev.sh).
interface Window {
  vesDesktop?: { version: string | null; platform: string };
}
