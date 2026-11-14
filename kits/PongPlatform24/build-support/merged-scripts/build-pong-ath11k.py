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
    # 7. On this tablet 15-20 % of the non-beacon frames vanished inside nw_sta_rx() without a count, TCP retransmitted
    #    ~9 % of its segments on timeouts, and the data frames' transmit results were thrown away (only EAPOL is
    #    counted). Count the receive drops by reason (action frames, Block Ack, ADDBA requests, data not from the
    #    access point, to_eth() short / not LLC (keep the first such frame's 8 bytes, frame control and flags),
    #    Ethernet-form frames dropped (EAPOL among them), other non-data) and the transmit results by length
    #    (acknowledged / not / freed without status). The station never answered the access point's ADDBA request;
    #    it now declines it (status 37), so the access point stops negotiating and sends unaggregated frames.
    #    nw_wifi_t970_rx_stats(b, n, 0|1) gives the board the two lines.
    edit(out / 'nw_mac80211_pong.c', [
        ('unsigned nw_eapol_tx[3];\n',
         'unsigned nw_eapol_tx[3];\n'
         'unsigned nw_wifi_t970_tx[3][3];   /* [< 128, < 600, larger][acknowledged, not, freed without status] */\n'
         'static int t970_len(const struct sk_buff *s) { return s->len < 128 ? 0 : s->len < 600 ? 1 : 2; }\n'),
        ('void ieee80211_free_txskb(struct ieee80211_hw *hw, struct sk_buff *skb) { (void)hw; ',
         'void ieee80211_free_txskb(struct ieee80211_hw *hw, struct sk_buff *skb) { (void)hw; if (skb) nw_wifi_t970_tx[t970_len(skb)][2]++; '),
        ('void ieee80211_tx_status_irqsafe(struct ieee80211_hw *hw, struct sk_buff *skb) { (void)hw; ',
         'void ieee80211_tx_status_irqsafe(struct ieee80211_hw *hw, struct sk_buff *skb) { (void)hw; if (skb) nw_wifi_t970_tx[t970_len(skb)][2]++; '),
        ('  (void)hw; if (!st || !st->skb) return;\n',
         '  (void)hw; if (!st || !st->skb) return;\n'
         '  nw_wifi_t970_tx[t970_len(st->skb)][!(IEEE80211_SKB_CB(st->skb)->flags & IEEE80211_TX_STAT_ACK)]++;\n'),
    ])
    edit(out / 'nw_sta_pong.c', [
        ('static void to_eth(struct sk_buff *skb) {\n',
         'unsigned nw_wifi_t970_rxd[12]; static u8 nw_t970_nollc[8];\n'
         'static void to_eth(struct sk_buff *skb) {\n'),
        ('  if (skb->len < hl + 8) return;\n  const u8 *llc = skb->data + hl; if (llc[0] != 0xaa || llc[1] != 0xaa || llc[2] != 3) return;\n',
         '  if (skb->len < hl + 8) { nw_wifi_t970_rxd[5]++; return; }\n'
         '  const u8 *llc = skb->data + hl; if (llc[0] != 0xaa || llc[1] != 0xaa || llc[2] != 3) {\n'
         '    if (!nw_wifi_t970_rxd[6]++) { memcpy(nw_t970_nollc, llc, 8); nw_wifi_t970_rxd[10] = le16_to_cpu(h->frame_control); nw_wifi_t970_rxd[11] = (unsigned)st->flag; }\n'
         '    return; }\n'),
        ('      if (skb->len >= 14 && !(skb->data[12] == 0x88 && skb->data[13] == 0x8e) && C.state >= NW_STA_KEYS && g_ev.rx) g_ev.rx(skb->data, skb->len);\n',
         '      if (skb->len >= 14 && !(skb->data[12] == 0x88 && skb->data[13] == 0x8e) && C.state >= NW_STA_KEYS && g_ev.rx) g_ev.rx(skb->data, skb->len);\n'
         '      else { nw_wifi_t970_rxd[7]++; if (skb->len >= 14 && skb->data[12] == 0x88 && skb->data[13] == 0x8e) nw_wifi_t970_rxd[8]++; }\n'),
        ('void nw_sta_rx(struct sk_buff *skb) {\n',
         '/* ADDBA Response declining the access point\'s request: category 3 (Block Ack), action 1, its dialog token,\n'
         ' * status 37 (request declined), its parameter set, timeout 0. */\n'
         'static void t970_addba_decline(const u8 *req) {\n'
         '  struct sk_buff *skb = new_frame(9); if (!skb) return;\n'
         '  mgmt_header(skb, IEEE80211_FTYPE_MGMT | IEEE80211_STYPE_ACTION);\n'
         '  u8 *b = skb_put_zero(skb, 9); b[0] = 3; b[1] = 1; b[2] = req[2]; b[3] = 37; b[4] = 0; b[5] = req[3]; b[6] = req[4];\n'
         '  tx_frame(skb); }\n'
         'extern unsigned nw_wifi_t970_tx[3][3];\n'
         'void nw_wifi_t970_rx_stats(char *b, int n, int which) {\n'
         '  const unsigned *r = nw_wifi_t970_rxd; if (n <= 0) return;\n'
         '  if (!which) snprintf(b, n, "rx drop: action %u back %u addba %u declined %u, not-ap %u, short %u, no-llc %u, 8023 %u (eapol %u), other %u",\n'
         '    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]);\n'
         '  else snprintf(b, n, "tx ack/nack/free small %u/%u/%u mid %u/%u/%u large %u/%u/%u; no-llc first %02x%02x%02x%02x%02x%02x%02x%02x fc %x flag %x",\n'
         '    nw_wifi_t970_tx[0][0], nw_wifi_t970_tx[0][1], nw_wifi_t970_tx[0][2], nw_wifi_t970_tx[1][0], nw_wifi_t970_tx[1][1], nw_wifi_t970_tx[1][2],\n'
         '    nw_wifi_t970_tx[2][0], nw_wifi_t970_tx[2][1], nw_wifi_t970_tx[2][2], nw_t970_nollc[0], nw_t970_nollc[1], nw_t970_nollc[2], nw_t970_nollc[3],\n'
         '    nw_t970_nollc[4], nw_t970_nollc[5], nw_t970_nollc[6], nw_t970_nollc[7], r[10], r[11]); }\n'
         'void nw_sta_rx(struct sk_buff *skb) {\n'),
        ('  if (ieee80211_is_mgmt(h->frame_control)) { on_mgmt(skb); return; }\n',
         '  if (ieee80211_is_action(h->frame_control) && skb->len >= 26 && !memcmp(h->addr2, C.ap.bssid, 6)) {\n'
         '    const u8 *a = skb->data + 24; nw_wifi_t970_rxd[0]++;\n'
         '    if (a[0] == 3) { nw_wifi_t970_rxd[1]++;\n'
         '      if (a[1] == 0 && skb->len >= 33) { nw_wifi_t970_rxd[2]++; if (C.state >= NW_STA_KEYS) { t970_addba_decline(a); nw_wifi_t970_rxd[3]++; } } } }\n'
         '  if (ieee80211_is_mgmt(h->frame_control)) { on_mgmt(skb); return; }\n'
         '  if (!ieee80211_is_data(h->frame_control)) nw_wifi_t970_rxd[9]++;\n'
         '  else if (memcmp(h->addr2, C.ap.bssid, 6)) nw_wifi_t970_rxd[4]++;\n'),
    ])
    # 8. Block Ack accepted instead of declined (edit 7 declined it: no loss, but no receive aggregation), DELBA
    #    handled, receive window 64 (it was 8); 9. rekeys logged, a pairwise rekey's keys installed while connected
    #    (they were derived and never installed); 10. transmit completions by reason (dp_tx.c) in a third line.
    edit(out / 'nw_sta_pong.c', [("/* ADDBA Response declining the access point's request: category 3 (Block Ack), action 1, its dialog token,\n * status 37 (request declined), its parameter set, timeout 0. */\nstatic void t970_addba_decline(const u8 *req) {\n  struct sk_buff *skb = new_frame(9); if (!skb) return;\n  mgmt_header(skb, IEEE80211_FTYPE_MGMT | IEEE80211_STYPE_ACTION);\n  u8 *b = skb_put_zero(skb, 9); b[0] = 3; b[1] = 1; b[2] = req[2]; b[3] = 37; b[4] = 0; b[5] = req[3]; b[6] = req[4];\n  tx_frame(skb); }\n", '/* Block Ack with the access point (net/mac80211/agg-rx.c, without mac80211): an ADDBA request is accepted through\n * ath11k\'s ampdu_action (it updates the REO queue of that TID; nothing goes on the air) and answered by this ADDBA\n * response: category 3, action 1, the request\'s dialog token, status, our parameter set (A-MSDU in A-MPDU as ath11k\n * supports it, immediate policy, the TID, the window), the request\'s timeout. A DELBA from the access point that ends\n * its sending session stops ours. */\nstatic void t970_addba_resp(const u8 *req, u16 status, u16 tid, u16 buf) {\n  struct sk_buff *skb = new_frame(9); if (!skb) return;\n  mgmt_header(skb, IEEE80211_FTYPE_MGMT | IEEE80211_STYPE_ACTION);\n  u16 capab = 1 | 1 << 1 | (tid & 15) << 2 | (buf & 0x3ff) << 6;\n  u8 *b = skb_put_zero(skb, 9); b[0] = WLAN_CATEGORY_BACK; b[1] = WLAN_ACTION_ADDBA_RESP; b[2] = req[2];\n  b[3] = status; b[4] = status >> 8; b[5] = capab; b[6] = capab >> 8; b[7] = req[5]; b[8] = req[6];\n  tx_frame(skb); }\nstatic void t970_back(const u8 *a, unsigned len) {\n  struct ieee80211_ampdu_params p = { .sta = C.sta };\n  if (a[1] == WLAN_ACTION_ADDBA_REQ && len >= 9) {\n    u16 capab = a[3] | a[4] << 8, buf = capab >> 6, status = WLAN_STATUS_REQUEST_DECLINED;\n    u16 max = C.sta->deflink.he_cap.has_he ? IEEE80211_MAX_AMPDU_BUF_HE : IEEE80211_MAX_AMPDU_BUF_HT;\n    p.tid = (capab >> 2) & 15;\n    if (p.tid < IEEE80211_FIRST_TSPEC_TSID && C.sta->deflink.ht_cap.ht_supported) {\n      if (!(capab & 2) || buf > max) status = WLAN_STATUS_INVALID_QOS_PARAM;\n      else {\n        if (!buf) buf = max;\n        if (buf > C.sta->max_rx_aggregation_subframes) buf = C.sta->max_rx_aggregation_subframes;\n        p.action = IEEE80211_AMPDU_RX_START; p.buf_size = buf; p.timeout = a[5] | a[6] << 8;\n        p.ssn = (u16)(a[7] | a[8] << 8) >> 4;\n        int r = g_ops->ampdu_action(g_hw, g_vif, &p);\n        if (!r) { status = WLAN_STATUS_SUCCESS; nw_wifi_t970_rxd[3]++; }\n        else lx_log(4, "wifi: rx block ack tid %u refused by the driver: %d", p.tid, r); } }\n    lx_log(6, "wifi: block ack request tid %u window %u ssn %u -> status %u", p.tid, buf, p.ssn, status);\n    t970_addba_resp(a, status, p.tid, buf);\n  } else if (a[1] == WLAN_ACTION_DELBA && len >= 6) {\n    u16 params = a[2] | a[3] << 8;\n    if (!(params & IEEE80211_DELBA_PARAM_INITIATOR_MASK)) return;   /* it ends our sending session: the firmware\'s */\n    p.action = IEEE80211_AMPDU_RX_STOP; p.tid = params >> 12;\n    int r = g_ops->ampdu_action(g_hw, g_vif, &p);\n    lx_log(6, "wifi: block ack tid %u ended by the access point (%d)", p.tid, r); } }\n'), ('  if (ieee80211_is_action(h->frame_control) && skb->len >= 26 && !memcmp(h->addr2, C.ap.bssid, 6)) {\n    const u8 *a = skb->data + 24; nw_wifi_t970_rxd[0]++;\n    if (a[0] == 3) { nw_wifi_t970_rxd[1]++;\n      if (a[1] == 0 && skb->len >= 33) { nw_wifi_t970_rxd[2]++; if (C.state >= NW_STA_KEYS) { t970_addba_decline(a); nw_wifi_t970_rxd[3]++; } } } }\n', '  if (ieee80211_is_action(h->frame_control) && skb->len >= 26 && !memcmp(h->addr2, C.ap.bssid, 6)) {\n    const u8 *a = skb->data + 24; nw_wifi_t970_rxd[0]++;\n    if (a[0] == WLAN_CATEGORY_BACK) { nw_wifi_t970_rxd[1]++;\n      if (a[1] == WLAN_ACTION_ADDBA_REQ) nw_wifi_t970_rxd[2]++;\n      if (C.sta && C.state >= NW_STA_KEYS) t970_back(a, skb->len - 24); } }\n'), ('addba %u declined %u,', 'addba %u accepted %u,'), ('sta->max_rx_aggregation_subframes = 8;', 'sta->max_rx_aggregation_subframes = 64;'), ('  else snprintf(b, n, "tx ack/nack/free small', '  else if (which == 2) snprintf(b, n, "tx tqm acked %u rem-mpdu %u rem-tx %u rem-notx %u aged %u r5 %u r6 %u r7 %u fw %u retries %u; htt ok %u drop %u ttl %u reinj %u inspect %u",\n    nw_wifi_t970_tqm[0], nw_wifi_t970_tqm[1], nw_wifi_t970_tqm[2], nw_wifi_t970_tqm[3], nw_wifi_t970_tqm[4], nw_wifi_t970_tqm[5],\n    nw_wifi_t970_tqm[6], nw_wifi_t970_tqm[7], nw_wifi_t970_tqm[8], nw_wifi_t970_tqm[9], nw_wifi_t970_htt[0], nw_wifi_t970_htt[1],\n    nw_wifi_t970_htt[2], nw_wifi_t970_htt[3], nw_wifi_t970_htt[4]);\n  else snprintf(b, n, "tx ack/nack/free small'), ('extern unsigned nw_wifi_t970_tx[3][3];\n', 'extern unsigned nw_wifi_t970_tx[3][3];\nextern unsigned nw_wifi_t970_tqm[10], nw_wifi_t970_htt[8];   /* dp_tx.c, edit 10 */\n'), ("  if (pairwise && ack && !mic) {   /* message 1: the access point's nonce */\n", '  if (pairwise && ack && !mic) {   /* message 1: the access point\'s nonce */\n    if (C.state == NW_STA_UP) lx_log(6, "wifi: pairwise rekey: message 1 while connected");\n'), ('    C.hs_stage = 3; C.m4_done = 0; C.m4_at = lx_now_ms(); return; }\n', '    C.hs_stage = 3; C.m4_done = 0; C.m4_at = lx_now_ms();\n    if (C.state == NW_STA_UP) { install_keys(); lx_log(6, "wifi: pairwise rekey: new keys installed"); }\n    return; }\n'), ('  if (!pairwise && ack) {   /* group key update */\n', '  if (!pairwise && ack) {   /* group key update */\n    lx_log(6, "wifi: group key update%s", gtk && gtklen >= 16 ? "" : " without a key");\n')])
    edit(gen / 'dp_tx.c', [('static void\nath11k_dp_tx_process_htt_tx_complete(struct ath11k_base *ab,\n', '/* SM-T970: transmit completions by TQM release reason ([0..7]), from the firmware ([8]), retries summed ([9]);\n * HTT completion status ([0..7]) of the firmware-released ones (build-pong-ath11k.py edit 10) */\nunsigned nw_wifi_t970_tqm[10], nw_wifi_t970_htt[8];\nstatic void\nath11k_dp_tx_process_htt_tx_complete(struct ath11k_base *ab,\n'), ('\tswitch (wbm_status) {\n\tcase HAL_WBM_REL_HTT_TX_COMP_STATUS_OK:\n', '\tnw_wifi_t970_htt[wbm_status & 7]++;\n\tswitch (wbm_status) {\n\tcase HAL_WBM_REL_HTT_TX_COMP_STATUS_OK:\n'), ('\t\tath11k_dp_tx_status_parse(ab, tx_status, &ts);\n', '\t\tath11k_dp_tx_status_parse(ab, tx_status, &ts);\n\t\tif (ts.buf_rel_source == HAL_WBM_REL_SRC_MODULE_FW) {\n\t\t\tnw_wifi_t970_tqm[8]++;\n\t\t} else if (ts.buf_rel_source == HAL_WBM_REL_SRC_MODULE_TQM) {\n\t\t\tnw_wifi_t970_tqm[ts.status & 7]++;\n\t\t\tif (ts.try_cnt > 1)\n\t\t\t\tnw_wifi_t970_tqm[9] += ts.try_cnt - 1;\n\t\t}\n')])
    # 11. The firmware's regulatory domain reaches the channels. nw_mac80211.c's regulatory_set_wiphy_regd() was a stub, so
    #     every channel kept max_reg_power 0: ath11k's channel list (op_start) and each vdev start told the firmware a
    #     0 dBm regulatory limit. At home (5765 MHz, -53 dBm) 97 % of the data frames went unacknowledged after 18 tries
    #     while the low-rate handshake got through. The domain is applied as cfg80211 does (net/wireless/reg.c,
    #     handle_channel): no rule over the channel's 20 MHz = disabled, otherwise the rule's EIRP, antenna gain and
    #     NO_IR / DFS flags; ath11k's notifier then sends a later domain to the firmware again. The station logs the
    #     joined channel's limits.
    edit(out / 'nw_mac80211_pong.c', [
        ('int regulatory_set_wiphy_regd(struct wiphy *w, struct ieee80211_regdomain *rd) { (void)w; (void)rd; return 0; }\n',
         'char nw_wifi_t970_cc[3];   /* SM-T970: the domain last applied (edit 11) */\n'
         'int regulatory_set_wiphy_regd(struct wiphy *w, struct ieee80211_regdomain *rd) {\n'
         '  static int kept; int allowed = 0, total = 0;\n'
         '  if (!w || !rd || !rd->n_reg_rules) return -EINVAL;\n'
         '  for (int b = 0; b < NUM_NL80211_BANDS; b++) { struct ieee80211_supported_band *sb = w->bands[b]; if (!sb) continue;\n'
         '    for (int i = 0; i < sb->n_channels; i++) { struct ieee80211_channel *c = &sb->channels[i]; const struct ieee80211_reg_rule *r = 0;\n'
         '      if (!kept) { c->orig_flags = c->flags; c->orig_mpwr = c->max_power; }\n'
         '      total++;\n'
         '      for (unsigned j = 0; j < rd->n_reg_rules && !r; j++)\n'
         '        if (rd->reg_rules[j].freq_range.start_freq_khz <= (c->center_freq - 10) * 1000u &&\n'
         '            rd->reg_rules[j].freq_range.end_freq_khz >= (c->center_freq + 10) * 1000u) r = &rd->reg_rules[j];\n'
         '      c->flags = c->orig_flags & ~(IEEE80211_CHAN_DISABLED | IEEE80211_CHAN_NO_IR | IEEE80211_CHAN_RADAR);\n'
         '      if (!r) { c->flags |= IEEE80211_CHAN_DISABLED; continue; }\n'
         '      if (r->flags & NL80211_RRF_NO_IR) c->flags |= IEEE80211_CHAN_NO_IR;\n'
         '      if (r->flags & NL80211_RRF_DFS) c->flags |= IEEE80211_CHAN_RADAR;\n'
         '      c->max_reg_power = r->power_rule.max_eirp / 100; c->max_antenna_gain = r->power_rule.max_antenna_gain / 100;\n'
         '      c->max_power = c->orig_mpwr && c->orig_mpwr < c->max_reg_power ? c->orig_mpwr : c->max_reg_power;\n'
         '      allowed++; } }\n'
         '  kept = 1; nw_wifi_t970_cc[0] = rd->alpha2[0]; nw_wifi_t970_cc[1] = rd->alpha2[1];\n'
         '  lx_log(6, "wifi: regulatory domain %c%c, %u rules, %d of %d channels allowed", rd->alpha2[0], rd->alpha2[1],\n'
         '         (unsigned)rd->n_reg_rules, allowed, total);\n'
         '  if (w->reg_notifier) { struct regulatory_request rq = { .initiator = NL80211_REGDOM_SET_BY_DRIVER }; w->reg_notifier(w, &rq); }\n'
         '  return 0; }\n'),
    ])
    edit(out / 'nw_sta_pong.c', [
        ('  send_auth(); lx_queue_delayed(&g_timer, 100); }\n',
         '  { extern char nw_wifi_t970_cc[3]; struct ieee80211_channel *t970c = chan_for(C.ap.freq);\n'
         '    if (t970c) lx_log(6, "wifi: channel limits: regulatory %d dBm, antenna %d dBi, domain %s, flags %x",\n'
         '                      t970c->max_reg_power, t970c->max_antenna_gain, nw_wifi_t970_cc[0] ? nw_wifi_t970_cc : "none",\n'
         '                      (unsigned)t970c->flags); }\n'
         '  send_auth(); lx_queue_delayed(&g_timer, 100); }\n'),
    ])
    return qrtr


def patch_lx_rt(text):
    """Make lx_rt.c safe across re-entrant lx_pump() waits inside timer callbacks."""
    edits = [
        ('int lx_queue_delayed(struct delayed_work *d, unsigned long ms) {\n'
         '  if (!ms) { d->armed = 0; return lx_queue_work(&d->work); }\n'
         '  int was = d->armed; if (!was) list_add_tail(&d->work.entry, &g_timers);\n'
         '  d->armed = 1; d->due = lx_now_ms() + ms; return !was; }\n',
         'int lx_queue_delayed(struct delayed_work *d, unsigned long ms) {\n'
         '  if (!ms) { if (d->armed) { d->armed = 0; list_del_init(&d->work.entry); } return lx_queue_work(&d->work); }\n'
         '  if (d->work.queued) lx_cancel_work(&d->work);\n'
         '  int was = d->armed; if (!was) list_add_tail(&d->work.entry, &g_timers);\n'
         '  d->armed = 1; d->due = lx_now_ms() + ms; return !was; }\n'),
        ('  u64 now = lx_now_ms();\n'
         '  struct list_head *p, *n;\n'
         '  for (p = g_timers.next; p != &g_timers; p = n) {\n'
         '    n = p->next; struct delayed_work *d = container_of(p, struct delayed_work, work.entry);\n'
         '    if (d->armed && now >= d->due && !is_running(&d->work)) {\n'
         '      d->armed = 0; list_del_init(p); ran = 1; where(2, (const void *)d->work.func);\n'
         '      g_running[g_nrunning++] = &d->work; d->work.func(&d->work); g_nrunning--; }\n'
         '  }\n',
         '  /* one due timer at a time off the shared list: waits inside a callback call lx_pump() recursively,\n'
         '   * which can fire or cancel a timer a prefetched next pointer would have stepped onto after list_del_init */\n'
         '  u64 now = lx_now_ms();\n'
         '  for (int budget = 256; budget > 0; budget--) {\n'
         '    struct delayed_work *d = 0; struct list_head *e;\n'
         '    for (e = g_timers.next; e != &g_timers; e = e->next) {\n'
         '      struct delayed_work *c = container_of(e, struct delayed_work, work.entry);\n'
         '      if (c->armed && now >= c->due && !is_running(&c->work)) { d = c; break; }\n'
         '    }\n'
         '    if (!d) break;\n'
         '    d->armed = 0; list_del_init(&d->work.entry); ran = 1; where(2, (const void *)d->work.func);\n'
         '    g_running[g_nrunning++] = &d->work; if (d->work.func) d->work.func(&d->work); g_nrunning--;\n'
         '  }\n'),
    ]
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError('lx_rt.c differs at %r (%d)' % (old[:50], text.count(old)))
        text = text.replace(old, new)
    return text


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
    runtime = patch_lx_rt(runtime[:begin] + runtime[end:])
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
