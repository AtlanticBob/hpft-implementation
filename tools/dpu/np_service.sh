#!/usr/bin/env bash
# NP service controller on the DPU: start | query | stop | status
LOG=/tmp/pcc_np.log
FIFO=/tmp/np_fifo
BIN=/home/ubuntu/bzx/doca34-apps/build/pcc/doca_pcc
case "$1" in
start)
    sudo pkill -9 -x doca_pcc 2>/dev/null
    pkill -9 -f "sleep 1200" 2>/dev/null
    sleep 1
    rm -f $FIFO; mkfifo $FIFO
    setsid bash -c "sleep 1200 > $FIFO" </dev/null >/dev/null 2>&1 &
    setsid sudo env HPFT_RATE_STDIN=1 $BIN -d mlx5_0 -np-nt -l 40 -w 1100 \
        < $FIFO > $LOG 2>&1 &
    sleep 6
    ps -C doca_pcc -o pid,etime,cmd | tail -1
    ;;
query)
    timeout 3 bash -c "echo '0x1 1' > $FIFO" || { echo QUERY_WRITE_BLOCKED; exit 1; }
    sleep 1
    grep -a HPFT_RSP $LOG | tail -1
    ;;
status)
    ps -C doca_pcc -o pid,etime,cmd | tail -1
    ;;
stop)
    sudo pkill -INT -x doca_pcc 2>/dev/null
    ;;
esac
