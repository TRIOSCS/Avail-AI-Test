import { defineConfig } from "vite";
import { resolve } from "path";

export default defineConfig({
  root: "app/static",
  base: "/static/",

  publicDir: resolve(__dirname, "app/static/public"),

  build: {
    outDir: resolve(__dirname, "app/static/dist"),
    emptyOutDir: true,
    manifest: true,
    sourcemap: false,
    minify: "terser",
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      input: {
        htmx_app: resolve(__dirname, "app/static/htmx_app.js"),
        // htmx_mobile.css is imported by htmx_app.js (so it ships in that bundle);
        // a standalone entry emitted an htmx_mobile-*.css that nothing loaded.
        styles: resolve(__dirname, "app/static/styles.css"),
      },
    },
  },

  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/auth": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
