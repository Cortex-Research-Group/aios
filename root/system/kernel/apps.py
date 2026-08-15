"""Userland: the apps the OS writes for itself.

This is what makes aiOS an OS rather than a chat window. When you ask for a
capability it does not have, the kernel writes one into apps/ and it becomes a
permanent command -- available on the next boot, on another machine, forever.

Every app carries the spec that produced it, so the userland is regenerable:
point a better model at the same specs and the OS rebuilds itself.

Layout:
    apps/<name>/
        manifest.json   name, description, capabilities, provenance
        spec.md         the prompt that created it -- the real source
        main.py         the generated program
        data/           the app's own writable scratch space
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")

# Capabilities an app may declare. The kernel shows these at install time so a
# generated program cannot quietly acquire reach you did not agree to.
CAPABILITIES = {
    "net": "make outbound network requests",
    "fs": "read and write files inside the aiOS root",
    "proc": "run shell commands",
    "secrets": "read specific API keys from the vault",
}


class AppError(Exception):
    pass


@dataclass
class App:
    name: str
    description: str
    path: Path
    caps: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    created: str = ""
    model: str = ""

    @property
    def entry(self) -> Path:
        return self.path / "main.py"

    @property
    def spec(self) -> str:
        p = self.path / "spec.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    @property
    def code(self) -> str:
        return self.entry.read_text(encoding="utf-8") if self.entry.exists() else ""


class Registry:
    def __init__(self, root: Path | None = None, executor=None):
        self.root = Path(root) if root else paths.APPS
        self.root.mkdir(parents=True, exist_ok=True)
        # None runs apps as local subprocesses; an executor confines them.
        # See kernel/sandbox.py.
        self.executor = executor

    # --- reading -------------------------------------------------------------

    def all(self) -> list[App]:
        apps = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and (d / "manifest.json").exists():
                try:
                    apps.append(self._load(d))
                except (json.JSONDecodeError, OSError):
                    continue  # a corrupt app should not take the OS down
        return apps

    def _load(self, d: Path) -> App:
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        return App(
            name=m["name"],
            description=m.get("description", ""),
            path=d,
            caps=m.get("caps", []),
            secrets=m.get("secrets", []),
            created=m.get("created", ""),
            model=m.get("model", ""),
        )

    def get(self, name: str) -> App | None:
        d = self.root / name
        return self._load(d) if (d / "manifest.json").exists() else None

    # --- writing -------------------------------------------------------------

    def install(
        self,
        name: str,
        description: str,
        spec: str,
        code: str,
        caps: list[str] | None = None,
        secrets: list[str] | None = None,
        model: str = "",
    ) -> App:
        if not NAME_RE.match(name):
            raise AppError(
                f"invalid app name {name!r}: lowercase letters, digits and dashes, starting with a letter"
            )
        caps = caps or []
        unknown = set(caps) - set(CAPABILITIES)
        if unknown:
            raise AppError(f"unknown capabilities: {', '.join(sorted(unknown))}")

        d = self.root / name
        (d / "data").mkdir(parents=True, exist_ok=True)

        manifest = {
            "name": name,
            "description": description,
            "caps": caps,
            "secrets": secrets or [],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
            "version": 1,
        }
        existing = self.get(name)
        if existing:  # rebuilding an app bumps its version, keeps its data
            try:
                prev = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                manifest["version"] = int(prev.get("version", 1)) + 1
                manifest["created"] = prev.get("created", manifest["created"])
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        (d / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (d / "spec.md").write_text(spec.strip() + "\n", encoding="utf-8")
        (d / "main.py").write_text(code, encoding="utf-8")
        return self._load(d)

    def remove(self, name: str) -> bool:
        import shutil

        d = self.root / name
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True

    # --- running -------------------------------------------------------------

    def run(
        self,
        name: str,
        args: list[str] | None = None,
        secrets: dict | None = None,
        timeout: int = 120,
    ) -> dict:
        """Execute an app in a subprocess.

        Only the secrets the app declared in its manifest are injected -- an app
        that never asked for a key cannot read one.
        """
        app = self.get(name)
        if not app:
            raise AppError(f"no such app: {name}")
        if not app.entry.exists():
            raise AppError(f"app {name} has no main.py")

        if self.executor is not None:
            return self.executor.run(app, args or [], secrets=secrets, timeout=timeout)

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(app.path / "data"),
            "AIOS_HOME": str(paths.HOME),
            "AIOS_APP_DATA": str(app.path / "data"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key in app.secrets:
            if secrets and key in secrets:
                env[key] = secrets[key]

        try:
            proc = subprocess.run(
                [sys.executable, str(app.entry), *(args or [])],
                cwd=str(app.path / "data"),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": -1, "stdout": "", "stderr": f"timed out after {timeout}s"}

        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
