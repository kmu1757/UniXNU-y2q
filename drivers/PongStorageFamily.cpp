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
#include <IOKit/IOService.h>
#include <kern/thread.h>
#include <libkern/c++/OSIterator.h>
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


// The kernel has no other way to say what the storage stack did after the transport published
// its disks: the root-mount wait prints once and then stays silent. Count the objects a few
// times, with the properties that decide whether the root volume can ever appear.
static unsigned countClass(const char *name)
{
  OSDictionary *matching = IOService::serviceMatching(name);
  OSIterator *iterator = matching ? IOService::getMatchingServices(matching) : nullptr;
  unsigned count = 0;
  while (iterator && iterator->getNextObject()) ++count;
  if (iterator) iterator->release();
  if (matching) matching->release();
  return count;
}

static void dumpStorage(unsigned seconds)
{
  IOLog("PONG_STORAGE t+%us: device=%u driver=%u media=%u gpt-scheme=%u bsd-client=%u\n", seconds,
        countClass("IOBlockStorageDevice"), countClass("IOBlockStorageDriver"), countClass("IOMedia"),
        countClass("IOGUIDPartitionScheme"), countClass("IOMediaBSDClient"));
  OSDictionary *matching = IOService::serviceMatching("IOMedia");
  OSIterator *iterator = matching ? IOService::getMatchingServices(matching) : nullptr;
  unsigned index = 0;
  while (iterator) {
    IOService *media = OSDynamicCast(IOService, iterator->getNextObject());
    if (!media) break;
    OSString *bsd = OSDynamicCast(OSString, media->getProperty("BSD Name"));
    OSString *content = OSDynamicCast(OSString, media->getProperty("Content"));
    OSString *uuid = OSDynamicCast(OSString, media->getProperty("UUID"));
    OSNumber *size = OSDynamicCast(OSNumber, media->getProperty("Size"));
    IOService *provider = media->getProvider();
    IOLog("PONG_STORAGE   media#%u bsd=%s whole=%d content=%s size=%llu uuid=%s provider=%s\n", index++,
          bsd ? bsd->getCStringNoCopy() : "-",
          media->getProperty("Whole") == kOSBooleanTrue,
          content ? content->getCStringNoCopy() : "-",
          size ? (unsigned long long) size->unsigned64BitValue() : 0ULL,
          uuid ? uuid->getCStringNoCopy() : "-",
          provider ? provider->getMetaClass()->getClassName() : "-");
  }
  if (iterator) iterator->release();
  if (matching) matching->release();
}

static void storageReport(void *, wait_result_t)
{
  static const unsigned marks[] = {3, 10, 30, 90};
  unsigned elapsed = 0;
  for (unsigned mark : marks) {
    IOSleep((mark - elapsed) * 1000);
    elapsed = mark;
    dumpStorage(mark);
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
  if (ok) {
    thread_t thread = nullptr;
    if (kernel_thread_start(storageReport, nullptr, &thread) == KERN_SUCCESS) thread_deallocate(thread);
    else IOLog("PONG_UFS: storage report thread not started\n");
  }
  return ok;
}
