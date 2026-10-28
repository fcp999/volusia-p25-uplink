# syntax=docker/dockerfile:1

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# hackrf: provides hackrf_info / hackrf_sweep / hackrf_transfer
# usbutils: lsusb, used by the wrapper's device check
# usb-reset sources are tiny, so we build one rather than pull a helper package
RUN apt-get update && apt-get install -y --no-install-recommends \
        hackrf \
        python3 \
        python3-numpy \
        usbutils \
        gcc \
        libc6-dev \
        ca-certificates \
        bash \
    && rm -rf /var/lib/apt/lists/*

# usbreset: minimal USBDEVFS_RESET tool, used by the wrapper between retries.
# Building it here keeps the image self-contained (no reliance on a distro pkg).
RUN cat > /tmp/usbreset.c <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>

int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "usage: usbreset /dev/bus/usb/BBB/DDD\n"); return 2; }
    int fd = open(argv[1], O_WRONLY);
    if (fd < 0) { perror("open"); return 1; }
    int rc = ioctl(fd, USBDEVFS_RESET, 0);
    if (rc < 0) { perror("USBDEVFS_RESET"); close(fd); return 1; }
    close(fd);
    printf("reset %s\n", argv[1]);
    return 0;
}
EOF
RUN gcc -O2 -o /usr/local/bin/usbreset /tmp/usbreset.c \
    && rm /tmp/usbreset.c \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Wrapper expects `usbreset <vid:pid>`. Adapt it to find the sysfs node and
# reset by bus/device path, which is what the C tool takes.
RUN cat > /usr/local/bin/usbreset-vidpid <<'EOF'
#!/bin/bash
# usbreset-vidpid VID:PID  -> reset every matching USB device
[ $# -eq 1 ] || { echo "usage: usbreset-vidpid VID:PID" >&2; exit 2; }
vid=${1%%:*}; pid=${1##*:}
found=0
for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] || continue
    [ "$(cat "$d/idVendor")"  = "$vid" ] || continue
    [ "$(cat "$d/idProduct")" = "$pid" ] || continue
    busnum=$(cat "$d/busnum"); devnum=$(cat "$d/devnum")
    node=$(printf "/dev/bus/usb/%03d/%03d" "$busnum" "$devnum")
    [ -e "$node" ] || continue
    echo "resetting $node ($d)"
    usbreset "$node" || exit 1
    found=1
done
[ "$found" = 1 ] || { echo "no device matching $1" >&2; exit 1; }
EOF
RUN chmod +x /usr/local/bin/usbreset-vidpid

# The wrapper calls `usbreset`; make the bare name resolve to the vid:pid form
# when given a VID:PID argument.
RUN cat > /usr/local/bin/usbreset <<'EOF'
#!/bin/bash
# If the argument looks like VID:PID, delegate to the VID:PID resolver.
if [[ "$1" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$ ]]; then
    exec /usr/local/bin/usbreset-vidpid "$1"
fi
exec /usr/local/bin/usbreset-node "$@"
EOF
RUN mv /usr/local/bin/usbreset /usr/local/bin/usbreset-node \
    && cat > /usr/local/bin/usbreset <<'EOF'
#!/bin/bash
if [[ "$1" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$ ]]; then
    exec /usr/local/bin/usbreset-vidpid "$1"
fi
exec /usr/local/bin/usbreset-node "$@"
EOF
RUN chmod +x /usr/local/bin/usbreset

WORKDIR /app

COPY p25_uplink_hackrf_peaks.py run_uplink_log.sh ./
RUN chmod +x run_uplink_log.sh

# Detector writes CSV logs here if you enable output dirs
RUN mkdir -p /app/logs

ENTRYPOINT ["/app/run_uplink_log.sh"]
