import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: Number(process.env.FRONTEND_PORT || 5173),
    proxy: {
      // Forward API + inference calls to the backend during dev.
      "/api": `http://127.0.0.1:${process.env.BACKEND_PORT || 8889}`,
      "/v1": `http://127.0.0.1:${process.env.BACKEND_PORT || 8889}`,
      "/v1beta": `http://127.0.0.1:${process.env.BACKEND_PORT || 8889}`,
    },
  },
});
