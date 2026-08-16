#!/usr/bin/env python3
"""Append a FAT32 data partition to Alpine's ISO image without touching it.

Alpine's hybrid ISO already carries a valid MBR (a whole-ISO entry plus an EFI
System Partition) so it boots BIOS and UEFI machines unmodified. This script
adds one more primary partition describing the aiOS data image concatenated
right after the ISO, by writing exactly one previously-unused 16-byte MBR
partition-table entry. Everything else in the first 512 bytes, and every byte
of the ISO itself, is copied through unchanged -- verified below, not assumed.

Why not a real partitioning tool: growing an existing, foreign, hybrid MBR
with `parted`/`sfdisk`/`diskutil` risks rewriting or reinterpreting the two
entries Alpine's own bootloader depends on. Touching one known-zero slot by
hand, with the untouched bytes asserted equal before writing anything, is the
smaller, checkable change.

Usage: mkusb-mbr.py <alpine.iso> <data.img> <out.img>
"""
import struct
import sys

MBR_SIG = b"\x55\xaa"
TABLE_OFFSET = 446
ENTRY_SIZE = 16
N_ENTRIES = 4
FAT32_LBA = 0x0C


def die(msg: str) -> "None":
    print(f"mkusb-mbr: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 4:
        die("usage: mkusb-mbr.py <alpine.iso> <data.img> <out.img>")
    iso_path, data_path, out_path = sys.argv[1:4]

    with open(iso_path, "rb") as f:
        iso = f.read()
    with open(data_path, "rb") as f:
        data = f.read()

    if len(iso) % 512 or len(data) % 512:
        die("both images must be a whole number of 512-byte sectors")
    if iso[510:512] != MBR_SIG:
        die(f"{iso_path} has no valid MBR boot signature -- refusing to touch it")

    start_lba = len(iso) // 512
    size_sectors = len(data) // 512
    if start_lba >= 2**32 or size_sectors >= 2**32:
        die("image too large for a 32-bit LBA MBR entry -- use a smaller data partition")

    mbr = bytearray(iso[:512])
    slot = None
    for i in range(N_ENTRIES):
        off = TABLE_OFFSET + i * ENTRY_SIZE
        if bytes(mbr[off:off + ENTRY_SIZE]) == b"\x00" * ENTRY_SIZE:
            slot = off
            break
    if slot is None:
        die("no free MBR partition slot -- this Alpine ISO's layout has changed, "
            "needs a human to look at it")

    # status(1) chs_start(3) type(1) chs_end(3) lba_start(4) sectors(4).
    # CHS fields filled FE FF FF, the same convention the existing EFI entry in
    # this table already uses once geometry can't be expressed in 10-bit CHS --
    # every BIOS and UEFI that matters reads the LBA fields instead.
    entry = struct.pack(
        "<B3sB3sLL",
        0x00, b"\xfe\xff\xff", FAT32_LBA, b"\xfe\xff\xff",
        start_lba, size_sectors,
    )
    assert len(entry) == ENTRY_SIZE
    mbr[slot:slot + ENTRY_SIZE] = entry

    # The one property that matters most: nothing outside the slot moved.
    if bytes(mbr[:slot]) != iso[:slot] or bytes(mbr[slot + ENTRY_SIZE:512]) != iso[slot + ENTRY_SIZE:512]:
        die("internal error: patch touched bytes outside the target slot -- aborting, nothing written")

    with open(out_path, "wb") as f:
        f.write(bytes(mbr))
        f.write(iso[512:])
        f.write(data)

    print(f"data partition: slot {(slot - TABLE_OFFSET) // ENTRY_SIZE + 1}, "
          f"start_lba={start_lba}, sectors={size_sectors} "
          f"({len(data) // (1024 * 1024)} MiB), total={len(iso) + len(data)} bytes")


if __name__ == "__main__":
    main()
