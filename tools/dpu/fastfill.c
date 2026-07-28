/* Bounded weighted water-filling, C implementation of fastfill.py.
 *
 * Why: the receiver's per-tick cost is 77% water-filling at every scale
 * measured (results/scale_20260727), and the Python control loop stops
 * fitting the 1 ms period at ~100 flow-sets. The scaling is near-linear -
 * 64x the flow-sets costs 35x the time - so the single-pass cascade's
 * O(N log N) is already right and what is left is the constant factor.
 * That is a language problem, not an algorithm problem, which is what
 * makes this rewrite the correct fix rather than a micro-optimisation.
 *
 * Loaded through ctypes (no Python headers needed on the DPU Arm, which
 * only has to host a plain gcc). fastfill.py falls back to its own pure
 * Python path when the shared object is absent, so the agent still runs
 * on a machine where this was never built.
 *
 * Semantics are byte-for-byte the ones fastfill.py documents, INCLUDING
 * its epsilons - property-tested against it, see fastfill_test.py.
 *
 * build: gcc -O2 -shared -fPIC -o libfastfill.so fastfill.c -lm
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---- plain bounded weighted water-filling -------------------------
 * Mirrors rx_agent.waterfill: raise a common water level until either
 * the cheapest cap saturates (that item freezes and releases the rest)
 * or the capacity runs out.
 */
void hpft_waterfill(double capacity, int n, const double *w,
                    const double *cap, double *out)
{
    char *active = (char *)calloc((size_t)n, 1);
    double remaining = capacity;
    int i, nact = 0;

    for (i = 0; i < n; i++) {
        out[i] = 0.0;
        if (cap[i] > 0.0) {
            active[i] = 1;
            nact++;
        }
    }

    while (nact > 0 && remaining > 1e-3) {
        double wsum = 0.0, t_sat = INFINITY, t, alloc_sum = 0.0;

        for (i = 0; i < n; i++) {
            if (!active[i])
                continue;
            wsum += w[i];
            double room = (cap[i] - out[i]) / w[i];
            if (room < t_sat)
                t_sat = room;
        }
        t = t_sat;
        if (wsum > 0.0 && remaining / wsum < t)
            t = remaining / wsum;

        for (i = 0; i < n; i++) {
            if (!active[i])
                continue;
            out[i] += w[i] * t;
            if (out[i] >= cap[i] - 1e-3) {
                out[i] = cap[i];
                active[i] = 0;
                nact--;
            }
        }
        for (i = 0; i < n; i++)
            alloc_sum += out[i];
        remaining = capacity - alloc_sum;

        /* capacity exhausted before the next saturation */
        if (t < t_sat && !isinf(t_sat))
            break;
    }
    free(active);
}

/* ---- single-pass ceilings -----------------------------------------
 * ceiling_i = what i would get if its own cap were replaced by repl_i
 * (infinity for the fair-share ceiling) while every sibling keeps its
 * cap. One sorted breakpoint grid with prefix sums serves all i, which
 * is what turns m independent fills into O(m log m).
 */
struct bp { double t; double c; double w; };

static int bp_cmp(const void *a, const void *b)
{
    double x = ((const struct bp *)a)->t, y = ((const struct bp *)b)->t;
    return (x > y) - (x < y);
}

void hpft_waterfill_ceilings(double capacity, int n, const double *w,
                             const double *cap, const double *repl,
                             double *out)
{
    if (n <= 0)
        return;

    struct bp *o = (struct bp *)malloc(sizeof(struct bp) * (size_t)n);
    double *pc = (double *)malloc(sizeof(double) * (size_t)(n + 1));
    double *sw = (double *)malloc(sizeof(double) * (size_t)(n + 1));
    double *ts = (double *)malloc(sizeof(double) * (size_t)n);
    int i;

    for (i = 0; i < n; i++) {
        o[i].t = cap[i] / w[i];
        o[i].c = cap[i];
        o[i].w = w[i];
    }
    qsort(o, (size_t)n, sizeof(struct bp), bp_cmp);

    pc[0] = 0.0;
    for (i = 0; i < n; i++) {
        pc[i + 1] = pc[i] + o[i].c;
        ts[i] = o[i].t;
    }
    sw[n] = 0.0;
    for (i = n - 1; i >= 0; i--)
        sw[i] = sw[i + 1] + o[i].w;

    for (i = 0; i < n; i++) {
        double r = repl ? repl[i] : INFINITY;
        double wi = w[i], ci = cap[i];
        double lo_t = 0.0, lo_g = 0.0, t_star;
        int found = 0, j;

        /* breakpoint grid = the shared one, plus r/w when r is finite.
         * Walk it in order; the first breakpoint whose consumption
         * reaches the capacity brackets the answer, then interpolate
         * inside that linear segment. */
        double extra = isinf(r) ? INFINITY : r / wi;
        int k = 0;
        int used_extra = 0;

        while (k < n || !used_extra) {
            double bp_t;

            if (k < n && (used_extra || ts[k] <= extra))
                bp_t = ts[k++];
            else if (!used_extra && !isinf(extra)) {
                bp_t = extra;
                used_extra = 1;
            } else if (!used_extra) {   /* r infinite: no extra breakpoint */
                used_extra = 1;
                continue;
            } else
                break;

            if (bp_t == lo_t && (k > 1 || used_extra))
                continue;               /* duplicate grid point */

            /* F(t) with i's cap swapped for r */
            int jj = 0;
            {   /* bisect_right(ts, bp_t) */
                int loi = 0, hii = n;
                while (loi < hii) {
                    int mid = (loi + hii) / 2;
                    if (ts[mid] <= bp_t)
                        loi = mid + 1;
                    else
                        hii = mid;
                }
                jj = loi;
            }
            double F = pc[jj] + sw[jj] * bp_t;
            double mi_c = ci < wi * bp_t ? ci : wi * bp_t;
            double mi_r = r < wi * bp_t ? r : wi * bp_t;
            double gb = F - mi_c + mi_r;

            if (gb >= capacity - 1e-6) {
                double slope = (bp_t > lo_t) ? (gb - lo_g) / (bp_t - lo_t)
                                             : 0.0;
                t_star = lo_t + (slope > 0.0 ? (capacity - lo_g) / slope
                                             : 0.0);
                found = 1;
                break;
            }
            lo_t = bp_t;
            lo_g = gb;
        }

        if (!found) {
            /* capacity never exhausted on the grid: only an item with an
             * infinite replacement still has an open slope beyond it */
            double slope = isinf(r) ? wi : 0.0;
            t_star = slope > 0.0 ? lo_t + (capacity - lo_g) / slope : lo_t;
        }
        (void)j;
        double v = wi * t_star;
        out[i] = (r < v) ? r : v;
    }

    free(o);
    free(pc);
    free(sw);
    free(ts);
}

/* ---- the whole three-layer allocation in one call ------------------
 * Replacing only the two primitives left the per-tick cost dominated by
 * marshalling: a 288-flow-set tick made ~37 ctypes round trips, each
 * rebuilding Python dicts and lists around a few microseconds of actual
 * arithmetic. Measured after that first step, water-filling was still
 * 72% of the tick - but it was no longer the ARITHMETIC. So the tree
 * itself moves here and the boundary is crossed once per tick.
 *
 * Computes both outputs of Scheduler.entitlements:
 *   e[i]    demand-capped share (the grant)
 *   ceil[i] fair-share ceiling: what i would get with its own demand set
 *           to infinity while siblings keep theirs (the VM layer instead
 *           replaces the cap by MaxRate, which is what vm_max is for)
 *
 * Flow-sets are given flat, already bucketed by (dst VM, class) on the
 * Python side, which is a plain O(N) pass rather than a dict of dicts.
 */
void hpft_entitlements(int n, const int *dst, const int *cls,
                       const double *wfs, const double *demand,
                       int n_vm, int n_cls,
                       const double *vm_w, const double *vm_max,
                       const double *cls_w, double c_root,
                       double *out_e, double *out_ceil)
{
    int nvc = n_vm * n_cls;
    double *cls_dem = (double *)calloc((size_t)nvc, sizeof(double));
    double *vm_dem = (double *)calloc((size_t)n_vm, sizeof(double));
    double *vm_cap = (double *)malloc(sizeof(double) * (size_t)n_vm);
    double *vm_share = (double *)malloc(sizeof(double) * (size_t)n_vm);
    double *vm_ceil = (double *)malloc(sizeof(double) * (size_t)n_vm);
    double *cw = (double *)malloc(sizeof(double) * (size_t)n_cls);
    double *cc = (double *)malloc(sizeof(double) * (size_t)n_cls);
    double *cs = (double *)malloc(sizeof(double) * (size_t)n_cls);
    double *ck = (double *)malloc(sizeof(double) * (size_t)n_cls);
    int *cnt = (int *)calloc((size_t)nvc, sizeof(int));
    int *head = (int *)malloc(sizeof(int) * (size_t)(nvc + 1));
    int *idx = (int *)malloc(sizeof(int) * (size_t)(n > 0 ? n : 1));
    int *fill = (int *)malloc(sizeof(int) * (size_t)nvc);
    double *fw = (double *)malloc(sizeof(double) * (size_t)(n > 0 ? n : 1));
    double *fc = (double *)malloc(sizeof(double) * (size_t)(n > 0 ? n : 1));
    double *fo = (double *)malloc(sizeof(double) * (size_t)(n > 0 ? n : 1));
    int i, v, c;

    /* bucket flow-sets by (vm, class): counting sort into idx[] */
    for (i = 0; i < n; i++) {
        int b = dst[i] * n_cls + cls[i];
        cls_dem[b] += demand[i];
        vm_dem[dst[i]] += demand[i];
        cnt[b]++;
    }
    head[0] = 0;
    for (i = 0; i < nvc; i++) {
        head[i + 1] = head[i] + cnt[i];
        fill[i] = head[i];
    }
    for (i = 0; i < n; i++) {
        int b = dst[i] * n_cls + cls[i];
        idx[fill[b]++] = i;
    }

    /* layer 1: root capacity across dst VMs */
    for (v = 0; v < n_vm; v++) {
        double m = vm_max[v];
        vm_cap[v] = (m < vm_dem[v]) ? m : vm_dem[v];
    }
    hpft_waterfill(c_root, n_vm, vm_w, vm_cap, vm_share);
    hpft_waterfill_ceilings(c_root, n_vm, vm_w, vm_cap, vm_max, vm_ceil);

    for (v = 0; v < n_vm; v++) {
        /* A VM with no demand right now must NOT be skipped: its grant is
         * zero but its CEILING is not - ceil is defined with this node's
         * own demand set to infinity, so an idle VM still has a share it
         * would be entitled to, and its flow-sets must receive it. Skipping
         * it left their ceilings at zero and was the one place the C path
         * disagreed with Python (143 of 16296 samples). */
        for (c = 0; c < n_cls; c++) {
            cw[c] = cls_w[v * n_cls + c];
            cc[c] = cls_dem[v * n_cls + c];
        }
        /* layer 2: the VM's share across its classes */
        hpft_waterfill(vm_share[v], n_cls, cw, cc, cs);
        hpft_waterfill_ceilings(vm_ceil[v], n_cls, cw, cc, NULL, ck);

        /* layer 3: the class share across senders */
        for (c = 0; c < n_cls; c++) {
            int b = v * n_cls + c, m = head[b + 1] - head[b], t;
            if (m <= 0)
                continue;
            for (t = 0; t < m; t++) {
                int f = idx[head[b] + t];
                fw[t] = wfs[f];
                fc[t] = demand[f];
            }
            hpft_waterfill(cs[c], m, fw, fc, fo);
            for (t = 0; t < m; t++)
                out_e[idx[head[b] + t]] = fo[t];
            hpft_waterfill_ceilings(ck[c], m, fw, fc, NULL, fo);
            for (t = 0; t < m; t++)
                out_ceil[idx[head[b] + t]] = fo[t];
        }
    }

    free(cls_dem); free(vm_dem); free(vm_cap); free(vm_share);
    free(vm_ceil); free(cw); free(cc); free(cs); free(ck);
    free(cnt); free(head); free(idx); free(fill);
    free(fw); free(fc); free(fo);
}
