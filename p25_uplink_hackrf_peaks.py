#!/usr/bin/env python3
"""
Volusia County FL - P25 Uplink Transmitter Finder (HackRF One, peak detection)

WHY THIS VERSION
  The previous hackrf_sweep detector compared every bin to one global noise
  floor. On an 800 MHz band that has strong public-safety downlinks at
  854-861 MHz, HackRF front-end overload raises the floor by 5-10 dB, so:
      threshold too low  -> hundreds of false positives
      threshold too high -> real 12.5 kHz P25 uplinks (+2..6 dB) never fire
  A P25 uplink is NARROWBAND (~12.5 kHz wide, often a few ms per talk-spurt).
  Downlink bleed is BROADBAND and smooth. So we detect LOCAL PEAKS, not
  absolute level: a bin must stick out above its own neighbours.

TWO MODES
  1. sweep   (default) - hackrf_sweep over the whole 809-816 MHz uplink band.
                         Fast, finds which CHANNELS carry traffic.
                         Cannot resolve < ~25 kHz detail or burst timing.

  2. channel (slow)    - retunes to each channel and does a narrow FFT.
                         Slower but measures real SNR per channel and
                         distinguishes "channel has a transmitter" from
                         "band is noisy". Use once sweep has narrowed it down.

WHAT THIS CAN AND CANNOT TELL YOU
  * It detects RF energy on Volusia P25 uplink channels = a radio
    transmitting near you. That is what "detect local transmitters" means.
  * It does NOT demodulate P25 and cannot tell you the talkgroup, unit ID,
    or which agency.
  * Uplinks are frequency-hopping-optional, short (~100-300 ms voice spurs,
    longer for data/registration). Sweep dwell is short, so you will see
    activity but may miss some bursts. Channel mode has much better duty
    cycle capture.
  * RSSI is a relative indicator only. No calibration = no distance.

USAGE
  python3 p25_uplink_hackrf_peaks.py sweep
  python3 p25_uplink_hackrf_peaks.py sweep --seconds 300 --csv sweep.csv
  python3 p25_uplink_hackrf_peaks.py channel --dwell 2 --rounds 20
  python3 p25_uplink_hackrf_peaks.py calib

Requires: hackrf tools installed (hackrf_sweep, hackrf_transfer), python3,
numpy (only for channel mode). Root/sudo for USB access.

Author: built for Fred's Kali box, HackRF serial ...406464c82346594b
"""

import argparse
import csv
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from collections import defaultdict

try:
    import numpy as np
except ImportError:
    np = None

# ---------------------------------------------------------------------------
# Volusia County P25 uplink plan  (uplink = downlink - 45 MHz)
# Source: RadioReference / prior lab notes. Site labels are approximate;
# East/West Simulcast + Holly Hill. Verify against current RR data before
# drawing conclusions about a specific site.
# ---------------------------------------------------------------------------
UPLINK_CHANNELS = {
    809.3625: ("East", "860.7375"),
    810.2125: ("West", "855.7375"),
    810.4625: ("East", "855.4625"),
    810.7375: ("West", "855.9875"),
    811.2625: ("East", "856.2625"),
    811.4875: ("East", "856.4875"),
    811.7125: ("HH/EW", "856.7125"),
    811.9375: ("East", "856.9375"),
    811.9875: ("East", "856.9875"),
    812.2125: ("HH",   "857.2125"),
    812.2625: ("West", "857.2625"),
    812.4875: ("East", "857.4875"),
    812.7375: ("East", "857.4875"),
    812.7625: ("East", "857.7625"),
    813.2125: ("HH",   "858.2125"),
    813.2625: ("East", "858.2625"),
    813.4875: ("East", "858.4875"),
    813.7125: ("EW",   "858.7125"),
    813.7375: ("East", "858.7375"),
    813.7625: ("West", "858.7625"),
    814.2125: ("HH",   "859.2125"),
    814.2625: ("East", "859.2625"),
    814.4875: ("West", "859.4875"),
    814.7125: ("East", "859.7125"),
    814.7375: ("EW",   "859.7375"),
    814.7625: ("East", "859.7625"),
    815.2125: ("HH",   "860.2125"),
    815.2625: ("West", "860.2625"),
    815.4875: ("East", "860.4875"),
    815.7125: ("West", "860.7125"),
    815.7375: ("East", "860.7375"),
    815.7625: ("West", "860.7625"),
}

# Sanity: dedupe (812.7375 had a bad downlink mapping in the old list)
CHANNELS_HZ = sorted(int(round(f * 1e6)) for f in UPLINK_CHANNELS)

FREQ_LOW_MHZ = 809
FREQ_HIGH_MHZ = 816

# P25 800 MHz downlink side is +45 MHz from the uplink plan. Towers transmit
# here continuously (control channel especially), so it is far easier to
# confirm the system is alive than by watching sparse uplink bursts.
DOWNLINK_LOW_MHZ = 854
DOWNLINK_HIGH_MHZ = 861

# Selected band for the current run. `uplink` is the original purpose;
# `downlink` is for proving the system is on the air at all.
BAND_LOW_MHZ = FREQ_LOW_MHZ
BAND_HIGH_MHZ = FREQ_HIGH_MHZ

# --- HackRF settings -------------------------------------------------------
SERIAL = os.environ.get("HACKRF_SERIAL", "0000000000000000406464c82346594b")
AMP = 0       # 800 MHz + nearby strong downlinks: leave the amp OFF
LNA = 32      # 0-40 dB, 8 dB steps
VGA = 22      # 0-62 dB, 2 dB steps
BIN_HZ = 2445 # ~2.4 kHz sweep resolution. HARD FLOOR from hackrf_sweep:
              # "-w must be no less than 2445". Smaller values are rejected,
              # so do not tune this downward.

# --- Peak detection tuning -------------------------------------------------
PEAK_RADIUS = 5      # compare each bin to +/- this many bins (5*2k = +/-10 kHz)
PEAK_HEIGHT_DB = 4.0 # how far above local median to count as a real peak
MIN_RUN_BINS = 2     # a peak must be >= 2 bins wide (rejects 1-bin spurs)
CHANNEL_TOL_HZ = 8000    # a peak this close to a known channel = ON that channel
COOLDOWN_SEC = 2.0   # do not re-print the same channel faster than this
CHANNEL_EDGE_HZ = 10000  # ignore peaks right at the edges of a sweep segment

# --- Channel mode (hackrf_transfer + FFT) -----------------------------------
# HackRF produces a large DC offset at the tune frequency. Tuning exactly onto
# a channel puts that artefact on top of the signal and reports ~55 dB SNR on
# empty channels. Measured on a live unit: a 59 dB spike at the LO with flat
# noise either side. So we detune and exclude the DC region explicitly.
OFFSET_HZ = 400_000      # tune this far off the channel
DC_EXCLUDE_HZ = 60_000   # ignore this much around the LO (spike + skirts)
SIG_WINDOW_HZ = 25_000   # search window around the expected signal position
ACTIVE_SNR_DB = 8.0      # peak-to-floor within the signal window to count


def need(root_msg="hackrf tools"):
    if shutil.which("hackrf_sweep") is None:
        sys.exit(f"ERROR: hackrf_sweep not found. Install {root_msg}.")


def sudo_prefix():
    return [] if os.geteuid() == 0 else ["sudo"]


def parse_sweep_line(line):
    """hackrf_sweep CSV -> (hz_low, bin_width, [db...]) or None."""
    p = line.strip().split(",")
    if len(p) < 8:
        return None
    try:
        hz_low = int(p[2])
        bin_w = float(p[4])
        dbs = [float(x) for x in p[6:]]
    except (ValueError, IndexError):
        return None
    return hz_low, bin_w, dbs


def sweep_cmd(one_pass=True):
    cmd = sudo_prefix() + [
        "hackrf_sweep",
        "-d", SERIAL,
        "-a", str(AMP),
        "-l", str(LNA),
        "-g", str(VGA),
        "-w", str(BIN_HZ),
        "-f", f"{BAND_LOW_MHZ}:{BAND_HIGH_MHZ}",
    ]
    if one_pass:
        cmd.append("-1")
    return cmd


def collect_sweep(one_pass=True, verbose=False):
    """Run sweep, return dict{bin_center_hz: dbm} merged at max per bin.

    stderr is captured and surfaced: a silent empty result is almost always a
    hackrf open/permission failure, and hiding it makes the failure
    indistinguishable from "no signal", which is the opposite of useful.
    """
    proc = subprocess.Popen(sweep_cmd(one_pass), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    merged = {}
    try:
        for line in proc.stdout:
            parsed = parse_sweep_line(line)
            if not parsed:
                continue
            hz_low, bin_w, dbs = parsed
            for i, db in enumerate(dbs):
                f = int(hz_low + i * bin_w)
                if f not in merged or db > merged[f]:
                    merged[f] = db
    finally:
        if one_pass:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    if not merged:
        try:
            err = (proc.stderr.read() or "").strip()
        except Exception:
            err = ""
        if err:
            print(f"  [hackrf_sweep] {err}")
        else:
            print("  [hackrf_sweep] produced no output (exit "
                  f"{proc.returncode}); check that the device is attached "
                  "and readable inside this container")
    elif verbose:
        print(f"  [hackrf_sweep] {len(merged)} bins")
    return merged


def find_peaks(bins):
    """
    Local-peak detection on an ordered (freq, db) list.
    Returns [(freq, db, prominence), ...] where prominence = db above the
    median of its neighbourhood.
    """
    freqs = [f for f, _ in bins]
    level = [d for _, d in bins]
    n = len(level)
    if n < 2 * PEAK_RADIUS + 1:
        return []
    out = []
    for i in range(PEAK_RADIUS, n - PEAK_RADIUS):
        window = level[i - PEAK_RADIUS:i + PEAK_RADIUS + 1]
        local_med = statistics.median(window)
        promo = level[i] - local_med
        if promo < PEAK_HEIGHT_DB:
            continue
        if level[i] != max(window):
            continue
        # width check: neighbours at half-prominence
        half = local_med + promo / 2.0
        width = 1
        j = i - 1
        while j >= 0 and level[j] >= half:
            width += 1
            j -= 1
        j = i + 1
        while j < n and level[j] >= half:
            width += 1
            j += 1
        if width < MIN_RUN_BINS:
            continue
        out.append((freqs[i], level[i], promo, width))
    return out


def nearest_channel(freq_hz):
    best = min(CHANNELS_HZ, key=lambda c: abs(c - freq_hz))
    return best, freq_hz - best


def channel_label(ch_hz):
    key = ch_hz / 1e6
    info = UPLINK_CHANNELS.get(round(key, 4))
    if info:
        return info
    # fall back to closest entry (float rounding safety)
    k = min(UPLINK_CHANNELS, key=lambda x: abs(x - key))
    return UPLINK_CHANNELS[k]


def cmd_calib():
    """
    Measure the actual floor and the strongest thing in band, so you can
    sanity-check gain. Two sweeps: amp off and amp on.
    """
    need()
    print("=" * 74)
    print("  HackRF receive calibration - Volusia P25 uplink band (809-816 MHz)")
    print("=" * 74)
    global AMP
    for amp in (0, 1):
        AMP = amp
        t0 = time.time()
        bins = collect_sweep()
        if not bins:
            print(f"  amp={amp}: no data (HackRF busy or unplugged?)")
            continue
        vals = list(bins.values())
        vmin, vmed, vmax = min(vals), statistics.median(vals), max(vals)
        fmax = max(bins, key=bins.get)
        print(f"\n  amp={amp}  ({time.time()-t0:.1f}s, {len(bins)} bins)")
        print(f"    floor   : {vmin:7.1f} dB")
        print(f"    median  : {vmed:7.1f} dB   <- global noise floor reference")
        print(f"    peak    : {vmax:7.1f} dB @ {fmax/1e6:.4f} MHz")
        # how many bins are in hard overload (flat-topped) territory
        hot = sum(1 for v in vals if v > vmed + 25)
        print(f"    bins > floor+25 dB: {hot}")
        if amp == 1 and hot > len(vals) * 0.02:
            print("    !! amp=1 looks like overload; keep AMP=0")
    print("\nIf median is above about -10 dB the front end is being driven hard;")
    print("drop VGA by 8 and re-run. Peak-to-median of 25+ dB on a downlink is")
    print("normal and means bleed, not an uplink.")


def cmd_sweep(seconds, csv_path, verbose):
    need()
    print("=" * 74)
    print("  Volusia P25 Uplink Finder - SWEEP mode (peak detection)")
    print(f"  Band {BAND_LOW_MHZ}-{BAND_HIGH_MHZ} MHz | bin {BIN_HZ} Hz | "
          f"LNA {LNA} VGA {VGA} AMP {AMP}")
    print(f"  Peak rule: >= {PEAK_HEIGHT_DB} dB above local median, "
          f"width >= {MIN_RUN_BINS} bins, within {CHANNEL_TOL_HZ//1000} kHz of a channel")
    print(f"  Duration: {'until Ctrl-C' if seconds <= 0 else str(seconds) + 's'}")
    print("=" * 74)

    writer = None
    fh = None
    if csv_path:
        fh = open(csv_path, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["utc", "ul_mhz", "dl_mhz", "site", "db",
                         "prominence_db", "width_bins", "off_hz"])

    # Track per-channel activity
    hits = defaultdict(list)      # ch_hz -> [(ts, db, promo)]
    last_print = {}
    sweeps = 0
    t_start = time.time()

    print(f"\n{'time':<9} {'UL MHz':<11} {'DL MHz':<11} {'site':<6} "
          f"{'dB':>7} {'prom':>6} {'w':>3} {'offset':>9}  ")
    print("-" * 74)

    try:
        while True:
            if seconds > 0 and time.time() - t_start >= seconds:
                break
            bins = collect_sweep(verbose=False)
            if not bins:
                time.sleep(1)
                continue
            sweeps += 1
            now = datetime.now()
            lo, hi = BAND_LOW_MHZ * 1e6, BAND_HIGH_MHZ * 1e6

            ordered = sorted((f, d) for f, d in bins.items() if lo <= f <= hi)
            peaks = find_peaks(ordered)

            found_this_pass = set()
            for peak in peaks:
                f, db, promo, width = peak
                ch, off = nearest_channel(f)
                if abs(off) > CHANNEL_TOL_HZ:
                    continue  # energy, but not on a known uplink channel
                found_this_pass.add(ch)
                last = last_print.get(ch, 0.0)
                is_new = ch not in hits
                hits[ch].append((now, db, promo))
                if is_new or (now.timestamp() - last) >= COOLDOWN_SEC:
                    last_print[ch] = now.timestamp()
                    dl, site = channel_label(ch)
                    print(f"{now.strftime('%H:%M:%S'):<9} "
                          f"{f/1e6:<11.4f} {dl:<11} {site:<6} "
                          f"{db:>7.1f} {promo:>6.1f} {width:>3} "
                          f"{off/1000:>+7.1f}k  {'NEW' if is_new else ''}")
                    if writer:
                        writer.writerow([now.isoformat(), f"{f/1e6:.4f}", dl,
                                         site, f"{db:.1f}", f"{promo:.1f}",
                                         width, off])
            if verbose and sweeps % 10 == 0:
                print(f"  ..{sweeps} sweeps, channels with traffic so far: {len(hits)}")
    except KeyboardInterrupt:
        print("\n  stopped by user")
    finally:
        if fh:
            fh.close()

    dt = time.time() - t_start
    print("\n" + "=" * 74)
    print(f"  SUMMARY - {sweeps} sweeps over {dt:.0f}s")
    print("=" * 74)
    if not hits:
        print("  No activity on known uplink channels.")
        print("  Check: antenna connected, amp/gain not overloaded (run 'calib'),")
        print("         and that you are actually inside Volusia County coverage.")
    else:
        print(f"  {'UL MHz':<10} {'DL MHz':<10} {'site':<6} "
              f"{'hits':>5} {'peak dB':>8} {'best prom':>10}")
        print("  " + "-" * 58)
        for ch in sorted(hits, key=lambda c: -len(hits[c])):
            recs = hits[ch]
            dl, site = channel_label(ch)
            print(f"  {ch/1e6:<10.4f} {dl:<10} {site:<6} "
                  f"{len(recs):>5} {max(r[1] for r in recs):>8.1f} "
                  f"{max(r[2] for r in recs):>10.1f}")
        print(f"\n  Busiest: {sorted(hits, key=lambda c: -len(hits[c]))[0]/1e6:.4f} MHz")
        if csv_path:
            print(f"  CSV: {csv_path}")

    return hits


def cmd_channel(dwell, rounds, csv_path, dump=False):
    """
    Narrowband per-channel measurement.

    CRITICAL: HackRF has a large DC offset at the tune frequency. Tuning
    exactly onto a channel puts that artefact on top of the signal, and a
    naive peak-to-median measurement then reports 50-60 dB SNR on every
    channel including empty ones. That is not signal, it is the receiver.

    So we tune OFFSET_HZ away from the channel and look for the signal in a
    window centred on where it should appear, explicitly excluding the DC
    region around the LO. The noise floor is estimated from the same slice,
    away from both the DC spike and the signal window.
    """
    need()
    if np is None:
        sys.exit("channel mode needs numpy: pip install numpy")
    rate = 2_000_000          # 2 MSPS
    nsamp = int(rate * dwell)
    if nsamp > 20_000_000:
        nsamp = 20_000_000
    print("=" * 74)
    print("  Volusia P25 Uplink Finder - CHANNEL mode (DC-offset aware)")
    print(f"  {len(CHANNELS_HZ)} channels x {dwell}s dwell x {rounds} rounds "
          f"@ {rate/1e6:.0f} MSPS")
    print(f"  Tune offset {OFFSET_HZ/1000:+.0f} kHz; DC region "
          f"+/-{DC_EXCLUDE_HZ/1000:.0f} kHz excluded")
    print(f"  LNA {LNA} VGA {VGA} AMP {AMP}  (~{len(CHANNELS_HZ)*dwell*rounds:.0f}s total)")
    print("=" * 74)

    fh = None
    writer = None
    if csv_path:
        fh = open(csv_path, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["utc", "ul_mhz", "dl_mhz", "site", "snr_db",
                         "peak_db", "peak_offset_hz"])

    stats = defaultdict(lambda: {"snr": [], "rounds": 0, "active": 0})
    tmp = tempfile.mktemp(suffix=".iq")

    try:
        for r in range(rounds):
            print(f"\n  -- round {r+1}/{rounds} --")
            for ch in CHANNELS_HZ:
                # Tune away from the channel so the DC spike lands elsewhere.
                lo = ch + OFFSET_HZ
                cmd = sudo_prefix() + [
                    "hackrf_transfer",
                    "-d", SERIAL,
                    "-r", tmp,
                    "-f", str(lo),
                    "-s", str(rate),
                    "-n", str(nsamp),
                    "-a", str(AMP),
                    "-l", str(LNA),
                    "-g", str(VGA),
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                if not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
                    continue
                iq = np.fromfile(tmp, dtype=np.int8)
                os.unlink(tmp)
                iq = iq[:(len(iq) // 2) * 2]
                c = iq[0::2].astype(np.float32) + 1j * iq[1::2].astype(np.float32)
                if len(c) < 4096:
                    continue

                nfft = 4096
                segs = len(c) // nfft
                psd = np.zeros(nfft, dtype=np.float64)
                win = np.hanning(nfft)
                for k in range(segs):
                    seg = c[k * nfft:(k + 1) * nfft] * win
                    psd += np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
                psd /= max(segs, 1)
                psd_db = 10 * np.log10(psd + 1e-12)

                # Bin frequency axis: centre is the LO, i.e. `lo`.
                freqs = np.linspace(lo - rate / 2, lo + rate / 2, nfft,
                                    endpoint=False)
                offs = freqs - lo                      # offset from the LO
                dc_mask = np.abs(offs) <= DC_EXCLUDE_HZ

                # Where the channel of interest should appear.
                sig_centre = ch - lo
                sig_mask = np.abs(offs - sig_centre) <= SIG_WINDOW_HZ
                floor_mask = (~dc_mask) & (~sig_mask)
                if not sig_mask.any() or floor_mask.sum() < 16:
                    continue

                floor = float(np.median(psd_db[floor_mask]))
                sig_idx = np.where(sig_mask)[0]
                pk = int(sig_idx[int(np.argmax(psd_db[sig_mask]))])
                peak = float(psd_db[pk])
                peak_off = float(offs[pk]) - sig_centre
                snr = peak - floor

                stats[ch]["snr"].append(snr)
                stats[ch]["rounds"] += 1

                if dump:
                    dl, site = channel_label(ch)
                    print(f"\n   --- {ch/1e6:.4f} MHz  {dl} {site}  "
                          f"(LO {lo/1e6:.4f}, floor {floor:.1f} dB) ---")
                    step = max(1, nfft // 48)
                    for i in range(0, nfft, step):
                        bar = "#" * int(max(0.0, (psd_db[i] - floor) * 1.5))
                        mark = (" <== SIGNAL" if sig_mask[i] else
                                "  [DC]" if dc_mask[i] else "")
                        print(f"    {freqs[i]/1e6:10.4f} {psd_db[i]:6.1f} {bar}{mark}")

                if snr >= ACTIVE_SNR_DB:
                    stats[ch]["active"] += 1
                    dl, site = channel_label(ch)
                    print(f"   {ch/1e6:>10.4f} MHz  SNR {snr:5.1f} dB  "
                          f"({peak_off/1000:+5.1f} kHz)  "
                          f"{dl} {site}   ** ACTIVE **")
                    if writer:
                        writer.writerow([datetime.now().isoformat(),
                                         f"{ch/1e6:.4f}", dl, site,
                                         f"{snr:.1f}", f"{peak:.1f}",
                                         f"{peak_off:.0f}"])
    except KeyboardInterrupt:
        print("\n  stopped by user")
    finally:
        if fh:
            fh.close()
        if os.path.exists(tmp):
            os.unlink(tmp)

    print("\n" + "=" * 74)
    print("  CHANNEL RANKING (by median SNR, DC spike excluded)")
    print("=" * 74)
    print(f"  {'UL MHz':<10} {'DL MHz':<10} {'site':<6} {'med SNR':>8} "
          f"{'max SNR':>8} {'active':>7}")
    print("  " + "-" * 56)
    ranked = sorted(stats.items(), key=lambda kv: -statistics.median(kv[1]["snr"])
                    if kv[1]["snr"] else 0)
    for ch, s in ranked:
        if not s["snr"]:
            continue
        dl, site = channel_label(ch)
        print(f"  {ch/1e6:<10.4f} {dl:<10} {site:<6} "
              f"{statistics.median(s['snr']):>8.1f} {max(s['snr']):>8.1f} "
              f"{s['active']:>4}/{s['rounds']}")
    print(f"\n  A channel is real when SNR > {ACTIVE_SNR_DB:.0f} dB with a peak "
          "inside the signal window and steady across rounds.")
    print("  If EVERY channel still reads high, the front end is overloaded:")
    print("  lower VGA (e.g. VGA=10 LNA=16) and re-run.")
    if csv_path:
        print(f"  CSV: {csv_path}")


def cmd_spectrum(freq_hz, seconds):
    """
    Diagnostic: dump the spectrum around one frequency as ASCII bars.

    The LO is deliberately offset from the frequency of interest. Tuning
    straight onto it puts HackRF's DC artefact exactly where the signal
    should be, and that artefact reads ~55 dB above floor - indistinguishable
    from a strong carrier. Measured on live hardware: naming 856.9375 as the
    tune frequency produced an 80.5 dB peak at 856.9375 and flat 26 dB noise
    either side, which is the receiver, not a tower.

    So: tune OFFSET_HZ away, label the spike where it actually lands, and
    report a verdict on the requested frequency specifically.
    """
    need()
    if np is None:
        sys.exit("spectrum needs numpy: pip install numpy")
    rate = 2_000_000
    nsamp = int(rate * seconds)
    lo = int(freq_hz + OFFSET_HZ)
    tmp = tempfile.mktemp(suffix=".iq")
    print(f"  target {freq_hz/1e6:.4f} MHz, LO {lo/1e6:.4f} MHz "
          f"({OFFSET_HZ/1000:+.0f} kHz offset)")
    print(f"  capturing {seconds:.1f}s (LNA {LNA} VGA {VGA} AMP {AMP})")
    subprocess.run(sudo_prefix() + [
        "hackrf_transfer", "-d", SERIAL, "-r", tmp, "-f", str(lo),
        "-s", str(rate), "-n", str(nsamp), "-a", str(AMP),
        "-l", str(LNA), "-g", str(VGA),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(tmp):
        sys.exit("  capture failed - check device")
    iq = np.fromfile(tmp, dtype=np.int8)
    os.unlink(tmp)
    iq = iq[:(len(iq) // 2) * 2]
    c = iq[0::2].astype(np.float32) + 1j * iq[1::2].astype(np.float32)
    nfft = 4096
    segs = max(1, len(c) // nfft)
    win = np.hanning(nfft)
    psd = np.zeros(nfft, dtype=np.float64)
    for k in range(segs):
        seg = c[k * nfft:(k + 1) * nfft] * win
        psd += np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
    psd_db = 10 * np.log10(psd / segs + 1e-12)
    freqs = np.linspace(lo - rate / 2, lo + rate / 2, nfft, endpoint=False)

    # Exclude the DC region from every measurement, including the floor.
    offs = freqs - lo
    dc_mask = np.abs(offs) <= DC_EXCLUDE_HZ
    floor = float(np.median(psd_db[~dc_mask]))

    # Verdict on the requested frequency only.
    sig_mask = np.abs(freqs - freq_hz) <= SIG_WINDOW_HZ
    verdict_pk = float(psd_db[sig_mask].max()) if sig_mask.any() else float("nan")
    verdict_snr = verdict_pk - floor

    print(f"  floor {floor:.1f} dB (DC region excluded)")
    print(f"  at {freq_hz/1e6:.4f} MHz: peak {verdict_pk:.1f} dB  "
          f"= {verdict_snr:+.1f} dB above floor")
    if verdict_snr >= ACTIVE_SNR_DB:
        print(f"  >>> SIGNAL PRESENT (>= {ACTIVE_SNR_DB:.0f} dB) - carrier at this frequency")
    else:
        print(f"  >>> no carrier detected (threshold {ACTIVE_SNR_DB:.0f} dB)")

    print(f"\n  {'freq MHz':<12} {'dB':>6}")
    step = max(1, nfft // 96)
    label_core = rate / nfft * 3      # ~3 bins either side of the LO
    for i in range(0, nfft, step):
        off = offs[i]
        tag = ""
        if abs(off) <= label_core:
            tag = " [DC SPIKE - artefact, ignored]"
        elif sig_mask[i]:
            tag = " <== target"
        print(f"  {freqs[i]/1e6:10.4f} {psd_db[i]:6.1f} "
              f"{'#' * int(max(0.0, psd_db[i] - floor))}{tag}")


def main():
    global LNA, VGA, AMP, SERIAL, BAND_LOW_MHZ, BAND_HIGH_MHZ

    ap = argparse.ArgumentParser(
        description="Volusia FL P25 uplink transmitter finder (HackRF)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s calib                       # check gain/overload first
  %(prog)s sweep --seconds 300        # 5 min wideband, find hot channels
  %(prog)s sweep --band downlink       # listen to the towers instead
  %(prog)s sweep --csv hot.csv        # log detections
  %(prog)s channel --dwell 2 --rounds 5   # confirm + rank channels
  %(prog)s channel --dump --rounds 1      # see the spectrum per channel
  %(prog)s spectrum 811.4625              # inspect one frequency
  %(prog)s spectrum 856.9375 --band downlink   # is the control channel up?
""")
    sub = ap.add_subparsers(dest="mode")

    p_cal = sub.add_parser("calib", help="measure noise floor / overload")

    p_sw = sub.add_parser("sweep", help="wideband peak scan (default)")
    p_sw.add_argument("--seconds", type=int, default=0,
                      help="0 = run until Ctrl-C (default: 0)")
    p_sw.add_argument("--csv", help="write detections to CSV")
    p_sw.add_argument("-v", "--verbose", action="store_true")

    p_ch = sub.add_parser("channel", help="narrowband per-channel SNR (DC-aware)")
    p_ch.add_argument("--dwell", type=float, default=2.0,
                      help="seconds per channel (default 2)")
    p_ch.add_argument("--rounds", type=int, default=5,
                      help="passes over the channel list (default 5)")
    p_ch.add_argument("--csv", help="write detections to CSV")
    p_ch.add_argument("--dump", action="store_true",
                      help="print the spectrum for each channel")

    p_sp = sub.add_parser("spectrum",
                          help="dump the spectrum at one frequency (diagnostic)")
    p_sp.add_argument("freq_mhz", type=float,
                      help="tune frequency in MHz, e.g. 811.4625")
    p_sp.add_argument("--seconds", type=float, default=1.0,
                      help="capture length (default 1.0)")

    # global tuning overrides
    for p in (p_cal, p_sw, p_ch, p_sp):
        p.add_argument("--lna", type=int, default=LNA)
        p.add_argument("--vga", type=int, default=VGA)
        p.add_argument("--amp", type=int, default=AMP, choices=[0, 1])
        p.add_argument("--serial", default=SERIAL)
        p.add_argument("--band", choices=["uplink", "downlink"], default="uplink",
                       help="800 MHz side to scan: uplink (809-816, radios) or "
                            "downlink (854-861, towers). Default: uplink")

    args = ap.parse_args()

    LNA, VGA, AMP, SERIAL = args.lna, args.vga, args.amp, args.serial
    if args.band == "downlink":
        BAND_LOW_MHZ, BAND_HIGH_MHZ = DOWNLINK_LOW_MHZ, DOWNLINK_HIGH_MHZ
    else:
        BAND_LOW_MHZ, BAND_HIGH_MHZ = FREQ_LOW_MHZ, FREQ_HIGH_MHZ

    mode = args.mode or "sweep"
    if mode == "calib":
        cmd_calib()
    elif mode == "channel":
        cmd_channel(args.dwell, args.rounds, args.csv,
                    dump=getattr(args, "dump", False))
    elif mode == "spectrum":
        cmd_spectrum(int(round(args.freq_mhz * 1e6)), args.seconds)
    else:
        secs = getattr(args, "seconds", 0)
        csvp = getattr(args, "csv", None)
        verb = getattr(args, "verbose", False)
        cmd_sweep(secs, csvp, verb)


if __name__ == "__main__":
    main()
