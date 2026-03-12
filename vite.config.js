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
      const files = ["app.js", "crm.js", "tickets.js"];
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

      /* --- Check 2: inline onclick/oninput/onchange/onkeydown handlers --- */
      const BUILTINS = new Set([
        "if", "event", "this", "document", "window", "console", "navigator",
        "getElementById", "querySelector", "querySelectorAll",
        "preventDefault", "stopPropagation", "focus", "click", "scrollBy",
        "contains", "match", "catch", "then", "fetch", "reload", "writeText",
      ]);

      // Build a combined definition set from BOTH JS files
      const allDefs = new Set();
      for (const file of files) {
        const code = readFileSync(resolve(staticDir, file), "utf8");
        for (const d of code.matchAll(/(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(/g))
          allDefs.add(d[1]);
        for (const d of code.matchAll(/(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[=;]/g))
          allDefs.add(d[1]);
        for (const d of code.matchAll(/import\s*\{([^}]+)\}\s*from/g))
          for (const tok of d[1].split(/[,\s]+/)) {
            const name = tok.trim();
            if (name) allDefs.add(name);
          }
      }

      const htmlPath = resolve(__dirname, "app/templates/index.html");
      const html = readFileSync(htmlPath, "utf8");
      const handlerRe = /\b(?:onclick|oninput|onchange|onkeydown)="([^"]*)"/g;
      for (const hm of html.matchAll(handlerRe)) {
        const body = hm[1];
        // Extract every function-call name: word followed by '('
        for (const call of body.matchAll(/([a-zA-Z_]\w*)\s*\(/g)) {
          const fn = call[1];
          if (BUILTINS.has(fn)) continue;
          if (!allDefs.has(fn))
            errors.push(`index.html: onclick handler calls '${fn}()' but it's not defined in app.js or crm.js`);
        }
      }

      /* --- Check 3: crm.js imports from 'app' must exist as app.js exports --- */
      const crmCode = readFileSync(resolve(staticDir, "crm.js"), "utf8");
      const appCode = readFileSync(resolve(staticDir, "app.js"), "utf8");

      // Collect all names imported from 'app' in crm.js
      const crmImports = new Set();
      for (const im of crmCode.matchAll(/import\s*\{([^}]+)\}\s*from\s*['"]app['"]/g)) {
        for (const tok of im[1].split(/[,\s]+/)) {
          const name = tok.trim();
          if (name && /^[a-zA-Z_]\w*$/.test(name)) crmImports.add(name);
        }
      }

      // Collect all exported names from app.js
      const appExports = new Set();
      for (const d of appCode.matchAll(/export\s+(?:async\s+)?function\s+(\w+)\s*\(/g))
        appExports.add(d[1]);
      for (const d of appCode.matchAll(/export\s+(?:const|let|var)\s+(\w+)\s*[=;]/g))
        appExports.add(d[1]);

      for (const name of crmImports) {
        if (!appExports.has(name))
          errors.push(`crm.js: imports '${name}' from app.js but app.js doesn't export it`);
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

  test: {
    root: resolve(__dirname),
    include: ["tests/frontend/**/*.test.{js,mjs,ts}"],
    environment: "jsdom",
  },

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
        tickets: resolve(__dirname, "app/static/tickets.js"),
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
