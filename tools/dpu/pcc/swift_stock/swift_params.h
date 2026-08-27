#ifndef SWIFT_PARAMS_H_
#define SWIFT_PARAMS_H_
/* Swift parameters, device-clock ns / bytes / fxp16. Calibrated on the
 * HyperFront lab fabric (BF3 <-> SN5600 <-> BF3, VxLAN overlay, 200G):
 * probe base RTT 2744 ns, single-flow full-load band 3.7-3.9 us. */
#define SW_PKT          (1024u)               /* RoCE path MTU, bytes */
#define SW_LINE_BPNS    (25u)                 /* 200 Gb/s in bytes per ns */
#define SW_BASE_TARGET  (12000u)              /* ns */
#define SW_FS_RANGE     (20000u)              /* ns, flow-scaling span */
#define SW_ALPHA        (50000u)              /* ns: fs_range/(1/sqrt(4)-1/sqrt(100)) */
#define SW_BETA         (5000u)               /* ns: alpha/sqrt(100) */
#define SW_AI           (SW_PKT)              /* bytes per RTT */
#define SW_B            (26214u)              /* 0.4 fxp16 */
#define SW_MAX_MDF      (16384u)              /* 0.25 fxp16 */
#define SW_INIT_CWND    (64u << 10)
#define SW_MIN_CWND     (SW_PKT)
#define SW_MAX_CWND     ((1u << 20) - SW_PKT) /* line x 40 us */
#define SW_MIN_RATE     (1u << (20 - 14))
#endif
