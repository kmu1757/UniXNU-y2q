// SPDX-License-Identifier: BSD-2-Clause-Patent
// Registration glue for the IOStorageFamily classes that build-pong-ufs links into the
// kernel image (IOStorage, IOMedia, IOBlockStorageDriver, IOMediaBSDClient, IOPartitionScheme,
// IOGUIDPartitionScheme, compiled from IOStorageFamily-323).
//
// A kext carries its matching rules in Info.plist. These classes are not in a kext, and the
// kernel cannot load one this early (kextd does not exist yet), so the personalities
// IOStorageFamily's Info.plist declares for the classes we link are added to the catalogue here.
#include <IOKit/IOLib.h>
#include <IOKit/IOCatalogue.h>
#include <libkern/c++/OSArray.h>
#include <libkern/c++/OSBoolean.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSString.h>
#include <libkern/c++/OSSymbol.h>

namespace {
// Returns a retained dictionary or null. Every key is optional except class and provider.
OSDictionary *personality(const char *cls, const char *provider, const char *category,
                          unsigned long long score, OSDictionary *propertyMatch,
                          const char *resourceMatch, const char *contentMask)
{
  OSDictionary *dict = OSDictionary::withCapacity(8);
  if (!dict) return nullptr;
  bool ok = true;
  auto text = [&](const char *key, const char *value) {
    if (!value) return;
    OSString *string = OSString::withCString(value);
    ok = ok && string && dict->setObject(key, string);
    if (string) string->release();
  };
  text("IOClass", cls);
  text("IOProviderClass", provider);
  text("IOMatchCategory", category);
  text("IOResourceMatch", resourceMatch);
  text("Content Mask", contentMask);
  if (score) {
    OSNumber *number = OSNumber::withNumber(score, 32);
    ok = ok && number && dict->setObject("IOProbeScore", number);
    if (number) number->release();
  }
  if (propertyMatch) ok = ok && dict->setObject("IOPropertyMatch", propertyMatch);
  if (!ok) {
    dict->release();
    return nullptr;
  }
  return dict;
}

OSDictionary *match(const char *key, OSObject *value)
{
  OSDictionary *dict = OSDictionary::withCapacity(1);
  if (dict && !dict->setObject(key, value)) {
    dict->release();
    dict = nullptr;
  }
  return dict;
}
}

extern "C" bool pong_storage_family_start(void)
{
  static bool registered;
  if (registered) return true;
  if (!gIOCatalogue) {
    IOLog("PONG_UFS: storage family: no IOCatalogue\n");
    return false;
  }
  OSString *generic = OSString::withCString("Generic");
  OSDictionary *deviceMatch = generic ? match("device-type", generic) : nullptr;
  OSDictionary *wholeMatch = match("Whole", kOSBooleanTrue);
  OSDictionary *driver = deviceMatch ? personality("IOBlockStorageDriver", "IOBlockStorageDevice",
                                                    nullptr, 0, deviceMatch, nullptr, nullptr) : nullptr;
  OSDictionary *guid = wholeMatch ? personality("IOGUIDPartitionScheme", "IOMedia", "IOStorage",
                                                 4000, wholeMatch, nullptr, "GUID_partition_scheme") : nullptr;
  OSDictionary *bsd = personality("IOMediaBSDClient", "IOMedia", "IOMediaBSDClient", 30000,
                                  nullptr, "IOBSD", nullptr);
  OSArray *array = OSArray::withCapacity(3);
  bool ok = driver && guid && bsd && array && array->setObject(driver) && array->setObject(guid) &&
            array->setObject(bsd);
  if (ok) ok = gIOCatalogue->addDrivers(array, true);
  if (array) array->release();
  if (bsd) bsd->release();
  if (guid) guid->release();
  if (driver) driver->release();
  if (wholeMatch) wholeMatch->release();
  if (deviceMatch) deviceMatch->release();
  if (generic) generic->release();
  IOLog("PONG_UFS: built-in IOStorageFamily personalities %s (IOBlockStorageDriver, "
        "IOGUIDPartitionScheme, IOMediaBSDClient)\n", ok ? "added" : "FAILED");
  registered = ok;
  return ok;
}
