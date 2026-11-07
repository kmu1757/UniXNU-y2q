# Third-party source notices

This device repository contains or builds against these separately licensed projects:

- UniXNU core, DarwinBoot, NativePrelink, NativeUfs, NativeRamBlock, and the Adreno modules: retain their per-file SPDX identifiers and upstream repository licenses.
- UniXNU-gts7xlwifi contributors (rui12366B/UniXNU-gts7xlwifi): DRAM extension, DRAM holes tracking, 4+3+1 three-cluster SMP topology, and ArmCpu SPI IPI routing, licensed under SPDX BSD-2-Clause-Patent.
- NativeMetal, NativeAdreno, and ArmSmmu4K implementations: retain the upstream UniXNU module licenses.
- iOS 16.6.1 / 20G81 components are obtained from Apple's restore service by `scripts/fetch-ios16-assets.py`; no Apple restore binaries are included here.

The device-level license does not replace a component's file-level license.
