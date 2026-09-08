#!/usr/bin/env bash
# NP service controller on the DPU: start | query | stop | status
LOG=/tmp/pcc_rp.log
FIFO=/tmp/rp_fifo
BIN=${RP_BIN:-/home/ubuntu/bzx/doca34-apps/build/pcc/doca_pcc}   # RP_BIN overrides (e.g. the stock Swift build)
case "$1" in
start)
    # graceful first: SIGINT lets the host app destroy the PCC context so
    # fw deregisters cleanly (a -9 mid probe-stream can leave the port's
    # event path wedged and kill the VF datapath); -9 only as fallback
    sudo pkill -INT -x doca_pcc 2>/dev/null && sleep 2
    sudo pkill -9 -x doca_pcc 2>/dev/null
    pkill -9 -f "sleep infinity" 2>/dev/null
    sleep 1
    rm -f $FIFO; mkfifo $FIFO
    setsid bash -c "sleep infinity > $FIFO" </dev/null >/dev/null 2>&1 &
    # no --remote-sw-handler: CCMAD RTT probes are answered by the REMOTE
    # NIC's HW handler (no NP process runs on the far DPU; with the flag
    # set, probes wait for a nonexistent NP process and vanish)
    setsid sudo env HPFT_RATE_STDIN=1 $BIN -d mlx5_0 -l 40 -w 1100 \
        < $FIFO > $LOG 2>&1 &
    sleep 6
    ps -C doca_pcc -o pid,etime,cmd | tail -1
    ;;
query)
    timeout 3 bash -c "echo '0xdeb 0' > $FIFO" || { echo QUERY_WRITE_BLOCKED; exit 1; }
    sleep 1
    grep -a HPFT_RSP $LOG | tail -1
    ;;
setcap)
    timeout 3 bash -c "echo '$2 $3' > $FIFO" || { echo SET_WRITE_BLOCKED; exit 1; }
    sleep 0.3
    grep -ac HPFT_SET $LOG
    ;;
status)
    ps -C doca_pcc -o pid,etime,cmd | tail -1
    ;;
stop)
    sudo pkill -INT -x doca_pcc 2>/dev/null
    ;;
esac
