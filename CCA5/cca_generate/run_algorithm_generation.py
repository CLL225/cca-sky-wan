import os
import re
import argparse
from openai import OpenAI
from Config import Config

from mutate_algorithm import summarize_telemetry
from repair_utils import (
    compile_check_candidate,
    extract_rust_code,
    strip_code_blocks,
    trim_compiler_feedback,
)

def build_prompt(app_type, network_type, strategy, prior_code="", compiler_feedback=""):
    network_hints = {
        "cellular": (
            "- Cellular links have rapidly changing bandwidth, deep buffers, and random radio loss.\n"
            "- Do not cut cwnd on isolated loss when queueing delay stays low.\n"
            "- Prefer mechanisms that can re-expand quickly after non-congestion loss."
        ),
        "satellite": (
            "- LEO satellite links have high BDP and occasional handover loss.\n"
            "- Distinguish persistent queue growth from brief handover events.\n"
            "- Avoid overreacting to short disruption spikes that are not backed by sustained delay."
        ),
        "wan": (
            "- WAN paths are comparatively stable but still see occasional random drops.\n"
            "- Use delay trend, delivery-rate trend, and inflight pressure together instead of loss alone.\n"
            "- Keep probing bounded so long-lived standing queues do not dominate the path."
        ),
    }

    app_hints = {
        "stream": (
            "- Primary utility is sustained throughput and link utilization.\n"
            "- Queue growth, jitter, unfair coexistence, and slow convergence still hurt the final score."
        ),
        "rpc": (
            "- Primary utility is finishing work quickly without building queue.\n"
            "- Favor fast ramp-up for bursts, quick stabilization, and low queueing delay."
        ),
        "video": (
            "- Primary utility is reaching and holding a practical bitrate with stable delay.\n"
            "- Extra throughput above the useful rate does not justify bufferbloat or jitter."
        ),
    }

    strategy_hints = {
        "zero-shot": "",
        "one-shot": """
Strategy guidance:
- Pick one coherent primary control law and at most one secondary stabilizer.
- Valid directions include delay-targeted control, delivery-rate trend control, utility-based target inflight control, bounded epoch or phase control, or a hybrid that switches cleanly between startup, steady state, and recovery.
- BBR, Copa, and CUBIC are baselines to beat, not templates to copy mechanically.
- Prefer a small number of state variables with clear invariants over a large ad hoc state machine.
""".strip(),
        "few-shot": """
Compact mechanism examples:
1. Delay-gated growth:
   - Increase cwnd while queue_delay is below a budget and delivery rate is still improving.
   - If queue_delay stays above budget for several ACK rounds, trim cwnd and hold neutral pacing until the queue clears.
2. Utility target inflight:
   - Per RTT epoch, estimate utility from useful throughput minus queue cost.
   - Raise or lower a target inflight or target cwnd using only the utility trend and local samples.
3. Competition-aware probing:
   - Probe briefly when delay is low and rate is rising.
   - End probing when rate plateaus, inflight stays near cwnd, or queue builds; then return to neutral or drain.
""".strip(),
        "cot": """
Reasoning structure:
- First identify the dominant failure mode this network and app combination is likely to create.
- Then choose the control signals, the steady-state objective, the fairness brake, and the recovery behavior.
- Keep the explanation concise and focused on observable signals and update rules.
""".strip(),
    }

    rust_contract = """
Rust contract:
You must implement a full Rust file that defines `LLMGen` and implements `CongestionController`.
Do not return pseudocode or partial snippets.

```rust
// Copyright (c) 2024 The TQUIC Authors.
#![allow(unused_variables)]

use std::time::{Duration, Instant};
use super::CongestionController;
use super::CongestionStats;
use crate::connection::rtt::RttEstimator;
use crate::connection::space::SentPacket;
use crate::RecoveryConfig;

#[derive(Debug)]
pub struct LLMGenConfig {
    pub initial_congestion_window: u64,
    pub min_congestion_window: u64,
    pub max_datagram_size: u64,
    pub initial_rtt: Duration,
}

impl LLMGenConfig {
    pub fn from(conf: &RecoveryConfig) -> Self {
        let max_datagram_size = conf.max_datagram_size as u64;
        Self {
            initial_congestion_window: conf
                .initial_congestion_window
                .saturating_mul(max_datagram_size),
            min_congestion_window: conf.min_congestion_window.saturating_mul(max_datagram_size),
            max_datagram_size,
            initial_rtt: conf.initial_rtt,
        }
    }
}

impl Default for LLMGenConfig {
    fn default() -> Self {
        Self {
            initial_congestion_window: 10 * 1200,
            min_congestion_window: 2 * 1200,
            max_datagram_size: 1200,
            initial_rtt: Duration::from_millis(333),
        }
    }
}

#[derive(Debug)]
pub struct LLMGen {
    pub initial_cwnd: u64,
    pub cwnd: u64,
    pub min_cwnd: u64,
    pub max_datagram_size: u64,
    pub stats: CongestionStats,
    // add custom state here
}

impl LLMGen {
    pub fn new(conf: LLMGenConfig) -> Self {
        let min_cwnd = conf.min_congestion_window.max(2 * conf.max_datagram_size);
        let initial_cwnd = conf.initial_congestion_window.max(min_cwnd);
        Self {
            initial_cwnd,
            cwnd: initial_cwnd,
            min_cwnd,
            max_datagram_size: conf.max_datagram_size,
            stats: Default::default(),
        }
    }
}

impl CongestionController for LLMGen {
    fn name(&self) -> &str { "LLMGen" }
    fn on_sent(&mut self, now: Instant, packet: &mut SentPacket, bytes_in_flight: u64) {}
    fn begin_ack(&mut self, now: Instant, bytes_in_flight: u64) {}
    fn on_ack(&mut self, packet: &mut SentPacket, now: Instant, app_limited: bool, rtt: &RttEstimator, bytes_in_flight: u64) {}
    fn end_ack(&mut self) {}
    fn on_congestion_event(&mut self, now: Instant, packet: &SentPacket, is_persistent_congestion: bool, lost_bytes: u64, bytes_in_flight: u64) {}
    fn in_slow_start(&self) -> bool { true }
    fn in_recovery(&self, sent_time: Instant) -> bool { false }
    fn congestion_window(&self) -> u64 { self.cwnd }
    fn pacing_rate(&self) -> Option<u64> { None }
    fn initial_window(&self) -> u64 { self.initial_cwnd }
    fn minimal_window(&self) -> u64 { self.min_cwnd }
    fn stats(&self) -> &CongestionStats { &self.stats }
    fn min_pacing_rate(&self) -> Option<u64> { None }
}
```
""".strip()

    implementation_rules = """
Implementation rules:
1. Update statistics correctly:
   - `bytes_sent_in_total` in `on_sent`
   - `bytes_acked_in_total` in `on_ack`
   - `bytes_lost_in_total` in `on_congestion_event`
   - `bytes_in_flight` consistently across send, ack, and loss paths
2. Use `packet.sent_size as u64` for byte accounting.
3. Use `rtt.smoothed_rtt()`, `rtt.latest_rtt()`, and `rtt.min_rtt()` as your RTT signals.
4. Use saturating arithmetic where growth or subtraction could overflow or underflow. There is no `saturating_div`.
5. Pacing must use floating-point time conversion or exact microsecond math to avoid zero truncation:
   `let pacing = (self.cwnd as f64 / rtt.smoothed_rtt().as_secs_f64()) as u64;`
6. Never let `cwnd` fall below `self.minimal_window()`.
7. On persistent congestion, force the controller back to the minimum window.
8. `initial_window()` and `minimal_window()` must return byte windows derived from config, not fixed packet counts.
9. Keep the code deterministic, self-contained, and compatible with TQUIC's trait interface.
""".strip()

    search_objective = f"""
You are designing a QUIC congestion control algorithm for `tquic` in Rust.
The goal is to beat strong baselines such as BBR, Copa, and CUBIC under the repository's score for a {app_type.upper()} application on a {network_type.upper()} network.

This score is not raw throughput only.
- Single-flow utility rewards application-appropriate performance.
- Fairness penalties punish poor two-flow coexistence.
- Convergence penalties punish slow or failed runtime convergence.

Design for actual measured behavior, not for looking similar to a named baseline.
""".strip()

    telemetry_context = summarize_telemetry(
        app_type=app_type,
        filename_prefix="cca_generated",
        target_num=None,
    )

    fairness_and_convergence = """
Fairness and convergence requirements:
- The algorithm will be tested both alone and against another flow on the same bottleneck.
- Target Jain fairness index >= 0.95.
- Target a long-run share near 40%-60% under two-flow competition.
- Use only local sender-side signals: queueing delay, delivery-rate trend, bytes_in_flight pressure, app-limited state, and the pattern of losses.
- If you probe, make probing bounded and reversible.
- Avoid permanent high gain, unbounded startup, or a controller that keeps queue high after rate stops improving.
- Favor fast, stable convergence to a useful operating point rather than a long oscillatory search.
""".strip()

    anti_anchor = """
Mechanism policy:
- You may borrow ideas from known CCAs, but do not submit a light rename or small retune of BBR, Copa, or CUBIC.
- It is acceptable to use a novel hybrid if its update rules are internally consistent.
- Choose the mechanism because the local signals justify it, not because a baseline name is familiar.
""".strip()

    output_format = """
Reply format:
1. `Design summary:` with 4-8 short bullets describing the control law, fairness mechanism, loss handling, and convergence behavior.
2. One complete Rust code block containing the final implementation.

Do not include pseudocode, placeholder comments like "implement later", or any extra code blocks.
""".strip()

    if strategy == "cot":
        output_format = """
Reply format:
1. `Diagnosis:` with short bullets for congestion signal, fairness mechanism, loss classification, and convergence behavior.
2. `Design summary:` with short bullets for the final control law.
3. One complete Rust code block containing the final implementation.

Do not include hidden reasoning, pseudocode, or partial code.
""".strip()

    repair_sections = []
    if compiler_feedback:
        repair_sections.append(
            "Compiler feedback to fix first:\n"
            + compiler_feedback
        )
    if prior_code:
        repair_sections.append(
            "Current candidate code to repair or refine:\n```rust\n"
            + prior_code
            + "\n```"
        )
        repair_sections.append(
            "If compiler feedback is present, fix those Rust errors first and preserve the intended congestion-control behavior."
        )

    sections = [
        search_objective,
        "Historical telemetry summary:\n" + telemetry_context,
        "Network context:\n" + network_hints[network_type],
        "Application priorities:\n" + app_hints[app_type],
        fairness_and_convergence,
        anti_anchor,
        strategy_hints.get(strategy, ""),
        rust_contract,
        implementation_rules,
        "\n\n".join(repair_sections) if repair_sections else "",
        output_format,
    ]

    return "\n\n".join(section for section in sections if section)

def get_unique_filename(base_name, ext):
    filename = f"{base_name}1{ext}"
    counter = 2
    while os.path.exists(filename):
        filename = f"{base_name}{counter}{ext}"
        counter += 1
    return filename, counter - 1

def generate_and_save_cca():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=str, choices=["stream", "rpc", "video"], default="stream", help="Target application type")
    parser.add_argument("--network", type=str, choices=["wan", "cellular", "satellite"], default="cellular", help="Target network scenario")
    parser.add_argument("--strategy", type=str, choices=["zero-shot", "one-shot", "few-shot", "cot"], default="one-shot", help="Prompting strategy for initial generation")
    parser.add_argument("--repair-attempts", type=int, default=int(os.environ.get("LLMGEN_REPAIR_ATTEMPTS", "2")), help="Maximum compiler-repair attempts")

    args = parser.parse_args()
    config = Config()
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    unique_fname, num = get_unique_filename("../congestion_control_algorithm/cca_generated", ".rs")
    log_filename = f"../logs/cca_optimization_log{num}.md"

    prompt = build_prompt(args.app, args.network, args.strategy)

    print(f"正在向大模型发送请求以生成 CC 算法 (App: {args.app}, Network: {args.network}, Strategy: {args.strategy})...")
    try:
        response = client.chat.completions.create(
            model=config.model_name,
            messages=[
                {"role": "system", "content": "You are a senior networking engineer and Rust developer specializing in QUIC protocol implementations."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2, 
        )
    except Exception as e:
        print(f"调用 API 失败: {e}")
        return 1

    content = response.choices[0].message.content
    print("生成完成！正在提取代码并执行编译检查...")

    rust_code = extract_rust_code(content)
    analysis_content = strip_code_blocks(content)
    if not rust_code:
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"⚠️ 未能严格按照 ```rust 代码块格式提取到代码，完整输出已保存至: {log_filename}")
        return 1

    current_code = rust_code
    current_analysis = analysis_content

    for attempt in range(args.repair_attempts + 1):
        with open(unique_fname, "w", encoding="utf-8") as f:
            f.write(current_code.strip() + "\n")
        with open(log_filename, "w", encoding="utf-8") as f:
            f.write(current_analysis.strip() + "\n")

        compile_log_filename = f"../logs/cca_compile_repair{num}_attempt{attempt + 1}.log"
        print(f"开始编译检查: attempt {attempt + 1}/{args.repair_attempts + 1}")
        compile_ok, compile_output = compile_check_candidate(unique_fname, compile_log_filename)
        if compile_ok:
            print(f"✅ 编译检查通过: {unique_fname}")
            print(f"完整对话日志已保存至: cca_optimization_log{num}.md")
            print(f"编译日志已保存至: cca_compile_repair{num}_attempt{attempt + 1}.log")
            return 0

        print(f"⚠️ 编译检查失败，日志已保存至: cca_compile_repair{num}_attempt{attempt + 1}.log")
        if attempt >= args.repair_attempts:
            print("❌ 已耗尽自动纠错次数，未生成可编译算法。")
            return 1

        repair_prompt = build_prompt(
            args.app,
            args.network,
            args.strategy,
            prior_code=current_code,
            compiler_feedback=trim_compiler_feedback(compile_output),
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
    raise SystemExit(generate_and_save_cca())
