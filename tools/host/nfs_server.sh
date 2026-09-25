#!/usr/bin/env bash
# One NFS export of large files out of tmpfs, for the nfs rows of evaluation
# E4.2: the storage node a VM reads model weights and datasets from. The
# kernel server (nfsd) does the serving; this script only makes sure it is up
# with enough threads and that the export exists with the files a row asks for.
#
#   nfs_server.sh start <files> <MB each> [threads]   -> exports /dev/shm/nfsexport
#   nfs_server.sh stop                                -> unexports it (files stay)
#
# The export is read-only to 10.1.0.0/16 (every VF subnet). Files are zeros:
# the reader opens them O_DIRECT so nothing is cached on the client side and
# every byte crosses the wire; content is irrelevant to the transfer.
set -eu
DIR=/dev/shm/nfsexport
case "${1:?start|stop}" in
  start)
    N=${2:?files}; MB=${3:?MB each}; TH=${4:-64}
    sudo mkdir -p "$DIR"
    for i in $(seq 0 $((N-1))); do
      f="$DIR/f$i"
      [ -f "$f" ] && [ "$(stat -c %s "$f")" -eq $((MB*1024*1024)) ] || sudo dd if=/dev/zero of="$f" bs=1M count="$MB" status=none
    done
    grep -q "^$DIR " /etc/exports 2>/dev/null || echo "$DIR 10.1.0.0/16(ro,sync,no_subtree_check,fsid=100)" | sudo tee -a /etc/exports >/dev/null
    sudo systemctl start nfs-server
    sudo rpc.nfsd "$TH"
    sudo exportfs -ra
    sudo exportfs -v | grep -q "^$DIR" && echo "nfs_server: exporting $DIR ($N x $MB MB, $TH nfsd threads)" || { echo "nfs_server: export failed"; exit 1; } ;;
  stop)
    sudo exportfs -u "10.1.0.0/16:$DIR" 2>/dev/null || true; echo "nfs_server: unexported $DIR" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
