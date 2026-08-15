"""Optional confined execution for generated apps.

The kernel writes code that nobody reviewed and then runs it. Locally that code
runs as an ordinary subprocess, which means the capability manifest is honest
disclosure but not enforcement -- an app with `proc` can do whatever your user
account can do.

When Modal is configured, this module turns those declarations into something
the app cannot argue with:

    no `net` capability   ->  block_network=True, the container has no route out
    no `fs` capability    ->  no volume mounted, nothing of yours is reachable
    declared secrets only ->  the only keys in its environment
    cpu / memory / timeout->  hard ceilings

modal is imported lazily and only when this backend is selected, so the kernel
keeps its zero-dependency guarantee on a stick, a VPS and a bare VM.
"""

import os

DEFAULT_APP = "aios-sandboxes"
DEFAULT_PYTHON = "3.12"


class SandboxError(Exception):
    pass


def available() -> bool:
    try:
        import modal  # noqa: F401

        return True
    except ImportError:
        return False


def from_config(config: dict):
    """Build the executor named by config, or None for plain subprocesses."""
    backend = (config or {}).get("sandbox", "off")
    if backend in ("off", None, ""):
        return None
    if backend == "modal":
        return ModalExecutor(
            volume=(config or {}).get("sandbox_volume"),
            cpu=(config or {}).get("sandbox_cpu", 0.5),
            memory=(config or {}).get("sandbox_memory", 512),
        )
    raise SandboxError(f"unknown sandbox backend: {backend}")


class ModalExecutor:
    """Runs an app inside a Modal sandbox, enforcing its declared capabilities."""

    name = "modal"

    def __init__(self, volume: str | None = None, cpu: float = 0.5, memory: int = 512,
                 python_version: str = DEFAULT_PYTHON, app_name: str = DEFAULT_APP):
        if not available():
            raise SandboxError(
                "the modal package is not installed here -- `pip install modal`, "
                "or set /sandbox off to run apps as local subprocesses"
            )
        self.volume_name = volume
        self.cpu = cpu
        self.memory = memory
        self.python_version = python_version
        self.app_name = app_name
        self._app = None
        self._image = None

    def _connect(self):
        import modal

        if self._app is None:
            self._app = modal.App.lookup(self.app_name, create_if_missing=True)
            # Generated apps are stdlib-only by contract, so a bare python image
            # is all they can legitimately need.
            self._image = modal.Image.debian_slim(python_version=self.python_version)
        return self._app, self._image

    def run(self, app, args=None, secrets=None, timeout: int = 120) -> dict:
        import modal

        modal_app, image = self._connect()

        # The manifest becomes the sandbox's shape.
        block_network = "net" not in app.caps
        env = {k: secrets[k] for k in app.secrets if secrets and k in secrets}

        volumes = {}
        if self.volume_name and "fs" in app.caps:
            volumes["/data"] = modal.Volume.from_name(self.volume_name, create_if_missing=True)
            env["AIOS_APP_DATA"] = f"/data/{app.name}"

        sb = None
        try:
            sb = modal.Sandbox.create(
                "sleep", "infinity",
                app=modal_app,
                image=image,
                cpu=self.cpu,
                memory=self.memory,
                timeout=timeout + 60,
                block_network=block_network,
                volumes=volumes,
                workdir="/tmp",
            )
            if "AIOS_APP_DATA" in env:
                sb.exec("mkdir", "-p", env["AIOS_APP_DATA"]).wait()

            # The program is handed over as an argument rather than written to a
            # file: Modal retired the Sandbox filesystem API, and `python -c`
            # needs no filesystem at all. Trailing arguments land in sys.argv[1:]
            # exactly where a generated app expects them.
            proc = sb.exec(
                "python", "-u", "-c", app.code, *(args or []),
                env=env or None,
                timeout=timeout,
            )
            stdout = proc.stdout.read()
            stderr = proc.stderr.read()
            proc.wait()
            code = proc.returncode

            return {
                "ok": code == 0,
                "code": code,
                "stdout": stdout,
                "stderr": stderr,
                "sandboxed": True,
                "network": "blocked" if block_network else "allowed",
            }
        except Exception as e:
            return {
                "ok": False,
                "code": -1,
                "stdout": "",
                "stderr": f"sandbox failed: {type(e).__name__}: {e}",
                "sandboxed": True,
            }
        finally:
            if sb is not None:
                try:
                    sb.terminate()
                except Exception:
                    pass  # a leaked sandbox will hit its own timeout
