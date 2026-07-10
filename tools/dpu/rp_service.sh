#!/usr/bin/env bash
# NP service controller on the DPU: start | query | stop | status
LOG=/tmp/pcc_rp.log
FIFO=/tmp/rp_fifo
BIN=/home/ubuntu/bzx/doca34-apps/build/pcc/doca_pcc
case "$1" in
start)
    sudo pkill -9 -x doca_pcc 2>/dev/null
    pkill -9 -f "sleep infinity" 2>/dev/null
    sleep 1
    rm -f $FIFO; mkfifo $FIFO
    setsid bash -c "sleep infinity > $FIFO" </dev/null >/dev/null 2>&1 &
    setsid sudo env HPFT_RATE_STDIN=1 $BIN -d mlx5_0 --remote-sw-handler -l 40 -w 1100 \
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
