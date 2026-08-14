#!/bin/sh
# Install or update aiOS on a remote machine over SSH.
#
# One path for both targets -- a local QEMU VM is just a host on port 2222:
#
#   ./build/deploy.sh root@localhost -p 2222      # the dev VM
#   ./build/deploy.sh root@203.0.113.9            # a VPS
#
# Re-run any time to push code changes. Apps, memory and the vault on the target
# are left untouched.
set -eu

[ $# -lt 1 ] && { echo "usage: $0 user@host [ssh args...]" >&2; exit 1; }
TARGET="$1"; shift
SSH_ARGS="$*"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\033[36m::\033[0m %s\n' "$1"; }

say "shipping payload to $TARGET"
# tar over ssh: no rsync dependency on the target, which a stock Alpine lacks.
tar -C "$REPO" -czf - root build/provision.sh \
    | ssh $SSH_ARGS "$TARGET" 'rm -rf /tmp/aios-src && mkdir -p /tmp/aios-src && tar -C /tmp/aios-src -xzf -'

say "provisioning"
ssh $SSH_ARGS "$TARGET" 'sh /tmp/aios-src/build/provision.sh && rm -rf /tmp/aios-src'

echo
say "aiOS is live on $TARGET"
echo "   ssh $SSH_ARGS $TARGET -t aios"
