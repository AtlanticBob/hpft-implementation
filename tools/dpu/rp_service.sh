#!/usr/bin/env bash
# RDMA executor (DOCA PCC RP) controller on the DPU: start | query | setcap | stop | status
LOG=/tmp/pcc_rp.log
FIFO=/tmp/rp_fifo
BIN=${RP_BIN:-/home/ubuntu/bzx/doca34-apps/build/pcc/doca_pcc}   # RP_BIN overrides (e.g. the stock Swift build)
case "$1" in
start)
    # graceful first: SIGINT lets the host app destroy the PCC context so
    # fw deregisters cleanly (a -9 mid probe-stream can leave the port's
    # event path wedged and kill the VF datapath); -9 only as fallback
    # No DEVX query may be in flight on this device while the PCC context is
    # torn down and rebuilt. The vport meter issues one every millisecond
    # (QUERY_VPORT_COUNTER through DEVX), and twice on 2026-09-10 the restart
    # left that query hung in the firmware command interface for good
    # (kernel stack: mlx5_core cmd_exec <- MLX5_IB_METHOD_DEVX_OTHER), the
    # meter's shared memory froze, the sender measured itself at zero and
    # the ledger handed its share away. Only an Arm reboot cleared it. So the
    # meter is stopped for the restart and started again afterwards; the gap
    # is about a second and lies before any flow starts.
    METER_WAS=$(systemctl is-active hpft-vport-meter 2>/dev/null)
    [ "$METER_WAS" = active ] && sudo systemctl stop hpft-vport-meter
    # Wait for the INT to finish before falling back to -9. The shutdown
    # destroys the PCC context on the device and takes a variable time on a
    # loaded Arm; a -9 that lands mid-destroy wedges the device's PCC path -
    # the next doca_pcc sits in D state, the vport meter on the same device
    # stops updating its shared memory, the sender measures itself at zero
    # and the ledger hands its share away (2026-09-10, hpft-dpu, twice in one
    # afternoon; only an Arm reboot clears it). Two seconds was not enough.
    if sudo pkill -INT -x doca_pcc 2>/dev/null; then
        for i in $(seq 1 30); do pgrep -x doca_pcc >/dev/null || break; sleep 0.5; done
    fi
    if pgrep -x doca_pcc >/dev/null; then
        echo "rp_service: doca_pcc did not exit on INT within 15 s, killing -9 (device may wedge)" >&2
        sudo pkill -9 -x doca_pcc 2>/dev/null
    fi
    pkill -9 -f "sleep infinity" 2>/dev/null
    sleep 1
    # Both files live in the sticky /tmp and are recreated here. If either was
    # last created by root (a start run under sudo), this user can neither
    # remove nor open it - Ubuntu's protected_regular refuses root the reverse
    # case too - and the RP silently never launches (2026-09-10, dpu2: three
    # 'starts' in a row, no process, no error anywhere but a redirect line).
    sudo rm -f $FIFO $LOG
    mkfifo $FIFO
    setsid bash -c "sleep infinity > $FIFO" </dev/null >/dev/null 2>&1 &
    # no --remote-sw-handler: CCMAD RTT probes are answered by the REMOTE
    # NIC's HW handler (no NP process runs on the far DPU; with the flag
    # set, probes wait for a nonexistent NP process and vanish)
    setsid sudo env HPFT_RATE_STDIN=1 $BIN -d mlx5_0 -l 40 -w 1100 \
        < $FIFO > $LOG 2>&1 &
    sleep 6
    ps -C doca_pcc -o pid,etime,cmd | tail -1
    [ "$METER_WAS" = active ] && sudo systemctl start hpft-vport-meter
    ;;
query)
    timeout 3 bash -c "echo '0xdef 0' > $FIFO" || { echo QUERY_WRITE_BLOCKED; exit 1; }
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
