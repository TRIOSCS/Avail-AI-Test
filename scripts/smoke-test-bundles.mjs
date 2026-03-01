/**
 * Post-build smoke test for Vite bundles.
 *
 * Reads the built JS bundles from app/static/dist/assets/, executes them
 * inside a jsdom window context using Node's vm module, and verifies that
 * every name from Object.assign(window, {...}) is defined on the window
 * object after execution.
 *
 * Called by: npm run postbuild (automatically after "npm run build")
 * Depends on: jsdom (devDependency), built output in app/static/dist/
 */

import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import vm from "vm";
import { JSDOM } from "jsdom";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const DIST = resolve(ROOT, "app/static/dist");

/* ── 1. Read manifest and locate bundle files ─────────────────────── */

let manifest;
try {
  manifest = JSON.parse(
    readFileSync(resolve(DIST, ".vite/manifest.json"), "utf8")
  );
} catch (err) {
  console.error("SMOKE TEST FAILED: could not read manifest.json —", err.message);
  process.exit(1);
}

const appEntry = manifest["app.js"];
const crmEntry = manifest["crm.js"];

if (!appEntry || !crmEntry) {
  console.error("SMOKE TEST FAILED: manifest missing app.js or crm.js entry");
  process.exit(1);
}

const appFile = resolve(DIST, appEntry.file);
const crmFile = resolve(DIST, crmEntry.file);

let appCode, crmCode;
try {
  appCode = readFileSync(appFile, "utf8");
  crmCode = readFileSync(crmFile, "utf8");
} catch (err) {
  console.error("SMOKE TEST FAILED: could not read bundle —", err.message);
  process.exit(1);
}

/* ── 2. Extract export names from each bundle's Object.assign ──────── */

function extractWindowExports(code) {
  const marker = "Object.assign(window,{";
  const start = code.indexOf(marker);
  if (start === -1) return [];

  const braceStart = start + marker.length - 1;
  let depth = 0;
  let end = braceStart;
  for (let i = braceStart; i < code.length; i++) {
    if (code[i] === "{") depth++;
    else if (code[i] === "}") {
      depth--;
      if (depth === 0) { end = i; break; }
    }
  }

  const block = code.slice(braceStart + 1, end);
  const keys = [];
  let d = 0;
  let current = "";
  for (const ch of block) {
    if (ch === "(" || ch === "{" || ch === "[") d++;
    else if (ch === ")" || ch === "}" || ch === "]") d--;
    if (ch === "," && d === 0) {
      const m = current.match(/^\s*([a-zA-Z_]\w*)\s*:/);
      if (m) keys.push(m[1]);
      current = "";
    } else {
      current += ch;
    }
  }
  const m = current.match(/^\s*([a-zA-Z_]\w*)\s*:/);
  if (m) keys.push(m[1]);

  return keys;
}

const appExportNames = extractWindowExports(appCode);
const crmExportNames = extractWindowExports(crmCode);
const allExpectedExports = [...appExportNames, ...crmExportNames];

if (allExpectedExports.length === 0) {
  console.error("SMOKE TEST FAILED: could not extract any export names from Object.assign(window, {...})");
  process.exit(1);
}

/* ── 3. Transform ESM bundles into executable scripts ──────────────── */

/**
 * The app bundle ends with: export{De as _, k as a, B as b, ...}
 * The crm bundle starts with: import{i as e, r as t, ...}from"./app-xxx.js"
 *
 * Strategy:
 *  - For app.js: replace the export{} statement with code that stores the
 *    export mapping on window.__appExports__, then wrap in an IIFE.
 *  - For crm.js: replace the import{} statement with var declarations that
 *    read from window.__appExports__, then wrap in an IIFE.
 */

function transformAppBundle(code) {
  // Match: export{InternalName as alias, ...};
  const exportMatch = code.match(/export\{([^}]+)\}\s*;?/);
  if (!exportMatch) {
    // No ESM exports — just wrap in IIFE
    return `(function(){${code}})();`;
  }

  // Parse export mappings: "De as _, k as a, ..." or "De as _,k as a,..."
  const exportBlock = exportMatch[1];
  const mappings = [];
  for (const part of exportBlock.split(",")) {
    const trimmed = part.trim();
    const asMatch = trimmed.match(/^(\w+)\s+as\s+(\w+)$/);
    if (asMatch) {
      mappings.push({ internal: asMatch[1], alias: asMatch[2] });
    } else if (/^\w+$/.test(trimmed)) {
      // bare name: export{foo} means internal=foo, alias=foo
      mappings.push({ internal: trimmed, alias: trimmed });
    }
  }

  // Replace the export statement with __appExports__ assignment
  const assignments = mappings
    .map((m) => `${JSON.stringify(m.alias)}:${m.internal}`)
    .join(",");
  const replacement = `window.__appExports__={${assignments}};`;

  const transformed = code.replace(/export\{[^}]+\}\s*;?/, replacement);
  return `(function(){${transformed}})();`;
}

function transformCrmBundle(code) {
  // Match: import{i as e, r as t, ...}from"./app-xxx.js"
  const importMatch = code.match(/import\{([^}]+)\}from"[^"]+"\s*;?/);
  if (!importMatch) {
    // No ESM imports — just wrap in IIFE
    return `(function(){${code}})();`;
  }

  // Parse import mappings: "i as e, r as t, d, ..."
  // "i as e" means: from app exports, take alias "i", bind to local var "e"
  // "d" (bare) means: take alias "d", bind to local var "d"
  const importBlock = importMatch[1];
  const declarations = [];
  for (const part of importBlock.split(",")) {
    const trimmed = part.trim();
    const asMatch = trimmed.match(/^(\w+)\s+as\s+(\w+)$/);
    if (asMatch) {
      // import alias "asMatch[1]" from app exports -> local var "asMatch[2]"
      declarations.push(`var ${asMatch[2]}=window.__appExports__[${JSON.stringify(asMatch[1])}];`);
    } else if (/^\w+$/.test(trimmed)) {
      declarations.push(`var ${trimmed}=window.__appExports__[${JSON.stringify(trimmed)}];`);
    }
  }

  // Replace the import statement with var declarations
  const transformed = code.replace(
    /import\{[^}]+\}from"[^"]+"\s*;?/,
    declarations.join("")
  );

  // Also strip any trailing export{} from crm if present
  const cleaned = transformed.replace(/export\{[^}]+\}\s*;?/g, "");

  return `(function(){${cleaned}})();`;
}

const appCodeClean = transformAppBundle(appCode);
const crmCodeClean = transformCrmBundle(crmCode);

/* ── 4. Create JSDOM with browser API stubs ────────────────────────── */

const html = `<!DOCTYPE html>
<html><head></head><body>
<script type="application/json" id="app-config">{"userName":"test","userEmail":"test@test.com","isAdmin":false,"isManager":false,"userRole":"buyer"}</script>
</body></html>`;

const dom = new JSDOM(html, {
  url: "https://localhost",
  pretendToBeVisual: true,
  runScripts: "dangerously",
});

const win = dom.window;

// Stub matchMedia
win.matchMedia = function (query) {
  return {
    matches: false,
    media: query,
    onchange: null,
    addListener: function () {},
    removeListener: function () {},
    addEventListener: function () {},
    removeEventListener: function () {},
    dispatchEvent: function () { return false; },
  };
};

// Stub IntersectionObserver
win.IntersectionObserver = class {
  constructor() {}
  observe() {}
  unobserve() {}
  disconnect() {}
};

// Stub MutationObserver
if (!win.MutationObserver) {
  win.MutationObserver = class {
    constructor() {}
    observe() {}
    disconnect() {}
    takeRecords() { return []; }
  };
}

// Stub ResizeObserver
win.ResizeObserver = class {
  constructor() {}
  observe() {}
  unobserve() {}
  disconnect() {}
};

// Stub fetch
win.fetch = function () {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve(""),
  });
};

// Stub localStorage
if (!win.localStorage) {
  const store = {};
  win.localStorage = {
    getItem: (k) => store[k] || null,
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { for (const k in store) delete store[k]; },
  };
}

// Stub history.pushState / replaceState
if (!win.history.pushState) {
  win.history.pushState = function () {};
}
if (!win.history.replaceState) {
  win.history.replaceState = function () {};
}

// Stub navigator.clipboard
win.navigator.clipboard = {
  writeText: () => Promise.resolve(),
  readText: () => Promise.resolve(""),
};

// Stub html2canvas
win.html2canvas = function () {
  return Promise.resolve({
    toDataURL: () => "",
    toBlob: (cb) => cb(new win.Blob()),
  });
};

// Stub getComputedStyle
if (!win.getComputedStyle) {
  win.getComputedStyle = function () {
    return new Proxy({}, { get: () => "" });
  };
}

// Stub requestAnimationFrame / cancelAnimationFrame
if (!win.requestAnimationFrame) {
  win.requestAnimationFrame = function (cb) { return setTimeout(cb, 0); };
}
if (!win.cancelAnimationFrame) {
  win.cancelAnimationFrame = function (id) { clearTimeout(id); };
}

// Stub scrollTo / scrollBy
win.scrollTo = win.scrollTo || function () {};
win.scrollBy = win.scrollBy || function () {};

// Stub DOMRect
if (!win.DOMRect) {
  win.DOMRect = class {
    constructor(x=0,y=0,w=0,h=0) {
      this.x=x; this.y=y; this.width=w; this.height=h;
      this.top=y; this.right=x+w; this.bottom=y+h; this.left=x;
    }
  };
}

// Stub element.animate
if (win.HTMLElement && !win.HTMLElement.prototype.animate) {
  win.HTMLElement.prototype.animate = function () {
    return { finished: Promise.resolve(), cancel() {}, finish() {} };
  };
}

/* ── 5. Execute bundles in JSDOM context via vm.Script ─────────────── */

const fatalErrors = [];

function runBundle(code, label) {
  try {
    const context = vm.createContext(win);
    const script = new vm.Script(code, { filename: label });
    script.runInContext(context);
  } catch (err) {
    const isFatal =
      err instanceof ReferenceError ||
      err instanceof SyntaxError ||
      err.name === "ReferenceError" ||
      err.name === "SyntaxError";

    if (isFatal) {
      fatalErrors.push({ label, error: err });
      console.error(`  FATAL ${err.name} in ${label}: ${err.message}`);
    } else {
      // Non-fatal: DOM errors, type errors from missing elements, etc.
      console.warn(`  [warn] ${label}: ${err.name || "Error"}: ${err.message}`);
    }
  }
}

console.log("Running smoke test on built bundles...");
console.log(`  app bundle: ${appEntry.file}`);
console.log(`  crm bundle: ${crmEntry.file}`);

// Run app first, then crm (crm reads from window.__appExports__)
runBundle(appCodeClean, "app.js");
runBundle(crmCodeClean, "crm.js");

if (fatalErrors.length > 0) {
  console.error(`\nSMOKE TEST FAILED: ${fatalErrors.length} fatal error(s) during bundle execution.`);
  for (const { label, error } of fatalErrors) {
    console.error(`  ${label}: ${error.name}: ${error.message}`);
  }
  process.exit(1);
}

/* ── 6. Verify exports are defined on window ───────────────────────── */

const missing = [];
for (const name of allExpectedExports) {
  if (typeof win[name] === "undefined") {
    missing.push(name);
  }
}

if (missing.length > 0) {
  console.error(`\nSMOKE TEST FAILED: ${missing.length} export(s) not found on window:`);
  for (const name of missing.slice(0, 20)) {
    console.error(`  - ${name}`);
  }
  if (missing.length > 20) {
    console.error(`  ... and ${missing.length - 20} more`);
  }
  process.exit(1);
}

const total = allExpectedExports.length;
console.log(`\nSMOKE TEST PASSED: bundles loaded, ${total} exports verified.`);

// Clean up and exit — JSDOM + app timers keep the event loop alive otherwise
dom.window.close();
process.exit(0);
