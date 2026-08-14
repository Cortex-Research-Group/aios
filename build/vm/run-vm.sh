#!/bin/sh
# Boot a local Alpine VM to run aiOS in.
#
# This is the development loop for the bootable target: same distro, same init,
# same console as a real USB boot, but it starts in three seconds and you can
# throw it away. Prove the image here, then write it to a stick.
#
#   ./run-vm.sh          boot in this terminal (serial console)
#   ./run-vm.sh -d       boot in the background, ssh in on port 2222
#   ./run-vm.sh reset    discard the VM disk and start clean
#   ./run-vm.sh ssh      shell into a running VM
#
# Quit the serial console with:  Ctrl-a x
set -eu

ALPINE_VER="3.22.4"
IMAGE="nocloud_alpine-${ALPINE_VER}-x86_64-bios-cloudinit-r0.qcow2"
URL="https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/${IMAGE}"

HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HERE/.cache"
DISK="$CACHE/aios-vm.qcow2"
SEED="$CACHE/seed.iso"
KEY="$CACHE/id_ed25519"
PIDFILE="$CACHE/vm.pid"
SSH_PORT=2222

MODE="${1:-boot}"   # captured before the arg list is rebuilt for qemu

say() { printf '\033[36m::\033[0m %s\n' "$1"; }
die() { printf '\033[31m!!\033[0m %s\n' "$1" >&2; exit 1; }

command -v qemu-system-x86_64 >/dev/null || die "qemu not found -- brew install qemu"
mkdir -p "$CACHE"

ssh_in() {
    exec ssh -i "$KEY" -p "$SSH_PORT" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        root@localhost "$@"
}

case "$MODE" in
    reset)
        rm -f "$DISK" "$SEED"
        say "VM disk discarded -- next boot is clean"
        exit 0
        ;;
    ssh)
        shift
        ssh_in "$@"
        ;;
    stop)
        [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && say "VM stopped" || say "no VM running"
        rm -f "$PIDFILE"
        exit 0
        ;;
esac

# --- ssh key ------------------------------------------------------------------

if [ ! -f "$KEY" ]; then
    say "generating a VM ssh key"
    ssh-keygen -t ed25519 -N "" -f "$KEY" -C aios-vm >/dev/null
fi

# --- base image ---------------------------------------------------------------

if [ ! -f "$CACHE/$IMAGE" ]; then
    say "downloading Alpine $ALPINE_VER cloud image (~60MB)"
    curl -fL --progress-bar -o "$CACHE/$IMAGE.part" "$URL"
    mv "$CACHE/$IMAGE.part" "$CACHE/$IMAGE"
fi

# The VM disk is a copy-on-write overlay on the pristine download, so a reset
# costs nothing and the base image is never dirtied.
if [ ! -f "$DISK" ]; then
    say "creating VM disk"
    qemu-img create -f qcow2 -F qcow2 -b "$CACHE/$IMAGE" "$DISK" 8G >/dev/null
fi

# --- cloud-init seed ----------------------------------------------------------

if [ ! -f "$SEED" ]; then
    say "building cloud-init seed"
    SEEDDIR="$CACHE/seed"
    rm -rf "$SEEDDIR"; mkdir -p "$SEEDDIR"

    printf 'instance-id: aios-vm\nlocal-hostname: aios\n' > "$SEEDDIR/meta-data"
    cat > "$SEEDDIR/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - $(cat "$KEY.pub")
packages:
  - python3
runcmd:
  - [ sh, -c, "sed -i 's|^#\\\\?PermitRootLogin.*|PermitRootLogin prohibit-password|' /etc/ssh/sshd_config" ]
  - [ rc-service, sshd, restart ]
EOF

    if command -v hdiutil >/dev/null 2>&1; then          # macOS
        hdiutil makehybrid -iso -joliet -default-volume-name CIDATA \
            -o "$CACHE/seed" "$SEEDDIR" >/dev/null
        [ -f "$CACHE/seed.iso" ] || mv "$CACHE/seed.cdr" "$SEED" 2>/dev/null || true
    elif command -v xorriso >/dev/null 2>&1; then        # Linux
        xorriso -as mkisofs -o "$SEED" -V CIDATA -J -r "$SEEDDIR" >/dev/null 2>&1
    else
        die "need hdiutil (macOS) or xorriso to build the seed image"
    fi
fi

# --- boot ---------------------------------------------------------------------

ACCEL=tcg; CPU=max
[ "$(uname -s)" = "Darwin" ] && { ACCEL=hvf; CPU=host; }
[ -e /dev/kvm ] && { ACCEL=kvm; CPU=host; }

set -- \
    -machine q35,accel=$ACCEL -cpu $CPU -smp 2 -m 2048 \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -drive file="$SEED",if=virtio,format=raw,readonly=on \
    -nic user,model=virtio-net-pci,hostfwd=tcp::${SSH_PORT}-:22 \
    -display none

if [ "$MODE" = "-d" ]; then
    say "booting in the background"
    qemu-system-x86_64 "$@" -serial file:"$CACHE/console.log" -daemonize -pidfile "$PIDFILE"
    say "waiting for ssh on port $SSH_PORT"
    for i in $(seq 1 60); do
        if ssh -i "$KEY" -p $SSH_PORT -o StrictHostKeyChecking=no \
               -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
               -o ConnectTimeout=2 root@localhost true 2>/dev/null; then
            echo
            say "VM is up"
            echo "   deploy aiOS:  ./build/deploy.sh root@localhost \"-i $KEY -p $SSH_PORT\""
            echo "   shell in:     $0 ssh"
            echo "   console log:  $CACHE/console.log"
            exit 0
        fi
        printf '.'; sleep 2
    done
    die "VM did not come up -- see $CACHE/console.log"
fi

say "booting (Ctrl-a x to quit)"
exec qemu-system-x86_64 "$@" -serial mon:stdio
