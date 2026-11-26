/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the proxy keeps the browser same-origin (no CORS needed). The API address defaults to
// http://127.0.0.1:8000; set REPOMIND_API (e.g. http://127.0.0.1:8001) when the backend runs on another port.
const API = process.env.REPOMIND_API ?? "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  server: { port: Number(process.env.REPOMIND_UI_PORT ?? 5173), proxy: { "/api": API } },
  build: { outDir: "dist", sourcemap: false },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/setupTests.ts"] },
});
