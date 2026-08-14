#!/bin/sh
# Turn a fresh Linux box into an aiOS machine.
#
# Runs on the target, not on your laptop. Idempotent -- safe to re-run to push
# an updated payload. Works on Alpine (apk) and Debian/Ubuntu (apt).
#
# The only dependency aiOS has is python3. That is the whole point: nothing to
# pip install means nothing that can fail at boot on a machine with no network.
set -eu

AIOS_DIR="${AIOS_DIR:-/opt/aios}"
PAYLOAD="${PAYLOAD:-$(cd "$(dirname "$0")/../root" 2>/dev/null && pwd || echo /tmp/aios-payload)}"
CONSOLE="${CONSOLE:-1}"   # set 0 to skip the autologin-into-aiOS setup
BIN_DIR="${BIN_DIR:-/usr/local/bin}"

say() { printf '\033[36m::\033[0m %s\n' "$1"; }

# --- python -------------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    say "installing python3"
    if command -v apk >/dev/null 2>&1; then
        apk add --no-cache python3 ca-certificates
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq python3 ca-certificates
    else
        echo "no supported package manager (need apk or apt-get)" >&2
        exit 1
    fi
fi
say "python $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"

# --- payload ------------------------------------------------------------------

if [ ! -d "$PAYLOAD/system" ]; then
    echo "no aiOS payload at $PAYLOAD (expected $PAYLOAD/system)" >&2
    exit 1
fi

say "installing aiOS to $AIOS_DIR"
mkdir -p "$AIOS_DIR"
# Preserve state across re-provisioning: only the system tree is replaced.
rm -rf "$AIOS_DIR/system"
cp -R "$PAYLOAD/system" "$AIOS_DIR/system"
cp "$PAYLOAD/aios" "$AIOS_DIR/aios"
chmod +x "$AIOS_DIR/aios"
for d in apps memory vault logs data; do mkdir -p "$AIOS_DIR/$d"; done
chmod 700 "$AIOS_DIR/vault"

mkdir -p "$BIN_DIR"
ln -sf "$AIOS_DIR/aios" "$BIN_DIR/aios"
say "aios is on PATH ($BIN_DIR/aios)"

# --- console ------------------------------------------------------------------
# Make aiOS the thing you get when the machine boots, rather than a shell you
# then have to type a command into. This is what makes it feel like an OS.

if [ "$CONSOLE" = "1" ]; then
    PROFILE=/root/.profile
    MARK="# --- aios autostart ---"
    if ! grep -qF "$MARK" "$PROFILE" 2>/dev/null; then
        say "setting aiOS as the console session"
        cat >> "$PROFILE" <<EOF

$MARK
# Launch aiOS on the physical console only. SSH sessions get a normal shell so
# the machine stays debuggable when the agent is wedged.
if [ -z "\${SSH_TTY:-}" ] && [ -t 0 ]; then
    case "\$(tty)" in
        /dev/tty1|/dev/console|/dev/ttyS0) exec $AIOS_DIR/aios ;;
    esac
fi
EOF
    fi

    # Autologin on tty1 and serial, so a VM or a booted stick lands straight in.
    if [ -f /etc/inittab ] && ! grep -q "autologin root" /etc/inittab; then
        say "enabling console autologin"
        sed -i 's|^tty1::respawn:.*|tty1::respawn:/sbin/agetty --autologin root --noclear tty1 linux|' /etc/inittab
        sed -i 's|^ttyS0::respawn:.*|ttyS0::respawn:/sbin/agetty --autologin root -L ttyS0 115200 vt100|' /etc/inittab
    fi
fi

say "done"
echo
echo "  aiOS installed at $AIOS_DIR"
echo "  run it with:  aios"
echo
