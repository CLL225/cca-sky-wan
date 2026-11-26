import os
import re
import argparse
import csv
import io
import math
from repair_utils import (
    compile_check_candidate,
    extract_rust_code,
    strip_code_blocks,
    trim_compiler_feedback,
)

MISSING_FAIRNESS_EVIDENCE_PENALTY = 40.0
MISSING_CONVERGENCE_EVIDENCE_PENALTY = 25.0

def read_file_content(filename):
    """
    Read the content of a file and return it as a string. 
    Raises FileNotFoundError if the file does not exist.
    """
    if not os.path.exists(filename):
        raise FileNotFoundError(f"找不到文件: {filename}。请确保文件存在。")
    with open(filename, "r", encoding="utf-8") as f:
        return f.read()

def get_next_version_id(base_dir, base_prefix):
    """Get the next available version number for a generated algorithm file in the specified directory."""
    counter = 1
    while True:
        rs_path = os.path.join(base_dir, f"{base_prefix}{counter}.rs")
        if not os.path.exists(rs_path):
            return counter
        counter += 1

def clamp(value, low, high):
    return max(low, min(high, value))

def clean_float(value, default=0.0):
    try:
        text = str(value).strip()
        if text == "":
            return default
        return float(re.sub(r'[^0-9.+eE-]+', '', text))
    except (TypeError, ValueError):
        return default

def calc_score(throughput, srtt, min_rtt, rttvar, duration, app_type, reference=None):
    """Trace-normalized single-flow utility.

    The score is intentionally not raw Mbps. It follows common CCA evaluation
    practice: compare against the best baseline on the same trace, reward the
    application's primary utility, and penalize queueing delay/jitter normalized
    by propagation RTT. Fairness and convergence are applied later as hard
    cross-run penalties when those logs exist.
    """
    if throughput <= 0 or srtt <= 0 or duration <= 0:
        return -9999.0

    reference = reference or {}
    ref_tput = max(reference.get("throughput", throughput), 1.0)
    ref_duration = max(reference.get("duration", duration), 0.001)
    ref_min_rtt = max(reference.get("min_rtt", min_rtt), 1.0)

    queue_delay = max(0.0, srtt - min_rtt)
    throughput_ratio = throughput / ref_tput
    completion_ratio = ref_duration / duration
    queue_ratio = queue_delay / ref_min_rtt
    jitter_ratio = max(0.0, rttvar) / ref_min_rtt

    queue_penalty = math.log1p(queue_ratio)
    jitter_penalty = math.log1p(jitter_ratio)

    if app_type == "stream":
        # Bulk/stream transfer: utilization is primary, but extra throughput is
        # capped to avoid rewarding queue-building aggressiveness indefinitely.
        score = 100.0 * clamp(throughput_ratio, 0.0, 1.25)
        score -= 20.0 * queue_penalty
        score -= 10.0 * jitter_penalty

    elif app_type == "rpc":
        # RPC mode in the current harness reports aggregate request-workload
        # completion over the run, not per-request tail latency. Reward useful
        # work per unit time, then make queueing delay/jitter the dominant cost.
        score = 80.0 * clamp(throughput_ratio, 0.0, 1.20)
        score += 40.0 * clamp(completion_ratio, 0.0, 1.20)
        score -= 30.0 * queue_penalty
        score -= 20.0 * jitter_penalty

    elif app_type == "video":
        # Real-time video has a target-rate style utility: reaching a practical
        # bitrate is valuable, but exceeding it should not dominate delay/jitter.
        target_tput = max(reference.get("video_target_throughput", 0.8 * ref_tput), 1.0)
        bitrate_score = clamp(throughput / target_tput, 0.0, 1.0)
        score = 100.0 * bitrate_score
        score -= 35.0 * queue_penalty
        score -= 25.0 * jitter_penalty

    else:
        score = 100.0 * clamp(throughput_ratio, 0.0, 1.0)
        score -= 25.0 * queue_penalty
        score -= 15.0 * jitter_penalty

    return score

def build_trace_references(rows):
    """Build per-trace normalization references from baselines when possible."""
    by_trace = {}
    for row in rows:
        by_trace.setdefault(row["trace"], []).append(row)

    references = {}
    for trace, trace_rows in by_trace.items():
        baseline_rows = [r for r in trace_rows if not r["algo"].startswith("llmgen")]
        ref_rows = baseline_rows if baseline_rows else trace_rows
        references[trace] = {
            "throughput": max((r["throughput"] for r in ref_rows), default=1.0),
            "duration": min((r["duration"] for r in ref_rows if r["duration"] > 0), default=1.0),
            "min_rtt": min((r["min_rtt"] for r in ref_rows if r["min_rtt"] > 0), default=1.0),
        }
        references[trace]["video_target_throughput"] = 0.8 * references[trace]["throughput"]

    return references

def csv_has_data_rows(log_content):
    if not log_content:
        return False

    reader = csv.DictReader(io.StringIO(log_content.strip()))
    return any(True for _ in reader)

def parse_experiment_logs(log_content, app_type):
    """Parse output.csv or output.log, returning baseline and generated algorithm runs with scores."""
    baselines = {}
    llm_runs_temp = {}
    parsed_rows = []

    reader = csv.DictReader(io.StringIO(log_content.strip()))
    for row in reader:
        # output.csv redirects the client's stdout *and* stderr (run_evaluation.sh
        # uses 2>&1), so a trace run that fails to connect, panics, or is killed
        # leaves a plain log line here instead of a CSV row. csv.DictReader turns
        # such a line into a row whose missing columns are None -- skip it
        # instead of crashing the whole evaluation.
        algo = (row.get('algorithm') or '').strip()
        trace = (row.get('trace') or '').strip()
        if not algo or not trace:
            print(f"Data parse warning, skipped non-CSV line: {row}")
            continue

        tput = clean_float(row.get('throughput'), None)
        srtt = clean_float(row.get('srtt'), None)
        min_rtt = clean_float(row.get('min_rtt'), None)
        rttvar = clean_float(row.get('rttvar'), None)
        duration = clean_float(row.get('duration'), None)
        if None in (tput, srtt, min_rtt, rttvar, duration):
            print(f"Data parse warning, skipped malformed row: {row}")
            continue

        parsed_rows.append({
            "algo": algo,
            "trace": trace,
            "duration": duration,
            "throughput": tput,
            "srtt": srtt,
            "min_rtt": min_rtt,
            "rttvar": rttvar,
        })

    references = build_trace_references(parsed_rows)

    for data in parsed_rows:
        ref = references.get(data["trace"], {})
        data["score"] = calc_score(
            data["throughput"],
            data["srtt"],
            data["min_rtt"],
            data["rttvar"],
            data["duration"],
            app_type,
            reference=ref,
        )
        
        if data["algo"].startswith("llmgen"):
            match = re.search(r'(\d+)$', data["algo"])
            if not match:
                continue
            num = int(match.group(1))
            if num not in llm_runs_temp:
                llm_runs_temp[num] = []
            llm_runs_temp[num].append(data)
        else:
            if data["algo"] not in baselines:
                baselines[data["algo"]] = []
            baselines[data["algo"]].append(data)
            
    llm_runs = []
    for num, runs in llm_runs_temp.items():
        if not runs: continue
        avg_tput = sum(x['throughput'] for x in runs) / len(runs)
        avg_srtt = sum(x['srtt'] for x in runs) / len(runs)
        avg_min_rtt = sum(x['min_rtt'] for x in runs) / len(runs)
        avg_rttvar = sum(x['rttvar'] for x in runs) / len(runs)
        avg_score = sum(x['score'] for x in runs) / len(runs)
        avg_duration = sum(x['duration'] for x in runs) / len(runs)
        avg_queue_delay = sum(max(0.0, x['srtt'] - x['min_rtt']) for x in runs) / len(runs)
        
        algo_name = f"llmgen{num}"
        raw_summary = (
            f"throughput: {avg_tput:.2f}kbps, duration: {avg_duration:.2f}s, "
            f"srtt: {avg_srtt:.2f}ms, queue_delay: {avg_queue_delay:.2f}ms"
        )
        
        llm_runs.append({
            "algo": algo_name,
            "num": num,
            "throughput": avg_tput,
            "srtt": avg_srtt,
            "min_rtt": avg_min_rtt,
            "rttvar": avg_rttvar,
            "queue_delay": avg_queue_delay,
            "duration": avg_duration,
            "score": avg_score,
            "base_score": avg_score,
            "fairness_penalty": 0.0,
            "convergence_penalty": 0.0,
            "raw": raw_summary
        })
        
    return baselines, llm_runs

def normalize_algo_name(name):
    """Normalize algorithm name, allowing llmgen in fairness.csv to match llmgenN in output.csv."""
    return re.sub(r'\d+$', '', str(name).strip().lower())

def is_target_algo(algo, target_algo):
    algo_norm = normalize_algo_name(algo)
    target_norm = normalize_algo_name(target_algo)
    return algo_norm == target_norm or str(algo).strip().lower() == str(target_algo).strip().lower()

def is_exact_or_family_match(algo, target_algo):
    algo = str(algo).strip().lower()
    target_algo = str(target_algo).strip().lower()
    if re.search(r'\d+$', target_algo):
        return algo == target_algo
    return normalize_algo_name(algo) == normalize_algo_name(target_algo)

def parse_fairness_logs(log_content, target_algo, target_cca_filename=None):
    """Parse fairness log content and extract Jain/share evidence related to the current generated algorithm."""
    rows = []
    reader = csv.DictReader(io.StringIO(log_content.strip()))

    for row in reader:
        algo1 = (row.get('algo1') or '').strip()
        algo2 = (row.get('algo2') or '').strip()
        cca_filename = (row.get('cca_filename') or '').strip()
        target_is_flow1 = is_target_algo(algo1, target_algo)
        target_is_flow2 = is_target_algo(algo2, target_algo)

        if not target_is_flow1 and not target_is_flow2:
            continue
        if target_cca_filename and cca_filename and cca_filename != target_cca_filename:
            continue

        flow1_share = clean_float(row.get('flow1_share'))
        flow2_share = clean_float(row.get('flow2_share'))

        if target_is_flow1 and target_is_flow2:
            target_share = max(flow1_share, flow2_share)
            opponent = algo2
            role = "self-competition"
        elif target_is_flow1:
            target_share = flow1_share
            opponent = algo2
            role = "flow1"
        else:
            target_share = flow2_share
            opponent = algo1
            role = "flow2"

        rows.append({
            "run_id": (row.get('run_id') or '').strip(),
            "network": (row.get('network') or '').strip(),
            "mode": (row.get('mode') or '').strip(),
            "algo1": algo1,
            "algo2": algo2,
            "cca_filename": cca_filename,
            "opponent": opponent,
            "role": role,
            "jain": clean_float(row.get('jain')),
            "target_share": target_share,
            "flow1_kbps": clean_float(row.get('flow1_kbps')),
            "flow2_kbps": clean_float(row.get('flow2_kbps')),
            "total_kbps": clean_float(row.get('total_kbps')),
            "bandwidth_mbit": clean_float(row.get('bandwidth_mbit')),
            "delay_ms": clean_float(row.get('delay_ms')),
            "loss_percent": clean_float(row.get('loss_percent')),
        })

    rows.sort(key=lambda x: x["jain"])
    return rows

def calc_fairness_penalty(fairness_rows):
    """Hard penalty for two-flow unfairness.

    A good single-flow utility is not enough for a publishable CCA result. Low
    Jain index or a generated-flow share outside 40%-60% indicates the algorithm
    is either too aggressive or too conservative under competition.
    """
    if not fairness_rows:
        return 0.0

    penalties = []
    for row in fairness_rows:
        jain = clamp(row["jain"], 0.0, 1.0)
        target_share = clamp(row["target_share"], 0.0, 1.0)
        jain_deficit = max(0.0, 0.95 - jain)
        share_violation = max(0.0, target_share - 0.60, 0.40 - target_share)
        penalties.append(220.0 * jain_deficit + 180.0 * share_violation)

    return sum(penalties) / len(penalties)

def parse_convergence_logs(log_content, target_algo):
    """Parse convergence_summary.csv content and extract convergence evidence related to the current generated algorithm."""
    rows = []
    reader = csv.DictReader(io.StringIO(log_content.strip()))
    for row in reader:
        algo = (row.get('algorithm') or '').strip()
        if not is_exact_or_family_match(algo, target_algo):
            continue

        rows.append({
            "algorithm": algo,
            "trace": (row.get('trace') or '').strip(),
            "duration_s": clean_float(row.get('duration_s')),
            "steady_throughput_kbps": clean_float(row.get('steady_throughput_kbps')),
            "threshold_kbps": clean_float(row.get('threshold_kbps')),
            "convergence_time_s": clean_float(row.get('convergence_time_s'), None),
            "converged": str(row.get('converged', '')).strip().lower() == "true",
        })

    return rows

def format_convergence_summary(convergence_rows):
    if not convergence_rows:
        return ("No convergence_summary.csv entries were found for this generated algorithm. "
                "Still optimize for fast startup and stable throughput after ramp-up.")

    converged_rows = [row for row in convergence_rows if row["converged"] and row["convergence_time_s"] is not None]
    avg_steady = sum(row["steady_throughput_kbps"] for row in convergence_rows) / len(convergence_rows)
    if converged_rows:
        avg_conv_time = sum(row["convergence_time_s"] for row in converged_rows) / len(converged_rows)
        worst_conv_time = max(row["convergence_time_s"] for row in converged_rows)
        conv_rate = len(converged_rows) / len(convergence_rows)
    else:
        avg_conv_time = 0.0
        worst_conv_time = 0.0
        conv_rate = 0.0

    lines = [
        f"Convergence log summary over {len(convergence_rows)} relevant runs:",
        f"- Converged runs: {len(converged_rows)}/{len(convergence_rows)} ({conv_rate:.2%}); avg steady throughput: {avg_steady:.2f}kbps",
    ]
    if converged_rows:
        lines.append(f"- Avg convergence time: {avg_conv_time:.2f}s; worst convergence time: {worst_conv_time:.2f}s")
    else:
        lines.append("- No run reached 90% of steady throughput for 3 consecutive samples.")

    return "\n".join(lines)

def calc_convergence_penalty(convergence_rows):
    """Penalty for no convergence or slow convergence."""
    if not convergence_rows:
        return 0.0

    penalties = []
    for row in convergence_rows:
        duration = max(row["duration_s"], 0.001)
        if not row["converged"] or row["convergence_time_s"] is None:
            penalties.append(60.0)
            continue

        convergence_ratio = clamp(row["convergence_time_s"] / duration, 0.0, 1.0)
        penalties.append(35.0 * convergence_ratio)

    return sum(penalties) / len(penalties)

def apply_external_metric_penalties(llm_runs, fairness_content=None, convergence_content=None, filename_prefix="cca_generated", require_convergence=False):
    """Apply fairness/convergence penalties to final mutation-selection scores.

    ``require_convergence`` makes a missing convergence log cost
    ``MISSING_CONVERGENCE_EVIDENCE_PENALTY`` instead of scoring as if there were
    nothing to penalise; the evaluator always sets it because the harness is
    expected to emit convergence_summary.csv for every run.
    """
    fairness_has_rows = csv_has_data_rows(fairness_content)
    convergence_has_rows = csv_has_data_rows(convergence_content)

    for run in llm_runs:
        target_algo = run["algo"]
        target_cca_filename = f"{filename_prefix}{run['num']}.rs"

        if fairness_has_rows:
            fairness_rows = parse_fairness_logs(
                fairness_content,
                target_algo,
                target_cca_filename,
            )
            if fairness_rows:
                run["fairness_penalty"] = calc_fairness_penalty(fairness_rows)
            else:
                run["fairness_penalty"] = MISSING_FAIRNESS_EVIDENCE_PENALTY

        if convergence_has_rows:
            convergence_rows = parse_convergence_logs(convergence_content, target_algo)
            if convergence_rows:
                run["convergence_penalty"] = calc_convergence_penalty(convergence_rows)
            else:
                run["convergence_penalty"] = MISSING_CONVERGENCE_EVIDENCE_PENALTY
        elif require_convergence:
            run["convergence_penalty"] = MISSING_CONVERGENCE_EVIDENCE_PENALTY

        run["score"] = (
            run["base_score"]
            - run["fairness_penalty"]
            - run["convergence_penalty"]
        )
        run["raw"] = (
            f"{run['raw']}, base_score: {run['base_score']:.2f}, "
            f"fairness_penalty: {run['fairness_penalty']:.2f}, "
            f"convergence_penalty: {run['convergence_penalty']:.2f}"
        )

def format_fairness_summary(fairness_rows):
    if not fairness_rows:
        return ("No fairness.csv entries were found for this generated algorithm. "
                "Still optimize for two-flow coexistence: target Jain >= 0.95 and a 40%-60% long-run share.")

    avg_jain = sum(x["jain"] for x in fairness_rows) / len(fairness_rows)
    min_jain = fairness_rows[0]["jain"]
    avg_share_error = sum(abs(x["target_share"] - 0.5) for x in fairness_rows) / len(fairness_rows)
    too_aggressive = sum(1 for x in fairness_rows if x["target_share"] > 0.60)
    too_conservative = sum(1 for x in fairness_rows if x["target_share"] < 0.40)

    lines = [
        f"Fairness log summary over {len(fairness_rows)} relevant runs:",
        f"- Average Jain: {avg_jain:.4f}; worst Jain: {min_jain:.4f}; average absolute share error from 0.5: {avg_share_error:.4f}",
        f"- Runs where generated algorithm took >60% share: {too_aggressive}; runs where it took <40% share: {too_conservative}",
        "- Worst fairness cases:",
    ]

    for item in fairness_rows[:5]:
        version_label = f", cca={item['cca_filename']}" if item.get("cca_filename") else ""
        lines.append(
            f"  * {item['run_id'] or item['algo1'] + '_vs_' + item['algo2']}: "
            f"{item['algo1']} vs {item['algo2']}{version_label}, Jain={item['jain']:.4f}, "
            f"generated_share={item['target_share']:.4f}, total={item['total_kbps']:.2f}kbps, "
            f"bandwidth={item['bandwidth_mbit']:.1f}mbit, delay={item['delay_ms']:.1f}ms, loss={item['loss_percent']:.3f}%"
        )

    if too_aggressive > too_conservative:
        lines.append("Diagnosis hint: the generated algorithm is probably too aggressive under competition; add sustained-queue/loss backoff and bounded probe-up/drain cycles.")
    elif too_conservative > too_aggressive:
        lines.append("Diagnosis hint: the generated algorithm is probably yielding too much bandwidth; add cautious recovery/probing when queueing delay is low.")
    else:
        lines.append("Diagnosis hint: fix convergence stability; avoid both permanent high gain and overreaction to isolated random losses.")

    return "\n".join(lines)

def get_algorithm_code(num):
    """Get the source code of the generated algorithm for a specific version number."""
    filename = f"../congestion_control_algorithm/cca_generated{num}.rs"
    if os.path.exists(filename):
        return read_file_content(filename)
    return "// Code file not found."

def get_algorithm_log(num):
    """Get the historical analysis/thought log for a specific version number."""
    filename = f"../logs/cca_optimization_log{num}.md"
    if os.path.exists(filename):
        return read_file_content(filename)
    return ""

def read_optional_file(path):
    if not os.path.exists(path):
        return None
    try:
        return read_file_content(path)
    except Exception:
        return None

def get_results_inputs():
    log_file_path = "../bin/results/output.csv"
    if not os.path.exists(log_file_path):
        log_file_path = "../bin/results/output.log"
    if not os.path.exists(log_file_path):
        return None, None, None

    try:
        experiment_results = read_file_content(log_file_path)
    except Exception:
        return None, None, None

    fairness_content = read_optional_file("../bin/results/fairness.csv")
    convergence_content = read_optional_file("../bin/results/convergence_summary.csv")
    return experiment_results, fairness_content, convergence_content

def summarize_telemetry(app_type="stream", filename_prefix="cca_generated", target_num=None):
    experiment_results, fairness_content, convergence_content = get_results_inputs()
    if not experiment_results or not csv_has_data_rows(experiment_results):
        return "No prior experiment telemetry is available yet."

    baselines, llm_runs = parse_experiment_logs(experiment_results, app_type)
    apply_external_metric_penalties(
        llm_runs,
        fairness_content=fairness_content,
        convergence_content=convergence_content,
        filename_prefix=filename_prefix,
    )

    lines = ["# Historical Telemetry Summary", "", "## Baselines"]
    if baselines:
        for algo, rows in sorted(baselines.items()):
            avg_score = sum(x["score"] for x in rows) / len(rows)
            avg_tput = sum(x["throughput"] for x in rows) / len(rows)
            avg_srtt = sum(x["srtt"] for x in rows) / len(rows)
            avg_queue = sum(max(0.0, x["srtt"] - x["min_rtt"]) for x in rows) / len(rows)
            lines.append(
                f"- {algo}: score={avg_score:.2f}, throughput={avg_tput:.2f}kbps, "
                f"srtt={avg_srtt:.2f}ms, queue_delay={avg_queue:.2f}ms"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Generated Runs"])
    if llm_runs:
        best_run = max(
            llm_runs,
            key=lambda run: (
                run["score"],
                run["throughput"],
                -run["queue_delay"],
                -run["duration"],
                -run["num"],
            ),
        )
        latest_run = max(llm_runs, key=lambda run: run["num"])
        worst_run = min(llm_runs, key=lambda run: run["score"])

        lines.append(
            f"- Best so far: llmgen{best_run['num']} "
            f"(final_score={best_run['score']:.2f}, throughput={best_run['throughput']:.2f}kbps, "
            f"queue_delay={best_run['queue_delay']:.2f}ms)"
        )
        lines.append(
            f"- Latest measured run: llmgen{latest_run['num']} "
            f"(final_score={latest_run['score']:.2f}, throughput={latest_run['throughput']:.2f}kbps, "
            f"queue_delay={latest_run['queue_delay']:.2f}ms)"
        )
        lines.append(
            f"- Worst measured run: llmgen{worst_run['num']} "
            f"(final_score={worst_run['score']:.2f}, throughput={worst_run['throughput']:.2f}kbps, "
            f"queue_delay={worst_run['queue_delay']:.2f}ms)"
        )
        lines.append("")
        for run in sorted(llm_runs, key=lambda x: x["num"]):
            marker = " (target parent)" if target_num is not None and run["num"] == target_num else ""
            lines.append(
                f"- llmgen{run['num']}{marker}: final_score={run['score']:.2f}, "
                f"base_score={run['base_score']:.2f}, fairness_penalty={run['fairness_penalty']:.2f}, "
                f"convergence_penalty={run['convergence_penalty']:.2f}, "
                f"throughput={run['throughput']:.2f}kbps, srtt={run['srtt']:.2f}ms, "
                f"queue_delay={run['queue_delay']:.2f}ms, duration={run['duration']:.2f}s"
            )
    else:
        lines.append("- none")

    if target_num is not None:
        label = f"llmgen{target_num}"
        cca_filename = f"{filename_prefix}{target_num}.rs"
        lines.extend(["", "## Target-Specific Fairness"])
        if fairness_content and csv_has_data_rows(fairness_content):
            lines.append(format_fairness_summary(parse_fairness_logs(fairness_content, label, cca_filename)))
        else:
            lines.append(format_fairness_summary([]))

        lines.extend(["", "## Target-Specific Runtime Convergence"])
        if convergence_content and csv_has_data_rows(convergence_content):
            lines.append(format_convergence_summary(parse_convergence_logs(convergence_content, label)))
        else:
            lines.append(format_convergence_summary([]))

    return "\n".join(lines)

def select_best_run(app_type="stream", filename_prefix="cca_generated"):
    """Select the best generated run from current logs after external penalties."""
    experiment_results, fairness_content, convergence_content = get_results_inputs()
    if not experiment_results or not csv_has_data_rows(experiment_results):
        return None

    _baselines, llm_runs = parse_experiment_logs(experiment_results, app_type)
    if not llm_runs:
        return None

    apply_external_metric_penalties(
        llm_runs,
        fairness_content=fairness_content,
        convergence_content=convergence_content,
        filename_prefix=filename_prefix,
    )

    return max(
        llm_runs,
        key=lambda run: (
            run["score"],
            run["throughput"],
            -run["queue_delay"],
            -run["duration"],
            -run["num"],
        ),
    )

def select_best_run_num(app_type="stream", filename_prefix="cca_generated"):
    best_run = select_best_run(app_type=app_type, filename_prefix=filename_prefix)
    if not best_run:
        return None
    return best_run["num"]

def summarize_run_delta(reference_run, other_run, reference_label="parent", other_label="latest"):
    score_delta = other_run["score"] - reference_run["score"]
    throughput_delta = other_run["throughput"] - reference_run["throughput"]
    queue_delta = other_run["queue_delay"] - reference_run["queue_delay"]
    srtt_delta = other_run["srtt"] - reference_run["srtt"]

    lines = [
        f"- Final score delta ({other_label} - {reference_label}): {score_delta:+.2f}",
        f"- Throughput delta: {throughput_delta:+.2f}kbps",
        f"- Queue delay delta: {queue_delta:+.2f}ms",
        f"- SRTT delta: {srtt_delta:+.2f}ms",
    ]

    if score_delta >= 3.0:
        lines.append(f"- {other_label} improved the score materially. Keep only the local changes that clearly caused this gain.")
    elif score_delta <= -3.0:
        lines.append(f"- {other_label} is a material regression. Identify what changed locally and avoid repeating that mistake.")
    else:
        lines.append(f"- {other_label} is close to {reference_label}. Use it as weak local evidence, not as the default design direction.")

    if queue_delta > 8.0 and throughput_delta < 200.0:
        lines.append(f"- Recent changes likely paid too much queue growth for too little useful throughput.")
    elif throughput_delta < -500.0 and queue_delta <= 5.0:
        lines.append(f"- Recent changes likely became too conservative and underfilled the path.")

    return "\n".join(lines)

def optimize_and_save_cca():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", type=str, default="llmgen", help="Generated algorithm label name")
    parser.add_argument("--num", type=int, default=1, help="Target algorithm version to improve")
    parser.add_argument("--app", type=str, choices=["stream", "rpc", "video"], default="stream", help="Target application for optimization metric")
    parser.add_argument("--network", type=str, choices=["wan", "cellular", "satellite"], default="cellular", help="Target network scenario")
    parser.add_argument("--repair-attempts", type=int, default=int(os.environ.get("LLMGEN_REPAIR_ATTEMPTS", "2")), help="Maximum compiler-repair attempts")
    args = parser.parse_args()
    
    current_num = args.num
    label_name = f"{args.name}{current_num}"

    print(f"正在分析历史日志，准备为 {args.app.upper()} 在 {args.network.upper()} 网络下优化 {label_name} ...")

    experiment_results, fairness_content, convergence_content = get_results_inputs()
    if not experiment_results:
        print("读取日志失败: 未找到 output.csv/output.log。")
        return
    try:
        telemetry_summary = summarize_telemetry(
            app_type=args.app,
            filename_prefix="cca_generated",
            target_num=current_num,
        )
    except Exception as e:
        print(f"构建历史 telemetry 摘要失败: {e}")
        telemetry_summary = "Historical telemetry summary unavailable."

    try:
        baselines, llm_runs = parse_experiment_logs(experiment_results, args.app)
    except Exception as e:
        print(f"读取日志失败: {e}")
        return
    
    if not llm_runs:
        print("未在日志中找到 llmgen 的记录！请检查日志文件是否包含目标数据。")
        return

    apply_external_metric_penalties(
        llm_runs,
        fairness_content=fairness_content,
        convergence_content=convergence_content,
    )

    all_runs = sorted(llm_runs, key=lambda x: x["num"])
    if not all_runs:
        print("未找到历史生成算法记录。")
        return

    parent_run = next((run for run in all_runs if run["num"] == current_num), None)
    if parent_run is None:
        print(f"未找到目标父代版本 llmgen{current_num} 的历史记录。")
        return

    latest_run = max(all_runs, key=lambda x: x["num"])
    worst_run = min(all_runs, key=lambda x: x["score"])
    best_run = max(
        all_runs,
        key=lambda run: (
            run["score"],
            run["throughput"],
            -run["queue_delay"],
            -run["duration"],
            -run["num"],
        ),
    )
    local_tuning_mode = parent_run["score"] >= 75.0

    # 构建 Prompt
    prompt_sections = []
    
    if args.network == 'satellite':
        prompt_sections.append(f"You are a Senior Networking Architect optimizing a custom QUIC congestion control algorithm in Rust to beat baselines on **{args.network.upper()}** networks (LEO) specifically for a **{args.app.upper()}** application.\n")
    else:
        prompt_sections.append(f"You are a Senior Networking Architect optimizing a custom QUIC congestion control algorithm in Rust to beat baselines on **{args.network.upper()}** networks specifically for a **{args.app.upper()}** application.\n")
    # 明确告知大模型当前的 Reward Function
    prompt_sections.append("### Current Evaluation Metric (Reward Score Focus):")
    prompt_sections.append(
        "The final score is a trace-normalized utility, not raw Mbps. "
        "For each trace, single-flow metrics are normalized against the best available baseline on that same trace. "
        "The final mutation-selection score is: application utility - fairness penalty - convergence penalty."
    )
    if args.app == "stream":
        prompt_sections.append("STREAM utility primarily rewards normalized throughput/link utilization, capped above the best baseline so queue-building aggression is not rewarded indefinitely. Queueing delay and jitter are still penalized.")
    elif args.app == "rpc":
        prompt_sections.append("RPC utility rewards aggregate request-workload progress relative to the best baseline and strongly penalizes queueing delay/jitter. The current harness does not expose per-request tail latency, so do not claim p95/p99 RPC latency from this score alone.")
    elif args.app == "video":
        prompt_sections.append("VIDEO utility uses target-rate style throughput saturation: reaching the target bitrate is rewarded you can estimate the target bitrate based on baseline statistics, but excess throughput does not compensate for queueing delay or jitter.")

    prompt_sections.append("""
### Multi-Flow Fairness Objective:
The algorithm is also evaluated with two concurrent QUIC flows sharing one bottleneck.
- Target Jain fairness index >= 0.95.
- The generated flow should converge toward a 40%-60% long-run throughput share against BBR, CUBIC, Copa, and another generated flow.
- A version with high single-flow throughput but Jain 0.5-0.8 is considered a failure.
- Use local signals only: sustained queueing delay (`srtt - min_rtt`), rate plateau, losses with elevated queueing delay, and bytes_in_flight near cwnd.
""")

    prompt_sections.append("\n### Baseline Performance (Your targets):")
    for b_algo, b_data in baselines.items():
        avg_score = sum(x['score'] for x in b_data) / len(b_data)
        avg_tput = sum(x['throughput'] for x in b_data) / len(b_data)
        avg_srtt = sum(x['srtt'] for x in b_data) / len(b_data)
        avg_duration = sum(x['duration'] for x in b_data) / len(b_data)
        prompt_sections.append(f"- {b_algo.upper()}: Avg Score: {avg_score:.2f}, Avg Tput: {avg_tput:.2f}kbps, Avg SRTT: {avg_srtt:.2f}ms, Avg Duration: {avg_duration:.2f}s")

    prompt_sections.append("\n### Historical Telemetry Summary:")
    prompt_sections.append(telemetry_summary)

    parent_log = get_algorithm_log(parent_run['num'])
    log_injection_parent = f"\n<Design Notes from this version>:\n{parent_log}\n</Design Notes>\n" if parent_log else ""

    prompt_sections.append(f"\nPARENT VERSION TO IMPROVE ({parent_run['algo']}):\n"
                           f"Stats: {parent_run['raw']} | Score: {parent_run['score']:.2f}\n"
                           f"This is the mutation parent. Optimize this version directly unless a concrete bug forces redesign.\n"
                           f"{log_injection_parent}"
                           f"```rust\n{get_algorithm_code(parent_run['num'])}\n```")

    if latest_run['num'] != parent_run['num']:
        latest_log = get_algorithm_log(latest_run['num'])
        log_injection_latest = f"\n<Design Notes from this version>:\n{latest_log}\n</Design Notes>\n" if latest_log else ""

        prompt_sections.append(f"\nLATEST ATTEMPT RESULT ({latest_run['algo']}):\n"
                               f"Stats: {latest_run['raw']} | Score: {latest_run['score']:.2f}\n"
                               f"Use this only as recent local evidence. It is newer than the parent but not automatically better.\n"
                               f"{summarize_run_delta(parent_run, latest_run, reference_label='parent', other_label='latest attempt')}\n"
                               f"{log_injection_latest}")

    network_hint = ""
    if args.network == "cellular":
        network_hint = "Cellular links have rapidly changing bandwidth, deep buffers, and random radio loss. \
        Do not cut cwnd on isolated loss when queueing delay stays low. \
        Prefer mechanisms that can re-expand quickly after non-congestion loss."
    elif args.network == "satellite":
        network_hint = "- LEO satellite links have high BDP and occasional handover loss. \
            Distinguish persistent queue growth from brief handover events. \
            Avoid overreacting to short disruption spikes that are not backed by sustained delay."
    else:
        network_hint = "- WAN paths are comparatively stable but still see occasional random drops. \
            Use delay trend, delivery-rate trend, and inflight pressure together instead of loss alone. \
            Keep probing bounded so long-lived standing queues do not dominate the path."

    prompt_sections.append(f"""
Details of CongestionStats, RttEstimator, SentPacket and RecoveryConfig:
1. The `CongestionStats` struct: update `bytes_sent_in_total`, `bytes_acked_in_total`, `bytes_lost_in_total`, and `bytes_in_flight` correctly in the relevant callbacks.
2. The `RttEstimator`: use `rtt.smoothed_rtt()`, `rtt.latest_rtt()`, and `rtt.min_rtt()` to compute queueing delay. Returned values are `Duration`.
3. The `SentPacket`: use `sent_size` cast to `u64` for accounting.
4. The `RecoveryConfig`: contains `max_datagram_size: usize` and `initial_congestion_window: u64`.

Hard implementation constraints:
1. Network awareness: {network_hint}
2. Pacing must not use integer-second time truncation. Use `as_secs_f64()` or exact microsecond math, and return bytes per second.
3. Keep `self.cwnd >= self.minimal_window()` after any reduction.
4. On persistent congestion, cut back to the minimum window.
5. Use saturating arithmetic where overflow or underflow is possible. There is no `saturating_div`.
6. Use only local sender-observable signals. Do not assume oracle knowledge of bandwidth or the other flow.

Design guidance:
- Diagnose whether the parent's failure comes from underutilization, queue overshoot, weak loss classification, poor pacing, unstable probing, unfair coexistence, or slow convergence.
- Valid directions include delay-targeted control, delivery-rate trend control, target-inflight control, bounded probing, epoch utility control, or a coherent hybrid.
- Do not mechanically imitate BBR, Copa, or CUBIC. Choose the mechanism that best fits the observed failure mode.
- If fairness is poor, add an explicit coexistence brake that reacts to sustained queue pressure, plateaued delivery rate, or repeated congestion-marking loss.
""")

    if local_tuning_mode:
        prompt_sections.append("""
### Mutation mode: Local refinement
- The parent already has a competitive score. Keep its primary control law, main state structure, and phase decomposition unless there is a concrete bug.
- Change at most 2 to 4 localized items such as thresholds, gains, backoff strength, startup exit logic, pacing logic, or loss-classification conditions.
- Preserve the parent's measured strengths and only repair the specific regressions shown by telemetry.
- Do not rewrite startup, steady-state control, and recovery all at once.
""")
    else:
        prompt_sections.append("""
### Mutation mode: Guided redesign
- The parent is still far from competitive. You may replace the main control law if the telemetry shows the current one is fundamentally wrong.
- Keep the implementation coherent: one main mechanism, one secondary stabilizer, and explicit loss/congestion classification.
- If you redesign aggressively, explain what concrete failure mode forced that redesign.
""")

    prompt_sections.append("""
Your task:
1. Explain why the parent version is succeeding or failing under the present score, fairness evidence, and convergence evidence.
2. If a latest attempt exists, extract its useful local lessons without inheriting its regressions just because it is newer.
3. Produce a revised control law that should improve application utility without sacrificing fairness and runtime convergence.
4. Write the final optimized Rust code.

The final code must be a mutation of the parent version, not an unrelated restart, unless you identify a concrete architectural bug that justifies redesign.

Reply format:
Diagnosis:
- bullet list

Design summary:
- bullet list

```rust
// Complete Rust code
```
""")

    OPTIMIZE_PROMPT = "\n".join(prompt_sections)

    try:
        from Config import Config
        from openai import OpenAI
    except ImportError as e:
        print(f"加载 OpenAI 客户端依赖失败: {e}")
        print("评分函数与日志解析仍可本地导入测试；如需生成新算法，请先安装 openai 依赖。")
        return

    config = Config()
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    print("正在向大模型发送精简优化请求...") 

    try: 
        response = client.chat.completions.create( 
            model=config.model_name, 
            messages=[ 
                {"role": "system", "content": "You are a senior networking engineer optimizing a QUIC congestion control algorithm in Rust."}, 
                {"role": "user", "content": OPTIMIZE_PROMPT} 
            ], 
            temperature=0.2,
        ) 
    except Exception as e: 
        print(f"调用 API 失败: {e}") 
        return 1

    content = response.choices[0].message.content 
    print("代码与分析生成完成！正在提取并执行编译检查...")

    # 获取唯一且匹配的版本号
    next_id = get_next_version_id("../congestion_control_algorithm", "cca_generated")

    code_filename = f"../congestion_control_algorithm/cca_generated{next_id}.rs" 
    log_filename = f"../logs/cca_optimization_log{next_id}.md"

    optimized_code = extract_rust_code(content)
    analysis_content = strip_code_blocks(content)

    if not optimized_code: 
        print("⚠️ 未能严格按照 ```rust 格式提取代码，已将完整输出保存至日志以供人工检查。")
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(content)
        return 1

    current_code = optimized_code
    current_analysis = analysis_content

    for attempt in range(args.repair_attempts + 1):
        with open(code_filename, "w", encoding="utf-8") as f:
            f.write(current_code.strip() + "\n")
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(current_analysis.strip() + "\n")

        compile_log_filename = f"../logs/cca_compile_repair{next_id}_attempt{attempt + 1}.log"
        print(f"开始编译检查: attempt {attempt + 1}/{args.repair_attempts + 1}")
        compile_ok, compile_output = compile_check_candidate(code_filename, compile_log_filename)
        if compile_ok:
            print(f"✅ 成功提取优化后的算法代码: {code_filename}")
            print(f"✅ 成功分离模型分析过程，保存为日志: {log_filename}") 
            print(f"✅ 编译检查通过，日志: {compile_log_filename}")
            return 0

        print(f"⚠️ 编译检查失败，日志已保存至: {compile_log_filename}")
        if attempt >= args.repair_attempts:
            print("❌ 已耗尽自动纠错次数，未生成可编译算法。")
            return 1

        repair_prompt = (
            OPTIMIZE_PROMPT
            + "\n\n### Compiler Feedback To Fix First:\n"
            + trim_compiler_feedback(compile_output)
            + "\n\n### Current Candidate Code:\n```rust\n"
            + current_code
            + "\n```\n\nReturn one complete corrected Rust code block. Fix compiler errors first, then preserve the intended congestion-control behavior."
        )
        print(f"开始自动纠错: attempt {attempt + 1}/{args.repair_attempts}")
        try:
            repair_response = client.chat.completions.create(
                model=config.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a senior networking engineer. Fix Rust compiler errors first, then preserve the intended congestion-control behavior.",
                    },
                    {"role": "user", "content": repair_prompt},
                ],
                temperature=0.2,
            )
        except Exception as e:
            print(f"调用 API 进行自动纠错失败: {e}")
            return 1

        repair_content = repair_response.choices[0].message.content
        repaired_code = extract_rust_code(repair_content)
        repaired_analysis = strip_code_blocks(repair_content)
        if not repaired_code:
            with open(log_filename, "w", encoding="utf-8") as f:
                f.write(repair_content)
            print(f"❌ 自动纠错未返回有效 Rust 代码，输出已保存至: {log_filename}")
            return 1

        current_code = repaired_code
        current_analysis = repaired_analysis

    return 1

if __name__ == "__main__":
    raise SystemExit(optimize_and_save_cca())
