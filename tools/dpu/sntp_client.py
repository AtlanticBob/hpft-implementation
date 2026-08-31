#!/usr/bin/env python3
"""SNTP client for the DPUs (no systemd-timesyncd, no chrony there).
Every INTERVAL s: several exchanges with the host's responder over tmfifo,
keep the lowest-RTT sample, apply the offset with adjtimex(ADJ_SETOFFSET)
(an exact, immediate correction; the drift is a few ms per 10 s so the
steps are small once locked). Prints offset/rtt so `dpu_time_sync.sh
status` can read them. usage: sudo sntp_client.py <server_ip> [interval_s=2]
"""
import ctypes, socket, struct, sys, time

srv = sys.argv[1]; itv = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
NTP_EPOCH = 2208988800
ADJ_SETOFFSET, ADJ_NANO = 0x0100, 0x2000


class timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                ("esterror", ctypes.c_long), ("status", ctypes.c_int), ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                ("tolerance", ctypes.c_long), ("time_sec", ctypes.c_long), ("time_usec", ctypes.c_long), ("tick", ctypes.c_long),
                ("ppsfreq", ctypes.c_long), ("jitter", ctypes.c_long), ("shift", ctypes.c_int), ("stabil", ctypes.c_long),
                ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long), ("stbcnt", ctypes.c_long),
                ("tai", ctypes.c_int), ("pad", ctypes.c_int * 11)]


libc = ctypes.CDLL("libc.so.6", use_errno=True)


def apply_offset(off_s):
    t = timex(); t.modes = ADJ_SETOFFSET | ADJ_NANO
    sec = int(off_s // 1); nsec = int((off_s - sec) * 1e9)
    t.time_sec, t.time_usec = sec, nsec          # with ADJ_NANO, time_usec carries nanoseconds
    if libc.adjtimex(ctypes.byref(t)) < 0:
        raise OSError(ctypes.get_errno(), "adjtimex")


def to_ts(b):
    s, f = struct.unpack("!II", b); return s - NTP_EPOCH + f / (1 << 32)


ADJ_FREQUENCY, ADJ_TICK = 0x0002, 0x4000


def set_rate(ppm):
    """Coarse part in tick (1 unit = 100 ppm), fine part in freq (2^-16 ppm units)."""
    t = timex(); t.modes = ADJ_TICK | ADJ_FREQUENCY
    coarse = int(round(ppm / 100.0)); coarse = max(-10, min(10, coarse))
    fine = ppm - 100.0 * coarse; fine = max(-500.0, min(500.0, fine))
    t.tick = 10000 + coarse; t.freq = int(fine * 65536)
    if libc.adjtimex(ctypes.byref(t)) < 0:
        raise OSError(ctypes.get_errno(), "adjtimex rate")


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.settimeout(1.0)
rate_ppm = 0.0; hist = []            # (monotonic, offset) since the last rate update
while True:
    best = None
    for _ in range(4):
        t1 = time.time()
        pkt = bytearray(48); pkt[0] = (4 << 3) | 3
        pkt[40:48] = struct.pack("!II", int(t1) + NTP_EPOCH, int((t1 - int(t1)) * (1 << 32)))
        try:
            sock.sendto(bytes(pkt), (srv, 123)); data, _ = sock.recvfrom(1024)
        except (socket.timeout, OSError) as e:
            print("sntp: no reply from %s (%s)" % (srv, e), flush=True); continue
        t4 = time.time(); t2, t3 = to_ts(data[32:40]), to_ts(data[40:48])
        rtt = (t4 - t1) - (t3 - t2); off = ((t2 - t1) + (t3 - t4)) / 2
        if best is None or rtt < best[0]:
            best = (rtt, off)
        time.sleep(0.05)
    if best:
        rtt, off = best; st = "applied"
        try:
            apply_offset(off)
        except OSError as e:
            st = "FAILED %s" % e
        # frequency discipline: the offsets we keep having to apply, per second,
        # are the residual drift; fold them into the kernel's rate
        hist.append((time.monotonic(), off))
        if len(hist) >= 5:
            span = hist[-1][0] - hist[0][0]; drift = sum(o for _, o in hist) / span * 1e6 if span > 0 else 0.0
            # off = server - client: positive means this clock runs behind, so speed it up
            rate_ppm = max(-1000.0, min(1000.0, rate_ppm + drift))
            try:
                set_rate(rate_ppm); hist = []
            except OSError as e:
                st += " rate-FAILED %s" % e
        print("sntp: offset %+.3f ms rtt %.3f ms rate %+.0f ppm %s" % (off * 1e3, rtt * 1e3, rate_ppm, st), flush=True)
    time.sleep(itv)
