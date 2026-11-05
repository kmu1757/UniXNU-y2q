#!/usr/bin/env python3
"""Compile the Pong UFS transport and optional diagnostics."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

DEVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEVICE / 'scripts'))
import pong_paths
DEFAULT_TREE = pong_paths.build_tree(name='Pong-iPadOS18-native')
DEVELOPER = pong_paths.developer_dir()


def patch_native_ufs_for_t970(ufs_source: Path):
    ufs_source = ufs_source.resolve()
    # 1. NativeUfs.cpp: support SM8250 ufshc
    n_cpp = ufs_source / 'NativeUfs.cpp'
    if n_cpp.is_file():
        txt = n_cpp.read_text(encoding='utf-8')
        if 'sm8250-ufshc' not in txt:
            txt = txt.replace(
                'contains (bytes, data->getLength (), "qcom,sm8450-ufshc"));',
                'contains (bytes, data->getLength (), "qcom,sm8450-ufshc") ||\n                     contains (bytes, data->getLength (), "qcom,sm8250-ufshc") ||\n                     contains (bytes, data->getLength (), "qcom,ufshc"));'
            ).replace(
                '!strcmp (bytes, "qcom,sm8450-ufshc"));',
                '!strcmp (bytes, "qcom,sm8450-ufshc") ||\n                     !strcmp (bytes, "qcom,sm8250-ufshc") ||\n                     !strcmp (bytes, "qcom,ufshc"));'
            )
            n_cpp.write_text(txt, encoding='utf-8')
            print(f'Applied SM8250 compatible patch to {n_cpp}', flush=True)

    # 2. PongUfsTransport.cpp: weak SMMU stubs and generic SHNGROOT bounds
    t_cpp = ufs_source / 'PongUfsTransport.cpp'
    if t_cpp.is_file():
        txt = t_cpp.read_text(encoding='utf-8')
        if 'pong_smmu_lease_lock(void) {' not in txt:
            txt = txt.replace(
                'extern "C" bool pong_smmu_lease_lock(void) __attribute__((weak_import));\nextern "C" void pong_smmu_lease_unlock(void) __attribute__((weak_import));',
                '__attribute__((weak)) extern "C" bool pong_smmu_lease_lock(void) { return true; }\n__attribute__((weak)) extern "C" void pong_smmu_lease_unlock(void) {}'
            ).replace(
                'gRootDiagnostic.first = kPongUserdataFirst4k;\n  gRootDiagnostic.last = kPongUserdataLast4k;',
                'gRootDiagnostic.first = window.baseByte / 4096ULL;\n  gRootDiagnostic.last = (window.baseByte + window.size) / 4096ULL;'
            ).replace(
                'if (layout.userdataFirst != kPongUserdataFirst4k || layout.userdataLast != kPongUserdataLast4k ||\n      layout.userdataLast >= gHost.blocks)',
                'if (layout.userdataFirst > layout.userdataLast || layout.userdataLast >= gHost.blocks)'
            )
            t_cpp.write_text(txt, encoding='utf-8')
            print(f'Applied weak SMMU stubs and geometry patch to {t_cpp}', flush=True)

    # 3. RootLayout.h: pinned SHNGROOT boot arg support (pong_shngroot_4k)
    r_h = ufs_source / 'RootLayout.h'
    if r_h.is_file():
        txt = r_h.read_text(encoding='utf-8')
        if 'pong_shngroot_4k' not in txt:
            target = '  if (!found || layout->userdataLast < layout->userdataFirst) {\n    *why = "no userdata partition";\n    return false;\n  }\n'
            addition = (
                '  if (!found || layout->userdataLast < layout->userdataFirst) {\n    *why = "no userdata partition";\n    return false;\n  }\n'
                '  uint64_t pinnedLba = 0;\n'
                '  if (PE_parse_boot_argn ("pong_shngroot_4k", &pinnedLba, sizeof (pinnedLba)) && pinnedLba != 0) {\n'
                '    layout->userdataFirst = pinnedLba;\n'
                '    layout->userdataLast = pinnedLba + (20ULL * 1024 * 1024 * 1024 / 4096) - 1;\n'
                '  }\n'
            )
            txt = txt.replace(target, addition)
            r_h.write_text(txt, encoding='utf-8')
            print(f'Applied pong_shngroot_4k pinned LBA patch to {r_h}', flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=Path, default=DEFAULT_TREE,
                        help='XNU build tree whose Darwin 24 kernel headers should be used')
    parser.add_argument('--skip-image-verification', action='store_true',
                        help='compile/link without source or archive digests')
    args = parser.parse_args()
    tree = args.tree.resolve()
    if platform.system() != 'Darwin' or platform.machine() != 'x86_64':
        raise SystemExit('This continuation uses the Intel macOS toolchain')
    out = tree / 'build/native-ufs'
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([os.sys.executable, str(DEVICE/'scripts/prepare-ipados18-storage.py'), '--tree', str(tree)],check=True)
    ufs_source = DEVICE / 'drivers/NativeUfs'
    patch_native_ufs_for_t970(ufs_source)
    source = DEVICE / 'drivers/NativeUfs/NativeUfs.cpp'
    framework = tree / '.cache/fakeroot/System/Library/Frameworks/Kernel.framework/Versions/A'
    env = dict(os.environ, DEVELOPER_DIR=str(DEVELOPER))
    sdk = subprocess.check_output(
        ['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True, env=env).strip()
    obj = out / 'NativeUfs.o'
    trace_enabled = 'CONFIG_XNU_PONG_EARLY_TRACE=y' in (DEVICE / 'configs/pong_ipados18_defconfig').read_text().splitlines()
    command = ['xcrun', '--sdk', 'iphoneos', 'clang++', '-target', 'arm64-apple-ios18.0',
        '-isysroot', sdk, '-mkernel', '-ffreestanding', '-fno-builtin', '-fno-exceptions',
        '-fno-rtti', '-fno-stack-check', '-std=gnu++17', '-fblocks', '-O2',
        '-DKERNEL=1', '-DKERNEL_PRIVATE=1', '-DXNU_KERNEL_PRIVATE=1',
        '-DXNU_PONG_EARLY_TRACE=' + str(int(trace_enabled)),
        '-DXNU_PONG_UFS_DATA=' + str(int('CONFIG_XNU_OWNER_DATA_UFS=y' in (DEVICE/'configs/pong_ipados18_defconfig').read_text().splitlines())),
        '-I' + str(DEVICE / 'mac-build/storage-abi18/include'),
        '-I' + str(tree / '.cache/xnu-work/EXTERNAL_HEADERS'),
        '-I' + str(framework / 'PrivateHeaders'), '-I' + str(framework / 'Headers'),
        '-Wall', '-Wextra', '-Werror', '-Wno-deprecated-declarations',
        '-Wno-unused-parameter', '-c', str(source), '-o', str(obj)]
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    (out / 'build.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit(result.stderr)
    transport = out / 'PongUfsTransport.o'
    command[command.index(str(source))] = str(source.with_name('PongUfsTransport.cpp'))
    command[-1] = str(transport)
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    (out / 'transport-build.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit(result.stderr)
    subprocess.run([str(Path(os.sys.executable)), str(DEVICE / 'scripts/bind-ipados18-storage.py'),
                    '--object', str(transport), '--output', str(out)], check=True, env=env)
    archive = out / 'libNativeUfs.a'
    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'libtool', '-static', '-o', str(archive), str(obj), str(transport), str(out / 'original-bindings.o')],
                   check=True, env=env)
    report = {
        'host': platform.machine(), 'target': 'arm64-apple-ios18.0',
        'build_tree': str(tree),
        'source': str(source),
        'image_verification': 'skipped by request' if args.skip_image_verification else 'digest recorded',
        'read_write_capable': True, 'runtime_write_arg': 'pong_ufs_readwrite=1',
        'write_scope': 'declared SHNGROOT OS/SystemOS/Data windows; each mount retains its own flags',
        'storage_commands': ['NOP', 'READ ATTRIBUTE', 'START STOP ACTIVE', 'TEST UNIT READY', 'READ CAPACITY', 'READ(10)', 'WRITE(10) FUA', 'SYNCHRONIZE CACHE(10)'],
        'smmu_writes': 'exclusive UFS SID 0xe0 only after queue stop',
        'optional_probes_default': 'off',
        'board': 'Nothing Phone (2) Pong / SM8475',
        'ufs_hc': '0x01d84000/0x3000', 'gcc_ufs_page': '0x00187000/0x1000',
        'stream_id': '0xe0', 'hardware_tested': False,
        'smmu_owned_context_banks': 78, 'smmu_owned_match_groups': 120,
    }
    if not args.skip_image_verification:
        report['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        report['sources_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted(source.parent.iterdir()) if p.suffix in ('.h', '.cpp')}
        report['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')
    export = DEVICE / 'mac-build/native-ufs'
    export.mkdir(parents=True, exist_ok=True)
    for name in ('libNativeUfs.a', 'build-report.json', 'build.log', 'bindings.json', 'transport-build.log'):
        shutil.copyfile(out / name, export / name)
    print('Compiled Pong NativeUfs read/write path (boot-arg gated):', archive)


if __name__ == '__main__':
    main()