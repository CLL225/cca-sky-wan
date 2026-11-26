"""CCA5 evaluator -- evolving a QUIC congestion controller on real network traces.

The candidate is a Rust `LLMGen` congestion-control module. The objective is
CCA5's own, unchanged::

    final_score = base_score - fairness_penalty - convergence_penalty

where ``base_score`` is a per-trace utility normalised against the best
non-llmgen baseline on that same trace. Rather than re-implementing that
formula, this evaluator imports CCA5's scoring functions from
``cca_generate/mutate_algorithm.py``, so the number the search optimises is
identical to the one the original 30-round evolution loop used, and a future
change to the formula is picked up automatically.

The measurement pipeline is likewise CCA5's, unmodified:

  1. install the candidate as ``tquic/src/congestion_control/llmgen.rs``
  2. ``cargo build --release``         -- a candidate that does not compile scores 0
  3. ``run_evaluation.sh``             -- per-trace single-flow metrics + convergence summary
  4. ``run_fairness_suite.sh``         -- two-flow Jain / share evidence
  5. score with CCA5's scoring functions

Everything runs under a lock and in a per-candidate temporary directory: the
tquic source tree and CCA5's results directory are single-instance shared state,
so concurrent evaluations would otherwise overwrite each other's candidate and
mix their measurements.

This evaluator must run inside the CCA5 container (it needs root for mahimahi's
network namespaces, plus cargo and the tquic tree). Set ``CCA5_ROOT`` to
override the location of the CCA5 checkout.
"""

import contextlib
import csv
import fcntl
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------

#: Application type the utility is scored for (stream | rpc | video). The app
#: type selects a different weighting inside calc_score.
_APP_TYPE = "video"

#: Trace family replayed. Five cellular traces.
_NETWORK = "satellite"

#: llmgen plus the baselines it is normalised against on each trace.
_ALGOS = "bbr cubic copa llmgen"

#: The baselines are candidate-independent (a candidate only replaces
#: ``llmgen.rs``), so re-measuring them for every candidate wasted most of the
#: trace replay time and made the per-trace normalisation reference drift by a
#: few percent between evaluations. They are measured once, cached, and reused;
#: every ``_BASELINE_REFRESH_EVERY`` evaluations the full set is re-measured as
#: a drift check.
_BASELINE_ALGOS = ("bbr", "cubic", "copa")
_LLMGEN_ONLY = "llmgen"
#: Baselines are a fixed-version performance library: measured
#: ``_BASELINE_REPEATS`` times, reduced to a per-(algorithm, trace) median and
#: kept until the network code or traces change.  Setting either value to 0
#: disables that refresh trigger (pinned mode).
_BASELINE_REPEATS = 3
_BASELINE_REFRESH_EVERY = 0
_BASELINE_MAX_AGE_S = 0
_BASELINE_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".baseline_cache"
)
_BASELINE_KEY = None

_DATA_SIZE = "0"
_DURATION = "20"  # seconds of trace replayed per run
_FAIRNESS_DURATION = "20"

#: Installed under this exact name on purpose. The tquic client takes the digits
#: trailing ``--cca-filename`` as the algorithm suffix
#: (``tquic/iperf/src/main.rs:504``), so the CSV reports ``llmgen1``; and CCA5's
#: fairness lookup rebuilds the name as ``f"cca_generated{num}.rs"``, so passing
#: this same name to run_fairness_suite.sh is what makes the fairness rows match
#: instead of being scored as missing evidence.
_CANDIDATE_FILENAME = "cca_generated1.rs"

#: run_evaluation.sh writes the CSV header to a hard-coded ``./bin/results/output.csv``
#: and does not honour ``RESULTS_DIR`` for it. With a redirected results directory the
#: file starts headerless, and ``csv.DictReader`` would silently eat the first trace as
#: its header row -- one trace lost per evaluation.
_OUTPUT_HEADER = "algorithm,trace,duration,throughput,srtt,min_rtt,rttvar\n"

#: Per-step ceilings. The real bound is ``evaluator.timeout`` in config.yaml;
#: these only stop one wedged step from consuming the whole budget.
_BUILD_TIMEOUT_S = 600
_TRACE_TIMEOUT_S = 900
_FAIRNESS_TIMEOUT_S = 420

#: Number of trailing log lines kept in an error artifact.
_LOG_TAIL_LINES = 25

_EVAL_LOCK = threading.Lock()
_LOCK_FILE = "/tmp/skydiscover_cca5.lock"

_SCORING = None
_SCORING_ERROR = None


def _resolve_cca5_root():
    """Locate the CCA5 checkout (it lives outside the benchmark dir)."""
    candidates = [
        os.environ.get("CCA5_ROOT"),
        "/workspace/CCA5",
        str(Path(__file__).resolve().parents[4] / "CCA5"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(os.path.join(candidate, "run_evaluation.sh")):
            return os.path.abspath(candidate)
    return None


_CCA5_ROOT = _resolve_cca5_root()


def _cca5(*parts):
    return os.path.join(_CCA5_ROOT, *parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _serialized():
    """Serialise evaluations.

    The candidate is compiled into the shared tquic tree, and the scripts mutate
    shared results directories, so two evaluations running at once would clobber
    each other. ``max_parallel_iterations`` defaults to 1 (sequential), but the
    framework sizes its evaluation pool at ``max(..., 4)``, so this is a
    correctness guard rather than an optimisation.
    """
    with _EVAL_LOCK:
        handle = os.open(_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)


def _load_scoring():
    """Import CCA5's own scoring functions (lazily, once)."""
    global _SCORING, _SCORING_ERROR
    if _SCORING is None and _SCORING_ERROR is None:
        try:
            scoring_dir = _cca5("cca_generate")
            if scoring_dir not in sys.path:
                sys.path.insert(0, scoring_dir)
            from mutate_algorithm import (  # deliberately lazy: path depends on CCA5_ROOT
                apply_external_metric_penalties,
                csv_has_data_rows,
                parse_experiment_logs,
                parse_fairness_logs,
            )

            # Diagnostics only, so it must not be able to break scoring: a CCA5
            # checkout without this helper still evaluates every candidate, it
            # just loses the per-case fairness breakdown. The four imports above
            # are load-bearing and stay unguarded.
            try:
                from mutate_algorithm import format_fairness_summary
            except ImportError:
                format_fairness_summary = None

            _SCORING = (
                parse_experiment_logs,
                apply_external_metric_penalties,
                csv_has_data_rows,
                parse_fairness_logs,
                format_fairness_summary,
            )
        except Exception as exc:  # pragma: no cover - depends on the CCA5 checkout
            _SCORING_ERROR = f"cannot import CCA5 scoring from {_cca5('cca_generate')}: {exc}"
    return _SCORING, _SCORING_ERROR


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _tail(text, lines=_LOG_TAIL_LINES):
    if not text:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _diagnostics(results_dir, trace_run, fairness_run):
    """Explain why a run failed.

    run_evaluation.sh redirects the client's stdout *and* stderr into output.csv
    rather than to stderr, so a client-side failure shows up nowhere else. Without
    reading it back the failure would surface as a bare "no rows in output.csv"
    with no trace of the actual cause.
    """
    parts = [
        "output.csv (client stdout/stderr):\n"
        + (_tail(_read_text(os.path.join(results_dir, "output.csv")), 15) or "(empty)")
    ]
    for label, proc in (("trace replay", trace_run), ("fairness suite", fairness_run)):
        if proc is None:
            continue
        parts.append(f"{label} rc={proc.returncode}")
        stderr_tail = _tail(proc.stderr)
        if stderr_tail:
            parts.append(f"{label} stderr tail:\n{stderr_tail}")
    return "\n".join(parts)


def _failure(message, detail=""):
    """Build the failure result.

    The Python evaluator path funnels a returned dict straight into
    ``EvaluationResult.metrics``, so ``error`` arrives there as a string. That is
    the convention this codebase's other Python evaluators follow, and the
    search's failure check is ``combined_score == 0 and metrics["error"]`` -- so
    a harness-level failure is reported as a failed evaluation rather than as a
    legitimate score of zero.
    """
    result = {"combined_score": 0.0, "runs_successfully": 0.0, "error": message}
    if detail:
        result["error_detail"] = detail
    return result


def _run(argv, env, timeout, label):
    print(f"[cca5] {label}: {' '.join(argv)}", flush=True)
    return subprocess.run(
        argv,
        cwd=_CCA5_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Baseline cache
#
# bbr/cubic/copa do not depend on the candidate, so measuring them once per
# (network, app, duration, trace set, tquic source) and reusing the rows keeps
# the normalisation reference fixed across candidates and saves most of the
# trace replay time. The rows are prepended to output.csv before the llmgen
# run, so CCA5's parser still sees baselines and candidate in one CSV.
# ---------------------------------------------------------------------------


def _baseline_cache_key():
    """Key a baseline set by everything that can change its measurement."""
    global _BASELINE_KEY
    if _BASELINE_KEY is not None:
        return _BASELINE_KEY

    digest = hashlib.sha256()
    for part in (_NETWORK, _APP_TYPE, _DATA_SIZE, _DURATION, "|".join(_BASELINE_ALGOS)):
        digest.update(part.encode())

    trace_dir = _cca5("bin", "traces", _NETWORK)
    try:
        names = sorted(os.listdir(trace_dir))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(trace_dir, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if os.path.isfile(path):
            digest.update(f"{name}:{stat.st_size}:{int(stat.st_mtime)}".encode())

    for name in ("replay_wan.py", "replay_lens.py", "run_tc.sh",
                 "run_evaluation.sh", "analyze_convergence.py",
                 "run_fairness.sh", "run_fairness_suite.sh"):
        path = _cca5(name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        digest.update(f"{name}:{stat.st_size}:{int(stat.st_mtime)}".encode())

    for knob in ("WAN_PROFILE", "WAN_RATE_MBIT", "WAN_RTT_MS", "WAN_LOSS_PCT",
                 "WAN_QUEUE_BDP", "TRACE_START_S", "SATELLITE_LOSS_PCT",
                 "SATELLITE_QUEUE_BDP"):
        digest.update(f"{knob}={os.environ.get(knob, '')}".encode())

    src_root = _cca5("tquic", "src")
    for root, dirs, files in os.walk(src_root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, src_root).replace(os.sep, "/")
            if rel.endswith("congestion_control/llmgen.rs"):
                continue  # the candidate; baseline behaviour does not depend on it
            try:
                stat = os.stat(path)
            except OSError:
                continue
            digest.update(f"{rel}:{stat.st_size}:{int(stat.st_mtime)}".encode())

    _BASELINE_KEY = digest.hexdigest()[:16]
    return _BASELINE_KEY


def _baseline_cache_path(key):
    return os.path.join(_BASELINE_CACHE_DIR, f"baselines_{key}.json")


def _load_baseline_cache(key):
    try:
        with open(_baseline_cache_path(key), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if payload.get("key") != key:
        return None
    if _BASELINE_MAX_AGE_S and time.time() - float(payload.get("created_at", 0)) > _BASELINE_MAX_AGE_S:
        return None
    if _BASELINE_REFRESH_EVERY and int(payload.get("uses", 0)) >= _BASELINE_REFRESH_EVERY:
        return None
    return payload if payload.get("lines") else None


def _save_baseline_cache(key, lines, uses=0, meta=None):
    try:
        os.makedirs(_BASELINE_CACHE_DIR, exist_ok=True)
        path = _baseline_cache_path(key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {"key": key, "created_at": time.time(), "uses": uses,
                 "lines": lines, "meta": meta or {}},
                handle,
            )
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[cca5] baseline cache write failed: {exc}", flush=True)


def _aggregate_baseline_lines(samples):
    """Reduce repeated baseline measurements to per-(algorithm, trace) medians.

    Returns ``(lines, meta)`` where ``lines`` are CSV rows holding the median of
    each numeric field and ``meta`` records the sample count and standard
    deviation so the score log can show how stable the reference is.
    """
    grouped = {}
    for lines in samples:
        for line in lines:
            fields = line.split(",")
            key = (fields[0].strip(), fields[1].strip())
            grouped.setdefault(key, []).append([float(x) for x in fields[2:]])

    out_lines = []
    meta = {}
    for (algo, trace), values in sorted(grouped.items()):
        n = len(values)
        fields = []
        spreads = []
        for col in range(len(values[0])):
            column = sorted(v[col] for v in values)
            fields.append(statistics.median(column))
            spreads.append(statistics.pstdev(column) if n > 1 else 0.0)
        out_lines.append(",".join(
            [algo, trace] + [f"{fields[0]:.8f}"] + [f"{v:.3f}" for v in fields[1:]]
        ))
        meta[f"{algo}/{trace}"] = {"n": n, "median": fields, "sd": spreads}
    return out_lines, meta


def _expected_trace_count():
    """Number of trace scenarios the replay script will run for _NETWORK."""
    if _NETWORK == "wan":
        return 1
    trace_dir = _cca5("bin", "traces", _NETWORK)
    try:
        names = [n for n in sorted(os.listdir(trace_dir))
                 if os.path.isfile(os.path.join(trace_dir, n))]
    except OSError:
        return 0
    if _NETWORK == "satellite":
        return len([n for n in names if n.endswith(".csv")])
    # cellular: paired traces use *.down as the scenario label
    micro = [n for n in names if n.endswith((".csv", ".down"))]
    return len(micro)


def _count_baseline_traces(content):
    traces = set()
    for line in content.splitlines():
        fields = line.strip().split(",")
        if len(fields) == 7 and fields[0].strip() in _BASELINE_ALGOS:
            traces.add(fields[1].strip())
    return traces


def _extract_baseline_lines(output_csv):
    """Return the well-formed bbr/cubic/copa rows from output.csv."""
    lines = []
    for raw in output_csv.splitlines():
        line = raw.strip()
        if not line or line.startswith("algorithm,"):
            continue
        fields = line.split(",")
        if len(fields) != 7 or fields[0].strip() not in _BASELINE_ALGOS:
            continue
        try:
            for value in fields[2:]:
                float(value)
        except ValueError:
            continue
        lines.append(line)
    return lines


def _scan_output(content):
    """Count data rows, distinct traces and llmgen rows.

    A partial run (some trace or algorithm failed) still yields a score, so the
    counts are surfaced as metrics instead of being treated as fatal.
    """
    rows = 0
    traces = set()
    llmgen_rows = 0
    for row in csv.DictReader(io.StringIO(content.strip())):
        algorithm = (row.get("algorithm") or "").strip()
        trace = (row.get("trace") or "").strip()
        if not algorithm or not trace:
            continue
        rows += 1
        traces.add(trace)
        if algorithm.startswith("llmgen"):
            llmgen_rows += 1
    return rows, len(traces), llmgen_rows


def _fairness_cases_oneline(rows, format_fairness_summary):
    """Render CCA5's worst-case fairness lines as one compact string.

    ``format_fairness_summary`` also prints a header, the averages and a
    diagnosis hint, but each of those is already reported as its own metric
    (``fairness_avg_jain``, ``fairness_total_cases``, ``fairness_diagnosis``).
    Repeating them here only costs prompt space, and this string is echoed in
    the score breakdown of every context program, so it is the worst place to
    spend it. What is *only* here is the attribution: which opponent each bad
    case came from, its share, and the link parameters.

    Returns "" when there is nothing to attribute, so callers can omit the key.
    """
    cases = []
    for line in format_fairness_summary(rows).splitlines():
        line = line.strip()
        if not line.startswith("* "):
            continue
        # Drop the bullet and the run id prefix -- a timestamp followed by the
        # same "algo vs algo" text that follows it.
        cases.append(line[2:].split(": ", 1)[-1])
    return " | ".join(cases)


def _fairness_diagnostics(fairness_content, parse_fairness_logs, format_fairness_summary=None):
    """Summarize fairness evidence without changing the scoring result.

    Two levels of detail, because they answer different questions. The numeric
    ``fairness_min_jain`` / ``fairness_min_share`` fields are what the search
    ranks and displays, but they are taken as independent minima over the rows,
    so they can come from *different* cases and never name an opponent. CCA5's
    own ``format_fairness_summary`` renders the worst cases with the opponent,
    its share and the link parameters -- which is what a candidate needs in
    order to know which of llmgen-vs-{bbr,cubic,copa,llmgen} it is losing.
    """
    rows = parse_fairness_logs(
        fairness_content,
        "llmgen1",
        "cca_generated1.rs",
    )
    if not rows:
        metrics = {
            "fairness_min_jain": 0.0,
            "fairness_avg_jain": 0.0,
            "fairness_min_share": 0.0,
            "fairness_max_share": 0.0,
            "fairness_over_60_count": 0.0,
            "fairness_under_40_count": 0.0,
            "fairness_total_cases": 0.0,
            "fairness_diagnosis": "fairness_evidence_missing",
        }
        return metrics

    jains = [row["jain"] for row in rows]
    shares = [row["target_share"] for row in rows]
    low_jain = any(jain < 0.95 for jain in jains)
    too_aggressive = any(share > 0.60 for share in shares)
    too_conservative = any(share < 0.40 for share in shares)

    if low_jain and too_aggressive and too_conservative:
        diagnosis = "low_jain_and_mixed_share_violation"
    elif low_jain and too_aggressive:
        diagnosis = "low_jain_and_too_aggressive"
    elif low_jain and too_conservative:
        diagnosis = "low_jain_and_too_conservative"
    elif low_jain:
        diagnosis = "low_jain"
    elif too_aggressive:
        diagnosis = "too_aggressive"
    elif too_conservative:
        diagnosis = "too_conservative"
    else:
        diagnosis = "good"

    metrics = {
        "fairness_min_jain": min(jains),
        "fairness_avg_jain": sum(jains) / len(jains),
        "fairness_min_share": min(shares),
        "fairness_max_share": max(shares),
        "fairness_over_60_count": float(sum(share > 0.60 for share in shares)),
        "fairness_under_40_count": float(sum(share < 0.40 for share in shares)),
        "fairness_total_cases": float(len(rows)),
        "fairness_diagnosis": diagnosis,
    }
    if format_fairness_summary is not None:
        cases = _fairness_cases_oneline(rows, format_fairness_summary)
        if cases:
            metrics["fairness_summary"] = cases
    return metrics


# ---------------------------------------------------------------------------
# Measurement steps
# ---------------------------------------------------------------------------


def _install_and_build(cca_dir):
    """Install the candidate as llmgen.rs and compile it. Returns (ok, detail).

    run_evaluation.sh builds too, but it runs without ``set -e``: when cargo
    fails, its ``cp ./target/release/tquic_perf ../bin/tquic_client`` fails as
    well and the script carries on, so the trace run measures whatever binary was
    left over from the previous evaluation. A candidate that does not compile
    would then be scored with its parent's number. Building here first and
    checking the exit code is what closes that hole.
    """
    llmgen_rs = _cca5("tquic", "src", "congestion_control", "llmgen.rs")
    shutil.copyfile(os.path.join(cca_dir, _CANDIDATE_FILENAME), llmgen_rs)

    proc = subprocess.run(
        ["cargo", "build", "--all", "--release"],
        cwd=_cca5("tquic"),
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_S,
    )
    if proc.returncode != 0:
        return False, _tail(proc.stderr)
    return True, ""


def _score(results_dir):
    """Score the measurement artefacts with CCA5's functions.

    Returns ``(metrics, error)``; exactly one of the two is meaningful.
    """
    (
        parse_experiment_logs,
        apply_external_metric_penalties,
        csv_has_data_rows,
        parse_fairness_logs,
        format_fairness_summary,
    ), err = (
        _load_scoring()
    )
    if err:
        return None, err

    output_csv = _read_text(os.path.join(results_dir, "output.csv"))
    fairness_csv = _read_text(os.path.join(results_dir, "fairness.csv"))
    convergence_csv = _read_text(os.path.join(results_dir, "convergence_summary.csv"))

    rows, num_traces, llmgen_rows = _scan_output(output_csv)
    if llmgen_rows == 0:
        return None, "no llmgen rows in output.csv -- every trace run failed"

    # A partial run must not be scored as if it were complete: a candidate that
    # only completes the easy traces would otherwise look better than one that
    # completes all of them.
    expected_traces = _expected_trace_count()
    if expected_traces and llmgen_rows < expected_traces:
        return None, (f"incomplete candidate coverage: {llmgen_rows}/"
                      f"{expected_traces} traces produced a row")
    baseline_traces = _count_baseline_traces(output_csv)
    if expected_traces and len(baseline_traces) < expected_traces:
        return None, (f"incomplete baseline coverage: {len(baseline_traces)}/"
                      f"{expected_traces} traces")

    # Without fairness evidence the score would silently drop the fairness
    # penalty, which is not the CCA5 objective. Treat it as a harness failure so
    # it is visible rather than quietly inflating the candidate's score.
    if not csv_has_data_rows(fairness_csv):
        return None, "fairness suite produced no fairness.csv rows"

    baselines, llm_runs = parse_experiment_logs(output_csv, _APP_TYPE)
    if not llm_runs:
        return None, "parse_experiment_logs produced no llmgen entry"

    apply_external_metric_penalties(
        llm_runs,
        fairness_content=fairness_csv or None,
        convergence_content=convergence_csv or None,
        filename_prefix="cca_generated",
        require_convergence=True,
    )
    run = llm_runs[0]
    fairness_metrics = _fairness_diagnostics(
        fairness_csv, parse_fairness_logs, format_fairness_summary
    )

    metrics = {
        "combined_score": float(run["score"]),
        "base_score": float(run["base_score"]),
        "fairness_penalty": float(run["fairness_penalty"]),
        "convergence_penalty": float(run["convergence_penalty"]),
        "throughput_kbps": float(run["throughput"]),
        "srtt_ms": float(run["srtt"]),
        "min_rtt_ms": float(run["min_rtt"]),
        "rttvar_ms": float(run["rttvar"]),
        "queue_delay_ms": float(run["queue_delay"]),
        "duration_s": float(run["duration"]),
        "runs_successfully": 1.0,
        "num_output_rows": float(rows),
        "num_traces": float(num_traces),
        "num_llmgen_rows": float(llmgen_rows),
        **fairness_metrics,
    }

    # baseline_* metrics: the system prompt tells the model to compare its
    # combined_score against the best baseline score on the same traces, and the
    # hybrid diversity strategy needs numeric spread to tell candidates apart.
    best_baseline = None
    for algorithm, runs in baselines.items():
        if not runs:
            continue
        avg_score = sum(r["score"] for r in runs) / len(runs)
        metrics[f"baseline_{algorithm}_score"] = avg_score
        metrics[f"baseline_{algorithm}_throughput_kbps"] = sum(
            r["throughput"] for r in runs
        ) / len(runs)
        best_baseline = avg_score if best_baseline is None else max(best_baseline, avg_score)

    if best_baseline is not None:
        metrics["best_baseline_score"] = best_baseline
        metrics["score_minus_best_baseline"] = metrics["combined_score"] - best_baseline

    metrics["summary"] = (
        f"llmgen1 score={metrics['combined_score']:.2f} "
        f"(base={metrics['base_score']:.2f} - fairness={metrics['fairness_penalty']:.2f} "
        f"- convergence={metrics['convergence_penalty']:.2f}); "
        f"throughput={metrics['throughput_kbps']:.1f}kbps, "
        f"queue_delay={metrics['queue_delay_ms']:.1f}ms over {num_traces} traces"
        + (
            f"; best_baseline={best_baseline:.2f}, "
            f"delta={metrics['score_minus_best_baseline']:+.2f}"
            if best_baseline is not None
            else ""
        )
    )
    return metrics, None


def _evaluate_locked(source):
    """Run the full CCA5 pipeline for one candidate. Caller holds the lock."""
    work_dir = tempfile.mkdtemp(prefix="cca5_eval_", dir="/tmp")
    keep_work_dir = False
    try:
        cca_dir = os.path.join(work_dir, "cca")
        results_dir = os.path.join(work_dir, "results")
        os.makedirs(cca_dir)
        os.makedirs(results_dir)

        # mkdtemp creates a 0700 directory, but the tquic client runs as
        # `testuser` (run_evaluation.sh wraps it in `su testuser`) and must
        # create its timeseries file under this tree. Without the execute bit
        # the client panics with PermissionDenied on iperf/src/main.rs:681 and
        # every trace silently yields no row. 0711 permits traversal without
        # exposing the directory listing, and cca/ stays closed to other users.
        os.chmod(work_dir, 0o711)
        os.chmod(cca_dir, 0o700)
        os.chmod(results_dir, 0o777)

        with open(os.path.join(cca_dir, _CANDIDATE_FILENAME), "w", encoding="utf-8") as handle:
            handle.write(source)
        baseline_key = _baseline_cache_key()
        cached_baselines = _load_baseline_cache(baseline_key)
        algos = _LLMGEN_ONLY
        output_csv_path = os.path.join(results_dir, "output.csv")
        with open(output_csv_path, "w", encoding="utf-8") as handle:
            handle.write(_OUTPUT_HEADER)
            if cached_baselines:
                for line in cached_baselines["lines"]:
                    handle.write(line + "\n")

        # The build overwrites the shared llmgen.rs; put it back afterwards so the
        # checkout is left as it was found (minus the build artefacts).
        llmgen_rs = _cca5("tquic", "src", "congestion_control", "llmgen.rs")
        backup = os.path.join(work_dir, "llmgen.rs.orig")
        had_llmgen = os.path.exists(llmgen_rs)
        if had_llmgen:
            shutil.copyfile(llmgen_rs, backup)

        try:
            ok, detail = _install_and_build(cca_dir)
            if not ok:
                keep_work_dir = True
                return _failure("candidate does not compile", detail)

            env = dict(os.environ, CCA_DIR=cca_dir, RESULTS_DIR=results_dir)

            # wan/satellite are replayed with tc + network namespaces
            # (run_tc.sh + replay_wan.py), not mahimahi; CCA5's README assigns
            # those networks to run_tc.sh. Its argument order matches
            # run_evaluation.sh's, so only the script name changes.
            trace_script = (
                "run_tc.sh" if _NETWORK in ("wan", "satellite") else "run_evaluation.sh"
            )
            if not cached_baselines:
                samples = []
                for rep in range(_BASELINE_REPEATS):
                    rep_dir = os.path.join(work_dir, f"baseline_{rep}")
                    os.makedirs(rep_dir, exist_ok=True)
                    os.chmod(rep_dir, 0o777)
                    rep_env = dict(os.environ, CCA_DIR=cca_dir, RESULTS_DIR=rep_dir)
                    _run(
                        [
                            "bash",
                            _cca5(trace_script),
                            _CANDIDATE_FILENAME,
                            " ".join(_BASELINE_ALGOS),
                            _APP_TYPE,
                            _NETWORK,
                            _DATA_SIZE,
                            _DURATION,
                        ],
                        env=rep_env,
                        timeout=_TRACE_TIMEOUT_S,
                        label=f"baseline replay {rep + 1}/{_BASELINE_REPEATS}",
                    )
                    samples.append(_extract_baseline_lines(
                        _read_text(os.path.join(rep_dir, "output.csv"))
                    ))

                median_lines, meta = _aggregate_baseline_lines(samples)
                if not median_lines:
                    return _failure("baseline measurement produced no rows",
                                    f"{len(samples)} repeats, all empty")
                with open(output_csv_path, "a", encoding="utf-8") as handle:
                    for line in median_lines:
                        handle.write(line + "\n")
                _save_baseline_cache(baseline_key, median_lines, uses=0, meta=meta)
                print(
                    f"[cca5] baselines: measured {_BASELINE_REPEATS}x, pinned median "
                    f"({len(median_lines)} rows)", flush=True,
                )

            trace_run = _run(
                [
                    "bash",
                    _cca5(trace_script),
                    _CANDIDATE_FILENAME,
                    algos,
                    _APP_TYPE,
                    _NETWORK,
                    _DATA_SIZE,
                    _DURATION,
                ],
                env=env,
                timeout=_TRACE_TIMEOUT_S,
                label="single-flow trace replay",
            )

            if cached_baselines:
                cached_baselines["uses"] = int(cached_baselines.get("uses", 0)) + 1
                _save_baseline_cache(
                    baseline_key, cached_baselines["lines"],
                    uses=cached_baselines["uses"], meta=cached_baselines.get("meta"),
                )
                print(
                    f"[cca5] baselines: pinned library reused "
                    f"({len(cached_baselines['lines'])} rows)", flush=True,
                )

            # Reuse the binaries the trace run just built.
            fairness_run = _run(
                [
                    "bash",
                    _cca5("run_fairness_suite.sh"),
                    _CANDIDATE_FILENAME,
                    _APP_TYPE,
                    "fairness",
                    _DATA_SIZE,
                    _FAIRNESS_DURATION,
                ],
                # The suite forwards FAIRNESS_SKIP_BUILD to run_fairness.sh; a bare
                # SKIP_BUILD would be overwritten by the suite's own default.
                env=dict(env, FAIRNESS_SKIP_BUILD="1"),
                timeout=_FAIRNESS_TIMEOUT_S,
                label="fairness suite",
            )
        finally:
            if had_llmgen:
                shutil.copyfile(backup, llmgen_rs)

        try:
            metrics, error = _score(results_dir)
        except Exception:
            # A crash while parsing/measuring is exactly when the artefacts are
            # needed for diagnosis (e.g. the output.csv row that broke the
            # parser), so keep them instead of wiping the temp tree.
            keep_work_dir = True
            raise
        if error:
            keep_work_dir = True
            return _failure(
                error,
                _diagnostics(results_dir, trace_run, fairness_run) + f"\nartefacts: {work_dir}",
            )

        print(f"[cca5] {metrics['summary']}", flush=True)
        return metrics

    finally:
        if keep_work_dir:
            print(f"[cca5] failure artefacts kept at {work_dir}", flush=True)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate(program_path):
    """Evaluate one candidate and return its metrics.

    Args:
        program_path: Path to the evolved Rust program.

    Returns:
        Dict of numeric metrics including the required ``combined_score``.
    """
    if _CCA5_ROOT is None:
        return _failure(
            "CCA5 checkout not found -- set CCA5_ROOT to the directory "
            "containing run_evaluation.sh"
        )

    try:
        with open(program_path, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError as exc:
        return _failure(f"cannot read candidate program: {exc}")

    if not source.strip():
        return _failure("candidate program is empty")

    try:
        with _serialized():
            return _evaluate_locked(source)
    except subprocess.TimeoutExpired as exc:
        return _failure(f"evaluation step timed out: {exc}")
    except Exception as exc:
        print(traceback.format_exc(), file=sys.stderr)
        return _failure(f"evaluation crashed: {exc}", traceback.format_exc())


if __name__ == "__main__":
    # Backwards-compat: bridges old evaluate() -> dict to the container JSON
    # protocol.  wrapper.py is auto-injected at build time from
    # skydiscover/evaluation/wrapper.py.
    from wrapper import run

    run(evaluate)
