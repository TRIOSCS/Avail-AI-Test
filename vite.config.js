import { defineConfig } from "vite";
import { resolve } from "path";
import { readFileSync } from "fs";

/**
 * Vite plugin: verify every name in Object.assign(window, {...}) is defined.
 * Prevents the recurring crash where removed functions leave dangling refs
 * that cause ReferenceError, killing the entire module.
 */
function checkExportsPlugin() {
  return {
    name: "check-js-exports",
    buildStart() {
      const files = ["app.js", "crm.js"];
      const staticDir = resolve(__dirname, "app/static");
      const errors = [];

      for (const file of files) {
        const code = readFileSync(resolve(staticDir, file), "utf8");
        const m = code.match(/Object\.assign\(window,\s*\{([\s\S]+?)\}\);/);
        if (!m) continue;

        // Extract names from Object.assign block
        const names = [];
        for (const line of m[1].split("\n")) {
          const clean = line.replace(/\/\/.*/, "").trim();
          for (const tok of clean.split(/[,\s]+/)) {
            const name = tok.trim();
            if (name && /^[a-zA-Z_]\w*$/.test(name)) names.push(name);
          }
        }

        // Extract definitions and imports
        const defs = new Set();
        for (const d of code.matchAll(/(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(/g))
          defs.add(d[1]);
        for (const d of code.matchAll(/(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[=;]/g))
          defs.add(d[1]);
        for (const d of code.matchAll(/import\s*\{([^}]+)\}\s*from/g))
          for (const tok of d[1].split(/[,\s]+/)) {
            const name = tok.trim();
            if (name) defs.add(name);
          }

        for (const name of names) {
          if (!defs.has(name))
            errors.push(`${file}: '${name}' in Object.assign but never defined or imported`);
        }
      }

      if (errors.length) {
        this.error(
          `${errors.length} undefined export(s) found:\n` +
          errors.map(e => `  ${e}`).join("\n") +
          "\n\nFix: remove the name from Object.assign or define the function."
        );
      }
    },
  };
}

export default defineConfig({
  root: "app/static",
  base: "/static/",

  plugins: [checkExportsPlugin()],

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
    sourcemap: false,
    minify: "terser",
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      input: {
        app: resolve(__dirname, "app/static/app.js"),
        crm: resolve(__dirname, "app/static/crm.js"),
        styles: resolve(__dirname, "app/static/styles.css"),
        mobile: resolve(__dirname, "app/static/mobile.css"),
        touch: resolve(__dirname, "app/static/touch.js"),
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
