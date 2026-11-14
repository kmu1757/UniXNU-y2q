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
import struct
import subprocess
import sys
from pong_native_inputs import build_env, input_path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CHIP_OLD = 'b->device_id != 0x1103 || !b->bar || b->bar_len < 0x200000'
CHIP_NEW = 'b->device_id != 0x1101 || !b->bar || b->bar_len < 0x100000'
# wifistat's transmit line: the handshake frames' completions and the data path's own failure counters.
TX_OLD = 'void nw_wifi_tx_reasons(char *out, int n) { if (n > 0) snprintf(out, n, "ath11k tx reason snapshot unavailable"); }'
TX_NEW = ('extern unsigned nw_eapol_tx[3];\n'
          'void nw_wifi_tx_reasons(char *out, int n) { struct ieee80211_hw *hw = nw_wifi_hw(); struct ath11k *ar = hw ? hw->priv : 0;\n'
          '  if (n > 0) snprintf(out, n, "handshake acked %u nack %u dropped %u, desc_na %u misc %d", nw_eapol_tx[0], nw_eapol_tx[1],\n'
          '    nw_eapol_tx[2], ar ? ar->ab->soc_stats.tx_err.desc_na[0] : 0, ar ? atomic_read(&ar->ab->soc_stats.tx_err.misc_fail) : 0); }')

RANDOM_C = '''/* The station's nonces come from the kernel's entropy source. */
extern void read_random(void *buffer, unsigned int bytes);
void PongWifiRandom(void *out, unsigned long bytes);
void PongWifiRandom(void *out, unsigned long bytes)
{
    read_random(out, (unsigned int)bytes);
}
'''


def late_initialiser(obj, constructor, entry):
    """Rename the object's C++ initialiser section and make its file constructor a global function `entry`, so the
    classes in it register when the board calls `entry` instead of at boot (the edit build-pong-wifi-native.py makes
    to PongWifiNet.o: all code and relocations stay, only one section name and one symbol change)."""
    raw = bytearray(obj.read_bytes())
    offset, symcmd = 32, None
    for _ in range(struct.unpack_from('<I', raw, 16)[0]):
        kind, length = struct.unpack_from('<II', raw, offset)
        if kind == 0x19:                                    # LC_SEGMENT_64
            for i in range(struct.unpack_from('<I', raw, offset + 64)[0]):
                section = offset + 72 + 80 * i
                if raw[section:section + 16].rstrip(b'\0') == b'__mod_init_func':
                    raw[section:section + 16] = b'__pong_init'.ljust(16, b'\0')
                    flags = struct.unpack_from('<I', raw, section + 64)[0]
                    struct.pack_into('<I', raw, section + 64, flags & ~255)
        elif kind == 2:                                     # LC_SYMTAB
            symcmd = offset
        offset += length
    if symcmd is None:
        raise SystemExit(f'{obj.name}: no symbol table')
    symoff, count, stroff, strsize = struct.unpack_from('<4I', raw, symcmd + 8)
    if stroff + strsize != len(raw):
        raise SystemExit(f'{obj.name}: the string table is not at the end')
    for i in range(count):
        at = symoff + 16 * i
        n = stroff + struct.unpack_from('<I', raw, at)[0]
        if raw[n:raw.index(0, n)] == constructor:
            struct.pack_into('<I', raw, at, strsize)
            raw[at + 4] |= 1                                # N_EXT
            raw.extend(entry + b'\0')
            struct.pack_into('<I', raw, symcmd + 20, strsize + len(entry) + 1)
            break
    else:
        raise SystemExit(f'{obj.name}: constructor {constructor.decode()} not found')
    late = obj.with_suffix('.late.o')
    late.write_bytes(raw)
    return late


# PongWifiNet.cpp as this board compiles it. Its log_traffic() (en0's address, packet counts, frames handed to the
# stack and refused, kinds in and out) is defined and never called: it becomes callable from the board, which asks
# for it every ten seconds while the station is up. The ARP lines stop after 12 per direction; 60 here.
NET_EDITS = (
    ('if (n >= 42 && shown[out]++ < 12) log(6, "wifi-net: arp %s op',
     'if (n >= 42 && shown[out]++ < 60) log(6, "wifi-net: arp %s op'),
    # where each received frame was addressed: broadcast, multicast, this station, someone else
    ('void on_rx(const uint8_t *eth, size_t length) {\n',
     'unsigned gDst[4];\nvoid on_rx(const uint8_t *eth, size_t length) {\n'),
    ('  note_frame(eth, length, false);\n',
     '  note_frame(eth, length, false);\n'
     '  gDst[(eth[0] & 1) ? ((eth[0] & eth[1] & eth[2] & eth[3] & eth[4] & eth[5]) == 0xff ? 0 : 1) : (memcmp(eth, gMac, 6) ? 3 : 2)]++;\n'),
    # Packet filter readers: XNU leaves the tap to the driver (if_fake.c, if_bond.c, if_bridge.c call bpf_tap_in/out
    # themselves). Attached but never tapped, a reader saw nothing: the address configuration's own ARP check of the
    # router (sent through the filter) never saw a reply and asked again every second while the kernel's ARP was content.
    ('  mbuf_pkthdr_setlen(m, length); mbuf_pkthdr_setrcvif(m, gIf);\n',
     '  mbuf_pkthdr_setlen(m, length); mbuf_pkthdr_setrcvif(m, gIf);\n'
     '  bpf_tap_in(gIf, DLT_EN10MB, m, nullptr, 0);   // the whole frame, before the header is set apart\n'),
    ('    note_frame(frame, length, true);\n',
     '    note_frame(frame, length, true);\n'
     '    bpf_tap_out(gIf, DLT_EN10MB, m, nullptr, 0);\n'),
    # auto-join (11-14): every set and the first request of each type in the log (it stopped after 96 lines)
    ('if(logged++<96 || (set && (rq.req_type==kAssociate || rq.req_type==kCipherKey || rq.req_type==kAuthType)))log(',
     'static uint32_t seenType[16]; const int ty = rq.req_type;\n    const bool firstOfType = ty >= 0 && ty < 512 && !(seenType[ty / 32] & (1u << (ty % 32)));\n    if (ty >= 0 && ty < 512) seenType[ty / 32] |= 1u << (ty % 32);\n    if(logged++<96 || set || firstOfType)log('),
    # auto-join (11-14): CHANNELS_INFO answered
    ('  case kCurrentNetwork: {\n',
     '  case kChannelsInfo: {\n    // CHANNELS_INFO in the layout iOS 16.5 has (ref/G986N-iOS redwood_apple80211.c:106-117, 357-377): count at 12,\n    // channel numbers from 0x32e, flags words from 0x12d0; only when the daemon asks with exactly that size. Unanswered,\n    // the daemon waited four minutes after en0 appeared before its next step, on every boot.\n    if (rq.req_len != 10016) return EOPNOTSUPP;\n    static uint8_t info[10016]; memset(info, 0, sizeof info);\n    uint32_t frequencies[400]; const unsigned actual = nw_wifi_channel_list(frequencies, 400);\n    const uint16_t count = static_cast<uint16_t>(actual); memcpy(info + 12, &count, 2);\n    for (unsigned i = 0; i < actual; i++) { channel c; fill_channel(c, frequencies[i]); info[0x32e + i] = static_cast<uint8_t>(c.channel); memcpy(info + 0x12d0 + 4 * i, &c.flags, 4); }\n    log(6, "wifi-net: channels info answered: %u channels", actual);\n    return copy_out(rq, info, sizeof info, kernel); }\n  case kCurrentNetwork: {\n'),
    # auto-join (11-14): POWER_CHANGED once en0 is up
    ('  publish_registry("en0");\n',
     '  publish_registry("en0");\n  lck_mtx_lock(gLock); notify_locked(NotifyPower); lck_mtx_unlock(gLock);   // the daemon learns the interface is powered\n'),
)
NET_TAIL = ('\nextern "C" void pong_wifi_net_traffic(void);\n'
            'extern "C" void pong_wifi_net_traffic(void) {\n'
            '  log_traffic();\n'
            '  uint8_t a[6] = {}; if (gIf) ifnet_lladdr_copy_bytes(gIf, a, 6);\n'
            '  log(6, "wifi-net: received for broadcast %u multicast %u this station %u others %u", gDst[0], gDst[1], gDst[2], gDst[3]);\n'
            '  log(6, "wifi-net: en0 %02x:%02x:%02x:%02x:%02x:%02x radio %02x:%02x:%02x:%02x:%02x:%02x flags %x", a[0], a[1], a[2], a[3], a[4], a[5],\n'
            '      gMac[0], gMac[1], gMac[2], gMac[3], gMac[4], gMac[5],\n'
            '      gIf ? static_cast<unsigned>(ifnet_flags(gIf)) : 0);\n'
            '}\n')


def network_source(out):
    text = (ROOT / 'drivers/PongWifi/PongWifiNet.cpp').read_text(encoding='utf-8')
    for old, new in NET_EDITS:
        if text.count(old) != 1:
            raise SystemExit('PongWifiNet.cpp differs from the pinned revision at ' + repr(old[:40]))
        text = text.replace(old, new)
    if text.count('void log_traffic() {') != 1:
        raise SystemExit('PongWifiNet.cpp: log_traffic() is not where the pinned revision has it')
    source = out / 'PongWifiNet.cpp'              # the same file name: the constructor symbol is named after it
    source.write_text(text + NET_TAIL, encoding='utf-8')
    return source


def network_objects(tree, out, env):
    """PongWifi's network side (en0, registry entries, Apple80211 requests) and the SM-T970 glue that starts it."""
    framework = tree / '.cache/fakeroot/System/Library/Frameworks/Kernel.framework/Versions/A'
    if not (framework / 'Headers').is_dir():
        raise SystemExit(f'no Kernel.framework headers at {framework}')
    sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], env=env, text=True).strip()
    flags = ['-target', 'arm64-apple-ios18.0', '-isysroot', sdk, '-mkernel', '-ffreestanding', '-fno-builtin',
             '-fno-exceptions', '-fno-rtti', '-fno-stack-protector', '-mgeneral-regs-only', '-std=gnu++17', '-fblocks',
             '-O2', '-DKERNEL=1', '-DKERNEL_PRIVATE=1', '-DXNU_KERNEL_PRIVATE=1', '-DARM64_BOARD_CONFIG_PONG=1',
             '-I' + str(framework / 'PrivateHeaders'), '-I' + str(framework / 'Headers'),
             '-I' + str(input_path('wifi_shared') / 'include'), '-Wno-deprecated-declarations']
    objects = []
    for source in (network_source(out), ROOT / 'drivers/T970Wifi/T970WifiNetGlue.cpp'):
        obj = out / (source.stem + '.o')
        r = subprocess.run(['xcrun', 'clang++', *flags, '-c', str(source), '-o', str(obj)], env=env,
                           capture_output=True, text=True)
        (out / (source.stem + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise SystemExit(r.stderr)
        if source.stem == 'PongWifiNet':
            obj = late_initialiser(obj, b'__GLOBAL__sub_I_PongWifiNet.cpp', b'_PongWifiNetStaticInit')
        objects.append(obj)
    return objects


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--tree', type=Path, help="the XNU build tree (its Kernel.framework headers): "
                   "also builds the network side and its SM-T970 glue into the archive")
    a = p.parse_args()
    out = a.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    glue = ROOT / 'drivers/PongWifi/ath11k/PongAth11k.c'
    text = glue.read_text(encoding='utf-8')
    if TX_NEW not in text:
        if text.count(TX_OLD) != 1:
            raise SystemExit('PongAth11k.c: the transmit reasons line differs from the pinned revision')
        text = text.replace(TX_OLD, TX_NEW)
        glue.write_text(text, encoding='utf-8')
    if CHIP_NEW not in text:
        if text.count(CHIP_OLD) != 1:
            raise SystemExit('PongAth11k.c: the chip check differs from the pinned revision')
        glue.write_text(text.replace(CHIP_OLD, CHIP_NEW), encoding='utf-8')
        print('PongAth11k.c: PCI device 0x1101 (QCA6390), BAR of at least 1 MiB', flush=True)

    subprocess.run([sys.executable, str(HERE / 'build-pong-ath11k.py'), '--output-dir', str(out),
                    '--jobs', str(a.jobs), '--t970'], check=True)
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
    members = [linked, obj] + (network_objects(a.tree.resolve(), out, env) if a.tree else [])
    subprocess.run(['xcrun', 'libtool', '-static', '-o', str(archive), *map(str, members)], env=env, check=True)
    print('undefined symbols of the ath11k object (libc and PongWifiRandom only):', flush=True)
    print((out / 'undefined-symbols.txt').read_text(), flush=True)
    print(archive)


if __name__ == '__main__':
    main()
