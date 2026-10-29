#!/bin/bash
# Container entrypoint / supervisor for the Volusia P25 uplink detector.
#
# PURPOSE
#   HackRF One on VMware (and some USB hubs) drops off the bus under sustained
#   use. The detector then dies with LIBUSB_ERROR_NO_DEVICE. This wrapper keeps
#   it running:
#     - verifies the device is present before each attempt
#     - restarts after a dropout, resetting the USB device first
#     - backs off progressively so a missing device does not spin
#     - exits cleanly in one-shot modes (calib) instead of restarting forever
#
#   The reset path uses `usbreset`, which this image provides. Do not "fix"
#   dropouts by switching the VM's USB controller to PCI passthrough - that
#   crashes xHCI and takes the whole guest's USB with it.

set -u

HACKRF_VIDPID="${HACKRF_VIDPID:-1d50:6089}"
HACKRF_SERIAL="${HACKRF_SERIAL:-0000000000000000406464c82346594b}"
DETECTOR="${DETECTOR:-/app/p25_uplink_hackrf_peaks.py}"

# Mode can arrive two ways:
#   docker compose run --rm detector calib     -> positional arg (compose "command")
#   MODE=calib docker compose up -d            -> environment variable
# Positional arg wins, because that is what the operator typed most recently.
MODE="${1:-${MODE:-sweep}}"
case "$MODE" in
    sweep|calib) ;;
    channel|spectrum)
        # These take their own arguments after the mode name; shift it off so
        # `detector spectrum 105.1 --seconds 2` passes the rest through.
        shift 2>/dev/null || true
        ;;
    *) echo "unknown mode '$MODE' (use sweep|channel|calib|spectrum)" >&2; exit 2 ;;
esac

# Free-form arguments after the mode (e.g. "spectrum 105.1 --seconds 2")
EXTRA_ARGS=("$@")

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/uplink_$(date +%Y%m%d).log"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# USB helpers
# ---------------------------------------------------------------------------
device_present() {
    lsusb -d "$HACKRF_VIDPID" >/dev/null 2>&1 && return 0
    if command -v hackrf_info >/dev/null 2>&1; then
        hackrf_info 2>/dev/null | grep -q "Serial number: $HACKRF_SERIAL" && return 0
    fi
    return 1
}

reset_device() {
    log "USB recovery: resetting $HACKRF_VIDPID"
    if usbreset "$HACKRF_VIDPID" >>"$LOG_FILE" 2>&1; then
        sleep 3
        device_present && { log "USB recovery: back"; return 0; }
    fi
    # sysfs fallback: unbind then rebind the usb driver
    local d buspath
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        if [ "$(cat "$d/idVendor")" = "${HACKRF_VIDPID%%:*}" ] && \
           [ "$(cat "$d/idProduct")" = "${HACKRF_VIDPID##*:}" ]; then
            buspath=$(basename "$d")
            log "USB recovery: unbind/bind $buspath"
            echo "$buspath" > /sys/bus/usb/drivers/usb/unbind 2>>"$LOG_FILE"
            sleep 3
            echo "$buspath" > /sys/bus/usb/drivers/usb/bind 2>>"$LOG_FILE"
            sleep 3
            device_present && { log "USB recovery: back"; return 0; }
        fi
    done
    log "USB recovery: FAILED - device did not return"
    return 1
}

# ---------------------------------------------------------------------------
# Detector invocation
# ---------------------------------------------------------------------------
detector_args() {
    # Only pass overrides that were actually set, so the detector's own
    # defaults stay authoritative and the log line reflects reality.
    local common=()
    [ -n "${HACKRF_SERIAL:-}" ] && common+=(--serial "$HACKRF_SERIAL")
    [ -n "${LNA:-}" ] && common+=(--lna "$LNA")
    [ -n "${VGA:-}" ] && common+=(--vga "$VGA")
    [ -n "${AMP:-}" ] && common+=(--amp "$AMP")

    case "$MODE" in
        calib)
            echo "calib" "${common[@]}"
            ;;
        channel)
            echo "channel" --dwell "${DWELL:-2}" --rounds "${ROUNDS:-5}" \
                 "${common[@]}" ${CSV:+--csv "$CSV"} "${EXTRA_ARGS[@]}"
            ;;
        spectrum)
            echo "spectrum" "${EXTRA_ARGS[@]}" "${common[@]}"
            ;;
        sweep|*)
            echo "sweep" "${common[@]}" ${CSV:+--csv "$CSV"} "${EXTRA_ARGS[@]}"
            ;;
    esac
}

run_once() {
    local args
    read -r -a args <<< "$(detector_args)"

    # hackrf tools are absent from sudo-bearing images and the device node is
    # root-only (crw-rw-r--), so run the detector as root explicitly when we
    # have the privilege but are not already uid 0.
    local runner=(python3 -u "$DETECTOR")
    if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
        runner=(sudo python3 -u "$DETECTOR")
    fi

    log "running: ${runner[*]} ${args[*]}"
    "${runner[@]}" "${args[@]}" 2>&1 | tee -a "$LOG_FILE"
    return "${PIPESTATUS[0]}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
log "=== container start ==="
log "mode=$MODE lna=${LNA:-32} vga=${VGA:-22} amp=${AMP:-0} serial=$HACKRF_SERIAL"

if ! lsusb -d "$HACKRF_VIDPID" >/dev/null 2>&1; then
    log "WARNING: HackRF ($HACKRF_VIDPID) not visible on /dev/bus/usb."
    log "  Host fix: on VMware/Proxmox, attach the HackRF to the guest;"
    log "  then confirm with: lsusb -d $HACKRF_VIDPID on the host AND guest."
    log "  On Windows/macOS Docker Desktop this cannot work - see README."
fi

backoff=5
attempt=0

while true; do
    attempt=$((attempt + 1))

    if ! device_present; then
        log "HackRF not present (attempt $attempt) - trying reset"
        if ! reset_device; then
            log "waiting ${backoff}s before retry"
            sleep "$backoff"
            backoff=$(( backoff * 2 > 120 ? 120 : backoff * 2 ))
            continue
        fi
    fi

    run_once
    rc=$?

    # One-shot modes: a real run that completed is not a fault.
    if [ "$MODE" = "calib" ] || [ "$MODE" = "channel" ]; then
        if device_present; then
            log "$MODE finished cleanly (rc=$rc)"
            exit "$rc"
        fi
        log "$MODE exited rc=$rc and the device is gone - recovering"
        reset_device || log "recovery failed"
        sleep "$backoff"
        backoff=$(( backoff * 2 > 120 ? 120 : backoff * 2 ))
        continue
    fi

    # sweep mode: continuous by design, so any exit is a fault worth retrying
    if device_present; then
        log "detector exited rc=$rc with device still present - restarting in ${backoff}s"
        sleep "$backoff"
        backoff=$(( backoff * 2 > 60 ? 60 : backoff * 2 ))
    else
        log "HackRF dropped during run (USB dropout) - recovering"
        reset_device || log "recovery failed"
        sleep "$backoff"
        backoff=$(( backoff * 2 > 120 ? 120 : backoff * 2 ))
    fi
done
