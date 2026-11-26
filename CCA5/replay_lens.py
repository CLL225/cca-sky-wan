#!/usr/bin/env python3
"""Satellite (LENS-style) trace replay on a dedicated router namespace.

Topology (created by run_tc.sh):

    client_ns --r_cli--[ router_ns ]--r_srv-- server_ns

Each direction gets HTB (bottleneck rate/queue) with a netem child
(propagation delay + loss).  The trace RTT is split evenly across both
directions, so the end-to-end RTT matches the trace instead of half of it.

Trace columns: timestamp,rtt_ms,loss_rate,bandwidth_mbps

Disconnects are distinguished from low bandwidth: rows with
bandwidth <= DISCONNECT_BW_MBIT are replayed as a hard outage
(1 kbit/s, 100% loss) and counted separately in the calibration log.

Environment:
    SATELLITE_TRACE        trace path
    TRACE_START_S          replay start offset in seconds (default 0)
    TRACE_UPDATE_MS        tc refresh interval (default 100)
    SATELLITE_LOSS_PCT     fixed loss override (%) ; default: use trace loss
    SATELLITE_QUEUE_BDP    queue in BDP units (default 1.0)
    RESULTS_DIR            calibration JSON output directory
"""
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

ROUTER_NS = "router_ns"
UPLINK_IF = "r_srv"
DOWNLINK_IF = "r_cli"
MTU = 1500


def env(name, default):
    return os.environ.get(name, default)


def run(ns, cmd):
    return subprocess.run(["ip", "netns", "exec", ns] + cmd.split(),
                          capture_output=True, text=True)


def bdp_packets(rate_mbit, rtt_ms, bdp, mtu=MTU):
    bdp_bytes = float(rate_mbit) * 1e6 / 8.0 * (float(rtt_ms) / 1000.0)
    return max(10, int(round(bdp_bytes * float(bdp) / mtu)))


def setup(initial_rate, initial_rtt, queue_bdp):
    qpkts = bdp_packets(initial_rate, initial_rtt, queue_bdp)
    for iface in (UPLINK_IF, DOWNLINK_IF):
        run(ROUTER_NS, f"tc qdisc del dev {iface} root")
        run(ROUTER_NS, f"tc qdisc add dev {iface} root handle 1: htb default 10")
        run(ROUTER_NS, f"tc class add dev {iface} parent 1: classid 1:10 "
                       f"htb rate {initial_rate}mbit ceil {initial_rate}mbit "
                       f"burst 64k cburst 64k")
        run(ROUTER_NS, f"tc qdisc add dev {iface} parent 1:10 handle 10: "
                       f"netem delay {initial_rtt/2.0:.3f}ms loss 0% limit {qpkts}")
    return qpkts


def update(rate_mbit, rtt_ms, loss_pct, qpkts):
    one_way = max(0.0, float(rtt_ms) / 2.0)
    for iface in (UPLINK_IF, DOWNLINK_IF):
        run(ROUTER_NS, f"tc class change dev {iface} parent 1: classid 1:10 "
                       f"htb rate {rate_mbit}mbit ceil {rate_mbit}mbit "
                       f"burst 64k cburst 64k")
        run(ROUTER_NS, f"tc qdisc change dev {iface} parent 1:10 handle 10: "
                       f"netem delay {one_way:.3f}ms loss {loss_pct}% limit {qpkts}")


def replay(trace_file, start_s, update_ms, loss_override, queue_bdp, log):
    rows = list(csv.DictReader(open(trace_file, encoding="utf-8")))
    if not rows:
        print(f"[!] empty trace: {trace_file}", file=sys.stderr)
        return

    first_rate = float(rows[0]["bandwidth_mbps"]) or 1.0
    first_rtt = float(rows[0]["rtt_ms"])
    qpkts = setup(max(first_rate, 1.0), first_rtt, queue_bdp)

    log.update({"trace": trace_file, "rows": len(rows), "start_offset_s": start_s,
                "queue_bdp": queue_bdp, "loss_override_pct": loss_override})
    write_calibration(log)   # snapshot right after tc setup

    base_ts = float(rows[0]["timestamp"])
    start_time = time.perf_counter()
    interval = max(1, int(update_ms)) / 1000.0
    idx = 0
    disconnects = 0
    low_bw = 0
    samples = []

    while idx < len(rows):
        row = rows[idx]
        elapsed = idx * interval
        target = start_time + elapsed
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)

        bw = float(row["bandwidth_mbps"])
        rtt = float(row["rtt_ms"])
        trace_loss = float(row["loss_rate"])
        loss = loss_override if loss_override is not None else trace_loss

        if bw <= 0.01:
            update(0.001, rtt, 100.0, qpkts)
            disconnects += 1
            state = "disconnect"
        else:
            if bw < 1.0:
                low_bw += 1
            qpkts = bdp_packets(bw, rtt, queue_bdp)
            update(bw, rtt, loss, qpkts)
            state = "low_bw" if bw < 1.0 else "ok"

        if idx % max(1, int(20 * 0.1 / interval)) == 0:
            samples.append({"elapsed_s": round(elapsed, 2), "bw_mbps": bw,
                            "rtt_ms": rtt, "loss_pct": loss, "state": state})

        if idx % 200 == 0 and idx:
            print(f"  -> {elapsed:.1f}s row={idx}/{len(rows)} bw={bw:.2f}Mbit "
                  f"rtt={rtt:.1f}ms state={state}")
        idx += 1

    log.update({
        "trace": trace_file,
        "rows": len(rows),
        "start_offset_s": start_s,
        "replayed_s": round(idx * interval, 1),
        "disconnect_rows": disconnects,
        "low_bandwidth_rows": low_bw,
        "queue_bdp": queue_bdp,
        "loss_override_pct": loss_override,
        "samples": samples[:200],
    })


def write_calibration(log):
    out_dir = env("RESULTS_DIR", ".")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"netcalib_satellite_{os.getpid()}.json")
        log["timestamp"] = datetime.now().isoformat(timespec="seconds")
        log["tc_dump"] = {iface: run(ROUTER_NS, f"tc -s qdisc show dev {iface}").stdout
                          for iface in (UPLINK_IF, DOWNLINK_IF)}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(log, handle, indent=2)
        print(f"[+] calibration written: {path}")
    except OSError as exc:
        print(f"[!] calibration write failed: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="LENS satellite trace replay")
    parser.add_argument("-t", "--trace", default=env("SATELLITE_TRACE", ""))
    parser.add_argument("--start-s", type=float, default=float(env("TRACE_START_S", 0)))
    parser.add_argument("--update-ms", type=int, default=int(env("TRACE_UPDATE_MS", 100)))
    parser.add_argument("--duration", type=float, default=float(env("DURATION", 20)))
    args = parser.parse_args()

    trace = args.trace
    if not trace or not os.path.exists(trace):
        print(f"[!] trace not found: {trace}", file=sys.stderr)
        sys.exit(2)

    loss_env = env("SATELLITE_LOSS_PCT", "")
    loss_override = float(loss_env) if loss_env else None
    queue_bdp = float(env("SATELLITE_QUEUE_BDP", 1.0))

    rows = list(csv.DictReader(open(trace, encoding="utf-8")))
    skip = int(args.start_s / (args.update_ms / 1000.0)) if args.start_s > 0 else 0
    if skip:
        rows = rows[skip:]
        if not rows:
            print(f"[!] start offset {args.start_s}s past end of trace", file=sys.stderr)
            sys.exit(2)
        tmp = trace + f".offset{int(args.start_s)}.csv"
        with open(tmp, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        trace = tmp

    log = {"loss_policy": "trace" if loss_override is None else "fixed",
           "disconnect_rows": 0, "low_bandwidth_rows": 0, "samples": []}
    print(f"[*] satellite replay trace={trace} start={args.start_s}s "
          f"update={args.update_ms}ms queue_bdp={queue_bdp} "
          f"loss={log['loss_policy']}")
    def _terminate(_sig, _frm):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        replay(trace, args.start_s, args.update_ms, loss_override, queue_bdp, log)
    finally:
        # Write calibration both on normal completion and on SIGTERM from
        # run_tc.sh, so every run leaves a verifiable network record.
        write_calibration(log)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for iface in (UPLINK_IF, DOWNLINK_IF):
            run(ROUTER_NS, f"tc qdisc del dev {iface} root")


if __name__ == "__main__":
    main()
