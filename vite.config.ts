import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/** iOS Safari / PWA «на экран Домой»: иногда ломает загрузку ES-модулей при crossorigin на том же origin. */
function htmlStripCrossorigin(): Plugin {
  return {
    name: "html-strip-crossorigin",
    transformIndexHtml(html) {
      return html.replace(/\s+crossorigin(?:=["'][^"']*["'])?/gi, "");
    },
  };
}

export default defineConfig({
  plugins: [react(), htmlStripCrossorigin()],
  server: {
    port: 5173,
    host: "0.0.0.0",
    allowedHosts: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
