#!/bin/bash
# Network emulator calibration helper (CCA-Sky validation stage 1).
# Creates client_ns / router_ns / server_ns, starts the replay script, and
# exposes probes that do not depend on the CC implementation.
set -u
CLIENT_IP=10.0.1.2
SERVER_IP=10.0.2.2
ROUTER_CLIENT_IP=10.0.1.1
ROUTER_SERVER_IP=10.0.2.1
STATE=/tmp/netcalib.state
UDP_PORT=9999
IPERF_PORT=5201

setup() {
    local network=$1 profile=${2:-trans_oceanic} trace=${3:-} offset=${4:-0}
    teardown >/dev/null 2>&1
    for ns in client_ns router_ns server_ns; do
        ip netns add $ns || exit 1
        ip -n $ns link set lo up
    done
    ip link add cli0 netns client_ns type veth peer name r_cli netns router_ns
    ip link add srv0 netns server_ns type veth peer name r_srv netns router_ns
    ip -n client_ns addr add $CLIENT_IP/24 dev cli0; ip -n client_ns link set cli0 up
    ip -n client_ns route add default via $ROUTER_CLIENT_IP
    ip -n router_ns addr add $ROUTER_CLIENT_IP/24 dev r_cli
    ip -n router_ns addr add $ROUTER_SERVER_IP/24 dev r_srv
    ip -n router_ns link set r_cli up; ip -n router_ns link set r_srv up
    ip -n server_ns addr add $SERVER_IP/24 dev srv0; ip -n server_ns link set srv0 up
    ip -n server_ns route add default via $ROUTER_SERVER_IP
    ip netns exec router_ns sysctl -w net.ipv4.ip_forward=1 >/dev/null
    for ns in client_ns router_ns server_ns; do
        ip netns exec $ns sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null 2>&1
        for dev in lo cli0 srv0 r_cli r_srv; do
            ip netns exec $ns sysctl -w net.ipv4.conf.$dev.rp_filter=0 >/dev/null 2>&1
        done
    done

    # UDP echo server (RTT probe) and iperf3 server.
    ip netns exec server_ns python3 -u -c "
import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('$SERVER_IP',$UDP_PORT))
while True:
    d,a=s.recvfrom(2048); s.sendto(d,a)
" >/tmp/netcalib.udpserver.log 2>&1 &
    echo $! > /tmp/netcalib.udppid
    ip netns exec server_ns iperf3 -s -p $IPERF_PORT >/tmp/netcalib.iperfserver.log 2>&1 &
    echo $! > /tmp/netcalib.iperfpid

    cd /workspace/CCA5 || exit 1
    if [ "$network" = "wan" ]; then
        RESULTS_DIR=/tmp/netcalib DURATION=3600 \
        WAN_PROFILE=$profile python3 tests/../replay_wan.py -p "$profile" >/tmp/netcalib.replay.log 2>&1 &
    fi
    if [ "$network" = "satellite" ]; then
        local tpath="$trace"
        case "$tpath" in
            */*) ;;
            *) tpath=/workspace/CCA5/bin/traces/satellite/$tpath ;;
        esac
        SATELLITE_TRACE="$tpath" TRACE_START_S=$offset RESULTS_DIR=/tmp/netcalib \
        python3 tests/../replay_lens.py -t "$tpath" \
          --start-s $offset >/tmp/netcalib.replay.log 2>&1 &
    fi
    echo $! > /tmp/netcalib.replaypid

    for _ in $(seq 1 60); do
        ip netns exec router_ns tc qdisc show dev r_srv 2>/dev/null | grep -q htb && break
        sleep 0.1
    done
    echo "network=$network profile=$profile trace=$trace offset=$offset" > $STATE
    echo "[+] topology ready ($network/$profile)"
}

teardown() {
    for f in /tmp/netcalib.replaypid /tmp/netcalib.udppid /tmp/netcalib.iperfpid; do
        [ -f $f ] && kill $(cat $f) 2>/dev/null
    done
    pkill -f replay_wan.py 2>/dev/null; pkill -f replay_lens.py 2>/dev/null
    pkill -f "iperf3 -s -p $IPERF_PORT" 2>/dev/null
    for ns in client_ns router_ns server_ns; do ip netns delete $ns 2>/dev/null; done
    rm -f /tmp/netcalib.replaypid /tmp/netcalib.udppid /tmp/netcalib.iperfpid
    echo "[+] teardown done"
}

rtt() {
    local count=${1:-20}
    ip netns exec client_ns python3 -c "
import socket,time,statistics
n=$count; port=$UDP_PORT
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2.0)
vals=[]
for i in range(n):
    t=time.perf_counter()
    s.sendto(b'x'*64,('$SERVER_IP',port))
    try:
        s.recvfrom(2048)
        vals.append((time.perf_counter()-t)*1000)
    except socket.timeout:
        pass
    time.sleep(0.05)
if vals:
    v=sorted(vals)
    print('rtt n=%d min=%.2f p50=%.2f mean=%.2f max=%.2f mdev=%.2f ms' %
          (len(v), v[0], statistics.median(v), statistics.mean(v), v[-1],
           statistics.pstdev(v)))
else:
    print('rtt n=0 (all timeouts)')
"
}

bw() {
    local dir=${1:-up} rate=${2:-600M} secs=${3:-10}
    local rev=""
    [ "$dir" = "down" ] && rev="-R"
    ip netns exec client_ns iperf3 -c $SERVER_IP -p $IPERF_PORT -u -b $rate -t $secs $rev \
        | awk '/receiver/{print "  receiver:",$7,$8} /sender/{print "  sender:  ",$7,$8} /lost/{print "  loss:    ",$0}'
}

loss() {
    local rate=${1:-200M} secs=${2:-10}
    ip netns exec client_ns iperf3 -c $SERVER_IP -p $IPERF_PORT -u -b $rate -t $secs \
        | awk '/sender|lost|receiver/{print "  "$0}'
}

qdisc() {
    for iface in r_srv r_cli; do
        echo "--- router $iface"
        ip netns exec router_ns tc -s -d qdisc show dev $iface | head -12
    done
}

"$@"
