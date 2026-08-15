"""Run aiOS on Modal.

    modal run build/modal/aios_modal.py            # interactive session
    modal run build/modal/aios_modal.py::status    # what's in the volume

State (apps, memory, logs, the vault) lives on a Modal Volume, so the OS you
grow in one session is the OS you come back to. The system code ships in the
image and is re-synced on every start, which mirrors how provision.sh treats a
VPS: replace system/, never touch state.

The honest tradeoff: Modal is serverless, so this is not an always-on machine at
an IP. You get a container when you ask for one and it stops when you leave.
What you get in exchange is free compute, persistent state, and -- the real
reason to run here -- modal.Sandbox for confining generated apps.
"""

import subprocess
import sys
from pathlib import Path

import modal

PAYLOAD = Path(__file__).resolve().parents[2] / "root"

app = modal.App("aios")

# The OS root. Everything the agent creates persists here.
root_volume = modal.Volume.from_name("aios-root", create_if_missing=True)

# App data written by sandboxed apps, kept separate from the OS root so a
# misbehaving app cannot reach the vault.
app_data = modal.Volume.from_name("aios-app-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    # modal is installed so the kernel can use the Sandbox executor from inside.
    .pip_install("modal")
    .add_local_dir(PAYLOAD / "system", remote_path="/opt/aios-system")
    .add_local_file(PAYLOAD / "aios", remote_path="/opt/aios-launcher")
)

# Must expose OPENROUTER_API_KEY. Create with:
#   modal secret create openrouter-api-key OPENROUTER_API_KEY=sk-or-...
openrouter = modal.Secret.from_name("openrouter-api-key")

AIOS_HOME = "/aios"


def _sync_system() -> None:
    """Refresh the code in the volume from the image, preserving all state."""
    import os
    import shutil

    os.makedirs(AIOS_HOME, exist_ok=True)
    target = f"{AIOS_HOME}/system"
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree("/opt/aios-system", target)
    shutil.copy("/opt/aios-launcher", f"{AIOS_HOME}/aios")
    os.chmod(f"{AIOS_HOME}/aios", 0o755)
    for d in ("apps", "memory", "vault", "logs", "data"):
        os.makedirs(f"{AIOS_HOME}/{d}", exist_ok=True)
    os.chmod(f"{AIOS_HOME}/vault", 0o700)


@app.function(
    image=image,
    volumes={AIOS_HOME: root_volume},
    secrets=[openrouter],
    timeout=3600,
    cpu=1,
    memory=1024,
)
def shell():
    """An interactive aiOS session."""
    import os

    modal.interact()  # attach the local terminal to this container
    _sync_system()

    # Confine generated apps in Modal sandboxes rather than plain subprocesses.
    os.environ.setdefault("AIOS_SANDBOX", "modal")
    os.environ.setdefault("AIOS_SANDBOX_VOLUME", "aios-app-data")
    os.environ["AIOS_HOME"] = AIOS_HOME
    os.environ["TERM"] = os.environ.get("TERM", "xterm-256color")

    try:
        subprocess.run([sys.executable, f"{AIOS_HOME}/system/aios.py"], check=False)
    finally:
        # Persist whatever the session grew. Without this the apps and memories
        # created in the last minutes are lost when the container stops.
        root_volume.commit()


@app.function(image=image, volumes={AIOS_HOME: root_volume}, timeout=120)
def status():
    """Report what the persisted OS currently contains."""
    import json
    import os

    if not os.path.isdir(f"{AIOS_HOME}/apps"):
        return "volume is empty -- run a session first"

    lines = []
    apps_dir = f"{AIOS_HOME}/apps"
    for name in sorted(os.listdir(apps_dir)):
        manifest = os.path.join(apps_dir, name, "manifest.json")
        if os.path.exists(manifest):
            m = json.load(open(manifest))
            caps = ", ".join(m.get("caps") or []) or "none"
            lines.append(f"  {name:<20} v{m.get('version', 1)}  [{caps}]  {m.get('description', '')}")

    mem_dir = f"{AIOS_HOME}/memory"
    notes = sorted(os.listdir(mem_dir)) if os.path.isdir(mem_dir) else []
    vault = "sealed" if os.path.exists(f"{AIOS_HOME}/vault/keys.enc") else "none (key from environment)"

    return (
        f"apps ({len(lines)}):\n" + ("\n".join(lines) or "  (none)") +
        f"\n\nmemory: {len(notes)} notes\nvault:  {vault}"
    )


@app.function(image=image, volumes={AIOS_HOME: root_volume}, timeout=900)
def selftest():
    """Prove that a capability declaration is enforcement, not documentation.

    Installs the same network-probing program twice -- once declaring `net`,
    once not -- and runs both through the sandbox executor. If the manifest is
    real, the second one cannot reach the internet.
    """
    import os
    import sys

    os.environ["AIOS_HOME"] = AIOS_HOME  # paths binds this at import time
    _sync_system()
    sys.path.insert(0, f"{AIOS_HOME}/system")

    from kernel import apps as kapps
    from kernel import sandbox as ksandbox

    probe = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://example.com', timeout=15)\n"
        "    print('NETWORK_REACHABLE')\n"
        "except Exception as e:\n"
        "    print('NETWORK_BLOCKED', type(e).__name__)\n"
    )

    reg = kapps.Registry(executor=ksandbox.ModalExecutor(volume="aios-app-data"))
    reg.install("nettest-allowed", "probes the network, declares net", "spec", probe, caps=["net"])
    reg.install("nettest-denied", "probes the network, declares nothing", "spec", probe, caps=[])

    allowed = reg.run("nettest-allowed", timeout=120)
    denied = reg.run("nettest-denied", timeout=120)

    report = [
        "app declaring   net : " + (allowed["stdout"].strip() or allowed["stderr"].strip()[:120]),
        "app declaring  none : " + (denied["stdout"].strip() or denied["stderr"].strip()[:120]),
        "",
        f"network flag  allowed={allowed.get('network')}  denied={denied.get('network')}",
    ]

    ok = "NETWORK_REACHABLE" in allowed["stdout"] and "NETWORK_BLOCKED" in denied["stdout"]
    report.append("")
    report.append("RESULT: capability enforcement is REAL" if ok else "RESULT: FAILED -- see output above")

    root_volume.commit()
    return "\n".join(report)


@app.local_entrypoint()
def main():
    shell.remote()
