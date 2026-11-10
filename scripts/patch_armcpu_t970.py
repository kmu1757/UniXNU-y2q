#!/usr/bin/env python3
"""Patch ArmCpu for SM-T970 (SM8250 SPI IPI routing and multi-cluster SMP)."""
from pathlib import Path
import sys

POLICY_ON = '  PongCpuPolicy::start();\n'
POLICY_OFF = '  if (false) PongCpuPolicy::start(); // T970: SM8250 has no EPSS at 0x17d91000\n'


def disable_cpu_policy(txt: str) -> str:
    """PongCpuPolicy.h is SM8475: its worker sleeps 3 s, then reads EPSS at 0x17d91000.

    SM8250 has nothing there (its EPSS is 0x18591000, driven by pong_cpu_perf). On T970 the
    read never returns, none of the worker's three result lines is ever logged, and the core
    that ran the worker stops for good. When that core is cpu0 every timer armed during
    start-up is lost (no USB, no touch, no screen, UFS polls that never wake).
    """
    if POLICY_OFF in txt:
        return txt
    assert txt.count(POLICY_ON) == 1, 'expected exactly one PongCpuPolicy::start() call'
    return txt.replace(POLICY_ON, POLICY_OFF)


LATENCY_AT = '  info.cluster_type = ml_get_topology_info()->cpus[logicalId].cluster_type;\n'
LATENCY = LATENCY_AT + """  // armcpu-idle-latency-us=N (default 0, at most 1000): delays shorter than N microseconds spin
  // instead of sleeping on a timer (ml_delay_should_spin). With 0 every IODelay(2) of the UFS
  // poll is a block, a timer interrupt and a wake-up.
  uint32_t idleLatencyUs = 0;
  PE_parse_boot_argn("armcpu-idle-latency-us", &idleLatencyUs, sizeof(idleLatencyUs));
  info.powergate_latency = idleLatencyUs <= 1000 ? idleLatencyUs * 1000 : 0;
"""


def add_idle_latency_knob(txt: str) -> str:
    if 'armcpu-idle-latency-us' in txt:
        return txt
    assert txt.count(LATENCY_AT) == 1, 'expected exactly one info.cluster_type assignment'
    return txt.replace(LATENCY_AT, LATENCY)


COUNTERS = (
    ('volatile uint64_t armcpu_timer_interrupts, armcpu_ipi_interrupts;\n',
     'volatile uint64_t armcpu_ipi_sent[8], armcpu_ipi_recv[8], armcpu_timer_recv[8];\n'),
    ('      __atomic_fetch_add(&armcpu_timer_interrupts, 1, __ATOMIC_RELAXED);\n',
     '      if (logicalId < 8) armcpu_timer_recv[logicalId]++;\n'),
    ('      __atomic_fetch_add(&armcpu_ipi_interrupts, 1, __ATOMIC_RELAXED);\n',
     '      if (logicalId < 8) armcpu_ipi_recv[logicalId]++;\n'),
    ('  if (!cpu) panic("ArmCpu: invalid CPU signal target");\n',
     '  armcpu_ipi_sent[cpu->logicalId & 7]++;\n'),
)


def add_ipi_counters(txt: str) -> str:
    """Per-core counts of IPIs asked for, IPIs taken and timer interrupts taken (USB command `ipis`).

    With backboardd and SpringBoard told to prefer the Gold cluster the display stopped for good
    (11-10): a count that grows in `sent` and not in `recv` says the wake-up never reaches that core.
    Runs after the SPI IPI step, whose needle contains the last anchor.
    """
    if 'armcpu_ipi_sent' in txt:
        return txt
    for anchor, addition in COUNTERS:
        assert txt.count(anchor) == 1, 'expected exactly one: ' + anchor.strip()
        txt = txt.replace(anchor, anchor + addition)
    return txt


BENCH = (
    ('volatile uint64_t armcpu_ipi_sent[8], armcpu_ipi_recv[8], armcpu_timer_recv[8];\n',
     'volatile uint32_t armcpu_ipi_mode;\nvolatile uint64_t armcpu_redist[8];\n'),
    ('  gSpiIpi = spiIpi == 1;\n',
     '  uint32_t ipiMode = 0;\n'
     '  if (PE_parse_boot_argn("armcpu-ipi-mode", &ipiMode, sizeof(ipiMode)) && ipiMode <= 2) armcpu_ipi_mode = ipiMode;\n'),
    ('    spiIpiConfigure(dist, logicalId, affinity);\n',
     '    if (logicalId < 8) armcpu_redist[logicalId] = redist;\n'),
)
BENCH_SEND_OLD = """  if (gSpiIpi && gDist) {
    spiIpiSend(gDist, cpu->logicalId & 7);
    return;
  }
"""
BENCH_SEND_NEW = """  if (gSpiIpi && gDist && armcpu_ipi_mode != 1) {
    if (armcpu_ipi_mode == 2) {
      write32(gDist, 0x40, kIpiSpiBase + (cpu->logicalId & 7)); // GICD_SETSPI_NSR
      __asm__ volatile("dsb sy" ::: "memory");
    } else {
      spiIpiSend(gDist, cpu->logicalId & 7);
    }
    return;
  }
"""
BENCH_TAIL = """
// T970 test bench (USB commands gic, gicr, gicw, rdr, rdw, ipimode): raw access to the distributor and to
// one core's redistributor frames, and the calling core's CPU interface state. armcpu_ipi_mode picks how
// signalCPU raises an IPI: 0 GICD_ISPENDR on the core's SPI, 1 SGI 0 through ICC_SGI1R_EL1, 2 GICD_SETSPI_NSR.
extern "C" uint32_t armcpu_ipi_spi_base(void)
{
  return kIpiSpiBase;
}

extern "C" uint32_t armcpu_gic_peek(uint32_t off)
{
  return (gDist && off < 0x10000 && (off & 3) == 0) ? read32(gDist, off) : 0xdeadbeefU;
}

extern "C" void armcpu_gic_poke(uint32_t off, uint32_t value)
{
  if (!gDist || off >= 0x10000 || (off & 3) != 0) return;
  write32(gDist, off, value);
  __asm__ volatile("dsb sy" ::: "memory");
}

extern "C" uint32_t armcpu_redist_peek(uint32_t cpu, uint32_t off)
{
  if (cpu >= 8 || armcpu_redist[cpu] == 0 || off >= 0x20000 || (off & 3) != 0) return 0xdeadbeefU;
  return read32(static_cast<uintptr_t>(armcpu_redist[cpu]), off);
}

extern "C" void armcpu_redist_poke(uint32_t cpu, uint32_t off, uint32_t value)
{
  if (cpu >= 8 || armcpu_redist[cpu] == 0 || off >= 0x20000 || (off & 3) != 0) return;
  write32(static_cast<uintptr_t>(armcpu_redist[cpu]), off, value);
  __asm__ volatile("dsb sy" ::: "memory");
}

extern "C" void armcpu_icc_state(uint64_t out[6])
{
  uint64_t value;
  __asm__ volatile("mrs %0, icc_ctlr_el1" : "=r"(value)); out[0] = value;
  __asm__ volatile("mrs %0, icc_pmr_el1" : "=r"(value)); out[1] = value;
  __asm__ volatile("mrs %0, icc_igrpen1_el1" : "=r"(value)); out[2] = value;
  __asm__ volatile("mrs %0, S3_0_C12_C11_3" : "=r"(value)); out[3] = value; // ICC_RPR_EL1
  __asm__ volatile("mrs %0, S3_0_C12_C12_2" : "=r"(value)); out[4] = value; // ICC_HPPIR1_EL1
  __asm__ volatile("mrs %0, mpidr_el1" : "=r"(value)); out[5] = value;
}
"""


def add_gic_bench(txt: str) -> str:
    """Raw GIC access and a switchable IPI transport (USB commands gic, gicr, gicw, rdr, rdw, ipimode).

    11-10: after start-up no core takes an IPI any more (about 40,000 asked for per Silver core, 1 to 4
    taken), so every cross-core wake-up waits for the target's next timer interrupt. These let one boot
    read the state of the IPI SPIs and try the other ways of raising them. Runs after add_ipi_counters.
    """
    if 'armcpu_ipi_mode' in txt:
        return txt
    for anchor, addition in BENCH:
        assert txt.count(anchor) == 1, 'expected exactly one: ' + anchor.strip()
        txt = txt.replace(anchor, anchor + addition)
    assert txt.count(BENCH_SEND_OLD) == 1, 'expected exactly one SPI IPI send block'
    return txt.replace(BENCH_SEND_OLD, BENCH_SEND_NEW) + BENCH_TAIL


TICK_HELPERS = r"""
// Fallback for IPIs that never arrive (11-10); see armcpu-idle-tick-us and armcpu-timer-ipi in ArmCpu::start.
static uint64_t armcpuFrequency(void)
{
  uint64_t value;
  __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(value));
  return value;
}

extern "C" uint64_t armcpu_now_ms(void)
{
  const uint64_t frequency = armcpuFrequency();
  uint64_t ticks;
  __asm__ volatile("isb\n\tmrs %0, cntvct_el0" : "=r"(ticks));
  return frequency >= 1000 ? ticks / (frequency / 1000) : 0;
}

// processor_idle: entering idle, ask for a wake-up after the tick. Absolute time counts timebase ticks on arm64.
static void armcpuIdleNotify(cpu_id_t, boolean_t enter, uint64_t *newTimeoutTicks)
{
  const uint64_t us = armcpu_idle_tick_us;
  *newTimeoutTicks = (enter && us != 0) ? us * armcpuFrequency() / 1000000 : 0;
}

// idle_timer: the tick expired. One shot; the next idle entry arms it again.
static void armcpuIdleTimer(void *refcon, uint64_t *newTimeoutTicks)
{
  const uintptr_t cpu = reinterpret_cast<uintptr_t>(refcon);
  if (cpu < 8) armcpu_idle_wakes[cpu]++;
  *newTimeoutTicks = 0;
}
"""
TICK_REGISTER = """  // armcpu-idle-tick-us=N (0: registered but off; at most 1000000): an idle core wakes after N microseconds
  // with nothing else due and looks at its run queue again, so a wake-up whose IPI never arrives costs at
  // most N (USB command idletick). armcpu-timer-ipi=1: every timer interrupt also runs the IPI callback
  // (USB command timeripi). Without the first argument the idle callbacks are not registered at all.
  uint32_t idleTickUs = 0, timerIpi = 0;
  if (PE_parse_boot_argn("armcpu-idle-tick-us", &idleTickUs, sizeof(idleTickUs))) {
    armcpu_idle_tick_us = idleTickUs <= 1000000 ? idleTickUs : 0;
    info.processor_idle = armcpuIdleNotify;
    info.idle_timer = armcpuIdleTimer;
    info.idle_timer_refcon = reinterpret_cast<void *>(static_cast<uintptr_t>(logicalId));
    armcpu_idle_registered = 1;
  }
  if (PE_parse_boot_argn("armcpu-timer-ipi", &timerIpi, sizeof(timerIpi))) armcpu_timer_ipi = timerIpi == 1;
"""
TICK = (
    ('volatile uint32_t armcpu_ipi_mode;\n',
     'volatile uint32_t armcpu_idle_tick_us, armcpu_timer_ipi, armcpu_idle_registered;\n'
     'volatile uint64_t armcpu_idle_wakes[8], armcpu_ipi_last_ms[8], armcpu_sgi_recv[8];\n'
     'volatile uint32_t armcpu_ipi_cfg[8][4];\n'),
    ('static ArmCpu *gCpus[8];\n', TICK_HELPERS),
    ('  info.powergate_latency = idleLatencyUs <= 1000 ? idleLatencyUs * 1000 : 0;\n', TICK_REGISTER),
    ('      rtclock_intr(0);\n',
     '      if (armcpu_timer_ipi && ipi_handler) ipi_handler();\n'),
    ('      if (logicalId < 8) armcpu_ipi_recv[logicalId]++;\n',
     '      if (logicalId < 8) {\n'
     '        armcpu_ipi_last_ms[logicalId] = armcpu_now_ms();\n'
     '        if (intid == 0) armcpu_sgi_recv[logicalId]++;\n'
     '      }\n'),
)
TICK_CFG_OLD = """  __asm__ volatile("dsb sy" ::: "memory");
}

static void spiIpiSend(uintptr_t dist, uint32_t core)
"""
TICK_CFG_NEW = """  __asm__ volatile("dsb sy" ::: "memory");
  if (core < 8) { // what the distributor holds right after the set-up: enable, trigger, route, pending
    armcpu_ipi_cfg[core][0] = *reinterpret_cast<volatile uint32_t *>(dist + 0x100 + (id / 32) * 4);
    armcpu_ipi_cfg[core][1] = *reinterpret_cast<volatile uint32_t *>(dist + 0xc00 + (id / 16) * 4);
    armcpu_ipi_cfg[core][2] = *reinterpret_cast<volatile uint32_t *>(dist + 0x6000 + 8ULL * id);
    armcpu_ipi_cfg[core][3] = *reinterpret_cast<volatile uint32_t *>(dist + 0x200 + (id / 32) * 4);
  }
}

static void spiIpiSend(uintptr_t dist, uint32_t core)
"""


def add_tick_fallback(txt: str) -> str:
    """Bounded wake-up latency without IPIs, and two more IPI diagnostics (USB tick, idletick, timeripi, ipitrace).

    half10 (11-10): no core takes an IPI after start-up, so a thread made runnable for an idle core waits for
    that core's next timer interrupt, and an idle Gold core has none. With the idle callbacks registered an
    idle core wakes on its own after the tick; with timer-ipi the timer interrupt also runs the IPI callback
    (pending ASTs and cross-calls). Both are off unless asked for. Runs after add_gic_bench.
    """
    if 'armcpu_idle_tick_us' in txt:
        return txt
    for anchor, addition in TICK:
        assert txt.count(anchor) == 1, 'expected exactly one: ' + anchor.strip()
        txt = txt.replace(anchor, anchor + addition)
    assert txt.count(TICK_CFG_OLD) == 1, 'expected exactly one end of spiIpiConfigure'
    return txt.replace(TICK_CFG_OLD, TICK_CFG_NEW)


IPI_LATE_TAIL = """
// Called once the root volume is mounted (bsd pong_ipi_late_init). Until then no IPI arrives: the IPI boot
// arguments are read at the end of the boot CPU's start(), after every core has run initCPU, so the cores
// signal themselves with an SGI and afterwards signalCPU pends SPIs that were never set up. That is kept on
// purpose for now: with IPIs live from the start the three SHNGROOT APFS containers open in parallel and
// the root volume never appears (11-10, reason unknown). From here on IPIs are SGIs, and every core is
// interrupted once: a core that was sent an IPI which never arrived sits idle in the dispatching state and
// is not signalled again until it takes an interrupt.
extern "C" void armcpu_ipi_enable(void)
{
  armcpu_ipi_mode = 1;
  for (unsigned i = 0; i < 8; i++) {
    if (gCpus[i]) gCpus[i]->signalCPU(gCpus[i]);
  }
}
"""


def add_ipi_late(txt: str) -> str:
    """armcpu_ipi_enable(): switch IPIs to SGIs and wake every core, for use after the root is mounted.

    Measured 11-10 on half14: after this (done by hand over USB) received IPIs follow sent ones and dragging
    the home screen goes from 11-14 to 16-21 frames a second, 42-57 with the display processes on Gold.
    Runs after add_tick_fallback.
    """
    if 'armcpu_ipi_enable' in txt:
        return txt
    assert 'volatile uint32_t armcpu_ipi_mode;' in txt, 'add_gic_bench must run first'
    return txt + IPI_LATE_TAIL


def patch_armcpu(armcpu_source: Path):
    armcpu_source = armcpu_source.resolve()
    target = armcpu_source / 'ArmCpu.cpp' if armcpu_source.is_dir() else armcpu_source
    if not target.is_file():
        return

    txt = add_idle_latency_knob(disable_cpu_policy(target.read_text(encoding='utf-8')))
    if 'armcpu-spi-ipi' in txt:
        # already carries the SPI IPI patch; still make sure the policy is off
        target.write_text(add_ipi_late(add_tick_fallback(add_gic_bench(add_ipi_counters(txt)))), encoding='utf-8')
        return

    # 1. Add SPI IPI state and helper functions after gBootAffinity
    needle_aff = 'static uint64_t gBootAffinity;'
    replacement_aff = """static uint64_t gBootAffinity;

// gts7xlwifi / SM8250: GICv3 ICC_SGI1R_EL1 does not deliver SGIs on T970.
// Route IPIs as software-pended SPIs using base SPI 544..551.
static bool gSpiIpi = false;
static bool gSelfIpi = false;
static uint32_t kIpiSpiBase = 1008;

static void spiIpiConfigure(uintptr_t dist, uint32_t core, uint64_t aff)
{
  const uint32_t id = kIpiSpiBase + core;
  *reinterpret_cast<volatile uint32_t *>(dist + 0x80 + (id / 32) * 4) |= (1U << (id % 32));
  *reinterpret_cast<volatile uint32_t *>(dist + 0xc00 + (id / 16) * 4) |= (2U << ((id % 16) * 2));
  *reinterpret_cast<volatile uint32_t *>(dist + 0x400 + (id / 4) * 4) = 0x80808080;
  *reinterpret_cast<volatile uint64_t *>(dist + 0x6000 + 8ULL * id) = aff & 0xff00ffffffULL;
  __asm__ volatile("dsb sy" ::: "memory");
  *reinterpret_cast<volatile uint32_t *>(dist + 0x100 + (id / 32) * 4) = 1U << (id % 32);
  __asm__ volatile("dsb sy" ::: "memory");
}

static void spiIpiSend(uintptr_t dist, uint32_t core)
{
  const uint32_t id = kIpiSpiBase + core;
  __asm__ volatile("dsb ishst" ::: "memory");
  *reinterpret_cast<volatile uint32_t *>(dist + 0x200 + (id / 32) * 4) = 1U << (id % 32);
  __asm__ volatile("dsb sy" ::: "memory");
}"""
    txt = txt.replace(needle_aff, replacement_aff)

    # 2. Update kSgiPpiEnable to enable SGIs 0..15
    txt = txt.replace(
        'static const uint32_t kSgiPpiEnable = 1U | (1U << 27);',
        'static const uint32_t kSgiPpiEnable = 0xffffU | (1U << 27);'
    )

    # 3. In ArmCpu::start: parse armcpu boot arguments
    needle_start = 'registerService();\n  uint64_t ctl, cval, now, frequency, daif;'
    replacement_start = """unsigned selfIpi = 0;
  PE_parse_boot_argn("armcpu-self-ipi", &selfIpi, sizeof(selfIpi));
  gSelfIpi = selfIpi == 1;
  unsigned spiIpi = 0;
  PE_parse_boot_argn("armcpu-spi-ipi", &spiIpi, sizeof(spiIpi));
  gSpiIpi = spiIpi == 1;
  uint32_t spiIpiBase = 0;
  if (PE_parse_boot_argn("armcpu-spi-ipi-base", &spiIpiBase, sizeof(spiIpiBase)) && spiIpiBase >= 32 && spiIpiBase + 8 <= 1020) {
    kIpiSpiBase = spiIpiBase;
  }
  if (gSpiIpi) {
    IOLog("ArmCpu: T970 SPI IPI routing active (base SPI %u..%u)\\n", kIpiSpiBase, kIpiSpiBase + 7);
  }
  registerService();
  uint64_t ctl, cval, now, frequency, daif;"""
    txt = txt.replace(needle_start, replacement_start)

    # 4. In ArmCpu::initCPU: configure SPI IPI and self-IPI
    needle_init = """  enableGic();
  enableCPUTimeBase(true);
  setCPUState(kIOCPUStateRunning);
  psci_smp_cpu_running();
  signalCPU(this); // cpu_signal_handler lifts SIGPdisabled on the local CPU."""
    replacement_init = """  enableGic();
  if (gSpiIpi && dist) {
    spiIpiConfigure(dist, logicalId, affinity);
  }
  enableCPUTimeBase(true);
  setCPUState(kIOCPUStateRunning);
  psci_smp_cpu_running();
  if (gSelfIpi) {
    signalCPU(this);
  } else {
    signalCPU(this); // cpu_signal_handler lifts SIGPdisabled on the local CPU.
  }"""
    txt = txt.replace(needle_init, replacement_init)

    # 5. In ArmCpu::signalCPU: route via SPI if gSpiIpi is enabled
    needle_signal = """void ArmCpu::signalCPU(IOCPU *target)
{
  ArmCpu *cpu = OSDynamicCast(ArmCpu, target);
  if (!cpu) panic("ArmCpu: invalid CPU signal target");
  uint64_t aff = cpu->affinity;"""
    replacement_signal = """void ArmCpu::signalCPU(IOCPU *target)
{
  ArmCpu *cpu = OSDynamicCast(ArmCpu, target);
  if (!cpu) panic("ArmCpu: invalid CPU signal target");
  if (gSpiIpi && gDist) {
    spiIpiSend(gDist, cpu->logicalId & 7);
    return;
  }
  uint64_t aff = cpu->affinity;"""
    txt = txt.replace(needle_signal, replacement_signal)

    # 6. In ArmCpu::interrupt: acknowledge SPI IPI
    needle_irq = """    } else if (intid == 0) {
      __atomic_fetch_add(&armcpu_ipi_interrupts, 1, __ATOMIC_RELAXED);
      if (ipi_handler) ipi_handler();"""
    replacement_irq = """    } else if (intid == 0 || (gSpiIpi && intid >= kIpiSpiBase && intid < kIpiSpiBase + 8)) {
      __atomic_fetch_add(&armcpu_ipi_interrupts, 1, __ATOMIC_RELAXED);
      if (ipi_handler) ipi_handler();"""
    txt = txt.replace(needle_irq, replacement_irq)

    txt = add_ipi_late(add_tick_fallback(add_gic_bench(add_ipi_counters(txt))))
    target.write_text(txt, encoding='utf-8')
    print(f'Applied SM-T970 ArmCpu patch to {target}', flush=True)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        patch_armcpu(Path(sys.argv[1]))
    else:
        patch_armcpu(Path('.'))
