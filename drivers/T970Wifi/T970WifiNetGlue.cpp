// SPDX-License-Identifier: BSD-2-Clause-Patent
// SM-T970: starts PongWifi's network side (drivers/PongWifi/PongWifiNet.cpp: the en0 interface, the registry entries
// by which the wireless daemon finds it, and the Apple80211 requests) for the ath11k station that the kernel's
// bsd/dev/pong_wifi_stack.c runs. Both entry points are called from that worker thread, nothing runs at boot.
//
// PongWifiNet.cpp's IOService classes are not registered by the kernel's own initialisers: the build renames the
// object's initialiser section, and the classes register here between preModLoad and postModLoad, once the radio
// has a hardware address (the same staging as the Nothing Phone's board file).
#include <IOKit/IOLib.h>
#include <IOKit/IORegistryEntry.h>
#include <IOKit/IODeviceTreeSupport.h>
#include <libkern/c++/OSMetaClass.h>
#include <mach/kmod.h>

extern "C" {
bool pong_wifi_net_prepare(void);
void pong_wifi_net_start(const uint8_t mac[6]);
void PongWifiNetStaticInit(void);
int t970_wifi_net_prepare(void);
int t970_wifi_net_up(const uint8_t mac[6]);
}
extern kmod_info_t g_kernel_kmod_info;

// Before nw_wifi_start: the module's station events must have their handlers (nw_abi.h).
extern "C" int t970_wifi_net_prepare(void)
{
    return pong_wifi_net_prepare() ? 0 : -1;
}

// Once the station is ready and reports a valid address. 0 when en0 was published (or already is).
extern "C" int t970_wifi_net_up(const uint8_t mac[6])
{
    static bool up;

    if (up) return 0;
    if (IORegistryEntry *chosen = IORegistryEntry::fromPath("/chosen", gIODTPlane)) {
        chosen->setProperty("mac-address-wifi0", const_cast<uint8_t *>(mac), 6);
        chosen->release();
    }
    void *handle = OSMetaClass::preModLoad(g_kernel_kmod_info.name);
    if (!handle) return -2;
    PongWifiNetStaticInit();
    if (OSMetaClass::postModLoad(handle) != kIOReturnSuccess) return -3;
    pong_wifi_net_start(mac);
    up = true;
    return 0;
}
