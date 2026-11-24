#!/usr/bin/env python3

import argparse
import csv
import os
import sys


SUMMARY_FIELDS = [
    "algorithm",
    "trace",
    "duration_s",
    "steady_throughput_kbps",
    "threshold_kbps",
    "convergence_time_s",
    "converged",
]


def parse_float(row, field):
    try:
        return float(row[field])
    except (KeyError, TypeError, ValueError):
        return None


def load_samples(path):
    samples = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            elapsed = parse_float(row, "elapsed_s")
            throughput = parse_float(row, "throughput_kbps")
            if elapsed is None or throughput is None:
                continue

            samples.append(
                {
                    "algorithm": row.get("algorithm", "unknown"),
                    "trace": row.get("trace", "unknown"),
                    "elapsed_s": elapsed,
                    "throughput_kbps": throughput,
                }
            )

    samples.sort(key=lambda sample: sample["elapsed_s"])
    return samples


def analyze(samples, steady_tail_ratio, threshold_ratio, hold_samples):
    if not samples:
        return {
            "algorithm": "unknown",
            "trace": "unknown",
            "duration_s": 0.0,
            "steady_throughput_kbps": 0.0,
            "threshold_kbps": 0.0,
            "convergence_time_s": "",
            "converged": "false",
        }

    duration = samples[-1]["elapsed_s"]
    steady_start = duration * (1.0 - steady_tail_ratio)
    steady_samples = [s for s in samples if s["elapsed_s"] >= steady_start]
    if not steady_samples:
        steady_samples = [samples[-1]]

    steady_throughput = sum(s["throughput_kbps"] for s in steady_samples) / len(
        steady_samples
    )
    threshold = steady_throughput * threshold_ratio
    convergence_time = ""
    converged = False

    if steady_throughput > 1e-9 and hold_samples > 0:
        for idx in range(0, len(samples) - hold_samples + 1):
            window = samples[idx : idx + hold_samples]
            if all(s["throughput_kbps"] >= threshold for s in window):
                convergence_time = samples[idx]["elapsed_s"]
                converged = True
                break

    return {
        "algorithm": samples[0]["algorithm"],
        "trace": samples[0]["trace"],
        "duration_s": duration,
        "steady_throughput_kbps": steady_throughput,
        "threshold_kbps": threshold,
        "convergence_time_s": convergence_time,
        "converged": "true" if converged else "false",
    }


def write_summary(summary, output_path=None, append=False):
    row = {
        "algorithm": summary["algorithm"],
        "trace": summary["trace"],
        "duration_s": f"{summary['duration_s']:.8f}",
        "steady_throughput_kbps": f"{summary['steady_throughput_kbps']:.3f}",
        "threshold_kbps": f"{summary['threshold_kbps']:.3f}",
        "convergence_time_s": (
            ""
            if summary["convergence_time_s"] == ""
            else f"{summary['convergence_time_s']:.8f}"
        ),
        "converged": summary["converged"],
    }

    if output_path is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerow(row)
        return

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    mode = "a" if append else "w"
    needs_header = not append or not os.path.exists(output_path) or os.path.getsize(output_path) == 0
    with open(output_path, mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze runtime convergence from tquic timeseries CSV."
    )
    parser.add_argument("input", help="Input timeseries CSV")
    parser.add_argument("--output", help="Output summary CSV")
    parser.add_argument("--append", action="store_true", help="Append to output CSV")
    parser.add_argument(
        "--steady-tail-ratio",
        type=float,
        default=0.2,
        help="Tail ratio used as steady-state window",
    )
    parser.add_argument(
        "--threshold-ratio",
        type=float,
        default=0.9,
        help="Convergence threshold as a ratio of steady throughput",
    )
    parser.add_argument(
        "--hold-samples",
        type=int,
        default=3,
        help="Number of consecutive samples required above threshold",
    )
    args = parser.parse_args()

    if not 0.0 < args.steady_tail_ratio <= 1.0:
        raise SystemExit("--steady-tail-ratio must be in (0, 1]")
    if args.threshold_ratio <= 0.0:
        raise SystemExit("--threshold-ratio must be > 0")
    if args.hold_samples <= 0:
        raise SystemExit("--hold-samples must be > 0")

    samples = load_samples(args.input)
    summary = analyze(
        samples,
        steady_tail_ratio=args.steady_tail_ratio,
        threshold_ratio=args.threshold_ratio,
        hold_samples=args.hold_samples,
    )
    write_summary(summary, output_path=args.output, append=args.append)


if __name__ == "__main__":
    main()
