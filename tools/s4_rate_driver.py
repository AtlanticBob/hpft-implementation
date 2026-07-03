#!/usr/bin/env python3
"""Drive the PCC rate FIFO on the DPU: precision sweep + square waves + max rate.

Runs ON THE DPU. Writes rate values (2^20 = line rate 200G) to the FIFO and
logs send timestamps to stdout as CSV: phase,ts_ns,rate.

Usage: s4_rate_driver.py <fifo>
"""
import sys
import time

fifo_path = sys.argv[1]
fifo = open(fifo_path, "w")

def set_rate(phase, val):
    fifo.write(f"{val}\n")
    fifo.flush()
    print(f"{phase},{time.time_ns()},{val}")

def g_to_units(g):
    return max(1, int(g / 200.0 * (1 << 20)))

print("phase,ts_ns,rate")
# P1: precision sweep, 8 s per level
for g in [1, 2, 4, 8, 12, 16, 32, 64]:
    set_rate(f"sweep_{g}g", g_to_units(g))
    time.sleep(8)

# P2: 10 Hz square wave 8G/4G for 20 s
t_end = time.time() + 20
level = True
while time.time() < t_end:
    set_rate("wave10hz", g_to_units(8 if level else 4))
    level = not level
    time.sleep(0.1)

# P3: max-rate back-to-back alternating sends, 200 sends
for i in range(200):
    set_rate("maxrate", g_to_units(8 if i % 2 == 0 else 4))

# park at 8G
set_rate("park", g_to_units(8))
