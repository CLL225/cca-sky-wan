#!/usr/bin/env python3
"""WAN link emulation on a dedicated router namespace.

Topology (created by run_tc.sh):

    client_ns(10.0.1.2) --r_cli--[ router_ns ]--r_srv-- server_ns(10.0.2.2)

Shaping runs on the router egress interfaces so end-host send/receive is
decoupled from the emulated network:

    r_srv egress : client -> server (uplink, carries application data)
    r_cli egress : server -> client (downlink, carries ACKs)

Each direction gets HTB (bottleneck rate + queue) with a netem child
(propagation delay and random loss).  The one-way delay is RTT/2 per
direction, so a configured RTT is reproduced end to end.

Profiles follow the CCA-Sky plan (inter_city .. dynamic).  Override any
parameter with environment variables:

    WAN_PROFILE        profile name (default: trans_oceanic)
    WAN_RATE_MBIT      bottleneck rate (Mbit/s)
    WAN_RTT_MS         end-to-end RTT (split evenly across both directions)
    WAN_LOSS_PCT       random loss (%)
    WAN_JITTER_MS      delay jitter (default 0, per plan)
    WAN_QUEUE_BDP      bottleneck queue in BDP units (default 1.0)
    WAN_QUEUE_PKTS     explicit queue length in packets (overrides BDP)
    WAN_DYNAMIC        "1" to sweep rate between WAN_DYNAMIC_MIN/MAX
    RESULTS_DIR        where the calibration JSON is written
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

ROUTER_NS = "router_ns"
UPLINK_IF = "r_srv"     # router -> server
DOWNLINK_IF = "r_cli"   # router -> client
MTU = 1500

PROFILES = {
    # name:              rate(Mbit) rtt(ms) loss(%) queue_bdp
    "inter_city":        (100,       40,     0.0,    1.0),
    "high_speed_wan":    (500,       75,     0.0,    1.0),
    "trans_oceanic":     (500,      150,     0.0,    1.0),
    "lossy_trans":       (500,      150,     0.1,    1.0),
    "high_loss":         (50,       100,     2.0,    1.0),
    "shallow_buffer":    (500,      150,     0.0,    0.5),
    "deep_buffer":       (500,      150,     0.0,    4.0),
    "dynamic":           (500,      150,     0.0,    2.0),
}


def env(name, default):
    return os.environ.get(name, default)


def run(ns, cmd, check=False):
    full = ["ip", "netns", "exec", ns] + cmd.split()
    proc = subprocess.run(full, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{cmd}: {proc.stderr.strip()}")
    return proc


def bdp_packets(rate_mbit, rtt_ms, bdp, mtu=MTU):
    bdp_bytes = float(rate_mbit) * 1e6 / 8.0 * (float(rtt_ms) / 1000.0)
    return max(10, int(round(bdp_bytes * float(bdp) / mtu)))


def configure_direction(iface, rate_mbit, rtt_ms, loss_pct, jitter_ms, queue_pkts, label, log):
    """HTB bottleneck + netem (delay/loss) on one router egress interface."""
    one_way = float(rtt_ms) / 2.0
    run(ROUTER_NS, f"tc qdisc del dev {iface} root")

    rate = f"{rate_mbit}mbit"
    run(ROUTER_NS, f"tc qdisc add dev {iface} root handle 1: htb default 10", check=True)
    run(ROUTER_NS,
        f"tc class add dev {iface} parent 1: classid 1:10 htb rate {rate} ceil {rate} "
        f"burst 64k cburst 64k", check=True)

    delay = f"delay {one_way:.3f}ms"
    if float(jitter_ms) > 0:
        delay += f" {jitter_ms}ms"
    run(ROUTER_NS,
        f"tc qdisc add dev {iface} parent 1:10 handle 10: netem {delay} "
        f"loss {loss_pct}% limit {queue_pkts}")

    entry = {
        "direction": label,
        "interface": iface,
        "rate_mbit": float(rate_mbit),
        "one_way_delay_ms": one_way,
        "loss_pct": float(loss_pct),
        "jitter_ms": float(jitter_ms),
        "queue_pkts": int(queue_pkts),
        "queue_bytes": int(queue_pkts) * MTU,
    }
    log.append(entry)
    print(f"[+] {label} {iface}: rate={rate_mbit}Mbit one-way={one_way:.1f}ms "
          f"RTT={rtt_ms}ms loss={loss_pct}% jitter={jitter_ms}ms queue={queue_pkts}pkts")


def apply_profile(profile, log, rate_override=None):
    rate, rtt, loss, qbdp = PROFILES.get(profile, PROFILES["trans_oceanic"])

    rate = rate_override if rate_override is not None else float(env("WAN_RATE_MBIT", rate))
    rtt = float(env("WAN_RTT_MS", rtt))
    loss = float(env("WAN_LOSS_PCT", loss))
    jitter = float(env("WAN_JITTER_MS", 0))
    qbdp = float(env("WAN_QUEUE_BDP", qbdp))
    qpkts_env = env("WAN_QUEUE_PKTS", "")
    qpkts = int(qpkts_env) if qpkts_env else bdp_packets(rate, rtt, qbdp)

    configure_direction(UPLINK_IF, rate, rtt, loss, jitter, qpkts, "uplink", log)
    configure_direction(DOWNLINK_IF, rate, rtt, loss, jitter, qpkts, "downlink", log)
    return dict(rate_mbit=float(rate), rtt_ms=rtt, loss_pct=loss,
                jitter_ms=jitter, queue_bdp=qbdp, queue_pkts=qpkts)


def dynamic_loop(params, log, stop_s):
    """Sweep the bottleneck rate between MIN and MAX every step seconds."""
    lo = float(env("WAN_DYNAMIC_MIN", 100))
    hi = float(env("WAN_DYNAMIC_MAX", 500))
    step = float(env("WAN_DYNAMIC_STEP_S", 5))
    qpkts = bdp_packets(hi, params["rtt_ms"], params["queue_bdp"])
    start = time.perf_counter()
    rate = lo
    direction = 1
    while time.perf_counter() - start < stop_s:
        for iface in (UPLINK_IF, DOWNLINK_IF):
            run(ROUTER_NS, f"tc class change dev {iface} parent 1: classid 1:10 "
                           f"htb rate {rate}mbit ceil {rate}mbit burst 64k cburst 64k")
            run(ROUTER_NS, f"tc qdisc change dev {iface} parent 1:10 handle 10: "
                           f"netem delay {params['rtt_ms']/2.0:.3f}ms "
                           f"loss {params['loss_pct']}% limit {qpkts}")
        print(f"[~] dynamic rate -> {rate:.0f} Mbit")
        if rate >= hi:
            direction = -1
        elif rate <= lo:
            direction = 1
        rate = max(lo, min(hi, rate + direction * (hi - lo) / 4.0))
        time.sleep(step)


def write_calibration(profile, params, log):
    out_dir = env("RESULTS_DIR", ".")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"netcalib_{profile}_{os.getpid()}.json")
        payload = {
            "profile": profile,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "requested": params,
            "directions": log,
            "tc_dump": {
                iface: run(ROUTER_NS, f"tc -s qdisc show dev {iface}").stdout
                for iface in (UPLINK_IF, DOWNLINK_IF)
            },
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"[+] calibration written: {path}")
    except OSError as exc:
        print(f"[!] calibration write failed: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="WAN emulation on router_ns")
    parser.add_argument("-p", "--profile", default=enabled_profile())
    parser.add_argument("-t", "--trace", default=None, help="unused, kept for CLI compat")
    parser.add_argument("--duration", type=float, default=float(env("DURATION", 20)))
    args = parser.parse_args()

    profile = args.profile
    print(f"[*] WAN profile={profile} router_ns={ROUTER_NS}")
    log = []
    params = apply_profile(profile, log)
    write_calibration(profile, params, log)

    if profile == "dynamic" or env("WAN_DYNAMIC", "0") == "1":
        dynamic_loop(params, log, args.duration + 2)

    # Stay alive while the client runs; run_tc.sh kills us when it finishes.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for iface in (UPLINK_IF, DOWNLINK_IF):
            run(ROUTER_NS, f"tc qdisc del dev {iface} root")


def enabled_profile():
    return env("WAN_PROFILE", "trans_oceanic")


if __name__ == "__main__":
    main()
