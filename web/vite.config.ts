import { defineConfig } from "vitest/config";
import preact from "@preact/preset-vite";

const API = "http://127.0.0.1:7317";

export default defineConfig({
  plugins: [preact()],
  // `pnpm dev`: the API checks Host, and Origin on commands, so both must be its own.
  server: { proxy: { "/api": { target: API, changeOrigin: true, headers: { origin: API } } } },
  build: { outDir: "dist" },
  test: { environment: "jsdom", globals: true, include: ["src/**/*.test.{ts,tsx}"] },
});
