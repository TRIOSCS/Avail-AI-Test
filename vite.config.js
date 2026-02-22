import { defineConfig } from "vite";
import { resolve } from "path";

export default defineConfig({
  root: "app/static",
  base: "/static/",

  resolve: {
    alias: {
      app: resolve(__dirname, "app/static/app.js"),
    },
  },

  publicDir: resolve(__dirname, "app/static/public"),

  build: {
    outDir: resolve(__dirname, "app/static/dist"),
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        app: resolve(__dirname, "app/static/app.js"),
        crm: resolve(__dirname, "app/static/crm.js"),
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
