"""Syscalls: the complete set of primitives the model may invoke.

This file is the OS's security boundary. Everything the agent can do to your
machine and your network passes through here, which is why the set is small and
deliberately boring. Filesystem calls are jailed to the aiOS root; mutating
calls route through ctx.confirm() unless autonomy is set to 'full'.

Tool names use underscores, not dots -- OpenAI-compatible tool schemas restrict
names to [a-zA-Z0-9_-].
"""

import html
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import paths
from .apps import CAPABILITIES, AppError, validate

MAX_OUTPUT = 20000  # chars returned to the model from any one syscall
SMOKE_TIMEOUT = 20  # seconds allowed for the post-build smoke run

REGISTRY: dict = {}


def syscall(name: str, description: str, schema: dict, mutating: bool = False):
    """Register a syscall and its JSON schema."""

    def wrap(fn):
        REGISTRY[name] = {
            "name": name,
            "description": description,
            "schema": schema,
            "mutating": mutating,
            "fn": fn,
        }
        return fn

    return wrap


def tools() -> list:
    """The syscall table, as OpenAI-format tool definitions."""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["schema"],
            },
        }
        for s in REGISTRY.values()
    ]


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def _resolve(path: str) -> Path:
    """Resolve a path inside the aiOS root, or refuse."""
    p = Path(path)
    if not p.is_absolute():
        p = paths.HOME / p
    else:
        # Treat absolute paths as root-relative: "/apps/x" means the aiOS root,
        # not the host's /apps. The OS should not depend on the host layout.
        p = paths.HOME / p.relative_to("/")
    p = p.resolve()
    if not paths.inside(p):
        raise PermissionError(f"path escapes the aiOS root: {path}")
    return p


def dispatch(name: str, args: dict, ctx) -> str:
    """Execute a syscall. Always returns a string for the model."""
    entry = REGISTRY.get(name)
    if not entry:
        return f"error: no such syscall {name!r}"

    if entry["mutating"] and not ctx.allow(name, args):
        return "denied: the user declined this action"

    try:
        return _truncate(str(entry["fn"](args, ctx)))
    except PermissionError as e:
        return f"denied: {e}"
    except AppError as e:
        return f"error: {e}"
    except Exception as e:  # a broken syscall must not kill the kernel
        return f"error: {type(e).__name__}: {e}"


# --- filesystem ---------------------------------------------------------------


@syscall(
    "fs_read",
    "Read a text file from inside the aiOS root. Paths are root-relative, e.g. /apps/notes/main.py",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Root-relative path"}},
        "required": ["path"],
    },
)
def _fs_read(args, ctx):
    p = _resolve(args["path"])
    if not p.exists():
        return f"error: no such file: {paths.rel(p)}"
    if p.is_dir():
        return f"error: {paths.rel(p)} is a directory (use fs_list)"
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"error: {paths.rel(p)} is not a text file ({p.stat().st_size} bytes)"


@syscall(
    "fs_write",
    "Write a text file inside the aiOS root, creating parent directories as needed.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Root-relative path"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    mutating=True,
)
def _fs_write(args, ctx):
    p = _resolve(args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return f"wrote {len(args['content'])} chars to {paths.rel(p)}"


@syscall(
    "fs_list",
    "List a directory inside the aiOS root.",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Root-relative path, default /"}},
    },
)
def _fs_list(args, ctx):
    p = _resolve(args.get("path") or "/")
    if not p.is_dir():
        return f"error: not a directory: {paths.rel(p)}"
    rows = []
    for child in sorted(p.iterdir()):
        if child.is_dir():
            rows.append(f"{child.name}/")
        else:
            rows.append(f"{child.name}  ({child.stat().st_size}b)")
    return "\n".join(rows) or "(empty)"


# --- process ------------------------------------------------------------------


@syscall(
    "proc_run",
    "Run a shell command. Working directory is the aiOS root. Use for git, package tools, scripts.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds, default 60"},
        },
        "required": ["command"],
    },
    mutating=True,
)
def _proc_run(args, ctx):
    timeout = int(args.get("timeout") or 60)
    try:
        proc = subprocess.run(
            args["command"],
            shell=True,
            cwd=str(paths.HOME),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout}s"
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return f"[exit {proc.returncode}]\n{out.strip() or '(no output)'}"


# --- network ------------------------------------------------------------------


@syscall(
    "net_fetch",
    "Fetch a URL over HTTP(S) and return the response body as text.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "description": "GET or POST, default GET"},
            "body": {"type": "string", "description": "Request body for POST"},
            "headers": {"type": "object", "description": "Extra request headers"},
        },
        "required": ["url"],
    },
)
def _net_fetch(args, ctx):
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        return "error: only http(s) URLs are supported"

    headers = {"User-Agent": "aiOS/0.1"}
    headers.update(args.get("headers") or {})
    body = args.get("body")
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8") if body else None,
        headers=headers,
        method=(args.get("method") or "GET").upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read(2_000_000)
            ctype = r.headers.get("Content-Type", "")
            status = r.status
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}] {e.read()[:2000].decode('utf-8', 'replace')}"
    except urllib.error.URLError as e:
        return f"error: cannot reach {url} ({e.reason})"

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"[HTTP {status}] binary response ({len(raw)} bytes, {ctype})"

    if "html" in ctype:
        text = _strip_html(text)
    return f"[HTTP {status}]\n{text}"


class _Text(HTMLParser):
    """Reduce HTML to readable text -- the model does not need the markup."""

    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def _strip_html(raw: str) -> str:
    p = _Text()
    try:
        p.feed(raw)
    except Exception:
        return raw
    return "\n".join(p.parts)


class _DDG(HTMLParser):
    """Scrape DuckDuckGo's HTML endpoint. Keyless, so the OS can search with
    nothing but an OpenRouter key in the vault."""

    def __init__(self):
        super().__init__()
        self.results, self._href, self._grab = [], None, None
        self._buf, self._tag, self._depth = [], None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if self._grab:
            # Snippets contain nested markup (<b> around matched terms); track
            # depth so only the matching close tag ends the capture.
            if tag == self._tag:
                self._depth += 1
            return
        if tag == "a" and "result__a" in cls:
            self._href, self._grab, self._buf = a.get("href"), "title", []
            self._tag, self._depth = tag, 0
        elif "result__snippet" in cls:
            self._grab, self._buf = "snippet", []
            self._tag, self._depth = tag, 0

    def handle_data(self, data):
        if self._grab:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if not self._grab or tag != self._tag:
            return
        if self._depth:
            self._depth -= 1
            return
        text = html.unescape("".join(self._buf)).strip()
        if self._grab == "title" and text:
            self.results.append({"title": text, "url": _unwrap(self._href), "snippet": ""})
        elif self._grab == "snippet" and self.results:
            self.results[-1]["snippet"] = text
        self._grab, self._buf, self._tag = None, [], None


def _unwrap(href: str | None) -> str:
    """DDG wraps results in a /l/?uddg= redirect."""
    if not href:
        return ""
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        return q.get("uddg", [href])[0]
    return href


@syscall(
    "web_search",
    "Search the web and get back titles, URLs and snippets. Follow up with net_fetch to read a result.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "Max results, default 6"},
        },
        "required": ["query"],
    },
)
def _web_search(args, ctx):
    limit = int(args.get("limit") or 6)
    data = urllib.parse.urlencode({"q": args["query"]}).encode()
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/",
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; aiOS/0.1)",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            page = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return f"error: search failed ({e}) -- is this machine online?"

    p = _DDG()
    p.feed(page)
    if not p.results:
        return "no results"
    return "\n\n".join(
        f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
        for i, r in enumerate(p.results[:limit])
    )


# --- memory -------------------------------------------------------------------


@syscall(
    "mem_write",
    "Remember a durable fact about the user or their work. Use for things worth knowing on a future boot.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title; reusing one updates that note"},
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "content"],
    },
)
def _mem_write(args, ctx):
    p = ctx.memory.write(args["title"], args["content"], args.get("tags"))
    return f"remembered: {paths.rel(p)}"


@syscall(
    "mem_search",
    "Search stored memory for relevant facts.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    },
)
def _mem_search(args, ctx):
    hits = ctx.memory.search(args["query"], int(args.get("limit") or 5))
    if not hits:
        return "no matching memories"
    return "\n\n".join(f"## {h['title']} ({h['updated']})\n{h['content']}" for h in hits)


# --- userland -----------------------------------------------------------------


@syscall(
    "app_build",
    (
        "Install a new app into the OS, or rebuild an existing one. This is how the OS gains "
        "permanent capabilities. The app is a standalone Python program using only the standard "
        "library; it becomes a command the user can run by name on any future boot. Write real, "
        "complete, working code -- not a sketch."
    ),
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Command name: lowercase, dashes, e.g. 'portfolio'"},
            "description": {"type": "string", "description": "One line, shown in the app list"},
            "spec": {
                "type": "string",
                "description": "What the app does and why, in markdown. Stored so the app can be regenerated later.",
            },
            "code": {"type": "string", "description": "Complete main.py. stdlib only. Reads args from sys.argv."},
            "caps": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(CAPABILITIES)},
                "description": "Capabilities the app needs: " + ", ".join(f"{k} ({v})" for k, v in CAPABILITIES.items()),
            },
            "secrets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Vault key names to inject as env vars, e.g. ['OPENROUTER_API_KEY']",
            },
        },
        "required": ["name", "description", "spec", "code"],
    },
    mutating=True,
)
def _app_build(args, ctx):
    app = ctx.registry.install(
        name=args["name"],
        description=args["description"],
        spec=args["spec"],
        code=args["code"],
        caps=args.get("caps") or [],
        secrets=args.get("secrets") or [],
        model=ctx.model,
    )
    lines = [
        f"installed app '{app.name}' at {paths.rel(app.path)} "
        f"(caps: {', '.join(app.caps) or 'none'})."
    ]

    # An app that installs cleanly and then does nothing is the most common way
    # a generated program fails, and a model will happily report success unless
    # it is told otherwise. Check before handing it back.
    suspect = False

    for problem in validate(args["code"]):
        lines.append(f"  PROBLEM: {problem}")
        suspect = True

    smoke = ctx.registry.run(app.name, [], secrets=ctx.secrets, timeout=SMOKE_TIMEOUT)
    stdout = (smoke.get("stdout") or "").strip()
    stderr = (smoke.get("stderr") or "").strip()

    if smoke["code"] == 0 and not stdout:
        lines.append(
            "  PROBLEM: smoke run exited 0 but printed nothing. The app does not do "
            "anything when run. Do not report this as working."
        )
        suspect = True
    elif smoke["code"] != 0:
        lines.append(f"  smoke run (no arguments) exited {smoke['code']}: {stderr[:300] or '(no stderr)'}")
        lines.append("  If this app requires arguments, that may be expected; otherwise fix it.")
    else:
        lines.append(f"  smoke run ok: {stdout.splitlines()[0][:120]}")

    if suspect:
        lines.append("FIX THE CODE AND CALL app_build AGAIN. Do not tell the user it works.")
    else:
        lines.append("The user can now run it by name.")

    return "\n".join(lines)


@syscall(
    "app_list",
    "List the apps currently installed in the OS.",
    {"type": "object", "properties": {}},
)
def _app_list(args, ctx):
    apps = ctx.registry.all()
    if not apps:
        return "no apps installed yet"
    return "\n".join(
        f"{a.name} -- {a.description} [caps: {', '.join(a.caps) or 'none'}]" for a in apps
    )


@syscall(
    "app_run",
    "Run an installed app and capture its output.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    },
    mutating=True,
)
def _app_run(args, ctx):
    r = ctx.registry.run(args["name"], args.get("args") or [], secrets=ctx.secrets)
    out = r["stdout"].strip() or "(no output)"
    if r["stderr"].strip():
        out += "\n[stderr]\n" + r["stderr"].strip()
    return f"[exit {r['code']}]\n{out}"


@syscall(
    "app_source",
    "Read an installed app's source code and spec, to debug or improve it.",
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def _app_source(args, ctx):
    app = ctx.registry.get(args["name"])
    if not app:
        return f"error: no such app: {args['name']}"
    return f"# spec.md\n{app.spec}\n\n# main.py\n{app.code}"
