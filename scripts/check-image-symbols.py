#!/usr/bin/env python3
"""Fail when a kernel.macho symbol points outside the image it ships in.

A symbol bound to an address that no segment of the file covers (for example an
IOBlockStorageDevice::gMetaClass pinned to an Apple kernelcache address while no
kernelcache is merged in) faults the first time the kernel touches it. This reads
only the symbol table and the segment commands, so it needs no Apple input and is
cheap enough to run on every build.

usage: check-image-symbols.py kernel.macho [--allow SYMBOL ...]
"""
import argparse
import struct
import sys

LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2
N_STAB = 0xE0
N_TYPE = 0x0E
N_SECT = 0x0E
N_ABS = 0x02


def read_image(data):
    magic = struct.unpack_from('<I', data, 0)[0]
    if magic != 0xFEEDFACF:
        raise SystemExit('not a 64-bit little-endian Mach-O')
    ncmds = struct.unpack_from('<I', data, 16)[0]
    offset, segments, symtab = 32, [], None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == LC_SEGMENT_64:
            name = data[offset + 8:offset + 24].rstrip(b'\0').decode()
            vmaddr, vmsize = struct.unpack_from('<QQ', data, offset + 24)
            segments.append((name, vmaddr, vmaddr + vmsize))
        elif cmd == LC_SYMTAB:
            symtab = struct.unpack_from('<4I', data, offset + 8)
        offset += size
    if symtab is None:
        raise SystemExit('no symbol table')
    return segments, symtab


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('kernel')
    parser.add_argument('--allow', nargs='*', default=[], help='symbols that may sit outside the image')
    args = parser.parse_args()
    data = open(args.kernel, 'rb').read()
    segments, (symoff, nsyms, stroff, _) = read_image(data)
    covered = [(lo, hi, name) for name, lo, hi in segments if hi > lo]
    if not covered:
        raise SystemExit('no segment has a size')
    image_lo, image_hi = min(s[0] for s in covered), max(s[1] for s in covered)

    def inside(value):
        return any(lo <= value < hi for lo, hi, _ in covered)

    checked, outside = 0, []
    for i in range(nsyms):
        strx, ntype, _, _, value = struct.unpack_from('<IBBHQ', data, symoff + i * 16)
        kind = ntype & N_TYPE
        if ntype & N_STAB or kind not in (N_SECT, N_ABS) or value == 0:
            continue
        # Absolute symbols below the kernel base are plain constants, not addresses.
        if kind == N_ABS and value < 0xFFFFFFF000000000:
            continue
        checked += 1
        if not inside(value):
            name = data[stroff + strx:data.index(b'\0', stroff + strx)].decode('latin1')
            if name not in args.allow:
                outside.append((name, value))
    print('image %#x-%#x, %d segments with size, %d symbols checked' % (image_lo, image_hi, len(covered), checked))
    if outside:
        for name, value in sorted(outside, key=lambda item: item[1]):
            print('  OUTSIDE %#x %s' % (value, name))
        print('FAIL: %d symbol(s) outside every segment of %s' % (len(outside), args.kernel))
        return 1
    print('PASS: every symbol address is inside a segment of the image')
    return 0


if __name__ == '__main__':
    sys.exit(main())
