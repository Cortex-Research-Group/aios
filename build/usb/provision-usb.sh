#!/bin/sh
# First-boot setup for aiOS on a stick made by make-usb.sh.
#
# Run this ONCE, by hand, at the console after the stick's first boot:
#
#   login: root          (Alpine's live media default -- no password)
#   # setup-interfaces -a && udhcpc                    (only if not on DHCP)
#   # mount -o ro /media/*/AIOSBOOT/provision-usb.sh /tmp 2>/dev/null; sh /media/*/provision-usb.sh
#
# make-usb.sh prints the exact command for the stick it just built. After this
# script finishes and reboots, every later boot is unattended: no login, no
# network, aiOS on the console within seconds.
#
# Every fact this script relies on about Alpine's lbu/apk mechanisms was
# checked against the actual alpine-conf package source (lbu, lbu_commit,
# setup-apkcache), not documentation or memory -- see HANDOFF.md. What is
# NOT verified is that Alpine's boot-time apkovl scan actually finds and
# restores this on a real reboot: that needs a real boot, which this session
# had no way to perform. Say so if you try it and it doesn't come back clean.
set -eu

LABEL="AIOSDATA"
MNT="/media/$LABEL"

say() { printf '\033[36m::\033[0m %s\n' "$1"; }
die() { printf '\033[31m!!\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this as root"

# --- locate and mount the data partition ---------------------------------

say "looking for the $LABEL partition"
DEV="$(blkid -L "$LABEL" 2>/dev/null || true)"
[ -n "$DEV" ] || die "no partition labeled $LABEL -- is this a stick make-usb.sh built?"
mkdir -p "$MNT"
if ! mountpoint -q "$MNT"; then
    mount "$DEV" "$MNT" || die "could not mount $DEV at $MNT"
fi

AIOS_DIR="$MNT/root"
[ -d "$AIOS_DIR/system" ] || die "no aiOS payload at $AIOS_DIR (expected $AIOS_DIR/system)"
chmod +x "$AIOS_DIR/aios"
say "found aiOS payload at $AIOS_DIR"

# --- apk cache lives on the stick, so python3 needs the network only this
# --- one time -- setup-apkcache's actual effect, per its source, is just
# --- this symlink; replicated directly rather than driving the interactive
# --- wizard.
say "caching apk packages on $MNT so later boots need no network"
mkdir -p "$MNT/apk-cache"
[ -L /etc/apk/cache ] && rm -f /etc/apk/cache
mkdir -p /etc/apk
ln -s "$MNT/apk-cache" /etc/apk/cache

say "installing python3 (needs network right now, cached for every boot after)"
apk update
apk add --no-cache python3 ca-certificates

# --- console autostart -----------------------------------------------------
# /root/.profile is NOT in lbu's default backup scope (only /etc/, plus a
# setup-alpine-created user's home -- neither applies here), so it would not
# survive the reboot this script ends with. /etc/profile is under /etc/ and
# is read by root's login shell too, so the hook goes there instead.

PROFILE=/etc/profile
MARK="# --- aios autostart ---"
if ! grep -qF "$MARK" "$PROFILE" 2>/dev/null; then
    say "setting aiOS as the console session"
    cat >> "$PROFILE" <<EOF

$MARK
# Launch aiOS on the physical console only. A login over ssh or serial from
# somewhere else gets a normal shell, so a wedged agent never locks out the
# only way back into the machine.
if [ -z "\${SSH_TTY:-}" ] && [ -t 0 ]; then
    case "\$(tty)" in
        /dev/tty1|/dev/console|/dev/ttyS0)
            mountpoint -q "$MNT" || mount "$DEV" "$MNT" 2>/dev/null
            exec $AIOS_DIR/aios
            ;;
    esac
fi
EOF
fi

if [ -f /etc/inittab ] && ! grep -q "autologin root" /etc/inittab; then
    say "enabling console autologin"
    sed -i 's|^tty1::respawn:.*|tty1::respawn:/sbin/agetty --autologin root --noclear tty1 linux|' /etc/inittab
    sed -i 's|^ttyS0::respawn:.*|ttyS0::respawn:/sbin/agetty --autologin root -L ttyS0 115200 vt100|' /etc/inittab
fi

# --- re-mount the data partition on every future boot, before autologin
# --- needs it. /etc/local.d/*.start is Alpine's documented "run this at
# --- boot" mechanism (openrc's 'local' service); harmless if /media/$LABEL
# --- is already mounted by the time this runs.
say "installing the boot-time remount hook"
mkdir -p /etc/local.d
cat > /etc/local.d/aios.start <<EOF
#!/bin/sh
DEV="\$(blkid -L "$LABEL" 2>/dev/null)"
[ -n "\$DEV" ] || exit 0
mkdir -p "$MNT"
mountpoint -q "$MNT" || mount "\$DEV" "$MNT" 2>/dev/null
EOF
chmod +x /etc/local.d/aios.start
rc-update add local default >/dev/null 2>&1 || true

# --- persist everything above across the reboot -----------------------------
# LBU_BACKUPDIR, per lbu_commit's actual source, skips the /media/<floppy|usb>
# convention entirely and writes straight to the directory given -- exactly
# what a partition with a name of our own choosing needs. (The commit flag
# that looks like a destination, -d, is not one -- it means "delete old apk
# overlay files"; see HANDOFF.md for how that was confirmed.)
say "persisting configuration to $MNT (survives reboot)"
cat > /etc/lbu/lbu.conf <<EOF
LBU_BACKUPDIR=$MNT
EOF
lbu commit

say "done -- rebooting in 5s. This is the last time this stick needs a login."
sleep 5
reboot
