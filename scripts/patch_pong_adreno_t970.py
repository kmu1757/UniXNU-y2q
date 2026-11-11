#!/usr/bin/env python3
"""Patch PongAdreno for SM-T970 (SM8250 / Adreno 650 hardware constants and ZAP metadata size)."""
from pathlib import Path
import sys


ZAP_H_REPLACEMENTS = (
    ('PongZapReservedPhysical=0x8b91a000,', 'PongZapReservedPhysical=0x9031a000,'),
)

ZAP_C_REPLACEMENTS = (
    ('if (loads!=1 || signatures!=1 || header>4096 || signature>4096-header ||',
     'if (loads!=1 || signatures!=1 || header>4096 || signature>8192-header ||'),
    ('!reserved->cpu || meta->bytes!=4096 || !meta->physical ||',
     '!reserved->cpu || (meta->bytes!=4096 && meta->bytes!=8192) || meta->bytes<plan->metadata_bytes || !meta->physical ||'),
)

CP_CPP_REPLACEMENTS = (
    ('extern "C" int get_preemption_level(void);\n',
     'extern "C" int get_preemption_level(void);\n'
     'static IOLock *gPongScmCallLock;\n'
     '__attribute__((weak)) extern "C" bool pong_scm_call_lock(void)\n'
     '{\n'
     '    if(!__atomic_load_n(&gPongScmCallLock,__ATOMIC_ACQUIRE)) {\n'
     '        IOLock *lock=IOLockAlloc(),*empty=nullptr;\n'
     '        if(lock && !__atomic_compare_exchange_n(&gPongScmCallLock,&empty,lock,false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE))\n'
     '            IOLockFree(lock);\n'
     '    }\n'
     '    if(!gPongScmCallLock)return false;\n'
     '    IOLockLock(gPongScmCallLock);\n'
     '    return true;\n'
     '}\n'
     '__attribute__((weak)) extern "C" void pong_scm_call_unlock(void)\n'
     '{\n'
     '    if(gPongScmCallLock)IOLockUnlock(gPongScmCallLock);\n'
     '}\n'),
    ('if(gpu->metadata.allocate(4096,gpu->output_bits,true,4096) || gpu->metadata.makeWriteCombined())return false;',
     'if(gpu->metadata.allocate(8192,gpu->output_bits,true,4096) || gpu->metadata.makeWriteCombined())return false;'),
    ('PongZapConfig c={PongAdrenoZapMdt,PongAdrenoZapMdtBytes,PongAdrenoZapSignature,PongAdrenoZapSignatureBytes,2,\n'
     '        PongAdrenoZapCode,PongAdrenoZapCodeBytes,1,\n'
     '        {static_cast<volatile uint8_t *>(gpu->metadata.mapping()),gpu->metadata.spans()[0].physical,4096},',
     'PongZapConfig c={PongAdrenoZapMdt,PongAdrenoZapMdtBytes,PongAdrenoZapSignature,PongAdrenoZapSignatureBytes,1,\n'
     '        PongAdrenoZapCode,PongAdrenoZapCodeBytes,2,\n'
     '        {static_cast<volatile uint8_t *>(gpu->metadata.mapping()),gpu->metadata.spans()[0].physical,8192},'),
)

GMU_H_REPLACEMENTS = (
    ('PongAdrenoChipId=0x07030002,', 'PongAdrenoChipId=0x06050002,'),
    ('PongAdrenoGmemBytes=0x200000,', 'PongAdrenoGmemBytes=0x100000,'),
    ('PongAdrenoGpuIrq=332,PongAdrenoHfiIrq=336,PongAdrenoGmuIrq=337,',
     'PongAdrenoGpuIrq=300,PongAdrenoHfiIrq=304,PongAdrenoGmuIrq=305,'),
    ('PongAdrenoCxGdscr=0x9108,', 'PongAdrenoCxGdscr=0x106c,'),
    ('PongAdrenoCxHwControl=0x953c,', 'PongAdrenoCxHwControl=0x1540,'),
    ('PongAdrenoGxGdscr=0x905c,', 'PongAdrenoGxGdscr=0x100c,'),
    ('PongAdrenoSmmuClockVote=0x7000};', 'PongAdrenoSmmuClockVote=0x5000};'),
)


def apply_replacements(path: Path, pairs: tuple[tuple[str, str], ...]) -> bool:
    if not path.is_file():
        return False
    raw = path.read_text(encoding='utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    txt = raw.replace('\r\n', '\n')
    changed = False
    for old, new in pairs:
        if new in txt:
            continue
        assert txt.count(old) == 1, f'{path.name}: expected {old!r} once, found {txt.count(old)}'
        txt = txt.replace(old, new)
        changed = True
    if changed:
        path.write_bytes(txt.replace('\n', eol).encode('utf-8'))
    return changed


def patch_pong_adreno(adreno_source: Path) -> None:
    root = adreno_source.resolve()
    if not root.is_dir():
        return
    modified = []
    for rel, pairs in (
        ('PongAdrenoZap.h', ZAP_H_REPLACEMENTS),
        ('PongAdrenoZap.c', ZAP_C_REPLACEMENTS),
        ('PongAdrenoCp.cpp', CP_CPP_REPLACEMENTS),
        ('PongAdrenoGmu.h', GMU_H_REPLACEMENTS),
    ):
        target = root / rel
        if apply_replacements(target, pairs):
            modified.append(rel)
    if modified:
        print(f'Applied SM-T970 PongAdreno patch to {root}: {", ".join(modified)}', flush=True)


if __name__ == '__main__':
    patch_pong_adreno(Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.'))
