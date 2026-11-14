#!/usr/bin/env python3
"""Build Linux 7.0.14 ath11k PCI/MHI using UniXNU's shared Wi-Fi ABI.

Sources are local public Linux and UniXNU Ath12kWifi checkouts.
The driver is not replaced by success stubs. Missing core symbols fail the
link recipe; power/DMA/MSI remain the consuming Pong board's responsibility.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
from pong_native_inputs import build_env, input_path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import pong_paths


def t970_edits(gen, mhi, shared, out):
    """SM-T970 (QCA6390 hw2.0). Returns the nw_qrtr.c to compile. Every anchor must match exactly once.

    1. debug.c: XNU's vsnprintf has no %pV, so every ath11k_info/warn/err line printed as "0x...V". The message is
       formatted first. Boot and QMI debug lines (ATH11K_DBG_BOOT | ATH11K_DBG_QMI = 0x60) print as well.
    2. pci.c: the WLAON power control write also clears SHUTDOWN_EN (bit 0), as Qualcomm's cnss2 does for this chip.
    3. nw_qrtr.c: the firmware announces its servers (WLFW) only to a node whose HELLO reached its name service after
       its own. The module answers no remote HELLO once it has sent one; on the same tablet that left QMI silent in
       two runs of three (UniXNU-gts7xlwifi docs/wifi-sources.md, combo5). The first remote HELLO is answered once.
    4. MHI main.c: a transfer event the host does not know (the same tablet stopped on 0x11, BAD_TRE, in the board
       file download) is logged with its channel, its words, the ring offsets, the TRE it names and the channel
       context as the device left it.
    """
    def edit(target, pairs, source=None):
        text = (source or target).read_text()
        for old, new in pairs:
            if text.count(old) != 1:
                raise RuntimeError('T970: %s differs at %r (%d)' % (target.name, old[:50], text.count(old)))
            text = text.replace(old, new)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_text(text)

    fmt = ('\t{ va_list t970c; char t970b[224]; va_copy(t970c, args); vsnprintf(t970b, sizeof t970b, fmt, t970c); '
           'va_end(t970c);\n\t  %s }\n')
    edit(gen / 'debug.c', [
        ('\tdev_info(ab->dev, "%pV", &vaf);\n', fmt % 'dev_info(ab->dev, "%s", t970b);'),
        ('\tdev_err(ab->dev, "%pV", &vaf);\n', fmt % 'dev_err(ab->dev, "%s", t970b);'),
        ('\tdev_warn_ratelimited(ab->dev, "%pV", &vaf);\n', fmt % 'dev_warn(ab->dev, "%s", t970b);'),
        ('\tif (ath11k_debug_mask & mask)\n\t\tdev_printk(KERN_DEBUG, ab->dev, "%s %pV", ath11k_dbg_str(mask), &vaf);\n',
         '\tif ((ath11k_debug_mask | 0x60) & mask)\n'
         + fmt % 'dev_info(ab->dev, "%s %s", ath11k_dbg_str(mask), t970b);'),
    ])
    edit(gen / 'pci.c', [
        ('\tval &= ~QFPROM_PWR_CTRL_VDD4BLOW_MASK;\n', '\tval &= ~(QFPROM_PWR_CTRL_VDD4BLOW_MASK | 0x1);\n'),
    ])
    qrtr = out / 'nw_qrtr_t970.c'
    edit(qrtr, [
        ('static int g_hello_sent;\n',
         'static int g_hello_sent;\nstatic int g_hello_answered;   /* SM-T970: a remote HELLO has been answered */\n'),
        ('    if (src_node != LOCAL_NODE && !g_hello_sent) { g_hello_sent = 1; send_remote(',
         '    if (src_node != LOCAL_NODE && (!g_hello_sent || !g_hello_answered)) { g_hello_sent = 1; g_hello_answered = 1;\n'
         '      lx_log(6, "qrtr: HELLO from node %u answered", src_node); send_remote('),
        ('  g_xmit = 0; g_hello_sent = 0;\n', '  g_xmit = 0; g_hello_sent = 0; g_hello_answered = 0;\n'),
        ('  g_xmit = xmit; g_ctx = ctx; g_hello_sent = xmit != 0;\n',
         '  g_xmit = xmit; g_ctx = ctx; g_hello_sent = xmit != 0; g_hello_answered = 0;\n'),
        ('  if (len < sizeof *p) return;\n  switch (le32_to_cpu(p->cmd)) {\n',
         '  if (len < sizeof *p) return;\n'
         '  if (src_node != LOCAL_NODE) lx_log(6, "qrtr: ctrl %u from %u:%u svc %x", le32_to_cpu(p->cmd), src_node, src_port,\n'
         '                                     le32_to_cpu(p->server.service));\n'
         '  switch (le32_to_cpu(p->cmd)) {\n'),
    ], source=shared / 'src/nw_qrtr.c')
    edit(mhi / 'main.c', [
        ('\tdefault:\n\t\tdev_err(dev, "Unknown event 0x%x\\n", ev_code);\n\t\tbreak;\n',
         '\tdefault:\n\t{\n'
         '\t\tdma_addr_t t970p = MHI_TRE_GET_EV_PTR(event);\n'
         '\t\tstruct mhi_ring_element *t970t = is_valid_ring_ptr(tre_ring, t970p) ? mhi_to_virtual(tre_ring, t970p) : NULL;\n'
         '\t\tconst __le32 *t970w = (const __le32 *)&mhi_cntrl->mhi_ctxt->chan_ctxt[mhi_chan->chan];\n'
         '\t\tdev_err(dev, "Unknown event 0x%x ch %u ev %llx %08x %08x ring %llx+%x rp %x wp %x\\n", ev_code, mhi_chan->chan,\n'
         '\t\t\t(unsigned long long)le64_to_cpu(event->ptr), le32_to_cpu(event->dword[0]), le32_to_cpu(event->dword[1]),\n'
         '\t\t\t(unsigned long long)tre_ring->iommu_base, (unsigned)tre_ring->len,\n'
         '\t\t\t(unsigned)((u8 *)tre_ring->rp - (u8 *)tre_ring->base), (unsigned)((u8 *)tre_ring->wp - (u8 *)tre_ring->base));\n'
         '\t\tif (t970t)\n'
         '\t\t\tdev_err(dev, "t970: TRE %llx %08x %08x\\n", (unsigned long long)le64_to_cpu(t970t->ptr),\n'
         '\t\t\t\tle32_to_cpu(t970t->dword[0]), le32_to_cpu(t970t->dword[1]));\n'
         '\t\tdev_err(dev, "t970: ctxt %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x\\n", le32_to_cpu(t970w[0]),\n'
         '\t\t\tle32_to_cpu(t970w[1]), le32_to_cpu(t970w[2]), le32_to_cpu(t970w[3]), le32_to_cpu(t970w[4]),\n'
         '\t\t\tle32_to_cpu(t970w[5]), le32_to_cpu(t970w[6]), le32_to_cpu(t970w[7]), le32_to_cpu(t970w[8]),\n'
         '\t\t\tle32_to_cpu(t970w[9]), le32_to_cpu(t970w[10]));\n'
         '\t\tbreak;\n\t}\n'),
    ])
    # 5. nw_sta.c (the copy with the kernel's entropy source): the station sends the key handshake as 802.11 data
    #    frames while every other frame goes out as an Ethernet frame the firmware converts. On this tablet the
    #    association succeeded and the access point then never accepted handshake message 2 (four retries, reason
    #    2). With nw_wifi_t970_eapol_eth (default 1, the board can clear it before the start) the handshake frames
    #    take the Ethernet path too, as the stock Android driver sends them to this firmware.
    edit(out / 'nw_sta_pong.c', [
        ('static void send_eapol(const struct eapol_key *k, unsigned total) {\n',
         'volatile int nw_wifi_t970_eapol_eth = 1;\n'
         'static void send_eapol(const struct eapol_key *k, unsigned total) {\n'
         '  if (nw_wifi_t970_eapol_eth) { u8 e[14 + 512]; if (total > 512) return;\n'
         '    memcpy(e, C.ap.bssid, 6); memcpy(e + 6, g_vif->addr, 6); e[12] = 0x88; e[13] = 0x8e; memcpy(e + 14, k, total);\n'
         '    tx_eth(e, 14 + total); return; }\n'),
    ])
    # 6. nw_sta.c: on this tablet no broadcast frame reached the network stack while unicast frames did. Count the
    #    group-addressed data frames at the station's receive hook and in to_eth(): seen at all, from the access
    #    point, protected, decrypted, IV stripped, delivered, dropped for a bad LLC header or for being short; keep
    #    the first one's eight bytes after the header. nw_wifi_t970_rx_stats() gives the board the line.
    edit(out / 'nw_sta_pong.c', [
        ('static void to_eth(struct sk_buff *skb) {\n',
         'unsigned nw_wifi_t970_grp[10]; static u8 nw_t970_grp_head[8];\n'
         'void nw_wifi_t970_rx_stats(char *b, int n) {\n'
         '  if (n > 0) snprintf(b, n, "group data %u from-ap %u prot %u decr %u ivstr %u delivered %u bad-llc %u short %u; first %02x%02x%02x%02x%02x%02x%02x%02x flag %x fc %x",\n'
         '    nw_wifi_t970_grp[8], nw_wifi_t970_grp[0], nw_wifi_t970_grp[1], nw_wifi_t970_grp[2], nw_wifi_t970_grp[3], nw_wifi_t970_grp[4],\n'
         '    nw_wifi_t970_grp[5], nw_wifi_t970_grp[6], nw_t970_grp_head[0], nw_t970_grp_head[1], nw_t970_grp_head[2], nw_t970_grp_head[3],\n'
         '    nw_t970_grp_head[4], nw_t970_grp_head[5], nw_t970_grp_head[6], nw_t970_grp_head[7], nw_wifi_t970_grp[7], nw_wifi_t970_grp[9]); }\n'
         'static void to_eth(struct sk_buff *skb) {\n'),
        ('  if (skb->len < hl + 8) return;\n  const u8 *llc = skb->data + hl; if (llc[0] != 0xaa || llc[1] != 0xaa || llc[2] != 3) return;\n',
         '  const int t970g = is_multicast_ether_addr(h->addr1);\n'
         '  if (t970g) { nw_wifi_t970_grp[0]++; if (ieee80211_has_protected(h->frame_control)) nw_wifi_t970_grp[1]++;\n'
         '    if (st->flag & RX_FLAG_DECRYPTED) nw_wifi_t970_grp[2]++; if (st->flag & RX_FLAG_IV_STRIPPED) nw_wifi_t970_grp[3]++;\n'
         '    if (nw_wifi_t970_grp[0] == 1) { if (skb->len >= hl + 8) memcpy(nw_t970_grp_head, skb->data + hl, 8);\n'
         '      nw_wifi_t970_grp[7] = (unsigned)st->flag; nw_wifi_t970_grp[9] = le16_to_cpu(h->frame_control); } }\n'
         '  if (skb->len < hl + 8) { if (t970g) nw_wifi_t970_grp[6]++; return; }\n'
         '  const u8 *llc = skb->data + hl; if (llc[0] != 0xaa || llc[1] != 0xaa || llc[2] != 3) { if (t970g) nw_wifi_t970_grp[5]++; return; }\n'),
        ('  if (g_ev.rx) g_ev.rx(eth, 14 + plen); }\n',
         '  if (t970g) nw_wifi_t970_grp[4]++;\n  if (g_ev.rx) g_ev.rx(eth, 14 + plen); }\n'),
        ('  if (ieee80211_is_data(h->frame_control) && !memcmp(h->addr2, C.ap.bssid, 6)) {\n',
         '  if (ieee80211_is_data(h->frame_control) && is_multicast_ether_addr(h->addr1)) nw_wifi_t970_grp[8]++;\n'
         '  if (ieee80211_is_data(h->frame_control) && !memcmp(h->addr2, C.ap.bssid, 6)) {\n'),
    ])
    return qrtr


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--linux', type=Path, default=input_path('wifi_linux'))
    p.add_argument('--shared', type=Path, default=input_path('wifi_shared'))
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--t970', action='store_true', help='SM-T970 (QCA6390): the four edits of t970_edits()')
    a = p.parse_args(); linux = a.linux.resolve(); shared = a.shared.resolve(); out = a.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not (linux / 'drivers/net/wireless/ath/ath11k/pci.c').is_file(): raise RuntimeError('Linux ath11k PCI sources required')
    for file in ('include/nw_abi.h', 'shim/src/lx_rt.c', 'src/nw_sta.c'):
        if not (shared / file).is_file(): raise RuntimeError('UniXNU Ath12kWifi shared ABI required: ' + file)
    source = linux / 'drivers/net/wireless/ath/ath11k'
    gen = out / 'ath11k'; gen.mkdir(exist_ok=True)
    for f in source.glob('*'):
        if f.suffix not in ('.c', '.h') or f.name == 'trace.h': continue
        target = gen / f.name
        if target.exists() or target.is_symlink(): target.unlink()
        target.symlink_to(f)
    traces = sorted({v for f in source.glob('*.[ch]') for v in re.findall(r'\btrace_ath11k_[a-z0-9_]+', f.read_text())})
    (gen / 'trace.h').write_text('\n'.join('#define ' + n + '(...) ' + ('0' if n.endswith('_enabled') else '((void)0)') for n in traces) + '\n')
    spectral = out / 'spectral_common.h'
    if spectral.exists() or spectral.is_symlink(): spectral.unlink()
    spectral.symlink_to(source.parent / 'spectral_common.h')
    # A kernel variable-like jiffies macro cannot also name this statistics
    # member. Rename only the generated member/access, not time expressions.
    for name in ('core.h', 'dp_rx.c'):
        content = (source / name).read_text().replace('unsigned long jiffies;', 'unsigned long bp_jiffies;').replace('bp_stats->jiffies', 'bp_stats->bp_jiffies')
        (gen / name).unlink(); (gen / name).write_text(content)
    pc = (source / 'pci.c').read_text()
    for old, new in [('static int ath11k_pci_init(void)', 'int pong_ath11k_pci_init(void)'),
                     ('static void ath11k_pci_exit(void)', 'void pong_ath11k_pci_exit(void)')]:
        if pc.count(old) != 1: raise RuntimeError('Linux 7.0.14 PCI entry differs: ' + old)
        pc = pc.replace(old, new)
    (gen / 'pci.c').unlink(); (gen / 'pci.c').write_text(pc)
    # Nothing CNSS2 selection, driven by actual QMI chip/board IDs. Linux's
    # board-2 container is not part of this phone's firmware partition.
    core = (gen / 'core.c').read_text()
    needle = '\tbd->fw = ath11k_core_firmware_request(ab, name);'
    replacement = r"""    char pong_bdf[48];
    if (!strcmp(name, ATH11K_DEFAULT_BOARD_FILE)) {
        u32 id = ab->qmi.target.board_id;
        const char *prefix = (ab->qmi.target.chip_id & 0x10) ? "bdwlang" : "bdwlan";
        if (id == 0xff) snprintf(pong_bdf, sizeof pong_bdf, "%s.elf", prefix);
        else if (id < 0xff) snprintf(pong_bdf, sizeof pong_bdf, "%s.e%02x", prefix, id);
        else snprintf(pong_bdf, sizeof pong_bdf, "bdwlan%02x.e%02x", (id >> 8) & 255, id & 255);
        name = pong_bdf;
        ath11k_info(ab, "Pong: actual QMI board firmware %s\n", name);
    }
""" + needle
    if core.count(needle) != 1: raise RuntimeError('ath11k BDF request ABI differs')
    (gen / 'core.c').unlink(); (gen / 'core.c').write_text(core.replace(needle, replacement))
    # A radio crash is terminal for this dispatcher incarnation. Do not send
    # new WMI packets or repeatedly schedule Linux recovery inside the work
    # item that is still bringing the MAC up; the board retains DMA/reset.
    mhi_source = (source / 'mhi.c').read_text()
    needle = '\tswitch (cb) {\n\tcase MHI_CB_SYS_ERROR:'
    replacement = '''    if (cb == MHI_CB_SYS_ERROR || cb == MHI_CB_EE_RDDM || cb == MHI_CB_FATAL_ERROR) {
        extern void pong_ath11k_mhi_failed(struct mhi_controller *, int);
        pong_ath11k_mhi_failed(mhi_cntrl, cb);
        ab_pci->mhi_pre_cb = cb;
        return;
    }
''' + needle
    if mhi_source.count(needle) != 1: raise RuntimeError('ath11k MHI status ABI differs')
    (gen / 'mhi.c').unlink(); (gen / 'mhi.c').write_text(mhi_source.replace(needle, replacement))
    wmi = (source / 'wmi.c').read_text()
    needle = '\tstruct ath11k_base *ab = wmi_ab->ab;\n\n\tmight_sleep();'
    replacement = '''\tstruct ath11k_base *ab = wmi_ab->ab;
    extern void pong_ath11k_wmi_submitted(u32);
    if (test_bit(ATH11K_FLAG_CRASH_FLUSH, &ab->dev_flags)) return -ESHUTDOWN;
    pong_ath11k_wmi_submitted(cmd_id);

\tmight_sleep();'''
    if wmi.count(needle) != 1: raise RuntimeError('ath11k WMI send ABI differs')
    (gen / 'wmi.c').unlink(); (gen / 'wmi.c').write_text(wmi.replace(needle, replacement))
    # Pong's mobile HSP firmware has no MBSSID AP context for a station.
    # Linux's generic fallback labels even a station NON_MBSSID_AP; don't
    # send AP-role flags on this STA-only board integration.
    mac_source = (source / 'mac.c').read_text()
    needle = '\tparams->mbssid_tx_vdev_id = 0;\n\n\tif (!test_bit('
    replacement = '''\tparams->mbssid_tx_vdev_id = 0;
    if (arvif->vif->type == NL80211_IFTYPE_STATION) goto pong_station_chains;

\tif (!test_bit('''
    if mac_source.count(needle) != 1: raise RuntimeError('ath11k vdev parameters differ')
    mac_source = mac_source.replace(needle, replacement)
    needle = '\tif (pdev->cap.supported_bands & WMI_HOST_WLAN_2G_CAP) {\n\t\tparams->chains'
    if mac_source.count(needle) != 1: raise RuntimeError('ath11k STA chain setup differs')
    mac_source = mac_source.replace(needle, 'pong_station_chains:\n' + needle)
    # ath12k station uses split link/vif notifications; ath11k implements
    # their combined legacy bss_info_changed callback. Forward real changes
    # to that implementation, including assoc/disassoc, rather than NULL or
    # a success stub. The non-MLO vif's default link is the selected BSS.
    marker = 'static const struct ieee80211_ops ath11k_ops = {'
    adapter = """static void pong_ath11k_vif_cfg_changed(struct ieee80211_hw *hw,
        struct ieee80211_vif *vif, u64 changed)
{
    ath11k_mac_op_bss_info_changed(hw, vif, &vif->bss_conf, changed);
}

"""
    if mac_source.count(marker) != 1: raise RuntimeError('ath11k operations table differs')
    mac_source = mac_source.replace(marker, adapter + marker)
    needle = '\t.bss_info_changed               = ath11k_mac_op_bss_info_changed,'
    if mac_source.count(needle) != 1: raise RuntimeError('ath11k combined BSS callback differs')
    mac_source = mac_source.replace(needle, needle +
        '\n\t.link_info_changed              = ath11k_mac_op_bss_info_changed,' +
        '\n\t.vif_cfg_changed                = pong_ath11k_vif_cfg_changed,')
    (gen / 'mac.c').unlink(); (gen / 'mac.c').write_text(mac_source)
    mac = (shared / 'src/nw_mac80211.c').read_text()
    mac += r"""
/* Linux ath11k still uses these mac80211 APIs; the shared station has one
 * hardware and an explicit active-vif list. Honour the supplied vif address. */
struct ieee80211_sta *ieee80211_find_sta_by_ifaddr(struct ieee80211_hw *hw, const u8 *addr, const u8 *local) {
    if (hw != g_hw || !g_sta || !ether_addr_equal(g_sta->addr, addr)) return 0;
    for (int i = 0; i < g_nvif; ++i) if (!local || ether_addr_equal(g_vif[i]->addr, local)) return g_sta;
    return 0;
}
void ieee80211_queue_work(struct ieee80211_hw *hw, struct work_struct *work) { if (hw == g_hw) lx_queue_work(work); }
"""
    (out / 'nw_mac80211_pong.c').write_text(mac)

    pinned = out / 'pinned'; pinned.mkdir(exist_ok=True)
    inputs = {'linux/mhi.h': 'linux/mhi.h', 'linux/ieee80211.h': 'linux/ieee80211.h',
        'linux/soc/qcom/qmi.h': 'linux/soc/qcom/qmi.h', 'net/cfg80211.h': 'net/cfg80211.h',
        'net/regulatory.h': 'net/regulatory.h', 'net/codel.h': 'net/codel.h',
        'net/ieee80211_radiotap.h': 'net/ieee80211_radiotap.h',
        'linux/nl80211.h': 'uapi/linux/nl80211.h', 'linux/rfkill.h': 'uapi/linux/rfkill.h',
        'uapi/linux/rfkill.h': 'uapi/linux/rfkill.h', 'net/mac80211.real.h': 'net/mac80211.h'}
    inputs.update({str(f.relative_to(linux / 'include')): str(f.relative_to(linux / 'include'))
                   for f in (linux / 'include/linux').glob('ieee80211-*.h')})
    for target, name in inputs.items():
        src = linux / 'include' / name
        if not src.is_file(): raise RuntimeError('Missing Linux public header: ' + str(src))
        dest = pinned / target; dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink(): dest.unlink()
        dest.symlink_to(src)
    mhi = out / 'mhi'; mhi.mkdir(exist_ok=True)
    for name in ('init.c', 'main.c', 'pm.c', 'boot.c', 'internal.h'):
        dest = mhi / name
        if dest.exists() or dest.is_symlink(): dest.unlink()
        dest.symlink_to(linux / 'drivers/bus/mhi/host' / name)
    (mhi / 'trace.h').write_text((shared / 'shim/mhi/trace.h').read_text())
    common = out / 'common.h'
    if common.exists() or common.is_symlink(): common.unlink()
    common.symlink_to(linux / 'drivers/bus/mhi/common.h')
    env = build_env()
    clang = subprocess.check_output(['xcrun', '-f', 'clang'], env=env, text=True).strip()
    resource = subprocess.check_output([clang, '-print-resource-dir'], text=True).strip() + '/include'
    overlay = ROOT / 'drivers/PongWifi/ath11k/compat'
    flags = ['-target', 'arm64-apple-ios18.0', '-mkernel', '-O2', '-g0', '-ffreestanding',
        '-fno-builtin', '-fno-common', '-fno-strict-aliasing', '-fwrapv', '-fno-stack-protector',
        '-fno-delete-null-pointer-checks', '-mgeneral-regs-only', '-std=gnu11',
        '-Wno-incompatible-pointer-types', '-Wno-error', '-Wno-unused-value',
        '-nostdinc', '-isystem', str(shared / 'shim/libc'), '-isystem', resource,
        '-I', str(overlay), '-include', 'PongCompat.h', '-I', str(shared / 'shim/include'),
        '-I', str(shared / 'include'), '-I', str(pinned), '-I', str(shared / 'src'),
        '-I', str(gen), '-I', str(out), '-I', str(source.parent), '-DCONFIG_ATH11K_PCI=1',
        '-DLX_BOOT_SETTLE_MS=0']
    ath = 'core hal hal_tx hal_rx wmi mac reg htc qmi dp dp_tx dp_rx ce peer dbring hw pcic fw p2p mhi pci'.split()
    jobs = [(gen / (n + '.c'), out / ('ath11k_' + n + '.o'), []) for n in ath]
    jobs += [(mhi / (n + '.c'), out / ('mhi_' + n + '.o'), ['-I', str(mhi)]) for n in ('init', 'main', 'pm', 'boot')]
    # WPA station nonces use Darwin's entropy source, not a timer/counter.
    sta = (shared / 'src/nw_sta.c').read_text()
    begin = sta.index('static void make_nonce(u8 *out) {')
    end = sta.index('static void derive_ptk(void)', begin)
    sta = sta[:begin] + 'extern void PongWifiRandom(void *, size_t);\nstatic void make_nonce(u8 *out) { PongWifiRandom(out, 32); }\n' + sta[end:]
    (out / 'nw_sta_pong.c').write_text(sta)
    # Preserve the user's no-checksum policy in the shared loader too.
    runtime = (shared / 'shim/src/lx_rt.c').read_text()
    begin = runtime.index('  static int settled;\n', runtime.index('int lx_request_firmware('))
    end = runtime.index('  *fw = f;', begin)
    runtime = runtime[:begin] + runtime[end:]
    (out / 'lx_rt_pong.c').write_text(runtime)
    common_sources = ['src/nw_crypto.c', 'src/nw_mac80211.c', 'src/nw_net.c', 'src/nw_pci.c',
        'src/nw_qrtr.c', 'src/nw_qrtr_mhi.c', 'src/nw_sta.c', 'shim/src/lx_rt.c', 'shim/src/mhi_hook.c']
    replacements = {'src/nw_sta.c': out / 'nw_sta_pong.c',
                    'src/nw_mac80211.c': out / 'nw_mac80211_pong.c',
                    'shim/src/lx_rt.c': out / 'lx_rt_pong.c'}
    if a.t970: replacements['src/nw_qrtr.c'] = t970_edits(gen, mhi, shared, out)
    jobs += [(replacements.get(n, shared / n), out / ('shared_' + Path(n).stem + '.o'), []) for n in common_sources]
    jobs += [(linux / ('drivers/soc/qcom/' + n + '.c'), out / (n + '.o'), []) for n in ('qmi_interface', 'qmi_encdec')]
    jobs += [(ROOT / 'drivers/PongWifi/ath11k/PongAth11k.c', out / 'PongAth11k.o', [])]
    commands = []
    def compile(job):
        src, obj, extra = job; cmd = [clang, *flags, *extra, '-c', str(src), '-o', str(obj)]
        commands.append(cmd)
        r = subprocess.run(cmd, capture_output=True, text=True)
        (out / (obj.stem + '.log')).write_text(r.stdout + r.stderr)
        return obj.name if r.returncode else None
    with ThreadPoolExecutor(a.jobs) as pool: failed = [r for r in pool.map(compile, jobs) if r]
    (out / 'compile-commands.json').write_text(json.dumps(commands, indent=2) + '\n')
    if failed: raise SystemExit('ath11k compile failures (retained .log): ' + ', '.join(failed))
    archive = out / 'libPongAth11k.a'
    linked = out / 'ath11k-combined.o'
    subprocess.run(['xcrun', 'ld', '-r', '-arch', 'arm64', *[str(o) for _, o, _ in jobs], '-o', str(linked)], env=env, check=True)
    nm = pong_paths.tool('llvm-nm', 'PONG_LLVM_NM', ('/opt/local/bin/llvm-nm-mp-18',))
    objcopy = pong_paths.tool('llvm-objcopy', 'PONG_LLVM_OBJCOPY', ('/opt/local/bin/llvm-objcopy-mp-18',))
    undefined = subprocess.check_output([nm, '-u', '-j', str(linked)], text=True).splitlines()
    (out / 'undefined-symbols.txt').write_text('\n'.join(undefined) + '\n')
    allowed = {'_PongWifiRandom','_bzero','_memcmp','_memcpy','_memmove','_memset','_snprintf','_strchr','_strcmp','_strlen','_strncmp','_strncpy','_strtoul','_vsnprintf'}
    if set(undefined) - allowed: raise RuntimeError('unimplemented ath11k imports: ' + repr(set(undefined) - allowed))
    # Export only the documented board/station boundary. Linux shims have the
    # same C names as XNU helpers, so namespace every other defined symbol.
    exports = {'_PongWifiRandom', '_nw_wifi_where'}
    defined = subprocess.check_output([nm, '--defined-only', '--extern-only', '-j', str(linked)], text=True).splitlines()
    renames = {n: '_pong11_' + n[1:] for n in defined if not n.startswith('_nw_wifi_') and n not in exports}
    mapping = out / 'symbol-map.txt'; mapping.write_text(''.join(k + ' ' + v + '\n' for k, v in renames.items()))
    namespaced = out / 'PongAth11k-linked.o'
    subprocess.run([objcopy, '--redefine-syms=' + str(mapping), str(linked), str(namespaced)], check=True)
    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'libtool', '-static', '-o', str(archive), str(namespaced)], env=env, check=True)
    (out / 'build-report.json').write_text(json.dumps(dict(linux='v7.0.14', shared=str(shared),
        guest='22H374', archive=str(archive), real_pci_mhi=True, wifi_operational=False,
        missing_symbols_policy='no fabricated-success stubs', image_verification='skipped by request'), indent=2) + '\n')
    print(archive)


if __name__ == '__main__': main()
