#!/usr/bin/env python3
"""SM-T970 (QCA6390): build the Linux ath11k / MHI / QMI stack and the shared station into one archive.

First step of the Wi-Fi integration for this tablet. The archive is force-loaded into the kernel
(CONFIG_XNU_T970_ATH11K_LINK=y) and nothing calls it: no board file starts nw_wifi_start yet. One CI run
then shows whether the stack compiles and links against this XNU.

It reuses build-pong-ath11k.py unchanged. Two things differ from the Nothing Phone build:
  - drivers/PongWifi/ath11k/PongAth11k.c accepts PCI device 0x1101 (QCA6390) with a 1 MiB BAR instead of
    0x1103 (QCA6490) with 2 MiB; the chip-specific parameters are ath11k's own (pci.c picks them by device id).
  - PongWifiRandom, the archive's only import besides libc, comes from a small object here instead of
    PongWifiPlatform.cpp, which is SM8475-only and is not built.
The board-file name rule (bdwlang / bdwlan) and the station-only mac.c change of the Nothing Phone build are kept
for now; they matter only once the stack runs.
"""
import argparse
from pathlib import Path
import subprocess
import sys
from pong_native_inputs import build_env

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CHIP_OLD = 'b->device_id != 0x1103 || !b->bar || b->bar_len < 0x200000'
CHIP_NEW = 'b->device_id != 0x1101 || !b->bar || b->bar_len < 0x100000'

RANDOM_C = '''/* The station's nonces come from the kernel's entropy source. */
extern void read_random(void *buffer, unsigned int bytes);
void PongWifiRandom(void *out, unsigned long bytes);
void PongWifiRandom(void *out, unsigned long bytes)
{
    read_random(out, (unsigned int)bytes);
}
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--jobs', type=int, default=4)
    a = p.parse_args()
    out = a.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    glue = ROOT / 'drivers/PongWifi/ath11k/PongAth11k.c'
    text = glue.read_text(encoding='utf-8')
    if CHIP_NEW not in text:
        if text.count(CHIP_OLD) != 1:
            raise SystemExit('PongAth11k.c: the chip check differs from the pinned revision')
        glue.write_text(text.replace(CHIP_OLD, CHIP_NEW), encoding='utf-8')
        print('PongAth11k.c: PCI device 0x1101 (QCA6390), BAR of at least 1 MiB', flush=True)

    subprocess.run([sys.executable, str(HERE / 'build-pong-ath11k.py'), '--output-dir', str(out),
                    '--jobs', str(a.jobs)], check=True)
    linked = out / 'PongAth11k-linked.o'
    if not linked.is_file():
        raise SystemExit('build-pong-ath11k.py left no ' + str(linked))

    env = build_env()
    clang = subprocess.check_output(['xcrun', '-f', 'clang'], env=env, text=True).strip()
    source = out / 't970_wifi_random.c'
    source.write_text(RANDOM_C)
    obj = out / 't970_wifi_random.o'
    subprocess.run([clang, '-target', 'arm64-apple-ios18.0', '-mkernel', '-O2', '-g0', '-ffreestanding',
                    '-fno-builtin', '-std=gnu11', '-c', str(source), '-o', str(obj)], env=env, check=True)

    archive = out / 'libT970Ath11k.a'
    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'libtool', '-static', '-o', str(archive), str(linked), str(obj)], env=env, check=True)
    print('undefined symbols of the ath11k object (libc and PongWifiRandom only):', flush=True)
    print((out / 'undefined-symbols.txt').read_text(), flush=True)
    print(archive)


if __name__ == '__main__':
    main()
