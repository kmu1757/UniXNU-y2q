#!/usr/bin/env python3
"""Patch NativeUfs for SM-T970 (SM8250 ufshc and SHNGEXT1 extents table support)."""
from pathlib import Path
import sys

def patch_native_ufs(ufs_source: Path):
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

    # 2. RootLayout.h: SHNGEXT1 extents structures, translateFileBlock4k, loadShngExtentTable
    r_h = ufs_source / 'RootLayout.h'
    if r_h.is_file():
        txt = r_h.read_text(encoding='utf-8')
        if 'ShngExtentHeader' not in txt:
            ext_structs = """#pragma pack(push, 1)
struct ShngExtentHeader {
  uint64_t magic;         // 0x31545845474e4853ULL ("SHNGEXT1")
  uint32_t version;       // 1
  uint32_t extentCount;   // e.g. 1139
  uint64_t totalBytes;    // image size
  uint32_t crc32;         // IEEE CRC32 of entries
  uint32_t reserved;      // 0
};

struct ShngExtentEntry {
  uint64_t logicalBlock;   // 4KB block index in image
  uint64_t physicalLba;    // 4KB physical block LBA on UFS
  uint32_t blockCount;     // count of 4KB blocks in this extent
  uint32_t reserved;        // 0
};
#pragma pack(pop)

static const uint32_t kMaxExtents = 4096;
static ShngExtentEntry gExtents[kMaxExtents];
static uint32_t gExtentCount = 0;
static uint64_t gContiguousBase4k = 0;
static uint64_t gContiguousTotal4k = 0;

static bool translateFileBlock4k(uint64_t logical4k, uint64_t *outPhys4k, uint32_t *outRemaining4k) {
  if (gExtentCount == 0) {
    if (gContiguousBase4k == 0) return false;
    *outPhys4k = gContiguousBase4k + logical4k;
    *outRemaining4k = (gContiguousTotal4k > logical4k) ? static_cast<uint32_t>(gContiguousTotal4k - logical4k) : 0;
    return *outRemaining4k > 0;
  }
  int low = 0, high = static_cast<int>(gExtentCount) - 1;
  while (low <= high) {
    int mid = low + (high - low) / 2;
    const ShngExtentEntry &ext = gExtents[mid];
    if (logical4k < ext.logicalBlock) {
      high = mid - 1;
    } else if (logical4k >= ext.logicalBlock + ext.blockCount) {
      low = mid + 1;
    } else {
      uint64_t delta = logical4k - ext.logicalBlock;
      *outPhys4k = ext.physicalLba + delta;
      *outRemaining4k = ext.blockCount - static_cast<uint32_t>(delta);
      return true;
    }
  }
  return false;
}

static bool loadShngExtentTable(uint64_t tableLba4k, uint64_t totalImageBytes) {
  if (tableLba4k == 0) return false;
  if (hostRead4k(0, tableLba4k, 1, gScratch) != kIOReturnSuccess) {
    return false;
  }
  const ShngExtentHeader *hdr = reinterpret_cast<const ShngExtentHeader *>(gScratch);
  if (hdr->magic != 0x31545845474e4853ULL) {
    return false;
  }
  if (hdr->version != 1 || hdr->extentCount == 0 || hdr->extentCount > kMaxExtents) {
    return false;
  }
  uint32_t payloadBytes = sizeof(ShngExtentHeader) + hdr->extentCount * sizeof(ShngExtentEntry);
  uint32_t blocksNeeded = (payloadBytes + 4095) / 4096;
  if (blocksNeeded > (kDataMax / 4096)) {
    return false;
  }
  if (blocksNeeded > 1) {
    if (hostRead4k(0, tableLba4k, blocksNeeded, gScratch) != kIOReturnSuccess) {
      return false;
    }
    hdr = reinterpret_cast<const ShngExtentHeader *>(gScratch);
  }
  const uint8_t *entriesData = gScratch + sizeof(ShngExtentHeader);
  uint32_t entriesBytes = hdr->extentCount * sizeof(ShngExtentEntry);
  if (rootCrc32(entriesData, entriesBytes) != hdr->crc32) {
    return false;
  }
  const ShngExtentEntry *src = reinterpret_cast<const ShngExtentEntry *>(entriesData);
  for (uint32_t i = 0; i < hdr->extentCount; i++) {
    gExtents[i] = src[i];
  }
  gExtentCount = hdr->extentCount;
  return true;
}
"""
            t1 = '// Find the partition named "userdata"'
            txt = txt.replace(t1, ext_structs + '\n' + t1)

            t2 = '  if (!found || layout->userdataLast < layout->userdataFirst) {\n    *why = "no userdata partition";\n    return false;\n  }\n'
            add2 = """  if (!found || layout->userdataLast < layout->userdataFirst) {
    *why = "no userdata partition";
    return false;
  }
  uint64_t pinnedLba = 0;
  uint64_t extentsLba = 0;
  PE_parse_boot_argn ("pong_extents_4k", &extentsLba, sizeof (extentsLba));
  if (PE_parse_boot_argn ("pong_shngroot_4k", &pinnedLba, sizeof (pinnedLba)) && pinnedLba != 0) {
    layout->userdataFirst = pinnedLba;
    layout->userdataLast = pinnedLba + (20ULL * 1024 * 1024 * 1024 / 4096) - 1;
  } else if (extentsLba != 0) {
    if (loadShngExtentTable(extentsLba, 0)) {
      layout->userdataFirst = gExtents[0].physicalLba;
      layout->userdataLast = layout->userdataFirst + (20ULL * 1024 * 1024 * 1024 / 4096) - 1;
    }
  }
"""
            txt = txt.replace(t2, add2)

            t3 = '          layout->windows = n;\n          layout->imageBytes = imageBytes;\n          layout->manifestStatus = 0;\n'
            add3 = """          layout->windows = n;
          layout->imageBytes = imageBytes;
          layout->manifestStatus = 0;
          gContiguousBase4k = layout->userdataFirst;
          gContiguousTotal4k = (imageBytes + 4095) / 4096;
          if (extentsLba != 0) {
            if (gExtentCount == 0) loadShngExtentTable(extentsLba, imageBytes);
          } else if (pinnedLba != 0) {
            loadShngExtentTable(pinnedLba + 1, imageBytes);
          }
"""
            txt = txt.replace(t3, add3)
            r_h.write_text(txt, encoding='utf-8')
            print(f'Applied SHNGEXT1 extents support to {r_h}', flush=True)

    # 3. PongUfsTransport.cpp: weak SMMU stubs, RootDiskWindow fileOffset, and extent translation
    t_cpp = ufs_source / 'PongUfsTransport.cpp'
    if t_cpp.is_file():
        txt = t_cpp.read_text(encoding='utf-8')
        if 'pong_smmu_lease_lock(void) {' not in txt:
            target_smmu = 'extern "C" bool pong_smmu_lease_lock(void) __attribute__((weak_import));\nextern "C" void pong_smmu_lease_unlock(void) __attribute__((weak_import));'
            repl_smmu = '__attribute__((weak)) extern "C" bool pong_smmu_lease_lock(void) { return true; }\n__attribute__((weak)) extern "C" void pong_smmu_lease_unlock(void) {}'
            assert target_smmu in txt, "PongUfsTransport.cpp weak SMMU target not found"
            txt = txt.replace(target_smmu, repl_smmu)

        # Update PongUfsBlock::start FIRST before any baseByte string replacements
        old_start = """  gRootDiagnostic.stage = "first root read";
  if (hostRead4k(0, window.baseByte / 4096, 1, bounce) != kIOReturnSuccess)
    return transportFailure("first root read");
  const bool expected = !memcmp(bounce + window.signatureOffset, window.signature, strlen(window.signature));
  // New restores publish a raw APFS OS container, while older OS inputs use
  // a GPT disk. Both are actual media formats handled by IOStorage/APFS.
  const bool rawApfsRoot = !strcmp(window.name, "OS") && !memcmp(bounce + 32, "NXSB", 4) &&
    OSReadLittleInt32(bounce, 36) == 4096;
  if (!expected && !rawApfsRoot) {
    // Invalid filesystem contents belong to this window, not to the shared
    // UFS controller. Do not invalidate already published OS/SystemOS disks.
    IOLog("PONG_UFS: %s window has no supported filesystem header base=%llx bytes=%llx; media not published\\n",
          window.name, window.baseByte, window.size);
    char detail[96];
    snprintf(detail, sizeof(detail), "%s filesystem header invalid", window.name);
    PongProbeRecord(4, "ufs-transport", PongProbeFail, detail, window.baseByte);
    return false;
  }
  gRootDiagnostic.first = kPongUserdataFirst4k;
  gRootDiagnostic.last = kPongUserdataLast4k;
  gRootDiagnostic.windowBytes = window.size;
  const bool writableWindow = gHost.writable && !(window.baseByte & 4095) && !(window.size & 4095);
  IOLog("PONG_UFS: SHNGROOT %s first read complete signature=1 base=%llx bytes=%llx; publish 512-byte %s disk\\n",
        window.name, window.baseByte, window.size, writableWindow ? "read-write" : "read-only");"""

        new_start = """  gRootDiagnostic.stage = "first root read";
  uint64_t firstPhys4k = 0;
  uint32_t firstRemaining4k = 0;
  if (!translateFileBlock4k(window.fileOffset / 4096, &firstPhys4k, &firstRemaining4k))
    return transportFailure("first root block translation failed");
  if (hostRead4k(0, firstPhys4k, 1, bounce) != kIOReturnSuccess)
    return transportFailure("first root read");
  const bool expected = !memcmp(bounce + window.signatureOffset, window.signature, strlen(window.signature));
  const bool rawApfsRoot = !strcmp(window.name, "OS") && !memcmp(bounce + 32, "NXSB", 4) &&
    OSReadLittleInt32(bounce, 36) == 4096;
  if (!expected && !rawApfsRoot) {
    IOLog("PONG_UFS: %s window has no supported filesystem header offset=%llx bytes=%llx; media not published\\n",
          window.name, window.fileOffset, window.size);
    char detail[96];
    snprintf(detail, sizeof(detail), "%s filesystem header invalid", window.name);
    PongProbeRecord(4, "ufs-transport", PongProbeFail, detail, window.fileOffset);
    return false;
  }
  gRootDiagnostic.first = firstPhys4k;
  gRootDiagnostic.last = firstPhys4k + (window.size / 4096ULL);
  gRootDiagnostic.windowBytes = window.size;
  const bool writableWindow = gHost.writable && !(window.fileOffset & 4095) && !(window.size & 4095);
  IOLog("PONG_UFS: SHNGROOT %s first read complete signature=1 offset=%llx bytes=%llx extents=%u; publish 512-byte %s disk\\n",
        window.name, window.fileOffset, window.size, gExtentCount, writableWindow ? "read-write" : "read-only");"""

        assert old_start in txt, "PongUfsTransport.cpp old_start not found"
        txt = txt.replace(old_start, new_start)

        # Update PongUfsBlock::doAsyncReadWrite
        old_rw = """  while (done < bytes) {
    const uint64_t absolute = window.baseByte + block * kRootLogicalBlock + done;
    const uint32_t inside = static_cast<uint32_t>(absolute & 4095);
    uint64_t wanted = bytes - done;
    if (wanted > kDataMax - inside) wanted = kDataMax - inside;
    uint32_t physicalBlocks = static_cast<uint32_t>((inside + wanted + 4095) / 4096);
    uint64_t available = uint64_t(physicalBlocks) * 4096 - inside;
    uint64_t take = wanted < available ? wanted : available;
    if (write) {
      uint64_t writeStart = absolute & ~UINT64_C(4095);
      uint64_t writeEnd = writeStart + uint64_t(physicalBlocks) * 4096;
      uint64_t windowEnd = window.baseByte + window.size;
      if (writeStart < window.baseByte || writeEnd < writeStart || writeEnd > windowEnd) {
        result = kIOReturnNotWritable;
        break;
      }
    }
    IOLockLock(gHost.lock);
    if (!gHost.ready || gHost.poisoned) {
      result = kIOReturnNotReady;
    } else {
      // Prepared storage descriptors can copy directly to/from the shared
      // payload. Keep ownership until the copy and any RMW write complete.
      // Full physical-block writes need no read of the old contents.
      if (!write || inside || take != uint64_t(physicalBlocks) * 4096)
        result = hostRead4kLocked(0, absolute / 4096, physicalBlocks);
      if (result == kIOReturnSuccess && write && !hostIdle()) result = kIOReturnNotReady;
      if (result == kIOReturnSuccess) {
        uint8_t *data = gHost.virt + kDataOffset + inside;
        uint64_t copied = write ? buffer->readBytes(done, data, take) : buffer->writeBytes(done, data, take);
        if (copied != take) result = kIOReturnUnderrun;
        else if (write) result = hostWrite4kLocked(0, absolute / 4096, physicalBlocks);
      }
    }
    IOLockUnlock(gHost.lock);
    if (result != kIOReturnSuccess) break;
    done += take;
  }"""

        new_rw = """  while (done < bytes) {
    const uint64_t currentFileByte = window.fileOffset + block * kRootLogicalBlock + done;
    const uint64_t logicalBlock4k = currentFileByte / 4096;
    const uint32_t inside = static_cast<uint32_t>(currentFileByte & 4095);
    uint64_t physBlock4k = 0;
    uint32_t extentRemaining4k = 0;
    if (!translateFileBlock4k(logicalBlock4k, &physBlock4k, &extentRemaining4k)) {
      result = kIOReturnBadArgument;
      break;
    }
    uint64_t wanted = bytes - done;
    if (wanted > kDataMax - inside) wanted = kDataMax - inside;
    uint32_t wantedBlocks = static_cast<uint32_t>((inside + wanted + 4095) / 4096);
    if (wantedBlocks > extentRemaining4k) {
      wantedBlocks = extentRemaining4k;
    }
    uint64_t available = uint64_t(wantedBlocks) * 4096 - inside;
    uint64_t take = wanted < available ? wanted : available;
    if (take == 0) {
      result = kIOReturnBadArgument;
      break;
    }
    if (write) {
      uint64_t windowEnd = window.fileOffset + window.size;
      if (currentFileByte + take > windowEnd) {
        result = kIOReturnNotWritable;
        break;
      }
    }
    IOLockLock(gHost.lock);
    if (!gHost.ready || gHost.poisoned) {
      result = kIOReturnNotReady;
    } else {
      if (!write || inside || take != uint64_t(wantedBlocks) * 4096)
        result = hostRead4kLocked(0, physBlock4k, wantedBlocks);
      if (result == kIOReturnSuccess && write && !hostIdle()) result = kIOReturnNotReady;
      if (result == kIOReturnSuccess) {
        uint8_t *data = gHost.virt + kDataOffset + inside;
        uint64_t copied = write ? buffer->readBytes(done, data, take) : buffer->writeBytes(done, data, take);
        if (copied != take) result = kIOReturnUnderrun;
        else if (write) result = hostWrite4kLocked(0, physBlock4k, wantedBlocks);
      }
    }
    IOLockUnlock(gHost.lock);
    if (result != kIOReturnSuccess) break;
    done += take;
  }"""

        assert old_rw in txt, "PongUfsTransport.cpp old_rw not found"
        txt = txt.replace(old_rw, new_rw)

        # Update RootDiskWindow struct definition
        assert 'uint64_t baseByte;' in txt, "PongUfsTransport.cpp baseByte struct member not found"
        txt = txt.replace('uint64_t baseByte;', 'uint64_t fileOffset;')

        # Update reportWriteProtection
        assert 'window.baseByte & 4095' in txt, "PongUfsTransport.cpp reportWriteProtection baseByte not found"
        txt = txt.replace('window.baseByte & 4095', 'window.fileOffset & 4095')

        # Update PongUfsBlock::init
        assert 'selected.baseByte & 4095' in txt, "PongUfsTransport.cpp init selected.baseByte not found"
        txt = txt.replace('selected.baseByte & 4095', 'selected.fileOffset & 4095')

        # Update geometry check
        old_geo = "if (layout.userdataFirst != kPongUserdataFirst4k || layout.userdataLast != kPongUserdataLast4k ||\n      layout.userdataLast >= gHost.blocks)"
        new_geo = "if (layout.userdataFirst > layout.userdataLast || layout.userdataLast >= gHost.blocks)"
        assert old_geo in txt, "PongUfsTransport.cpp old_geo not found"
        txt = txt.replace(old_geo, new_geo)

        # Update gRootWindows assignment:
        # layout.userdataFirst * 4096ULL + layout.window[index].offset -> layout.window[index].offset
        old_gw = "layout.userdataFirst * 4096ULL + layout.window[index].offset,"
        new_gw = "layout.window[index].offset,"
        assert old_gw in txt, "PongUfsTransport.cpp old_gw not found"
        txt = txt.replace(old_gw, new_gw)

        assert 'baseByte' not in txt, "PongUfsTransport.cpp unexpected baseByte remaining!"

        t_cpp.write_text(txt, encoding='utf-8')
        print(f'Applied PongUfsTransport extents and translation patch to {t_cpp}', flush=True)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        patch_native_ufs(Path(sys.argv[1]))
    else:
        patch_native_ufs(Path('.'))
