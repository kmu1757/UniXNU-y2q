# Third-party source notices

This device repository contains or builds against these separately licensed projects:

- UniXNU core, DarwinBoot, NativePrelink, NativeUfs, NativeRamBlock, and the Adreno modules: retain their per-file SPDX identifiers and upstream repository licenses.
- UniXNU-gts7xlwifi contributors (rui12366B/UniXNU-gts7xlwifi): DRAM extension, DRAM holes tracking, 4+3+1 three-cluster SMP topology, ArmCpu SPI IPI routing, and PCIe0 power-up and link training for the QCA6390 (`bsd/dev/pong_wifi_link.c` in `patches/xnu-ipados18`, from `package/T970WifiProbe.cpp` and `package/T970Rpmh.inc`), licensed under SPDX BSD-2-Clause-Patent; `BootWatchdogPolicy.c` launchd daemon exclusion policy (`lite_jobs`), licensed under MIT License.
- skip-setup framework: setup-assistant bypass preferences and SpringBoard power/watchdog keys (`SBDisableAutoDim`, `SBDontDimOrLockOnAC`, `SBDisableIdleSleep`, `FBLaunchWatchdogExceptions`, `LockdownActivationIndicatesCloudConfigurationAvailable`), licensed under SPDX BSD-2-Clause-Patent.
- ios-sweet: migration state and SetupDone preference seeding, licensed under MIT License.
- NativeMetal, NativeAdreno, and ArmSmmu4K implementations: retain the upstream UniXNU module licenses.
- iOS 16.6.1 / 20G81 components are obtained from Apple's restore service by `scripts/fetch-ios16-assets.py`; no Apple restore binaries are included here.

The device-level license does not replace a component's file-level license.
