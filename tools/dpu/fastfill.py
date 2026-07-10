"""Single-pass ceilings for bounded weighted water-filling.

waterfill_ceilings(C, items) == {k: waterfill(C, items with k's cap
replaced by repl_k)[k] for k in items}, computed in O(m log m) total
instead of m independent fills (O(m^2)). repl_k defaults to infinity
(the fair-share ceiling of design_e); a finite repl_k supports the
VM layer where the cap is replaced by MaxRate, not removed.

Math: with all caps, consumption at water level t is
F(t) = sum_j min(c_j, w_j t)  (piecewise linear, breakpoints t_j=c_j/w_j).
Replacing item i's cap by r_i changes only i's own term, so the new
level solves F(t) - min(c_i, w_i t) + min(r_i, w_i t) = C: binary-search
the same breakpoint grid with prefix sums. Ceiling_i = min(r_i, w_i t).
"""
import bisect


def waterfill_ceilings(capacity, items, repl=None):
    if not items:
        return {}
    keys = list(items)
    ws = {k: items[k][0] for k in keys}
    cs = {k: items[k][1] for k in keys}
    # sorted breakpoints and prefix sums of the all-caps consumption F
    order = sorted(keys, key=lambda k: cs[k] / ws[k])
    ts = [cs[k] / ws[k] for k in order]
    pc = [0.0]          # prefix sum of caps saturated below segment
    for k in order:
        pc.append(pc[-1] + cs[k])
    sw = [0.0] * (len(order) + 1)   # suffix sum of weights (unsaturated)
    for i in range(len(order) - 1, -1, -1):
        sw[i] = sw[i + 1] + ws[order[i]]
    pos = {k: i for i, k in enumerate(order)}

    def F(t):
        j = bisect.bisect_right(ts, t)
        return pc[j] + sw[j] * t

    out = {}
    for k in keys:
        r = float("inf") if repl is None else repl.get(k, float("inf"))
        w, c = ws[k], cs[k]

        def g(t):
            # consumption with k's cap replaced by r
            return F(t) - min(c, w * t) + min(r, w * t)

        # find the level t* with g(t*) = capacity on the breakpoint grid
        # extended by r/w (the replacement's own breakpoint)
        grid = ts + ([r / w] if r != float("inf") else [])
        grid = sorted(set(grid))
        lo_t, lo_g = 0.0, 0.0
        t_star = None
        for bp in grid:
            gb = g(bp)
            if gb >= capacity - 1e-6:
                # linear segment [lo_t, bp]
                slope = (gb - lo_g) / (bp - lo_t) if bp > lo_t else 0.0
                t_star = lo_t + ((capacity - lo_g) / slope if slope > 0 else 0.0)
                break
            lo_t, lo_g = bp, gb
        if t_star is None:
            # capacity not exhausted even past all breakpoints
            j = len(ts)
            w_open = sw[j] - (0.0 if cs[k] / ws[k] > ts[-1] else 0.0)
            # remaining slope = weights never saturating = w (k, if r=inf)
            slope = w if r == float("inf") else 0.0
            if slope > 0:
                t_star = lo_t + (capacity - lo_g) / slope
            else:
                t_star = lo_t
        out[k] = min(r, w * t_star)
    return out
