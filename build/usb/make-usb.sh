#!/bin/sh
# Build a bootable aiOS USB stick.
#
#   ./make-usb.sh --image-only out.img     build and verify a disk image, write nothing
#   ./make-usb.sh --device /dev/diskN       build and write to a real device (macOS)
#   ./make-usb.sh --device /dev/sdX         build and write to a real device (Linux)
#
# The image is: an unmodified, checksum-verified official Alpine Standard ISO
# (already a hybrid BIOS+UEFI image -- this is exactly what Alpine's own docs
# describe as dd-able), immediately followed by a FAT32 partition carrying the
# aiOS payload, referenced by one new MBR partition-table entry written into a
# slot the ISO leaves at all zero. mkusb-mbr.py does that edit and proves by
# assertion that nothing else in the table moved.
#
# --image-only is not a lesser mode: it is the one this script can actually
# prove correct. It was built by assembling exactly this way and inspecting
# the result -- fdisk on the composite file, then attaching it as a virtual
# disk with hdiutil and mounting the data partition to diff its contents
# against the source tree byte-for-byte. What was NOT possible in that
# session: booting it. No qemu (fails building on this class of Mac -- see
# HANDOFF.md), no Docker, and real hardware was out of scope without someone
# at the keyboard. Say so plainly until someone reports back that a stick
# built this way actually boots.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CACHE="$HERE/.cache"
DATA_MB="${AIOS_USB_DATA_MB:-512}"
RELEASES_URL="https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/latest-releases.yaml"

say() { printf '\033[36m::\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$1" >&2; }
die() { printf '\033[31m!!\033[0m %s\n' "$1" >&2; exit 1; }

MODE=""
OUT=""
DEVICE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --image-only) MODE="image"; OUT="$2"; shift 2 ;;
        --device) MODE="device"; DEVICE="$2"; shift 2 ;;
        *) die "unknown argument: $1 (use --image-only <path> or --device <dev>)" ;;
    esac
done
[ -n "$MODE" ] || die "usage: $0 --image-only <path> | --device <dev>"

command -v python3 >/dev/null || die "python3 not found"
command -v git >/dev/null || die "git not found"
DOSFS="$(brew --prefix dosfstools 2>/dev/null)/sbin" || die "dosfstools not found -- brew install dosfstools"
command -v mcopy >/dev/null || die "mcopy not found -- brew install mtools"
[ -x "$DOSFS/mkfs.fat" ] || die "mkfs.fat not found at $DOSFS -- brew install dosfstools"

mkdir -p "$CACHE"

# --- fetch the current Alpine Standard release, checksum and all -----------
# Not a pinned version: the manifest is fetched fresh every run and used as
# the source of truth for both the filename and the sha256, so this never
# silently drifts the way a hardcoded version string does.

say "checking the current Alpine Standard x86_64 release"
MANIFEST="$(curl -fsL "$RELEASES_URL")" || die "could not reach $RELEASES_URL"
read -r ISO_FILE ISO_SHA256 <<PYEOF
$(printf '%s' "$MANIFEST" | python3 -c '
import sys, re
text = sys.stdin.read()
for block in text.split("\n-\n"):
    if "flavor: alpine-standard" in block and "arch: x86_64" in block:
        file = re.search(r"file: (\S+)", block).group(1)
        sha = re.search(r"sha256: (\S+)", block).group(1)
        print(file, sha)
        break
else:
    sys.exit("no alpine-standard x86_64 entry in manifest")
')
PYEOF
[ -n "${ISO_FILE:-}" ] || die "could not find alpine-standard in the release manifest"
say "current release: $ISO_FILE"

ISO_PATH="$CACHE/$ISO_FILE"
ISO_URL="https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/$ISO_FILE"

if [ -f "$ISO_PATH" ] && [ "$(shasum -a 256 "$ISO_PATH" | cut -d' ' -f1)" = "$ISO_SHA256" ]; then
    say "using cached, checksum-verified $ISO_FILE"
else
    say "downloading $ISO_FILE (~270MB)"
    curl -fL --progress-bar -o "$ISO_PATH.part" "$ISO_URL" || die "download failed"
    GOT="$(shasum -a 256 "$ISO_PATH.part" | cut -d' ' -f1)"
    [ "$GOT" = "$ISO_SHA256" ] || die "checksum mismatch: got $GOT, expected $ISO_SHA256 -- refusing a corrupt or tampered ISO"
    mv "$ISO_PATH.part" "$ISO_PATH"
    say "checksum verified"
fi

# --- build the data partition ----------------------------------------------

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say "assembling the aiOS payload (tracked files only, via git archive)"
mkdir -p "$WORK/payload"
git -C "$REPO" archive HEAD root | tar -x -C "$WORK/payload"
cp "$HERE/provision-usb.sh" "$WORK/payload/"
cat > "$WORK/payload/README.txt" <<'EOF'
aiOS on a stick -- first boot, once:

  login: root
  (no password on Alpine's live media)

  if this machine needs a manual network setup:
    setup-interfaces -a && udhcpc

  then:
    sh /media/AIOSDATA/provision-usb.sh

That installs python3, sets aiOS as the console session, and commits the
config so every later boot needs no login and no network at all.
EOF

DATA_IMG="$WORK/data.img"
say "building a ${DATA_MB}MiB FAT32 data partition"
: > "$DATA_IMG"
truncate -s "${DATA_MB}M" "$DATA_IMG" 2>/dev/null || dd if=/dev/zero of="$DATA_IMG" bs=1m count="$DATA_MB" status=none
"$DOSFS/mkfs.fat" -F 32 -n AIOSDATA "$DATA_IMG" >/dev/null

# mtools writes into the image file directly -- no mount, no sudo.
(cd "$WORK/payload" && mcopy -s -i "$DATA_IMG" -- * ::) \
    || die "mcopy failed to populate the data partition"

# --- assemble: ISO bytes, then the data partition, then one MBR edit -------

FINAL="${OUT:-$WORK/aios-usb.img}"
say "patching in the data partition's MBR entry"
python3 "$HERE/mkusb-mbr.py" "$ISO_PATH" "$DATA_IMG" "$FINAL" || die "image assembly failed"

# --- verify what can be verified without booting anything -------------------

if command -v hdiutil >/dev/null 2>&1; then
    say "verifying the assembled image (mount + byte-diff against the payload)"
    ATTACH="$(hdiutil attach -imagekey diskimage-class=CRawDiskImage -nomount "$FINAL")"
    DATA_DEV="$(printf '%s\n' "$ATTACH" | awk '/Windows_FAT_32|Microsoft Basic Data/{print $1; exit}')"
    [ -n "$DATA_DEV" ] || { hdiutil detach "$(printf '%s\n' "$ATTACH" | head -1 | cut -d' ' -f1 | sed 's/s[0-9]*$//')" >/dev/null 2>&1; die "verification failed: no FAT32 data partition found in the assembled image"; }
    WHOLE_DISK="$(printf '%s\n' "$DATA_DEV" | sed 's/s[0-9]*$//')"

    diskutil mount "$DATA_DEV" >/dev/null
    MNT="$(diskutil info "$DATA_DEV" | awk -F': +' '/Mount Point/{print $2}')"
    if [ -z "$MNT" ] || ! diff -rq "$WORK/payload/root" "$MNT/root" >/dev/null 2>&1; then
        diskutil eject "$WHOLE_DISK" >/dev/null 2>&1 || true
        die "verification failed: mounted data partition does not match the payload byte-for-byte"
    fi
    diskutil eject "$WHOLE_DISK" >/dev/null 2>&1
    say "verified: data partition mounts and matches the source tree exactly"
else
    warn "no hdiutil on this platform -- image built but not mount-verified"
fi

say "image ready: $FINAL ($(du -h "$FINAL" | cut -f1))"

if [ "$MODE" = "image" ]; then
    echo
    echo "  Not written to any device. To use it:"
    echo "    macOS:  diskutil unmountDisk /dev/diskN && sudo dd if=$FINAL of=/dev/rdiskN bs=1m"
    echo "    Linux:  sudo dd if=$FINAL of=/dev/sdX bs=4M status=progress && sync"
    echo "  Then boot it and follow the README.txt on the AIOSDATA partition."
    echo
    exit 0
fi

# --- device write, heavily gated ---------------------------------------------
# This is the one truly irreversible step in this whole tool, and the one
# this session could not test even once: no qemu, no Docker, and writing to
# a real disk to "just see" is not a test, it's a bet with someone's data.
# Every gate below exists because a wrong device argument here is not a bug
# report, it's a wiped drive -- possibly the wrong one.

case "$(uname -s)" in
    Darwin)
        [ -e "$DEVICE" ] || die "no such device: $DEVICE"
        INFO="$(diskutil info "$DEVICE" 2>&1)" || die "diskutil does not recognize $DEVICE"
        BOOT_DISK="$(diskutil info / | awk -F': +' '/Part of Whole/{print $2}')"
        THIS_DISK="$(printf '%s\n' "$INFO" | awk -F': +' '/Part of Whole/{print $2}')"
        LOCATION="$(printf '%s\n' "$INFO" | awk -F': +' '/Device Location/{print $2}')"

        [ "$THIS_DISK" != "$BOOT_DISK" ] || die "refusing: $DEVICE is this machine's boot disk"
        [ "$LOCATION" = "External" ] || die "refusing: $DEVICE reports as $LOCATION, not External"

        echo
        echo "$INFO"
        echo
        warn "EVERYTHING on $DEVICE (disk $THIS_DISK) will be permanently erased."
        printf 'Type the disk identifier exactly (e.g. "disk4") to continue: '
        read -r CONFIRM
        [ "$CONFIRM" = "$THIS_DISK" ] || die "confirmation did not match -- nothing written"

        diskutil unmountDisk "$DEVICE" >/dev/null || die "could not unmount $DEVICE"
        RAW="$(printf '%s\n' "$DEVICE" | sed 's|/dev/disk|/dev/rdisk|')"
        say "writing $FINAL to $RAW"
        dd if="$FINAL" of="$RAW" bs=1m
        diskutil eject "$DEVICE" >/dev/null 2>&1 || true
        say "done. Remove the stick, put it in the target machine, and boot from it."
        ;;
    Linux)
        [ -b "$DEVICE" ] || die "no such block device: $DEVICE"
        ROOT_DEV="$(findmnt -n -o SOURCE / || true)"
        [ "$DEVICE" != "$ROOT_DEV" ] || die "refusing: $DEVICE looks like the root filesystem's device"
        BASENAME="$(basename "$DEVICE")"
        REMOVABLE_FLAG="/sys/block/$BASENAME/removable"
        [ -f "$REMOVABLE_FLAG" ] && [ "$(cat "$REMOVABLE_FLAG")" = "1" ] \
            || die "refusing: $DEVICE does not report as removable (checked $REMOVABLE_FLAG)"

        echo
        lsblk "$DEVICE" 2>&1 || true
        echo
        warn "EVERYTHING on $DEVICE will be permanently erased."
        printf 'Type the device path exactly (e.g. "/dev/sdb") to continue: '
        read -r CONFIRM
        [ "$CONFIRM" = "$DEVICE" ] || die "confirmation did not match -- nothing written"

        umount "${DEVICE}"* 2>/dev/null || true
        say "writing $FINAL to $DEVICE"
        dd if="$FINAL" of="$DEVICE" bs=4M status=progress conv=fsync
        say "done. Remove the stick, put it in the target machine, and boot from it."
        ;;
    *)
        die "unsupported platform: $(uname -s)"
        ;;
esac
