#!/usr/bin/env bash
# One nginx instance serving one large object over one VF, for the http rows of
# validation/run/run.sh (evaluation E4.1: a tenant whose traffic is an HTTP
# download instead of an iperf3 stream).
#
#   http_server.sh start <ip> <port> <object MB> [workers]
#   http_server.sh stop  <port>
#
# Each row gets its own prefix directory, its own config and its own port, so
# several rows can serve from the same host without sharing anything but the
# object files. The instance runs as the calling user (no root): the ports are
# above 1024 and everything it writes lives under /tmp.
#
# The object sits in /dev/shm (tmpfs) and is served with sendfile, so the
# server is a memory-to-network copy and the disk is never in the path - a
# tenant that is supposed to be network-bound must not be limited by storage.
# The file is written once with real pages (not a sparse hole), because a
# sparse file would be served out of the shared zero page and would flatter
# the server's memory bandwidth.
set -eu
CMD=${1:?start|stop}

case "$CMD" in
start)
  IP=${2:?ip}; PORT=${3:?port}; MB=${4:?object MB}; W=${5:-4}
  ROOT=/dev/shm/hpft_http; PREFIX=/tmp/hpft_ngx$PORT
  mkdir -p "$ROOT" "$PREFIX/logs" "$PREFIX/tmp"
  OBJ=$ROOT/obj${MB}m.bin
  if [ ! -s "$OBJ" ] || [ "$(stat -c %s "$OBJ")" -ne $((MB * 1024 * 1024)) ]; then
    dd if=/dev/zero of="$OBJ.part" bs=1M count="$MB" status=none
    mv "$OBJ.part" "$OBJ"
  fi
  cat > "$PREFIX/nginx.conf" <<CONF
worker_processes $W;
worker_rlimit_nofile 65536;
daemon on;
pid logs/nginx.pid;
error_log logs/error.log warn;
events { worker_connections 8192; multi_accept on; }
http {
    access_log off;
    default_type application/octet-stream;
    sendfile on;
    tcp_nopush on;
    output_buffers 4 512k;
    keepalive_requests 10000000;
    keepalive_timeout 600s;
    client_body_temp_path tmp;
    proxy_temp_path tmp;
    fastcgi_temp_path tmp;
    uwsgi_temp_path tmp;
    scgi_temp_path tmp;
    server {
        listen $IP:$PORT backlog=8192 reuseport;
        root $ROOT;
    }
}
CONF
  # nginx opens its compiled-in default error log before it reads the config,
  # which this user cannot write, so starting it prints one "could not open
  # error log file" alert. It is only an alert - the config's own error_log
  # takes over immediately - and nginx 1.18 has no -e to move that first open.
  nginx -p "$PREFIX" -c nginx.conf
  for _ in $(seq 20); do
    ss -ltn "sport = :$PORT" | grep -q ":$PORT" && { echo "http_server: listening on $IP:$PORT, $(stat -c %s "$OBJ") B object, $W workers"; exit 0; }
    sleep 0.2
  done
  echo "http_server: nginx did not listen on $IP:$PORT"; tail -5 "$PREFIX/logs/error.log" 2>/dev/null; exit 1 ;;
stop)
  PORT=${2:?port}; PREFIX=/tmp/hpft_ngx$PORT
  [ -f "$PREFIX/logs/nginx.pid" ] && nginx -p "$PREFIX" -c nginx.conf -s quit 2>/dev/null || true
  sleep 0.3
  [ -f "$PREFIX/logs/nginx.pid" ] && kill -9 "$(cat "$PREFIX/logs/nginx.pid")" 2>/dev/null || true
  rm -f "$PREFIX/logs/nginx.pid"; exit 0 ;;
*) echo "usage: http_server.sh start <ip> <port> <object MB> [workers] | stop <port>"; exit 2 ;;
esac
