# Volusia P25 Uplink Finder (HackRF)

Detects nearby radio transmitters on Volusia County, FL public-safety P25
uplink channels (809–816 MHz, uplink = downlink − 45 MHz).

## What this is

A containerized HackRF One scanner that watches the 800 MHz P25 uplink band
and reports which channels carry RF energy — i.e. which channels have a radio
transmitting within range.

**It does not demodulate P25.** You get frequency, site label, signal level,
and peak prominence. No talkgroup IDs, no unit IDs, no audio.

## Hardware requirements

- **HackRF One**, USB 2.0, plugged into the Docker host directly.
  Serial in use: `0000000000000000406464c82346594b`
- Linux host with USB passthrough to Docker.
- **Windows / Docker Desktop will NOT work.** See *Platform notes* below.
- A suitable 800 MHz antenna. The stock telescopic whip is poor at 800 MHz.

## Platform notes — read this before deploying

The HackRF is a raw USB device. The container needs `/dev/bus/usb` access.

| Host | Works? | Why |
|---|---|---|
| Linux (bare metal or VM with USB device passthrough to the host) | Yes | `/dev/bus/usb` maps into the container |
| Windows + Docker Desktop | **No** | USB devices are inside the WSL2 VM / Hyper-V backend; there is no `/dev/bus/usb` path to bind. The device would have to be attached to the Docker Desktop VM first, which is not exposed as a supported flow. |
| macOS + Docker Desktop | **No** | Same class of problem; USB is owned by the macOS host. |
| VMware / ESXi VM, USB *device* passthrough | Yes, **with flakiness** | HackRF drops off the bus under sustained load. Use the watchdog. Do **not** switch the USB controller to PCI passthrough to fix it — that crashes xHCI. |

If your HackRF is plugged into a Windows box, the container pattern does not
apply. Run the Python script natively on Windows instead, or move the HackRF
to a Linux host.

## Quick start

```bash
git clone <repo> && cd volusia-p25-uplink
cp .env.example .env          # optional overrides
docker compose up -d
docker compose logs -f
```

Detections stream to stdout and to `./logs/uplink_YYYYMMDD.log`.

## Modes

The `MODE` env var selects behaviour.

### `sweep` (default)
Wideband `hackrf_sweep` across 809–816 MHz. Fast. Finds which channels are
hot. Runs continuously until stopped.

```bash
MODE=sweep docker compose up -d
```

### `channel`
Retunes to each of the 32 known uplink channels and does a narrowband FFT.
Slower, but measures true SNR per channel and catches short bursts a sweep
steps over.

```bash
MODE=channel DWELL=2 ROUNDS=5 docker compose up -d
```

### `calib`
One-shot. Measures noise floor and overload, prints a small report, exits.
Run this first on new hardware or a new antenna.

```bash
docker compose run --rm detector calib
```

## Detection method

The naive approach — compare each bin to one global noise floor — fails on
this band. Public-safety downlinks at 854–861 MHz overload the HackRF front
end and raise the floor by 5–10 dB across 809–816 MHz. Low threshold gives
hundreds of false positives; high threshold misses real 12.5 kHz uplinks that
only reach +2…6 dB.

So this uses **local peak detection** instead:

- a bin must be a local maximum
- at least `PEAK_HEIGHT_DB` (4 dB) above the median of its ±`PEAK_RADIUS`
  (±10 kHz) neighbourhood
- at least `MIN_RUN_BINS` (2) bins wide
- within `CHANNEL_TOL_HZ` (8 kHz) of a known uplink channel

Narrowband P25 uplinks pass. Broadband downlink bleed — smooth and wide —
does not.

## Configuration

Environment variables, set in `.env` or via compose `environment:`.

| Var | Default | Meaning |
|---|---|---|
| `MODE` | `sweep` | `sweep`, `channel`, or `calib` |
| `HACKRF_SERIAL` | `…406464c82346594b` | Device serial |
| `LNA` | `32` | IF gain 0–40 dB, 8 dB steps |
| `VGA` | `22` | Baseband gain 0–62 dB, 2 dB steps |
| `AMP` | `0` | RF amp. Keep **off** at 800 MHz — downlinks nearby |
| `BIN_HZ` | `2000` | Sweep bin width |
| `PEAK_HEIGHT_DB` | `4.0` | Peak prominence threshold |
| `DWELL` | `2` | Seconds per channel, `channel` mode |
| `ROUNDS` | `5` | Passes over channel list, `channel` mode |

If `calib` reports a median above about −10 dB, the front end is being driven
hard — drop `VGA` by 8 and re-run.

## The watchdog

`run_uplink_log.sh` wraps the detector and handles USB dropouts, which are
expected on VMware device passthrough.

- checks the device is present before each attempt
- on death, distinguishes "device vanished" from "detector crashed"
- recovers via `usbreset`, falling back to kernel unbind/bind in sysfs
- exponential backoff 5 s → 120 s
- logs every reset; `docker compose logs` shows the count

`usbreset` is included in the image. If the device does not come back after a
reset, the fix is above the guest — in vSphere, VM → Removable Devices →
HackRF One → Connect. No API path is reliable for that.

## Channel plan

32 uplink channels across East Simulcast, West Simulcast, and Holly Hill.
Site labels are approximate and derived from RadioReference data captured
earlier; **verify against current RadioReference before attributing activity
to a specific site or agency.**

Edit `UPLINK_CHANNELS` in `p25_uplink_hackrf_peaks.py` to update.

## Limitations, stated plainly

- No P25 demodulation. Frequency and signal strength only.
- RSSI is uncalibrated. Relative comparisons are valid; absolute dB and
  any distance estimate are not.
- Sweep mode dwells milliseconds per bin; short voice spurs can be missed.
  Channel mode has far better capture odds.
- 809–816 MHz uplinks are receive-only observations. This performs no
  transmission.
- Reception depends entirely on antenna, height, and distance to the
  nearest site. Indoors with a stock whip you may see nothing.

## Files

```
p25_uplink_hackrf_peaks.py   detector, three modes
run_uplink_log.sh            USB-hardened wrapper
Dockerfile                   python + hackrf tools
docker-compose.yml           single service, /dev/bus/usb passthrough
.env.example                 configuration template
```

## License

Personal use. Channel plan data belongs to its respective sources.
