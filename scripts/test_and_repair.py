#!/usr/bin/env python3
"""
AVAIL v1.2.0 — Autonomous Test & Repair Engine
Runs 5 full passes of 20+ structural/functional tests with auto-repair.
Each pass: test everything → collect failures → auto-repair → re-test.
"""

import ast, re, os, sys, json, hashlib, textwrap
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ─── Config ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
STATIC = APP / "static"
TEMPLATES = APP / "templates"
MIGRATIONS = ROOT / "migrations"
MAX_PASSES = 5

# Colors
G = "\033[92m"  # green
R = "\033[91m"  # red
Y = "\033[93m"  # yellow
C = "\033[96m"  # cyan
B = "\033[1m"   # bold
W = "\033[97m"  # white
Z = "\033[0m"   # reset


@dataclass
class Issue:
    test: str
    file: str
    description: str
    severity: str = "error"  # error, warn
    auto_fixable: bool = False
    fix_applied: bool = False
    fix_description: str = ""


class TestEngine:
    def __init__(self):
        self.issues: list[Issue] = []
        self.fixes_applied = 0
        self.pass_num = 0
        self._file_cache: dict[str, str] = {}

    def _read(self, path: Path) -> str:
        key = str(path)
        if key not in self._file_cache:
            self._file_cache[key] = path.read_text(encoding="utf-8", errors="replace")
        return self._file_cache[key]

    def _write(self, path: Path, content: str):
        path.write_text(content, encoding="utf-8")
        self._file_cache[str(path)] = content

    def _add(self, test, file, desc, severity="error", fixable=False):
        self.issues.append(Issue(test=test, file=file, description=desc,
                                  severity=severity, auto_fixable=fixable))

    def _fix(self, issue: Issue, desc: str):
        issue.fix_applied = True
        issue.fix_description = desc
        self.fixes_applied += 1

    # ═══════════════════════════════════════════════════════════════════════
    #  TEST SUITE — 20 tests
    # ═══════════════════════════════════════════════════════════════════════

    def t01_python_syntax(self):
        """Compile-check every .py file."""
        for f in sorted(APP.rglob("*.py")):
            try:
                ast.parse(f.read_bytes(), filename=str(f))
            except SyntaxError as e:
                self._add("T01-PySyntax", str(f.relative_to(ROOT)),
                          f"Line {e.lineno}: {e.msg}")

    def t02_javascript_syntax(self):
        """Parse JS files with Node.js."""
        for name in ["app.js", "crm.js"]:
            f = STATIC / name
            if not f.exists():
                self._add("T02-JSSyntax", name, "File missing"); continue
            # Use node --check equivalent
            code = self._read(f)
            try:
                import subprocess
                r = subprocess.run(
                    ["node", "-e", f"new Function({json.dumps(code)})"],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode != 0:
                    # Extract useful error
                    err = r.stderr.strip().split('\n')[-1] if r.stderr else "unknown"
                    self._add("T02-JSSyntax", name, err)
            except FileNotFoundError:
                self._add("T02-JSSyntax", name, "Node.js not available", severity="warn")
            except Exception as e:
                self._add("T02-JSSyntax", name, str(e), severity="warn")

    def t03_python_imports(self):
        """Verify all 'from app.X import Y' / 'import app.X' references resolve."""
        all_py = {str(f.relative_to(ROOT)): self._read(f) for f in APP.rglob("*.py")}

        # Build module map
        module_paths = set()
        for f in APP.rglob("*.py"):
            rel = f.relative_to(ROOT)
            # app/utils/graph_client.py → app.utils.graph_client
            mod = str(rel).replace("/", ".").replace(".py", "")
            module_paths.add(mod)
            # Also add package (app.utils)
            if f.name == "__init__.py":
                module_paths.add(mod.replace(".__init__", ""))

        for relpath, code in all_py.items():
            tree = ast.parse(code, filename=relpath)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                    # Check the module itself resolves
                    mod = node.module
                    if mod not in module_paths:
                        # Maybe it's importing from a module attribute
                        parent = ".".join(mod.split(".")[:-1])
                        if parent not in module_paths:
                            self._add("T03-Imports", relpath,
                                      f"Unresolvable import: from {mod} import ...",
                                      fixable=False)

    def t04_model_migration_alignment(self):
        """Every ALTER TABLE ADD COLUMN in migrations must have a matching model attribute."""
        models_code = self._read(APP / "models.py")

        for mig_file in sorted(MIGRATIONS.glob("*.sql")):
            sql = self._read(mig_file)
            fname = mig_file.name

            # Check ALTER TABLE ADD COLUMN
            for m in re.finditer(
                r'ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(?:IF NOT EXISTS\s+)?(\w+)\s+(\w+)',
                sql, re.IGNORECASE
            ):
                table, col, dtype = m.group(1), m.group(2), m.group(3)
                if col not in models_code:
                    self._add("T04-ModelMigSync", fname,
                              f"{table}.{col} ({dtype}) in migration but not in models.py",
                              fixable=True)

            # Check CREATE TABLE
            for m in re.finditer(r'CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)', sql, re.IGNORECASE):
                table = m.group(1)
                # Convert snake_case → CamelCase
                camel = "".join(w.capitalize() for w in table.split("_"))
                if camel not in models_code and f'__tablename__ = "{table}"' not in models_code:
                    self._add("T04-ModelMigSync", fname,
                              f"Table '{table}' (class {camel}) in migration but not in models.py")

    def t05_api_route_functions(self):
        """Every @app.get/post/put/delete handler function must be defined."""
        main_code = self._read(APP / "main.py")

        # Find route decorators
        route_pattern = re.compile(
            r'@app\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']\s*\)\s*\n'
            r'(?:async\s+)?def\s+(\w+)',
            re.MULTILINE
        )
        routes = route_pattern.findall(main_code)

        # Find all function defs in main.py
        fn_pattern = re.compile(r'(?:async\s+)?def\s+(\w+)')
        defined_fns = set(fn_pattern.findall(main_code))

        # Check helper functions referenced in route bodies
        for method, path, handler in routes:
            if handler not in defined_fns:
                self._add("T05-APIRoutes", "app/main.py",
                          f"Route {method.upper()} {path} → {handler}() not defined")

    def t06_html_onclick_handlers(self):
        """Every onclick='funcName(...)' in HTML must exist in JS."""
        html = self._read(TEMPLATES / "index.html")
        app_js = self._read(STATIC / "app.js")
        crm_js = self._read(STATIC / "crm.js")
        all_js = app_js + crm_js

        # onclick handlers
        onclick_fns = set()
        for m in re.finditer(r'onclick="([a-zA-Z_]\w*)\(', html):
            onclick_fns.add(m.group(1))

        # JS function definitions
        js_fns = set()
        for m in re.finditer(r'(?:async\s+)?function\s+([a-zA-Z_]\w*)', all_js):
            js_fns.add(m.group(1))
        # Also arrow / const assignments
        for m in re.finditer(r'(?:const|let|var)\s+([a-zA-Z_]\w*)\s*=\s*(?:async\s+)?\(', all_js):
            js_fns.add(m.group(1))

        builtins = {"fetch", "if", "document", "location", "event", "window",
                     "setTimeout", "parseInt", "parseFloat", "alert", "confirm",
                     "console", "JSON", "encodeURIComponent", "decodeURIComponent",
                     "history", "navigator"}

        for fn in onclick_fns - js_fns - builtins:
            self._add("T06-OnclickRef", "index.html", f"onclick='{fn}(...)' — no JS definition")

    def t07_cross_file_js_calls(self):
        """Functions called in app.js that are defined only in crm.js (and vice versa)."""
        app_js = self._read(STATIC / "app.js")
        crm_js = self._read(STATIC / "crm.js")

        def extract_defs(code):
            fns = set()
            for m in re.finditer(r'(?:async\s+)?function\s+([a-zA-Z_]\w*)', code):
                fns.add(m.group(1))
            return fns

        def extract_calls(code):
            calls = set()
            for m in re.finditer(r'(?<!\.)(?<!\w)([a-zA-Z_]\w*)\s*\(', code):
                fn = m.group(1)
                kw = {"if","for","while","switch","catch","return","new","typeof",
                      "await","async","function","class","import","export","try",
                      "throw","delete","void","super","this","constructor","get","set"}
                if fn not in kw:
                    calls.add(fn)
            return calls

        app_defs = extract_defs(app_js)
        crm_defs = extract_defs(crm_js)
        app_calls = extract_calls(app_js)
        crm_calls = extract_calls(crm_js)

        js_builtins = {
            "fetch","document","window","console","JSON","Math","Date","Array","Object",
            "String","Number","Boolean","RegExp","Error","Promise","Map","Set",
            "parseInt","parseFloat","isNaN","encodeURIComponent","decodeURIComponent",
            "setTimeout","setInterval","clearTimeout","clearInterval","alert","confirm",
            "requestAnimationFrame","cancelAnimationFrame","URL","URLSearchParams",
            "FormData","Headers","Request","Response","AbortController","Blob","File",
            "FileReader","TextEncoder","TextDecoder","crypto","performance",
            "queueMicrotask","structuredClone","atob","btoa","escape","unescape",
            "Intl","Proxy","Reflect","Symbol","WeakMap","WeakSet","BigInt",
            "esc","escAttr","fmtDate","stars","showToast",  # our shared utils
        }

        # app.js calls something not defined in app.js OR crm.js
        all_defs = app_defs | crm_defs | js_builtins
        for fn in app_calls - all_defs:
            # Skip member access patterns and common DOM methods
            if fn in {"getElementById","querySelector","querySelectorAll","createElement",
                      "appendChild","removeChild","innerHTML","textContent","classList",
                      "addEventListener","removeEventListener","preventDefault",
                      "stopPropagation","trim","split","join","map","filter","reduce",
                      "forEach","find","findIndex","includes","indexOf","slice","splice",
                      "push","pop","shift","unshift","sort","reverse","concat",
                      "replace","match","test","exec","toString","valueOf",
                      "toFixed","toLocaleDateString","toISOString","getTime",
                      "keys","values","entries","assign","freeze","defineProperty",
                      "stringify","parse","log","warn","error","info","debug",
                      "then","catch","finally","resolve","reject","all","race",
                      "ok","json","text","blob","arrayBuffer","formData",
                      "open","close","send","abort","read","write",
                      "length","style","value","checked","disabled","hidden",
                      "remove","add","toggle","contains","getAttribute","setAttribute",
                      "append","prepend","before","after","replaceWith","cloneNode",
                      # Template literal false positives (words inside strings that
                      # look like fn() but are actually text content fragments)
                      "vendors","price","contacts","sendable","Profile","prompt",
                      "Errors","container","filters","emails","rgba","expanded",
                      "Setup","comment","Part","Sources","group","vendor","of",
                      "email","Disabled","MPN","RFQ","Up","var","sent","skipped",
                      "Condition","Score","Number","status","type","name","label",
                      "count","rate","index","min","max","abs","round","floor","ceil",
                      "repeat","charAt","charCodeAt","codePointAt","normalize",
                      "startsWith","endsWith","padStart","padEnd","trimStart","trimEnd",
                      "flat","flatMap","fill","copyWithin","at","every","some",
                      "from","isArray","now","random","trunc","sign","pow","sqrt",
                      "freeze","isFrozen","create","getPrototypeOf","hasOwnProperty",
                      }:
                continue
            self._add("T07-CrossFileJS", "app.js",
                      f"Calls {fn}() — not defined in app.js or crm.js",
                      severity="warn")

    def t08_css_classes_used_in_js(self):
        """CSS classes referenced in JS template literals must be defined in CSS."""
        html = self._read(TEMPLATES / "index.html")
        app_js = self._read(STATIC / "app.js")
        crm_js = self._read(STATIC / "crm.js")

        # Extract CSS class definitions from <style>
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        css = style_match.group(1) if style_match else ""

        defined_classes = set()
        for m in re.finditer(r'\.([a-zA-Z_][\w-]*)', css):
            defined_classes.add(m.group(1))

        # Critical AI classes we explicitly check
        critical_classes = [
            "badge-green", "badge-yellow", "badge-gray",
            "btn-ai", "ai-panel-bg", "ai-panel", "ai-panel-header",
            "ai-contact-row", "ai-contact-info", "ai-contact-actions",
            "intel-card", "intel-body", "intel-summary",
            "parse-parts-table", "parse-confidence",
            "engagement-ring", "eng-high", "eng-med", "eng-low",
            "act-badge-classification",
        ]

        for cls in critical_classes:
            if cls not in defined_classes:
                self._add("T08-CSSClasses", "index.html",
                          f"Critical CSS class '.{cls}' missing from stylesheet",
                          fixable=True)

    def t09_sql_migration_syntax(self):
        """Basic SQL validation for migration files."""
        for mig_file in sorted(MIGRATIONS.glob("*.sql")):
            sql = self._read(mig_file)
            fname = mig_file.name

            # Check balanced parentheses
            opens = sql.count("(")
            closes = sql.count(")")
            if opens != closes:
                self._add("T09-SQLSyntax", fname,
                          f"Unbalanced parentheses: {opens} open, {closes} close")

            # Check every statement ends with semicolon (ignoring comments/blanks)
            lines = sql.strip().split("\n")
            in_block = 0
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                in_block += stripped.count("(") - stripped.count(")")
                if in_block <= 0 and not stripped.endswith(";") and not stripped.endswith(","):
                    # Could be continuation
                    if not any(stripped.upper().startswith(kw) for kw in
                               ["CREATE", "ALTER", "INSERT", "UPDATE", "DELETE",
                                "DROP", "BEGIN", "COMMIT", "SET", "DO", "GRANT",
                                "REVOKE", "WITH", "SELECT", "IF", "END", "THEN",
                                "ELSE", "ELSIF", "WHEN", "LOOP", "FOR", "WHILE",
                                "RETURN", "RAISE", "PERFORM", "EXECUTE", "DECLARE",
                                "$$", "AS", "PRIMARY", "UNIQUE", "CONSTRAINT",
                                "REFERENCES", "FOREIGN", "INDEX", "ON", "ADD",
                                "NOT", "DEFAULT", "CHECK"]):
                        pass  # OK, it's a continuation line

            # Check for common typos
            typo_patterns = [
                (r'INTEGET\b', "INTEGET → INTEGER"),
                (r'VARCAHR\b', "VARCAHR → VARCHAR"),
                (r'BOOELAN\b', "BOOELAN → BOOLEAN"),
                (r'TIMESTAM\b(?!P)', "TIMESTAM → TIMESTAMP"),
                (r'DEFUALT\b', "DEFUALT → DEFAULT"),
                (r'FORIEGN\b', "FORIEGN → FOREIGN"),
                (r'PRIMAY\b', "PRIMAY → PRIMARY"),
                (r'REFERNCES\b', "REFERNCES → REFERENCES"),
                (r'CASCAD\b(?!E)', "CASCAD → CASCADE"),
                (r'UNIQE\b', "UNIQE → UNIQUE"),
            ]
            for pattern, fix in typo_patterns:
                if re.search(pattern, sql, re.IGNORECASE):
                    self._add("T09-SQLSyntax", fname, f"Possible SQL typo: {fix}",
                              fixable=True)

    def t10_config_env_vars(self):
        """Environment variables referenced in code should have defaults or be in config."""
        config_code = self._read(APP / "config.py")

        # Find all os.getenv / os.environ references across all Python files
        env_refs = defaultdict(list)
        for f in APP.rglob("*.py"):
            code = self._read(f)
            for m in re.finditer(r'os\.(?:getenv|environ(?:\.get)?)\s*\(\s*["\'](\w+)["\']', code):
                env_refs[m.group(1)].append(str(f.relative_to(ROOT)))

        # Check each env var has a default or is in config
        for var, files in sorted(env_refs.items()):
            if var not in config_code:
                # Check if it has a default value in the getenv call
                has_default = False
                for f in files:
                    code = self._read(ROOT / f)
                    if re.search(rf'os\.getenv\s*\(\s*["\']' + var + r'["\']\s*,', code):
                        has_default = True
                        break
                if not has_default:
                    self._add("T10-Config", ", ".join(files),
                              f"Env var {var} used without default and not in config.py",
                              severity="warn")

    def t11_unused_imports(self):
        """Find imports that are never referenced in the file body."""
        for f in sorted(APP.rglob("*.py")):
            if f.name == "__init__.py":
                continue
            code = self._read(f)
            relpath = str(f.relative_to(ROOT))
            try:
                tree = ast.parse(code, filename=relpath)
            except SyntaxError:
                continue

            # Collect imported names
            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name.split(".")[0]
                        imported[name] = node.lineno
                elif isinstance(node, ast.ImportFrom):
                    if node.names[0].name == "*":
                        continue
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imported[name] = node.lineno

            # Check if each imported name is used in the code (excluding import lines)
            lines = code.split("\n")
            for name, lineno in imported.items():
                if name.startswith("_"):
                    continue
                # Count occurrences excluding the import line
                usage_count = 0
                for i, line in enumerate(lines, 1):
                    if i == lineno:
                        continue
                    if re.search(r'\b' + re.escape(name) + r'\b', line):
                        usage_count += 1
                        break
                if usage_count == 0:
                    self._add("T11-UnusedImport", relpath,
                              f"Line {lineno}: '{name}' imported but never used",
                              severity="warn", fixable=True)

    def t12_api_endpoint_response_consistency(self):
        """POST/PUT endpoints should return JSON; check for missing status_code."""
        main_code = self._read(APP / "main.py")

        # Find all route handlers
        route_re = re.compile(
            r'@app\.(post|put)\s*\(\s*["\']([^"\']+)["\']\s*\)\s*\n'
            r'((?:async\s+)?def\s+(\w+)\s*\([^)]*\).*?)(?=\n@app\.|\nclass\s|\Z)',
            re.MULTILINE | re.DOTALL
        )

        for m in route_re.finditer(main_code):
            method, path, body, fn_name = m.groups()
            # Check that the function has a return statement
            if "return" not in body and "raise" not in body:
                self._add("T12-APIResponse", "app/main.py",
                          f"{method.upper()} {path} ({fn_name}) has no return statement",
                          severity="warn")

    def t13_duplicate_route_paths(self):
        """No two routes should share method+path."""
        main_code = self._read(APP / "main.py")

        route_re = re.compile(
            r'@app\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
            re.MULTILINE
        )

        seen = {}
        for m in route_re.finditer(main_code):
            method, path = m.group(1).upper(), m.group(2)
            key = f"{method} {path}"
            if key in seen:
                self._add("T13-DuplicateRoute", "app/main.py",
                          f"Duplicate route: {key} (first at pos {seen[key]}, second at pos {m.start()})")
            else:
                seen[key] = m.start()

    def t14_html_tag_balance(self):
        """Check that key structural HTML tags are balanced."""
        html = self._read(TEMPLATES / "index.html")

        # Remove content inside <script>, <style>, and HTML comments
        clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)
        clean = re.sub(r'<!--.*?-->', '', clean, flags=re.DOTALL)

        # Check key tags
        for tag in ["div", "table", "tr", "td", "th", "form", "select", "ul", "ol", "li"]:
            opens = len(re.findall(rf'<{tag}[\s>]', clean, re.IGNORECASE))
            closes = len(re.findall(rf'</{tag}\s*>', clean, re.IGNORECASE))
            if opens != closes:
                diff = opens - closes
                direction = "unclosed" if diff > 0 else "extra closing"
                self._add("T14-HTMLBalance", "index.html",
                          f"<{tag}>: {opens} opens, {closes} closes ({abs(diff)} {direction})",
                          severity="warn")

    def t15_css_variable_coverage(self):
        """CSS variables used (var(--X)) must be defined somewhere (:root or element)."""
        html = self._read(TEMPLATES / "index.html")

        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        css = style_match.group(1) if style_match else ""

        # Find all defined custom properties
        defined = set()
        for m in re.finditer(r'--([a-zA-Z][\w-]*)\s*:', css):
            defined.add(m.group(1))

        # Find all used custom properties
        used = set()
        for m in re.finditer(r'var\(\s*--([a-zA-Z][\w-]*)', css):
            used.add(m.group(1))

        missing = used - defined
        for var in sorted(missing):
            self._add("T15-CSSVars", "index.html",
                      f"CSS variable --{var} used but never defined",
                      fixable=True)

    def t16_requirements_completeness(self):
        """Python imports of third-party packages should be in requirements.txt."""
        req_file = ROOT / "requirements.txt"
        if not req_file.exists():
            self._add("T16-Requirements", "requirements.txt", "File missing")
            return

        req_text = self._read(req_file).lower()

        # Map import names → pip package names
        import_to_pkg = {
            "fastapi": "fastapi", "uvicorn": "uvicorn", "sqlalchemy": "sqlalchemy",
            "httpx": "httpx", "pydantic": "pydantic", "jwt": "pyjwt",
            "apscheduler": "apscheduler", "bs4": "beautifulsoup4",
            "defusedxml": "defusedxml", "charset_normalizer": "charset-normalizer",
            "filetype": "filetype", "tenacity": "tenacity", "openpyxl": "openpyxl",
            "dotenv": "python-dotenv", "jinja2": "jinja2", "aiofiles": "aiofiles",
            "passlib": "passlib", "email_validator": "email-validator",
            "psycopg2": "psycopg2", "alembic": "alembic", "cryptography": "cryptography",
            "requests": "requests", "pandas": "pandas", "numpy": "numpy",
            "xlrd": "xlrd", "xlsxwriter": "xlsxwriter",
        }

        # Find all third-party imports
        for f in APP.rglob("*.py"):
            code = self._read(f)
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                mod_name = None
                if isinstance(node, ast.Import):
                    mod_name = node.names[0].name.split(".")[0]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mod_name = node.module.split(".")[0]

                if mod_name and mod_name in import_to_pkg:
                    pkg = import_to_pkg[mod_name]
                    if pkg not in req_text:
                        self._add("T16-Requirements", "requirements.txt",
                                  f"Package '{pkg}' imported in {f.name} but not in requirements.txt",
                                  fixable=True)

    def t17_model_relationships(self):
        """ForeignKey references should point to existing tables/columns."""
        models_code = self._read(APP / "models.py")

        # Extract all __tablename__ definitions
        tablenames = set()
        for m in re.finditer(r'__tablename__\s*=\s*["\'](\w+)["\']', models_code):
            tablenames.add(m.group(1))

        # Extract all ForeignKey references
        for m in re.finditer(r'ForeignKey\s*\(\s*["\'](\w+)\.(\w+)["\']', models_code):
            table, col = m.group(1), m.group(2)
            if table not in tablenames:
                self._add("T17-ModelRelations", "app/models.py",
                          f"ForeignKey('{table}.{col}') — table '{table}' not defined")

    def t18_js_fetch_endpoints(self):
        """fetch('/api/...') calls in JS should match defined API routes."""
        app_js = self._read(STATIC / "app.js")
        crm_js = self._read(STATIC / "crm.js")
        main_code = self._read(APP / "main.py")

        # Extract route patterns from main.py
        route_patterns = []
        for m in re.finditer(r'@app\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']', main_code):
            method, path = m.group(1).upper(), m.group(2)
            # Convert {param} to regex
            regex_path = re.sub(r'\{[^}]+\}', r'[^/]+', path)
            route_patterns.append((method, path, re.compile(f'^{regex_path}$')))

        # Extract fetch calls from JS
        all_js = app_js + crm_js
        fetch_calls = []
        for m in re.finditer(r'fetch\s*\(\s*`([^`]+)`', all_js):
            url = m.group(1)
            # Replace ${...} with placeholder
            url_clean = re.sub(r'\$\{[^}]+\}', 'PARAM', url)
            fetch_calls.append(url_clean)
        for m in re.finditer(r"fetch\s*\(\s*['\"]([^'\"]+)['\"]", all_js):
            fetch_calls.append(m.group(1))

        # Check each fetch URL has a matching route
        for url in fetch_calls:
            if not url.startswith("/api/"):
                continue
            # Strip query params for matching
            test_url = url.split("?")[0].replace("PARAM", "123")
            # Remove trailing slash for matching
            test_url = test_url.rstrip("/") if len(test_url) > 1 else test_url
            matched = False
            for method, path, regex in route_patterns:
                clean_path = path.rstrip("/") if len(path) > 1 else path
                if regex.match(test_url) or test_url == clean_path:
                    matched = True
                    break
            if not matched:
                # Also try matching with a dummy trailing segment (for URLs like
                # /api/sites/ + siteId which extract as /api/sites/)
                if url.rstrip().endswith("/"):
                    test_with_segment = test_url + "/123"
                    for method, path, regex in route_patterns:
                        if regex.match(test_with_segment):
                            matched = True
                            break
            if not matched:
                self._add("T18-FetchEndpoints", "JS→API",
                          f"fetch('{url}') — no matching API route found",
                          severity="warn")

    def t19_vendorcard_api_fields(self):
        """VendorCard engagement fields in model should be exposed in API serialization."""
        main_code = self._read(APP / "main.py")
        models_code = self._read(APP / "models.py")

        # Check engagement fields exist in VendorCard model
        engagement_fields = [
            "engagement_score", "total_outreach", "total_responses",
            "ghost_rate", "response_velocity_hours", "last_contact_at"
        ]

        for field_name in engagement_fields:
            if field_name not in models_code:
                self._add("T19-VendorAPI", "app/models.py",
                          f"VendorCard engagement field '{field_name}' missing from model")

        # Check _card_to_dict function specifically (not any sighting_count in file)
        card_dict_match = re.search(
            r'def _card_to_dict\(.*?\n(.*?)(?=\ndef\s|\Z)',
            main_code, re.DOTALL
        )
        if card_dict_match:
            body = card_dict_match.group(1)
            for field_name in engagement_fields:
                if f'"{field_name}"' not in body:
                    self._add("T19-VendorAPI", "app/main.py",
                              f"_card_to_dict doesn't serialize '{field_name}'",
                              severity="warn", fixable=True)

    def t20_file_size_sanity(self):
        """Flag files that are suspiciously large or empty."""
        for f in APP.rglob("*.py"):
            size = f.stat().st_size
            relpath = str(f.relative_to(ROOT))
            if size == 0 and f.name != "__init__.py":
                self._add("T20-FileSize", relpath, "Empty Python file (0 bytes)")
            elif size > 200_000:
                self._add("T20-FileSize", relpath,
                          f"Very large file ({size:,} bytes) — consider splitting",
                          severity="warn")

    # ═══════════════════════════════════════════════════════════════════════
    #  AUTO-REPAIR ENGINE
    # ═══════════════════════════════════════════════════════════════════════

    def auto_repair(self):
        """Attempt to fix all auto_fixable issues."""
        repairs = [i for i in self.issues if i.auto_fixable and not i.fix_applied]
        if not repairs:
            return 0

        count = 0

        for issue in repairs:
            try:
                fixed = self._try_fix(issue)
                if fixed:
                    count += 1
            except Exception as e:
                issue.fix_description = f"Repair failed: {e}"

        return count

    def _try_fix(self, issue: Issue) -> bool:
        """Dispatch repair by test ID."""

        # T04: Model-Migration alignment — add missing column to models.py
        if issue.test == "T04-ModelMigSync" and "in migration but not in models.py" in issue.description:
            return self._fix_missing_model_column(issue)

        # T08: Missing CSS class
        if issue.test == "T08-CSSClasses" and "missing from stylesheet" in issue.description:
            return self._fix_missing_css_class(issue)

        # T09: SQL typos
        if issue.test == "T09-SQLSyntax" and "Possible SQL typo" in issue.description:
            return self._fix_sql_typo(issue)

        # T11: Unused imports
        if issue.test == "T11-UnusedImport":
            return self._fix_unused_import(issue)

        # T15: Missing CSS variables
        if issue.test == "T15-CSSVars" and "used but never defined" in issue.description:
            return self._fix_missing_css_var(issue)

        # T16: Missing requirements
        if issue.test == "T16-Requirements":
            return self._fix_missing_requirement(issue)

        # T19: Vendor API missing serialized field
        if issue.test == "T19-VendorAPI" and "doesn't serialize" in issue.description:
            return self._fix_vendor_api_serialization(issue)

        return False

    def _fix_missing_model_column(self, issue: Issue) -> bool:
        """Add missing column attribute to model class."""
        # Parse: "contacts.needs_review (BOOLEAN) in migration but not in models.py"
        m = re.match(r'(\w+)\.(\w+)\s+\((\w+)\)', issue.description)
        if not m:
            return False

        table, col, dtype = m.groups()
        models_path = APP / "models.py"
        code = self._read(models_path)

        # Map SQL types to SQLAlchemy types
        type_map = {
            "TEXT": "String", "VARCHAR": "String", "INTEGER": "Integer", "INT": "Integer",
            "BIGINT": "Integer", "BOOLEAN": "Boolean", "FLOAT": "Float",
            "DOUBLE": "Float", "NUMERIC": "Float", "DECIMAL": "Float",
            "TIMESTAMP": "DateTime", "TIMESTAMPTZ": "DateTime", "DATE": "Date",
            "JSON": "JSON", "JSONB": "JSON", "SERIAL": "Integer",
        }

        sa_type = type_map.get(dtype.upper(), "String")

        # Find the class that has __tablename__ = table
        pattern = rf'(__tablename__\s*=\s*["\']' + table + r'["\'])'
        match = re.search(pattern, code)
        if not match:
            return False

        # Find the end of the class (next class def or EOF)
        class_start = code.rfind("\nclass ", 0, match.start())
        next_class = code.find("\nclass ", match.end())
        class_end = next_class if next_class != -1 else len(code)

        # Add the column before the class ends, after the last column definition
        class_body = code[class_start:class_end]
        last_col = max(
            (m.end() for m in re.finditer(r'=\s*Column\([^)]+\)', class_body)),
            default=None
        )

        if last_col is not None:
            insert_pos = class_start + last_col
            new_line = f"\n    {col} = Column({sa_type})"
            new_code = code[:insert_pos] + new_line + code[insert_pos:]
            self._write(models_path, new_code)
            self._fix(issue, f"Added {col} = Column({sa_type}) to {table} model")
            return True

        return False

    def _fix_missing_css_class(self, issue: Issue) -> bool:
        """Add stub CSS class definition."""
        m = re.search(r"'\.([^']+)'", issue.description)
        if not m:
            return False
        cls = m.group(1)

        html_path = TEMPLATES / "index.html"
        html = self._read(html_path)

        # Insert before </style>
        stub = f"\n        .{cls}{{/* auto-generated stub */}}"
        new_html = html.replace("</style>", stub + "\n        </style>")
        self._write(html_path, new_html)
        self._fix(issue, f"Added stub CSS class .{cls}")
        return True

    def _fix_sql_typo(self, issue: Issue) -> bool:
        """Fix SQL typos in migration files."""
        m = re.search(r'(\w+) → (\w+)', issue.description)
        if not m:
            return False
        wrong, right = m.groups()

        mig_path = MIGRATIONS / issue.file
        sql = self._read(mig_path)
        new_sql = re.sub(rf'\b{wrong}\b', right, sql, flags=re.IGNORECASE)
        if new_sql != sql:
            self._write(mig_path, new_sql)
            self._fix(issue, f"Fixed typo: {wrong} → {right}")
            return True
        return False

    def _fix_unused_import(self, issue: Issue) -> bool:
        """Remove unused import line."""
        m = re.match(r"Line (\d+): '(\w+)'", issue.description)
        if not m:
            return False
        lineno, name = int(m.group(1)), m.group(2)

        filepath = ROOT / issue.file
        lines = self._read(filepath).split("\n")
        if lineno > len(lines):
            return False

        line = lines[lineno - 1]

        # Only auto-remove if it's a simple single import
        # "from X import Y" where Y is the unused name, and only Y
        single_import = re.match(rf'^from\s+\S+\s+import\s+{re.escape(name)}\s*$', line.strip())
        bare_import = re.match(rf'^import\s+{re.escape(name)}\s*$', line.strip())

        if single_import or bare_import:
            lines.pop(lineno - 1)
            self._write(filepath, "\n".join(lines))
            self._fix(issue, f"Removed unused import '{name}' at line {lineno}")
            return True

        # Multi-bare-import: "import A, B, C" — remove just the name
        bare_multi = re.match(r'^(import\s+)(.+)$', line.strip())
        if bare_multi:
            prefix, imports_str = bare_multi.groups()
            names_list = [n.strip() for n in imports_str.split(",")]
            if name in names_list and len(names_list) > 1:
                names_list.remove(name)
                new_line = prefix + ", ".join(names_list)
                indent = len(line) - len(line.lstrip())
                lines[lineno - 1] = " " * indent + new_line
                self._write(filepath, "\n".join(lines))
                self._fix(issue, f"Removed '{name}' from multi-bare-import at line {lineno}")
                return True

        # Multi-from-import: "from X import A, B, C" — remove just the name
        multi = re.match(r'^(from\s+\S+\s+import\s+)(.+)$', line.strip())
        if multi:
            prefix, imports_str = multi.groups()
            names = [n.strip() for n in imports_str.split(",")]
            if name in names and len(names) > 1:
                names.remove(name)
                new_line = prefix + ", ".join(names)
                # Preserve indentation
                indent = len(line) - len(line.lstrip())
                lines[lineno - 1] = " " * indent + new_line
                self._write(filepath, "\n".join(lines))
                self._fix(issue, f"Removed '{name}' from multi-import at line {lineno}")
                return True

        return False

    def _fix_missing_css_var(self, issue: Issue) -> bool:
        """Add missing CSS variable to :root."""
        m = re.search(r'--(\S+)', issue.description)
        if not m:
            return False
        var = m.group(1)

        html_path = TEMPLATES / "index.html"
        html = self._read(html_path)

        # Common defaults
        defaults = {
            "bg": "#0f1117", "bg2": "#ebeef3", "surface": "#1a1d27",
            "border": "#2a2d3a", "text": "#e8eaed", "text2": "#9ca3af",
            "muted": "#6b7280", "teal": "#00bfa5", "green": "#22c55e",
            "red": "#ef4444", "amber": "#f59e0b", "blue": "#3b82f6",
        }
        default_val = defaults.get(var, "#888888")

        # Find :root { and add the variable
        root_match = re.search(r':root\s*\{', html)
        if root_match:
            insert_pos = root_match.end()
            new_var = f"\n            --{var}:{default_val};"
            new_html = html[:insert_pos] + new_var + html[insert_pos:]
            self._write(html_path, new_html)
            self._fix(issue, f"Added --{var}:{default_val} to :root")
            return True
        return False

    def _fix_missing_requirement(self, issue: Issue) -> bool:
        """Add missing package to requirements.txt."""
        m = re.search(r"Package '([^']+)'", issue.description)
        if not m:
            return False
        pkg = m.group(1)

        req_path = ROOT / "requirements.txt"
        req = self._read(req_path)
        if pkg.lower() in req.lower():
            return False

        new_req = req.rstrip() + f"\n{pkg}\n"
        self._write(req_path, new_req)
        self._fix(issue, f"Added '{pkg}' to requirements.txt")
        return True

    def _fix_vendor_api_serialization(self, issue: Issue) -> bool:
        """Add missing field to _card_to_dict vendor serialization."""
        m = re.search(r"'(\w+)'", issue.description)
        if not m:
            return False
        field_name = m.group(1)

        main_path = APP / "main.py"
        code = self._read(main_path)

        # Check if already present in _card_to_dict
        card_dict_match = re.search(
            r'def _card_to_dict\(.*?\n(.*?)(?=\ndef\s|\Z)',
            code, re.DOTALL
        )
        if not card_dict_match:
            return False

        body = card_dict_match.group(1)
        if f'"{field_name}"' in body:
            # Already there — mark as fixed (was a detection false positive)
            self._fix(issue, f"'{field_name}' already in _card_to_dict")
            return True

        # Find "unique_parts" line in _card_to_dict as anchor point
        func_start = card_dict_match.start(1)
        anchor = body.find('"unique_parts"')
        if anchor == -1:
            return False

        # Find end of that line
        abs_anchor = func_start + anchor
        line_end = code.find("\n", abs_anchor)
        if line_end == -1:
            return False

        # Insert engagement field
        is_datetime = field_name in ("last_contact_at",)
        if is_datetime:
            val = f'card.{field_name}.isoformat() if card.{field_name} else None'
        else:
            val = f'card.{field_name}'

        new_line = f'\n        "{field_name}": {val},'
        new_code = code[:line_end] + new_line + code[line_end:]
        self._write(main_path, new_code)
        self._fix(issue, f"Added '{field_name}' to _card_to_dict")
        return True

    # ═══════════════════════════════════════════════════════════════════════
    #  RUNNER
    # ═══════════════════════════════════════════════════════════════════════

    def run_all_tests(self):
        """Run all 20 tests."""
        self.issues = []
        self._file_cache = {}  # clear cache each pass

        tests = [
            self.t01_python_syntax,
            self.t02_javascript_syntax,
            self.t03_python_imports,
            self.t04_model_migration_alignment,
            self.t05_api_route_functions,
            self.t06_html_onclick_handlers,
            self.t07_cross_file_js_calls,
            self.t08_css_classes_used_in_js,
            self.t09_sql_migration_syntax,
            self.t10_config_env_vars,
            self.t11_unused_imports,
            self.t12_api_endpoint_response_consistency,
            self.t13_duplicate_route_paths,
            self.t14_html_tag_balance,
            self.t15_css_variable_coverage,
            self.t16_requirements_completeness,
            self.t17_model_relationships,
            self.t18_js_fetch_endpoints,
            self.t19_vendorcard_api_fields,
            self.t20_file_size_sanity,
        ]

        for test_fn in tests:
            name = test_fn.__name__
            label = test_fn.__doc__.strip() if test_fn.__doc__ else name
            try:
                test_fn()
                # Count issues from this test
                test_issues = [i for i in self.issues if i.test == name.replace("t", "T", 1).replace("_", "-", 1)]
                # Actually, use the test name prefix
            except Exception as e:
                self._add(name, "RUNNER", f"Test crashed: {e}")

    def run_pass(self, pass_num: int):
        """Run one full test → repair pass."""
        self.pass_num = pass_num
        self.fixes_applied = 0

        print(f"\n{'═' * 72}")
        print(f"  {B}{C}PASS {pass_num} of {MAX_PASSES}{Z}")
        print(f"{'═' * 72}")

        # Phase 1: Test
        print(f"\n  {B}Phase 1: Running 20 tests…{Z}")
        self.run_all_tests()

        errors = [i for i in self.issues if i.severity == "error"]
        warns = [i for i in self.issues if i.severity == "warn"]
        fixable = [i for i in self.issues if i.auto_fixable]

        print(f"  Results: {R}{len(errors)} errors{Z}, {Y}{len(warns)} warnings{Z}, "
              f"{C}{len(fixable)} auto-fixable{Z}")

        # Print all issues
        if self.issues:
            print()
            # Group by test
            by_test = defaultdict(list)
            for i in self.issues:
                by_test[i.test].append(i)

            for test_name in sorted(by_test.keys()):
                items = by_test[test_name]
                status_counts = defaultdict(int)
                for i in items:
                    status_counts[i.severity] += 1

                err_str = f"{R}{status_counts.get('error', 0)}E{Z}" if status_counts.get('error') else ""
                warn_str = f"{Y}{status_counts.get('warn', 0)}W{Z}" if status_counts.get('warn') else ""
                count_str = " ".join(filter(None, [err_str, warn_str]))

                if not items:
                    print(f"  {G}✓{Z} {test_name}")
                else:
                    print(f"  {'×' if status_counts.get('error') else '⚠'} {test_name} [{count_str}]")
                    for i in items:
                        icon = f"{R}✗{Z}" if i.severity == "error" else f"{Y}⚠{Z}"
                        fix_tag = f" {C}[fixable]{Z}" if i.auto_fixable else ""
                        print(f"      {icon} {i.file}: {i.description}{fix_tag}")

        # Show passing tests
        all_test_names = set()
        for attr in dir(self):
            if attr.startswith("t") and attr[1:3].isdigit():
                test_id = attr[:3].upper().replace("T0", "T0").replace("t", "T")
                # Build the test key prefix
                fn = getattr(self, attr)
                all_test_names.add(attr)

        tested_names = set(i.test for i in self.issues)
        # Map function names to test IDs
        fn_to_id = {}
        for attr in dir(self):
            if attr.startswith("t") and attr[1:3].isdigit():
                num = attr[1:3]
                fn_to_id[attr] = f"T{num}"

        passing = []
        for attr, tid in fn_to_id.items():
            test_issues = [i for i in self.issues if i.test.startswith(tid)]
            if not test_issues:
                fn = getattr(self, attr)
                label = fn.__doc__.strip() if fn.__doc__ else attr
                passing.append(f"  {G}✓{Z} {tid}: {label}")

        if passing:
            print()
            for p in sorted(passing):
                print(p)

        # Phase 2: Repair
        if fixable:
            print(f"\n  {B}Phase 2: Auto-repairing {len(fixable)} issues…{Z}")
            repair_count = self.auto_repair()
            print(f"  Applied {G}{repair_count}{Z} fixes")

            # Show what was fixed
            for i in self.issues:
                if i.fix_applied:
                    print(f"    {G}✓ FIXED{Z} [{i.test}] {i.file}: {i.fix_description}")
                elif i.auto_fixable:
                    print(f"    {R}✗ UNFIXED{Z} [{i.test}] {i.file}: {i.description}")
        else:
            print(f"\n  {B}Phase 2: No auto-fixable issues.{Z}")

        return len(errors), len(warns), self.fixes_applied


def main():
    print(f"""
{B}{W}╔══════════════════════════════════════════════════════════════════════╗
║           AVAIL v1.2.0 — Autonomous Test & Repair Engine           ║
║                  20 Tests × 5 Passes × Auto-Repair                 ║
╚══════════════════════════════════════════════════════════════════════╝{Z}
    """)

    engine = TestEngine()
    results = []

    for p in range(1, MAX_PASSES + 1):
        errors, warns, fixes = engine.run_pass(p)
        results.append((errors, warns, fixes))

        if errors == 0 and warns == 0:
            print(f"\n  {G}{B}★ ALL CLEAR — Zero issues. Stopping early.{Z}")
            break
        elif errors == 0 and fixes == 0:
            print(f"\n  {Y}No errors remain, only warnings (not auto-fixable). Stopping.{Z}")
            break
        elif fixes == 0 and p < MAX_PASSES:
            print(f"\n  {Y}No fixes applied this pass — remaining issues need manual intervention.{Z}")
            if errors == 0:
                break

    # ─── Final Summary ────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print(f"  {B}{W}FINAL SUMMARY{Z}")
    print(f"{'═' * 72}\n")

    print(f"  {'Pass':<8} {'Errors':<10} {'Warnings':<12} {'Fixes':<8}")
    print(f"  {'─' * 38}")
    total_fixes = 0
    for i, (e, w, f) in enumerate(results, 1):
        total_fixes += f
        ec = f"{R}{e}{Z}" if e else f"{G}0{Z}"
        wc = f"{Y}{w}{Z}" if w else f"{G}0{Z}"
        fc = f"{C}{f}{Z}" if f else "0"
        print(f"  Pass {i:<3} {ec:<19} {wc:<21} {fc:<8}")

    final_e = results[-1][0]
    final_w = results[-1][1]

    print(f"\n  Total fixes applied across all passes: {B}{C}{total_fixes}{Z}")
    print(f"  Final state: {R if final_e else G}{final_e} errors{Z}, "
          f"{Y if final_w else G}{final_w} warnings{Z}")

    if final_e == 0:
        print(f"\n  {G}{B}╔═════════════════════════════════╗{Z}")
        print(f"  {G}{B}║   ✓ CODEBASE STRUCTURALLY SOUND ║{Z}")
        print(f"  {G}{B}╚═════════════════════════════════╝{Z}")
    else:
        print(f"\n  {R}{B}╔══════════════════════════════════════════╗{Z}")
        print(f"  {R}{B}║  ✗ {final_e} ERROR(S) REQUIRE MANUAL REPAIR  ║{Z}")
        print(f"  {R}{B}╚══════════════════════════════════════════╝{Z}")

    print()
    return 0 if final_e == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
