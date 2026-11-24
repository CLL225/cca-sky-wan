// Copyright (c) 2023 The TQUIC Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// EVOLVE-BLOCK-START
//! LLMGen: an evolving delay-based congestion controller.
//!
//! This module is compiled as `tquic/src/congestion_control/llmgen.rs` and is the
//! single file the evolution loop rewrites. It starts from tquic's Copa
//! implementation -- a delay-threshold controller that infers the buffer-filling
//! state from the RTT evolution and moves the congestion window additively -- so
//! the search begins from a working, low-queueing-delay controller rather than a
//! placeholder.
//!
//! NOTE: `copa_slow_start_delta`, `copa_steady_delta` and `copa_use_standing_rtt`
//! are fields of `RecoveryConfig` filled in from the runtime configuration, so
//! their VALUES cannot be changed from this file. Use them as inputs, or ignore
//! them and derive your own parameters.
//!
//! Everything between the EVOLVE-BLOCK markers is fair game. The public interface
//! must stay: the type names `LLMGen` / `LLMGenConfig` and the
//! `impl CongestionController for LLMGen`.
use std::time::Duration;
use std::time::Instant;

use log::*;

use super::minmax::MinMax;
use super::CongestionController;
use super::CongestionStats;
use crate::connection::rtt::RttEstimator;
use crate::connection::space::RateSamplePacketState;
use crate::connection::space::SentPacket;
use crate::RecoveryConfig;

/// Delta: determines how much to weigh delay compared to throughput.
pub const DEFAULT_DELTA: f64 = 0.04;

/// Max count while cwnd grows with the same direction. Speed up if
/// the count exceeds threshold.
const SPEED_UP_THRESHOLD: u64 = 3;

/// Default standing rtt filter length.
const STANDING_RTT_FILTER_WINDOW: Duration = Duration::from_millis(100);

/// Default min rtt filter length.
const MIN_RTT_FILTER_WINDOW: Duration = Duration::from_secs(10);

/// Pacing gain to cope with ack compression.
const PACING_GAIN: u64 = 2;

/// Delay oscillation rate to check if queueing delay is nearly empty:
/// queueing_delay < 0.1 * (Rtt_max - Rtt_min)
/// Where Rtt_max and Rtt_min is the max and min RTT in the last 4 rounds.
const DELAY_OSCILLATION_THRESHOLD: f64 = 0.1;

/// Max loss rate in one round. If the loss rate exceeds the threshold, switch
/// the mode to competitive mode.
const LOSS_RATE_THRESHOLD: f64 = 0.1;

/// Window over which the maximum delivery rate is tracked. Kept short so the
/// estimate tracks the current bottleneck instead of retaining startup or
/// bandwidth-spike peaks that inflate cwnd and pacing beyond the link rate.
const BW_WINDOW: Duration = Duration::from_millis(600);

/// Minimum elapsed time between delivery-rate samples.
const RATE_SAMPLE_INTERVAL: Duration = Duration::from_millis(10);

/// Startup pacing gain applied to the delivery-rate estimate.
const STARTUP_PACING_GAIN: f64 = 2.2;

/// Consecutive rounds without >=25% bandwidth growth that end startup.
const FULL_BW_CNT: u64 = 3;

/// Growth factor that renews the startup bandwidth-plateau detector.
const FULL_BW_GROWTH: f64 = 1.25;

/// Startup exits when the queueing delay reaches this fraction of min_rtt.
const STARTUP_QUEUE_EXIT_RATIO: f64 = 0.5;

/// Ratio of the fresh delivered rate to the recent peak below which the flow is
/// considered to be squeezed by a competing buffer-filling sender.
const SQUEEZE_RATIO: f64 = 0.72;

/// Consecutive squeezed rounds that flag a competing flow.
const SQUEEZE_ROUNDS: u64 = 2;

/// Rounds for which the bandwidth estimate is frozen after a delivery-rate
/// squeeze latches. While a competitor owns the bottleneck queue our own
/// delivery rate collapses, so an unfrozen windowed-maximum estimate decays
/// toward the starved rate and the flow can never push back -- the freeze keeps
/// BDP sizing and pacing stable long enough for the coexistence brake to
/// reclaim the fair share. It expires so a genuine bandwidth reduction (e.g.
/// cellular fading) is still tracked within a few tenths of a second.
const SQUEEZE_HOLD_ROUNDS: u64 = 16;

/// Queueing-delay ratio above which a sustained delivery-rate squeeze is
/// attributed to a buffer-filling peer: it fills the buffer while our own rate
/// is degraded, so the queue is not ours. Below this the peer is delay-sensitive
/// (e.g. Copa) reclaiming its fair share without building a queue, so we stay
/// polite and do NOT fight it -- this is what keeps coexistence fair vs Copa.
const COMPETITIVE_QMIN: f64 = 0.50;

/// Upper queueing-delay ratio at which the competitive boost stops overriding
/// the drain path. Above this the standing queue is so deep that maintaining
/// inflight to claim share costs more in queueing-delay penalty than the share
/// it recovers: the drain path takes over and the boost resumes only once the
/// queue has been worked back below this bound.
const COMPETITIVE_QMAX: f64 = 2.5;

/// Target-inflight gain forced while a competitor is detected. Kept modest so
/// the boost claims a fair share against a buffer-filling peer without
/// overrunning a delay-sensitive peer (Copa) that yields its share politely.
const COMPETITIVE_GAIN: f64 = 1.30;

/// Pacing gain forced while a competitor is detected.
const COMPETITIVE_PACING_GAIN: f64 = 1.15;

/// Queueing-delay ratio above which a sustained, unsqueezed standing queue is
/// evidence that a delay-based peer is yielding to our buffer.
const POLITE_QMIN: f64 = 0.45;

/// Consecutive unsqueezed rounds at `POLITE_QMIN` or deeper that arm the
/// coexistence brake.
const POLITE_ROUNDS: u64 = 3;

/// Target-inflight gain forced while the coexistence brake is armed: below 1.0
/// so we actively create room for a yielding peer instead of merely holding
/// the BDP.
const POLITE_GAIN: f64 = 0.90;

/// Pacing gain forced while the coexistence brake is armed.
const POLITE_PACE: f64 = 0.95;

/// Lower bound of the target-inflight gain. Slightly below 1.0 lets the drain
/// path actively create room for a yielding peer instead of merely holding the
/// BDP; this is what fairness against a delay-sensitive competitor (Copa)
/// requires, since our `bw_est` reflects an unfairly large share.
const GAIN_MIN: f64 = 0.95;

/// Upper bound of the (non-competitive) target-inflight gain. Kept modest so a
/// polite delay-based peer is not overrun while still saturating the BDP. A gain
/// of 1.20 caps the self-induced standing queue at ~20% of min_rtt, yet still
/// delivers the full bottleneck rate: cwnd = gain*BDP, so throughput is bounded
/// by the bottleneck and not by the gain (queue = (gain-1)*min_rtt in steady
/// state, but inflight/rtt = bandwidth regardless).
const GAIN_MAX: f64 = 1.20;

/// Initial target-inflight gain.
const GAIN_INIT: f64 = 1.05;

/// Per-round gain increment while the queue is low.
const GAIN_STEP_UP: f64 = 0.04;

/// Per-round gain decrement while the queue is high.
const GAIN_STEP_DOWN: f64 = 0.08;

/// Queueing-delay ratio (of min_rtt) above which the gain is drained. Measured
/// against the smoothed RTT (the real standing queue), so this now actively
/// bounds the queue we induce. Kept low so a standing queue is removed early,
/// which both lowers the single-flow queueing delay/jitter and yields bandwidth
/// to a delay-based peer (Copa).
const QUEUE_HIGH_RATIO: f64 = 0.25;

/// Minimum damping factor applied to the bandwidth estimate at extreme queue
/// depths. Prevents the flow from starving even when the standing queue is
/// several times min_rtt.
const BW_DAMP_MIN: f64 = 0.35;

/// Queueing-delay ratio (of min_rtt) below which the gain probes upward.
const QUEUE_LOW_RATIO: f64 = 0.08;

/// Pacing gain used to probe for more bandwidth when the queue is low.
const PACING_PROBE_GAIN: f64 = 1.06;

/// Pacing gain used to drain an over-full queue.
const PACING_DRAIN_GAIN: f64 = 0.90;

/// Pacing gain used while the queue sits inside the target band. Pacing at the
/// measured delivery rate (no headroom above it) avoids grabbing more than our
/// fair share when a competitor is present.
const PACING_CRUISE_GAIN: f64 = 1.00;

/// Slew-rate limit applied to the emitted pacing rate on each control tick, as a
/// fraction of the previously emitted rate. Bounds round-to-round rate steps.
const PACING_SLEW_FRAC: f64 = 0.25;

/// EWMA weight kept on the previous emitted pacing rate when low-passing the
/// slew-limited target: `new = a*prev + (1-a)*clamped`.
const PACING_EWMA_ALPHA: f64 = 0.60;

/// EWMA weight kept on the previous effective pacing gain so probe / cruise /
/// drain transitions are gradual instead of bang-bang.
const PACING_GAIN_EWMA_ALPHA: f64 = 0.20;

/// Per-round slew bound applied to the congestion window, as a fraction of the
/// window at the start of the round.
const CWND_SLEW_FRAC: f64 = 0.25;

/// Number of round-boundary `(time_s, qratio)` samples kept for the derivative
/// (trend) term. Four rounds spans the timescale on which a standing queue
/// builds or drains on a ~50 ms path without retaining startup-era history.
const TREND_HIST_LEN: usize = 4;

/// Round-to-round qratio slope above which the standing queue is considered to
/// be rising. A genuine build-up moves the ratio by a few hundredths per round;
/// ACK-compression jitter does not sustain this for two consecutive rounds.
const TREND_RISE_SLOPE: f64 = 0.03;

/// Round-to-round qratio slope below which the standing queue is considered to
/// be falling.
const TREND_FALL_SLOPE: f64 = 0.02;

/// Steeper rising slope at which the preemptive drain also fires *above*
/// `QUEUE_HIGH_RATIO`: a fast climb can jump the entire deadband in a single
/// round, so reacting only to the level would be one round too late.
const TREND_STEEP_SLOPE: f64 = 0.15;

/// Upper qratio bound of the steep-rise drain band. Above this the queue is
/// already deep enough that the level-based deadband (and, when a competitor
/// owns it, the coexistence branches) is the right instrument.
const TREND_DRAIN_QMAX: f64 = 1.0;

/// Consecutive same-sign slope samples required before the trend term acts, so
/// a single noisy sample can neither arm a drain nor arm a probe.
const TREND_SAME_SIGN_ROUNDS: u64 = 2;

/// Lower qratio bound of the preemptive-drain band: below this the queue is
/// effectively empty and there is nothing to preempt.
const TREND_DRAIN_QMIN: f64 = 0.10;

/// Pacing gain forced by the preemptive drain while the queue is rising inside
/// the deadband. Bounded well above the 0.85 floor so utilization is preserved.
const TREND_DRAIN_PACE: f64 = 0.95;

/// Upper qratio bound for the trend-gated probe: only an essentially empty
/// queue earns the extra headroom.
const TREND_PROBE_QMAX: f64 = 0.05;

/// One-round pacing gain allowed by the trend-gated probe on a falling, empty,
/// unsqueezed queue. Higher than `PACING_PROBE_GAIN` so a cellular bandwidth
/// upswing is captured quickly; the queue feedback claws the overshoot back on
/// the following rounds.
const TREND_PROBE_PACE: f64 = 1.13;

/// Rounds after startup exit during which the trend term is disabled. The first
/// steady-state samples still carry startup overshoot, which would otherwise
/// read as a steep queue trend and drive a spurious drain or probe.
const TREND_WARMUP_ROUNDS: u64 = 4;

/// LLMGen configurable parameters.
#[derive(Debug)]
pub struct LLMGenConfig {
    /// Minimal congestion window in bytes.
    min_cwnd: u64,

    /// Initial congestion window in bytes.
    initial_cwnd: u64,

    /// Initial Smoothed rtt.
    initial_rtt: Option<Duration>,

    /// Max datagram size in bytes.
    max_datagram_size: u64,

    /// Delta in slow start. Delta determines how much to weigh delay compared to
    /// throughput. A larger delta signifies that lower packet delays are preferable.
    slow_start_delta: f64,

    /// Delta in steady state.
    steady_delta: f64,

    /// Use rtt standing or latest rtt to calculate queueing delay.
    use_standing_rtt: bool,
}

impl LLMGenConfig {
    pub fn from(conf: &RecoveryConfig) -> Self {
        let max_datagram_size = conf.max_datagram_size as u64;
        let min_cwnd = conf.min_congestion_window.saturating_mul(max_datagram_size);
        let initial_cwnd = conf
            .initial_congestion_window
            .saturating_mul(max_datagram_size);
        let initial_rtt = Some(conf.initial_rtt);

        Self {
            min_cwnd,
            initial_cwnd,
            initial_rtt,
            max_datagram_size,
            slow_start_delta: conf.copa_slow_start_delta,
            steady_delta: conf.copa_steady_delta,
            use_standing_rtt: conf.copa_use_standing_rtt,
        }
    }
}

impl Default for LLMGenConfig {
    fn default() -> Self {
        Self {
            min_cwnd: 4 * crate::DEFAULT_SEND_UDP_PAYLOAD_SIZE as u64,
            initial_cwnd: 80 * crate::DEFAULT_SEND_UDP_PAYLOAD_SIZE as u64,
            initial_rtt: Some(crate::INITIAL_RTT),
            max_datagram_size: crate::DEFAULT_SEND_UDP_PAYLOAD_SIZE as u64,
            slow_start_delta: DEFAULT_DELTA,
            steady_delta: DEFAULT_DELTA,
            use_standing_rtt: true,
        }
    }
}

/// LLMGen congestion window growth direction.
#[derive(Eq, PartialEq, Debug)]
enum Direction {
    /// Cwnd increasing.
    Up,

    /// Cwnd decreasing.
    Down,
}

/// LLMGen competing mode with other flows.
#[derive(Eq, PartialEq, Debug)]
enum CompetingMode {
    /// Default mode, no competing flows.
    Default,

    /// Competitive mode, use adaptive delta.
    Competitive,
}

/// Velocity control states.
#[derive(Debug)]
struct Velocity {
    /// Cwnd growth direction.
    direction: Direction,

    /// Velocity coef.
    velocity: u64,

    /// Cwnd recorded at last time.
    last_cwnd: u64,

    /// Times while cwnd grows with the same direction. Speed up if cnt
    /// exceeds threshold.
    same_direction_cnt: u64,
}

impl Default for Velocity {
    fn default() -> Self {
        Self {
            direction: Direction::Up,
            velocity: 1,
            last_cwnd: 0,
            same_direction_cnt: 0,
        }
    }
}

/// Accumulate information from a single ACK/SACK.
#[derive(Debug)]
struct AckState {
    /// Ack time.
    now: Instant,

    /// Newly marked lost data size in bytes.
    newly_lost_bytes: u64,

    /// Newly acked data size in bytes.
    newly_acked_bytes: u64,

    /// Largest acked packet number.
    largest_acked_pkt_num: u64,

    /// Minimum rtt in this ACK packet.
    min_rtt: Duration,

    /// The last smoothed rtt in the current ACK packet.
    last_srtt: Duration,
}

impl Default for AckState {
    fn default() -> Self {
        let now = Instant::now();
        Self {
            now,
            newly_lost_bytes: 0,
            newly_acked_bytes: 0,
            largest_acked_pkt_num: 0,
            min_rtt: Duration::ZERO,
            last_srtt: Duration::ZERO,
        }
    }
}

/// Round trip counter for tracking packet-timed round trips which starts
/// at the transmission of some segment, and then ends at the ack of that segment.
#[derive(Debug, Default)]
struct RoundTripCounter {
    /// Count of packet-timed round trips.
    pub round_count: u64,

    /// A boolean that BBR sets to true once per packet-
    /// timed round trip, on ACKs that advance BBR.round_count.
    pub is_round_start: bool,

    /// Packet number denoting the end of
    /// a packet-timed round trip.
    pub round_end_pkt_num: u64,

    /// Total acked bytes when a new round starts.
    pub last_total_acked_bytes: u64,

    /// Total lost bytes when a new round starts.
    pub last_total_lost_bytes: u64,

    /// Lost rate in this round, calculated as `lost / (lost + acked)`.
    pub loss_rate: f64,
}

/// LLMGen: Practical Delay-Based Congestion Control for the Internet.
///
/// See <https://web.mit.edu/llmgen/>.
#[derive(Debug)]
pub struct LLMGen {
    /// Config
    config: LLMGenConfig,

    /// Statistics.
    stats: CongestionStats,

    /// The time origin point when LLMGen init, used for window filter updating.
    init_time: Instant,

    /// Competing mode.
    mode: CompetingMode,

    /// Is in slow start state.
    slow_start: bool,

    /// Congestion window/
    cwnd: u64,

    /// Velocity parameter, speeds-up convergence.
    velocity: Velocity,

    /// Weight factor for queueing delay. Use default value in default mode,
    /// and use an adaptive one in competitive mode.
    delta: f64,

    /// Minimum rtt in time window tau (srtt/2).
    standing_rtt_filter: MinMax,

    /// Minimum rtt in the last time period, e.g. 10s for default.
    min_rtt_filter: MinMax,

    /// Ack state in the current round.
    ack_state: AckState,

    /// Whether cwnd should be increased.
    increase_cwnd: bool,

    /// Target pacing rate.
    target_rate: u64,

    /// The last sent packet number.
    last_sent_pkt_num: u64,

    /// Round trip counter.
    round: RoundTripCounter,

    /// Min pacing rate
    min_pacing_rate: Option<u64>,

    /// Windowed-maximum delivery-rate estimate in bytes per second.
    bw_est: u64,

    /// Recent `(time_us, rate)` samples backing the max delivery-rate filter.
    bw_samples: Vec<(u64, u64)>,

    /// Total acked bytes recorded at the last delivery-rate sample.
    rate_last_acked: u64,

    /// Timestamp of the last delivery-rate sample.
    rate_last_time: Instant,

    /// Freshest delivery-rate sample in bytes per second.
    rate_now: u64,

    /// Minimum RTT observed, in microseconds.
    min_rtt_us: u64,

    /// Delivery rate recorded when the startup plateau detector last renewed.
    full_bw: u64,

    /// Consecutive rounds without >=25% bandwidth growth during startup.
    full_bw_count: u64,

    /// Target-inflight gain applied to the estimated BDP in steady state.
    gain: f64,

    /// Adaptive pacing gain applied to the delivery-rate estimate.
    pacing_gain: f64,

    /// Consecutive rounds where the delivered rate fell below the recent peak.
    squeeze_count: u64,

    /// Whether a competing buffer-filling flow is detected.
    competitive: bool,

    /// Slew-limited, EWMA-smoothed pacing rate in bytes per second. This is the
    /// value emitted to the pacer; zero means "uninitialised / limiter reset".
    smoothed_rate: f64,

    /// EWMA of the effective pacing gain steering toward the discrete
    /// probe / cruise / drain target.
    smoothed_pacing_gain: f64,

    /// Congestion window at the start of the current round, used as the anchor
    /// for the per-round slew limit. Zero means "no anchor".
    round_cwnd_ref: u64,

    /// Current queueing-delay ratio (queue_delay / min_rtt), updated each
    /// control tick. Used to damp the bandwidth estimate when the standing
    /// queue is deep so cwnd and pacing stop feeding it.
    qratio: f64,

    /// Remaining rounds for which `bw_est` is frozen at `squeeze_hold_bw`.
    /// Armed when a squeeze episode latches, decremented once per round.
    squeeze_hold: u64,

    /// Bandwidth estimate captured when the current squeeze episode latched.
    squeeze_hold_bw: u64,

    /// Consecutive unsqueezed rounds with a sustained standing queue, arming
    /// the coexistence brake that yields to a delay-based peer.
    polite_count: u64,

    /// Round-boundary `(time_s, qratio)` samples backing the derivative term.
    trend_hist: Vec<(f64, f64)>,

    /// Consecutive rounds whose round-to-round qratio slope was rising.
    trend_rise_cnt: u64,

    /// Consecutive rounds whose round-to-round qratio slope was falling.
    trend_fall_cnt: u64,

    /// Rounds since startup exit, gating the trend term during warmup.
    trend_warmup: u64,

    /// Whether the preemptive drain is armed for the current round.
    trend_drain: bool,

    /// Whether the one-round trend probe is armed for the current round.
    trend_probe: bool,
}

impl LLMGen {
    pub fn new(config: LLMGenConfig) -> Self {
        let slow_start_delta = config.slow_start_delta;
        let initial_cwnd = config.initial_cwnd;

        Self {
            config,
            stats: Default::default(),
            init_time: Instant::now(),
            mode: CompetingMode::Default,
            slow_start: true,
            cwnd: initial_cwnd,
            velocity: Velocity::default(),
            delta: slow_start_delta,
            standing_rtt_filter: MinMax::new(STANDING_RTT_FILTER_WINDOW.as_micros() as u64),
            min_rtt_filter: MinMax::new(MIN_RTT_FILTER_WINDOW.as_micros() as u64),
            ack_state: Default::default(),
            increase_cwnd: false,
            target_rate: 0,
            last_sent_pkt_num: 0,
            round: Default::default(),
            min_pacing_rate: None,
            bw_est: 0,
            bw_samples: Vec::new(),
            rate_last_acked: 0,
            rate_last_time: Instant::now(),
            rate_now: 0,
            min_rtt_us: 0,
            full_bw: 0,
            full_bw_count: 0,
            gain: GAIN_INIT,
            pacing_gain: PACING_CRUISE_GAIN,
            squeeze_count: 0,
            competitive: false,
            smoothed_rate: 0.0,
            smoothed_pacing_gain: PACING_CRUISE_GAIN,
            round_cwnd_ref: 0,
            qratio: 0.0,
            squeeze_hold: 0,
            squeeze_hold_bw: 0,
            polite_count: 0,
            trend_hist: Vec::new(),
            trend_rise_cnt: 0,
            trend_fall_cnt: 0,
            trend_warmup: 0,
            trend_drain: false,
            trend_probe: false,
        }
    }

    /// Update velocity.
    // Once per window, the sender compares the current cwnd to the cwnd value at
    // the time that the latest acknowledged packet was sent (i.e., cwnd at the
    // start of the current window). If the current cwnd is larger, then set direction
    // to 'up'; if it is smaller, then set direction to 'down'. Now, if direction is
    // the same as in the previous window, then double v. If not, then reset v to 1.
    // However, start doubling v only after the direction has remained the same for three RTTs.
    fn update_velocity(&mut self) {
        // in the case that cwnd should increase in slow start, we do not need
        // to update velocity, since cwnd is always doubled.
        if self.slow_start && self.increase_cwnd {
            return;
        }

        // First time to run here.
        if self.velocity.last_cwnd == 0 {
            self.velocity.last_cwnd = self.cwnd.max(self.config.min_cwnd);
            self.velocity.velocity = 1;
            self.velocity.same_direction_cnt = 0;

            return;
        }

        // Update velocity at the beginning of each round.
        if !self.is_round_start() {
            return;
        }

        // Check cwnd growth direction.
        // if in slow start, and target rate is not reached, then increase cwnd anyway.
        // otherwise, check and update direction to determine cwnd growth in next steps.
        let new_direction = if self.cwnd > self.velocity.last_cwnd {
            Direction::Up
        } else {
            Direction::Down
        };

        if new_direction != self.velocity.direction {
            // Direction changes, reset velocity.
            self.velocity.velocity = 1;
            self.velocity.same_direction_cnt = 0;
        } else {
            // Same direction, check to speed up.
            self.velocity.same_direction_cnt = self.velocity.same_direction_cnt.saturating_add(1);

            if self.velocity.same_direction_cnt >= SPEED_UP_THRESHOLD {
                self.velocity.velocity = self.velocity.velocity.saturating_mul(2);
            }
        }

        // if our current rate is much different than target, we double v every
        // RTT. That could result in a high v at some point in time. If we
        // detect a sudden direction change here, while v is still very high but
        // meant for opposite direction, we should reset it to 1.
        //
        // e.g. cwnd < last_recorded_cwnd && rate < target_rate
        // cwnd < last_recorded_cwnd means that direction is still DOWN while velocity may be large
        // rate < target_rate means that cwnd is about to increase
        // so a switch point is produced, we hope llmgen switch to increase up as soon as possible。
        if self.increase_cwnd
            && self.velocity.direction != Direction::Up
            && self.velocity.velocity > 1
        {
            self.velocity.direction = Direction::Up;
            self.velocity.velocity = 1;
        } else if !self.increase_cwnd
            && self.velocity.direction != Direction::Down
            && self.velocity.velocity > 1
        {
            self.velocity.direction = Direction::Down;
            self.velocity.velocity = 1;
        }

        self.velocity.direction = new_direction;
        self.velocity.last_cwnd = self.cwnd;
    }

    /// Update mode and parameter delta.
    fn update_mode(&mut self) {
        // Check if loss rate exceeds threshold when a new round starts. If so,
        // We assume that LLMGen should switch to competitive mode, to competing with
        // other buffer-filling flows.
        self.mode = if self.round.loss_rate >= LOSS_RATE_THRESHOLD {
            CompetingMode::Competitive
        } else {
            CompetingMode::Default
        };

        // Heavy loss may signal a capacity drop rather than radio noise: release
        // the smoothing-limiter state so a genuine backoff is applied at full
        // speed instead of being slewed into place.
        if self.round.loss_rate >= LOSS_RATE_THRESHOLD {
            self.smoothed_rate = 0.0;
            self.round_cwnd_ref = 0;
        }

        match self.mode {
            CompetingMode::Default => {
                self.delta = if self.slow_start {
                    self.config.slow_start_delta
                } else {
                    self.config.steady_delta
                };
            }
            CompetingMode::Competitive => {
                // Double delta to slow down the target rate.
                self.delta *= 2.0_f64;
                self.delta = self.delta.min(0.5);
            }
        }
    }

    /// Update congestion window from the estimated bandwidth-delay product.
    ///
    /// During startup the window grows by the acked bytes so it doubles per
    /// round, discovering the pipe quickly and converging fast. In steady state
    /// the window tracks `gain * bw_est * min_rtt` (an adaptive multiple of the
    /// BDP) which keeps the pipe saturated while bounding the standing queue.
    /// The window is always clamped above the in-flight floor and the minimal
    /// window so the sender never stalls and never drops below
    /// `minimal_window()`.
    fn update_cwnd(&mut self) {
        if self.slow_start {
            self.cwnd = self.cwnd.saturating_add(self.ack_state.newly_acked_bytes);
            // Startup doubling is never slew-limited; drop the anchor so the
            // first steady-state target is adopted immediately on exit.
            self.round_cwnd_ref = 0;
        } else {
            // Raw target from the existing control law: `gain * bw * min_rtt`,
            // where `bw` is the queue-damped bandwidth estimate. Damping the
            // estimate when the standing queue is deep lets cwnd shrink below
            // the raw BDP so the queue actually drains instead of persisting.
            let mut target = self.cwnd;
            let bw = self.effective_bw();
            if bw > 0 {
                let min_rtt = Duration::from_micros(self.min_rtt_us.max(1));
                let bdp = (bw as f64 * min_rtt.as_secs_f64()) as u64;
                target = (self.gain * bdp as f64) as u64;
            }

            if self.round_cwnd_ref == 0 {
                // No anchor yet (just left startup, or the limiter was reset on a
                // capacity drop): adopt the raw target so a real cut or raise is
                // not delayed by the slew filter.
                self.cwnd = target;
            } else {
                // Slew-limit the per-round window move around the round anchor.
                let lo = (self.round_cwnd_ref as f64 * (1.0 - CWND_SLEW_FRAC)) as u64;
                let hi = (self.round_cwnd_ref as f64 * (1.0 + CWND_SLEW_FRAC)) as u64;
                self.cwnd = target.max(lo).min(hi.max(lo));
            }

            // Re-anchor the band for the next round.
            if self.is_round_start() {
                self.round_cwnd_ref = self.cwnd;
            }
        }

        // Never let the window drop below what is already in flight plus one
        // datagram, otherwise the sender stalls until ACKs return.
        let floor = self
            .stats
            .bytes_in_flight
            .saturating_add(self.config.max_datagram_size);
        if self.cwnd < floor {
            self.cwnd = floor;
        }
        self.cwnd = self.cwnd.max(self.config.min_cwnd);
    }

    /// Check if a new round starts and update round.
    fn update_round(&mut self) {
        if self.ack_state.largest_acked_pkt_num >= self.round.round_end_pkt_num {
            // Calculate loss rate first and then update round states.
            let bytes_lost_in_this_round = self
                .stats
                .bytes_lost_in_total
                .saturating_sub(self.round.last_total_lost_bytes);
            let bytes_acked_in_this_round = self
                .stats
                .bytes_acked_in_total
                .saturating_sub(self.round.last_total_acked_bytes);

            self.round.loss_rate = bytes_lost_in_this_round as f64
                / bytes_lost_in_this_round.saturating_add(bytes_acked_in_this_round) as f64;

            self.round.last_total_acked_bytes = self.stats.bytes_acked_in_total;
            self.round.last_total_lost_bytes = self.stats.bytes_lost_in_total;
            self.round.round_count = self.round.round_count.saturating_add(1);
            self.round.round_end_pkt_num = self.last_sent_pkt_num;
            self.round.is_round_start = true;
        } else {
            self.round.is_round_start = false;
        }
    }

    /// Check if a new round starts.
    fn is_round_start(&self) -> bool {
        self.round.is_round_start
    }

    /// Update LLMGen model driven by ACK packet.
    fn update_model(&mut self) {
        // LLMGen algorithm processing steps:
        // 1. update d_q and srtt;
        // 2. set lambda_t to 1/(delta * d_q);
        // 3. adjust cwnd according to the relationship of lambda and lambda_t
        // 4. update velocity
        //
        // d_q = RTT_standing - minRTT, where RTT_standing is the minimum during time window srtt/2

        // Update standing rtt and min rtt.
        if self.config.use_standing_rtt {
            self.standing_rtt_filter
                .set_window(self.ack_state.last_srtt.as_micros() as u64);
        } else {
            self.standing_rtt_filter
                .set_window(self.ack_state.last_srtt.as_micros() as u64 / 2);
        }

        if self.ack_state.min_rtt == Duration::ZERO {
            self.ack_state.min_rtt = if self.ack_state.last_srtt == Duration::ZERO {
                self.config.initial_rtt.unwrap_or(Duration::from_millis(20))
            } else {
                self.ack_state.last_srtt
            };
        }

        // Update min rtt in period of standing window and 10s respectly.
        let elapsed = self.ack_state.now.saturating_duration_since(self.init_time);
        self.min_rtt_filter.update_min(
            elapsed.as_micros() as u64,
            self.ack_state.min_rtt.as_micros() as u64,
        );
        self.standing_rtt_filter.update_min(
            elapsed.as_micros() as u64,
            self.ack_state.min_rtt.as_micros() as u64,
        );

        // Adjust delta.
        self.update_mode();

        let min_rtt = Duration::from_micros(self.min_rtt_filter.get().max(1));
        let standing_rtt = self.get_standing_rtt();

        trace!(
            "{}. round_min_rtt = {}us, elapsed = {}us, min_rtt = {}us, standing_rtt = {}us",
            self.name(),
            self.ack_state.min_rtt.as_micros(),
            elapsed.as_micros(),
            min_rtt.as_micros(),
            standing_rtt.as_micros(),
        );

        // Queue-delay signal: use the smoothed RTT, which reflects the real,
        // persistent standing queue, instead of the standing-min filter. The
        // standing-min over an srtt-sized window collapses to ~min_rtt whenever
        // the queue drains even briefly, so queueing_delay reads ~0 and the
        // drain path never fires -- the controller then probes its gain upward
        // forever, building a large standing queue that crushes a delay-based
        // peer (Copa). srtt is already EMA-smoothed, so it is robust to single
        // ACK-compression spikes yet sensitive to a genuine standing queue.
        let srtt = if self.ack_state.last_srtt.is_zero() {
            standing_rtt
        } else {
            self.ack_state.last_srtt
        };
        let current_rate: u64 = (self.cwnd as f64 / standing_rtt.as_secs_f64()) as u64;
        let queueing_delay = srtt.saturating_sub(min_rtt);
        if queueing_delay.is_zero() {
            // taking care of inf targetRate case here, this happens in beginning where
            // we do want to increase cwnd, e.g. slow start or no queuing happens.
            self.increase_cwnd = true;

            trace!(
                "{}. queuing delay is zero. rtt_standing and min_rtt is the same: {}us",
                self.name(),
                min_rtt.as_micros()
            );

            self.target_rate = (self.cwnd as f64 / standing_rtt.as_secs_f64()) as u64;
        } else {
            // Limit queueing_delay in case it's too small and get a huge target rate.
            self.target_rate = (self.config.max_datagram_size as f64
                / self.delta
                / queueing_delay.max(Duration::from_micros(1)).as_secs_f64())
                as u64;

            trace!(
                "{}. target_rate = {}, delta = {}, max_datagram_size = {}",
                self.name(),
                self.target_rate,
                self.delta,
                self.config.max_datagram_size
            );

            self.increase_cwnd = self.target_rate >= current_rate;
        }

        // Record the freshest min RTT for BDP sizing and derive the queueing
        // delay ratio used by the startup and gain controllers.
        let min_rtt_micros = self.min_rtt_filter.get().max(1);
        self.min_rtt_us = min_rtt_micros;
        let min_rtt_secs = (min_rtt_micros as f64) / 1_000_000.0;
        let qratio = if min_rtt_secs > 0.0 {
            queueing_delay.as_secs_f64() / min_rtt_secs
        } else {
            0.0
        };
        self.qratio = qratio;

        // Coexistence detector: a delivered rate persistently below the recent
        // peak is the signature of a buffer-filling neighbour taking our share.
        // The latch lifts the gain/pacing in `update_gain` and pins the
        // bandwidth estimate so it cannot decay into the starvation equilibrium
        // while the neighbour owns the queue.
        if self.is_round_start() {
            if self.bw_est > 0
                && (self.rate_now as f64) < SQUEEZE_RATIO * (self.bw_est as f64)
            {
                self.squeeze_count = self.squeeze_count.saturating_add(1);
                if self.squeeze_count == 1 && self.qratio > COMPETITIVE_QMIN {
                    // Rate drop combined with a real standing queue: the
                    // neighbour owns the buffer. Pin the estimate at its
                    // pre-squeeze value so cwnd/pacing keep our fair share
                    // while the coexistence brake pushes back. A rate drop
                    // without a standing queue (e.g. a delay-sensitive peer
                    // politely reclaiming its share) does not arm the hold,
                    // keeping coexistence fair against yielding flows.
                    self.squeeze_hold = SQUEEZE_HOLD_ROUNDS;
                    self.squeeze_hold_bw = self.bw_est;
                }
            } else {
                self.squeeze_count = 0;
            }
            if self.squeeze_hold > 0 {
                self.squeeze_hold = self.squeeze_hold.saturating_sub(1);
            }
            self.competitive = self.squeeze_count >= SQUEEZE_ROUNDS;

            // Coexistence brake detector: a standing queue we own (qratio above
            // POLITE_QMIN) while our delivery rate is NOT squeezed is the
            // signature of a delay-based peer yielding to our buffer. Arm the
            // brake so `update_gain` can yield room back; a buffer-filling peer
            // would instead squeeze our rate and arm the competitive branch.
            if self.bw_est > 0
                && self.qratio > POLITE_QMIN
                && (self.rate_now as f64) >= SQUEEZE_RATIO * (self.bw_est as f64)
            {
                self.polite_count = self.polite_count.saturating_add(1);
            } else {
                self.polite_count = 0;
            }
        }

        // Startup exit (queue build-up or bandwidth plateau) then steady-state
        // gain adaptation once per round. The derivative (trend) term is
        // recomputed first so `update_gain` consumes fresh one-round flags.
        self.update_startup_exit(qratio);
        if !self.slow_start && self.is_round_start() {
            self.update_trend(qratio);
            self.update_gain(qratio);
        }

        self.update_velocity();
        self.update_cwnd();
        self.update_smoothed_rate();

        trace!(
            "{}. mode = {:?}, slow_start={}, delta={}, target_rate={}, current_rate={},
             increase_cwnd={}, queuing_delay={}us, rtt_standing={}us, cwnd={}",
            self.name(),
            self.mode,
            self.slow_start,
            self.delta,
            self.target_rate,
            current_rate,
            self.increase_cwnd,
            queueing_delay.as_micros(),
            standing_rtt.as_micros(),
            self.cwnd
        );
    }

    /// Exit startup once the delivery rate plateaus (no >=25% growth for
    /// `FULL_BW_CNT` rounds) or the queueing delay builds toward the buffer.
    fn update_startup_exit(&mut self, qratio: f64) {
        if !self.slow_start {
            return;
        }

        if self.bw_est as f64 >= self.full_bw as f64 * FULL_BW_GROWTH {
            self.full_bw = self.bw_est;
            self.full_bw_count = 0;
        } else if self.is_round_start() {
            self.full_bw_count = self.full_bw_count.saturating_add(1);
        }

        if self.full_bw_count >= FULL_BW_CNT || qratio > STARTUP_QUEUE_EXIT_RATIO {
            self.slow_start = false;
            self.gain = GAIN_INIT;
            self.pacing_gain = PACING_CRUISE_GAIN;
            // Reset the smoothing limiter so the first steady-state tick adopts
            // the cruise rate / window immediately instead of slewing down from
            // the (much larger) startup pacing gain.
            self.smoothed_pacing_gain = PACING_CRUISE_GAIN;
            self.smoothed_rate = 0.0;
            self.round_cwnd_ref = 0;
            // Drop startup-era delivery-rate samples so the steady-state
            // bandwidth estimate is rebuilt from post-startup samples only.
            // An inflated startup peak would otherwise persist for BW_WINDOW
            // and inflate cwnd/pacing beyond the link rate, building excess
            // standing queue in steady state.
            // Retain recent delivery-rate samples so the steady-state bandwidth
            // estimate starts from the discovered rate instead of dipping while
            // samples rebuild from scratch. BW_WINDOW ages out stale startup
            // peaks within 600ms, so the estimate self-corrects quickly.
            let now_us = self
                .ack_state
                .now
                .saturating_duration_since(self.init_time)
                .as_micros() as u64;
            let cutoff = now_us.saturating_sub(BW_WINDOW.as_micros() as u64);
            self.bw_samples.retain(|&(t, _)| t >= cutoff);
            if self.bw_est == 0 {
                self.bw_est = self.rate_now;
            }
        }
    }

    /// Adapt the target-inflight gain and pacing headroom to the queueing-delay
    /// ratio. A drained queue probes upward (gain and pace); an inflating queue
    /// is drained before it can grow a standing buffer. When a delivery-rate
    /// squeeze is latched together with a real standing queue, the gain is
    /// raised (bounded by `COMPETITIVE_GAIN`) to claim our share back from a
    /// buffer-filling peer regardless of how deep that peer's queue is.
    fn update_gain(&mut self, qratio: f64) {
        if qratio > QUEUE_HIGH_RATIO {
            self.gain = (self.gain - GAIN_STEP_DOWN).max(GAIN_MIN);
            self.pacing_gain = PACING_DRAIN_GAIN;
        } else if qratio < QUEUE_LOW_RATIO {
            self.gain = (self.gain + GAIN_STEP_UP).min(GAIN_MAX);
            self.pacing_gain = PACING_PROBE_GAIN;
        } else {
            self.pacing_gain = PACING_CRUISE_GAIN;
        }

        // Derivative (trend) feedback layered on the level-based deadband.
        // The deadband only reacts once the queue has already crossed
        // `QUEUE_HIGH_RATIO`, by which time the standing queue -- and with it
        // rttvar -- has already paid for the overshoot. A queue that is still
        // rising while inside the deadband is drained preemptively, and a queue
        // climbing steeply enough to jump the deadband in one round is drained
        // on the slope alone. A queue falling toward empty on an unsqueezed
        // path earns a bounded one-round probe. Both terms are clamped (the
        // drain can never push pacing below `TREND_DRAIN_PACE` or the gain
        // below `GAIN_MIN`; the probe lasts exactly one round) and both are
        // applied *before* the competitive boost and the polite brake below,
        // which stay the last word for coexistence.
        if self.trend_drain {
            self.gain = (self.gain - GAIN_STEP_DOWN).max(GAIN_MIN);
            self.pacing_gain = self.pacing_gain.min(TREND_DRAIN_PACE);
        }
        if self.trend_probe {
            self.pacing_gain = self.pacing_gain.max(TREND_PROBE_PACE);
        }

        if self.competitive && qratio > COMPETITIVE_QMIN && qratio < COMPETITIVE_QMAX {
            // A sustained delivery-rate squeeze combined with a real standing
            // queue is the signature of a buffer-filling competitor (BBR/Cubic):
            // it fills the buffer while our own delivery rate is degraded, so the
            // queue is not ours. Claim our share back with a strong gain and
            // pace. A delay-sensitive peer (Copa) reclaims its share without
            // building a queue, so qratio stays low and this branch stays off --
            // keeping coexistence fair and near 50/50 against a yielding peer.
            // The upper bound stops the boost from overriding drain once the
            // queue is extremely deep, where the queueing-delay penalty exceeds
            // the share the boost recovers.
            self.gain = self.gain.max(COMPETITIVE_GAIN);
            self.pacing_gain = self.pacing_gain.max(COMPETITIVE_PACING_GAIN);
        }

        // Coexistence brake: a sustained standing queue we own while not being
        // squeezed means a delay-based peer (Copa) is yielding to our buffer.
        // Yield back by dropping below the BDP so the peer can reclaim its fair
        // share. A buffer-filling peer would instead squeeze our delivery rate
        // and arm the competitive branch above, which this brake never overrides.
        if self.polite_count >= POLITE_ROUNDS && !self.competitive {
            self.gain = self.gain.min(POLITE_GAIN);
            self.pacing_gain = self.pacing_gain.min(POLITE_PACE);
        }

        // Steer the effective pacing gain toward the discrete target instead of
        // flipping straight to it, so probe / cruise / drain transitions are
        // gradual and no longer emit a rate step every round.
        self.smoothed_pacing_gain +=
            PACING_GAIN_EWMA_ALPHA * (self.pacing_gain - self.smoothed_pacing_gain);
    }

    /// Derivative (trend) feedback on the queueing-delay ratio.
    ///
    /// Called once per round boundary (never mid-round) while out of slow
    /// start. It appends the current `qratio` to a short history stamped with
    /// `as_secs_f64()` elapsed time, derives the round-to-round slope, and
    /// requires two consecutive same-sign slopes before arming anything, so a
    /// single ACK-compression spike cannot trigger a reaction. A rising queue
    /// still below `QUEUE_HIGH_RATIO` arms the preemptive drain (and a steeply
    /// rising queue arms it even above that threshold, up to
    /// `TREND_DRAIN_QMAX`); a falling, essentially empty, unsqueezed queue
    /// arms a one-round probe. Both flags are cleared at the top of every
    /// call, so they can only ever describe the current round, and the whole
    /// term is disabled during slow start and for `TREND_WARMUP_ROUNDS` rounds
    /// after startup exit. The flags are consumed by `update_gain`, which keeps
    /// the competitive/polite coexistence branches as the last word.
    fn update_trend(&mut self, qratio: f64) {
        self.trend_drain = false;
        self.trend_probe = false;

        if self.slow_start {
            self.trend_hist.clear();
            self.trend_rise_cnt = 0;
            self.trend_fall_cnt = 0;
            return;
        }

        if self.trend_warmup < TREND_WARMUP_ROUNDS {
            self.trend_warmup = self.trend_warmup.saturating_add(1);
        }

        // Saturating time math: elapsed seconds since init, never negative.
        let now_secs = self
            .ack_state
            .now
            .saturating_duration_since(self.init_time)
            .as_secs_f64();

        self.trend_hist.push((now_secs, qratio));
        while self.trend_hist.len() > TREND_HIST_LEN {
            self.trend_hist.remove(0);
        }

        if self.trend_hist.len() < 2 {
            return;
        }

        let n = self.trend_hist.len();
        let (t_prev, q_prev) = (self.trend_hist[n - 2].0, self.trend_hist[n - 2].1);
        let (t_now, q_now) = (self.trend_hist[n - 1].0, self.trend_hist[n - 1].1);

        // Reject a slope computed over a zero (or non-finite) interval: two
        // round boundaries landing on the same instant carry no information.
        let dt = t_now - t_prev;
        if !dt.is_finite() || dt <= 0.0 {
            return;
        }

        // Samples are taken exactly one per round, so `q_now - q_prev` is
        // already the per-round slope d(qratio)/round that the thresholds are
        // expressed in; `dt` is only used as the degeneracy guard above.
        let slope = q_now - q_prev;

        if slope >= TREND_RISE_SLOPE {
            self.trend_rise_cnt = self.trend_rise_cnt.saturating_add(1);
            self.trend_fall_cnt = 0;
        } else if slope <= -TREND_FALL_SLOPE {
            self.trend_fall_cnt = self.trend_fall_cnt.saturating_add(1);
            self.trend_rise_cnt = 0;
        } else {
            self.trend_rise_cnt = 0;
            self.trend_fall_cnt = 0;
        }

        // Trend term stays off during the post-startup warmup window.
        if self.trend_warmup < TREND_WARMUP_ROUNDS {
            return;
        }

        // Preemptive drain: the queue is rising through the deadband, so act
        // before it reaches `QUEUE_HIGH_RATIO` instead of after.
        if self.trend_rise_cnt >= TREND_SAME_SIGN_ROUNDS
            && qratio >= TREND_DRAIN_QMIN
            && qratio < QUEUE_HIGH_RATIO
        {
            self.trend_drain = true;
        }

        // Steep-rise drain: a fast climb can cross the whole deadband in one
        // round, so drain on the slope alone while the queue is still moderate.
        if self.trend_rise_cnt >= TREND_SAME_SIGN_ROUNDS
            && qratio >= QUEUE_HIGH_RATIO
            && qratio < TREND_DRAIN_QMAX
            && slope >= TREND_STEEP_SLOPE
        {
            self.trend_drain = true;
        }

        // Trend-gated probe: the queue is draining toward empty on a path where
        // our delivery rate is not being squeezed by a competitor, so a bounded
        // one-round pacing overshoot captures spare capacity immediately.
        if self.trend_fall_cnt >= TREND_SAME_SIGN_ROUNDS
            && qratio < TREND_PROBE_QMAX
            && !self.competitive
            && self.bw_est > 0
            && (self.rate_now as f64) >= SQUEEZE_RATIO * (self.bw_est as f64)
        {
            self.trend_probe = true;
        }
    }

    /// Effective bandwidth estimate after queue-aware damping.
    ///
    /// When the standing queue is deep (qratio well above the drain threshold),
    /// the raw windowed-maximum delivery-rate estimate is scaled down so that
    /// both the congestion window and the pacing rate stop feeding the queue and
    /// let it drain. The damping is continuous in qratio (no step), which keeps
    /// the emitted rate smooth and limits rttvar. At shallow queues — and during
    /// startup, where fast convergence matters more than queue bounding — the
    /// estimate is passed through unchanged, so steady-state throughput and
    /// convergence are unaffected.
    fn effective_bw(&self) -> u64 {
        if self.bw_est == 0 || self.slow_start {
            return self.bw_est;
        }
        // A latched delivery-rate squeeze means the standing queue belongs to a
        // competitor, not to us: damping our estimate in response would cut
        // cwnd and pacing below our fair share, let the peer take the link, and
        // -- because the estimate then decays toward the starved rate -- lock
        // the flow into a starvation equilibrium (share ~0.1%). Hold the
        // estimate undamped and let the coexistence brake in `update_gain`
        // reclaim share. When we are NOT squeezed, `rate_now ~= bw_est`, the
        // queue is at least partly ours and damping stays active -- which also
        // means the winning flow of a pairing is the one that gets braked.
        if self.squeeze_count > 0 {
            return self.bw_est;
        }
        if self.qratio <= QUEUE_HIGH_RATIO {
            return self.bw_est;
        }
        // Damp proportionally to queue depth: at qratio == QUEUE_HIGH_RATIO the
        // factor is 1.0; it decays as 1/(1+excess) and is floored at
        // BW_DAMP_MIN so the flow never starves even under a very deep queue.
        let excess = self.qratio - QUEUE_HIGH_RATIO;
        let damp = (1.0 / (1.0 + excess)).max(BW_DAMP_MIN);
        (self.bw_est as f64 * damp) as u64
    }

    /// Raw pacing rate implied by the current control law, in bytes per second.
    ///
    /// Uses the queue-damped delivery-rate estimate when available, else the
    /// congestion window divided by the standing RTT. All time math is exact f64
    /// so there is no integer-second truncation.
    fn raw_pacing_rate(&self) -> f64 {
        let bw = self.effective_bw();
        let base = if bw > 0 {
            bw as f64
        } else {
            let standing_rtt = self.get_standing_rtt();
            self.cwnd as f64 / standing_rtt.as_secs_f64().max(1e-6)
        };
        let gain = if self.slow_start {
            STARTUP_PACING_GAIN
        } else {
            self.smoothed_pacing_gain
        };
        base * gain
    }

    /// Advance the slew-limited, EWMA-smoothed emitted pacing rate.
    ///
    /// This is a filter on the existing control law's output, not a new control
    /// equation. The raw target from `raw_pacing_rate` is first clamped into
    /// +/-`PACING_SLEW_FRAC` of the previously emitted rate, then low-passed with
    /// `PACING_EWMA_ALPHA` (`new = 0.6*prev + 0.4*clamped`). That keeps the
    /// average rate and steady-state throughput unchanged while removing the
    /// round-timescale oscillation that inflates rttvar and queue delay.
    ///
    /// Startup bypasses the filter entirely (full doubling is preserved), and a
    /// limiter reset (`smoothed_rate == 0`) re-seeds straight from the raw target
    /// so a genuine backoff is never delayed.
    fn update_smoothed_rate(&mut self) {
        let raw = self.raw_pacing_rate();

        if self.slow_start || self.smoothed_rate <= 0.0 || !raw.is_finite() || raw <= 0.0 {
            if raw.is_finite() && raw > 0.0 {
                self.smoothed_rate = raw;
            }
            return;
        }

        let lo = self.smoothed_rate * (1.0 - PACING_SLEW_FRAC);
        let hi = self.smoothed_rate * (1.0 + PACING_SLEW_FRAC);
        let clamped = raw.max(lo).min(hi);
        self.smoothed_rate =
            PACING_EWMA_ALPHA * self.smoothed_rate + (1.0 - PACING_EWMA_ALPHA) * clamped;
    }

    /// Estimate the delivery rate from acked bytes over a sliding sample and
    /// keep the windowed maximum as the bandwidth estimate. The freshest sample
    /// is stored in `rate_now` for the coexistence detector. Uses
    /// `as_secs_f64()` for exact sub-second time math.
    fn update_bw(&mut self, now: Instant) {
        let elapsed = now.saturating_duration_since(self.rate_last_time);
        if elapsed < RATE_SAMPLE_INTERVAL {
            return;
        }
        let acked = self
            .stats
            .bytes_acked_in_total
            .saturating_sub(self.rate_last_acked);
        self.rate_last_acked = self.stats.bytes_acked_in_total;
        self.rate_last_time = now;

        let secs = elapsed.as_secs_f64().max(1e-6);
        let rate = (acked as f64 / secs) as u64;
        self.rate_now = rate;

        let now_us = now.saturating_duration_since(self.init_time).as_micros() as u64;
        self.bw_samples.push((now_us, rate));

        let cutoff = now_us.saturating_sub(BW_WINDOW.as_micros() as u64);
        self.bw_samples.retain(|&(t, _)| t >= cutoff);

        let mut peak = 0u64;
        for &(_, r) in self.bw_samples.iter() {
            if r > peak {
                peak = r;
            }
        }
        if peak > 0 {
            // While a squeeze freeze is active, never let the estimate fall
            // below the pinned pre-squeeze value: the low samples sitting in
            // the window are the competitor's starvation, not a bandwidth
            // reduction, and letting them drag the estimate down is what made
            // the flow unable to ever push back.
            self.bw_est = if self.squeeze_hold > 0 {
                peak.max(self.squeeze_hold_bw)
            } else {
                peak
            };
        }
    }

    fn update_min_pacing_rate(&mut self) {
        if let Some(pacing_rate) = self.pacing_rate() {
            self.min_pacing_rate = match self.min_pacing_rate {
                Some(min_pacing_rate) => Some(min_pacing_rate.min(pacing_rate)),
                None => self.pacing_rate(),
            }
        }
    }

    /// Get standing rtt.
    fn get_standing_rtt(&self) -> Duration {
        let standing_rtt = Duration::from_micros(self.standing_rtt_filter.get());
        if standing_rtt.is_zero() {
            return std::cmp::max(
                self.config.initial_rtt.unwrap_or(crate::INITIAL_RTT),
                Duration::from_micros(1),
            );
        }
        standing_rtt
    }
}

impl CongestionController for LLMGen {
    fn pacing_rate(&self) -> Option<u64> {
        // Emit the slew-limited, EWMA-smoothed rate produced by the control law
        // so the pacer does not step its rate every round. Falls back to the raw
        // law before the first smoothing tick.
        if self.smoothed_rate > 0.0 && self.smoothed_rate.is_finite() {
            return Some((self.smoothed_rate as u64).max(1));
        }
        let bw = self.effective_bw();
        let base = if bw > 0 {
            bw as f64
        } else {
            let standing_rtt = self.get_standing_rtt();
            self.cwnd as f64 / standing_rtt.as_secs_f64().max(1e-6)
        };
        let gain = if self.slow_start {
            STARTUP_PACING_GAIN
        } else {
            self.smoothed_pacing_gain
        };
        Some(((base * gain) as u64).max(1))
    }

    fn min_pacing_rate(&self) -> Option<u64> {
        match self.min_pacing_rate {
            Some(min_pacing_rate) => Some(min_pacing_rate),
            None => self.pacing_rate(),
        }
    }

    fn name(&self) -> &str {
        "LLMGen"
    }

    fn congestion_window(&self) -> u64 {
        self.cwnd.max(self.config.min_cwnd)
    }

    fn set_congestion_window(&mut self, cwnd: u64) {
        self.cwnd = cwnd.max(self.config.min_cwnd);
        self.velocity.last_cwnd = self.cwnd;
    }

    fn initial_window(&self) -> u64 {
        self.config.initial_cwnd
    }

    fn minimal_window(&self) -> u64 {
        self.config.min_cwnd
    }

    fn in_slow_start(&self) -> bool {
        self.slow_start
    }

    fn stats(&self) -> &CongestionStats {
        &self.stats
    }

    fn on_sent(&mut self, now: Instant, packet: &mut SentPacket, bytes_in_flight: u64) {
        self.stats.bytes_in_flight = bytes_in_flight;
        self.stats.bytes_sent_in_total = self
            .stats
            .bytes_sent_in_total
            .saturating_add(packet.sent_size as u64);

        self.last_sent_pkt_num = packet.pkt_num;
    }

    fn begin_ack(&mut self, now: Instant, bytes_in_flight: u64) {
        self.ack_state.newly_acked_bytes = 0;
        self.ack_state.newly_lost_bytes = 0;
        self.ack_state.now = now;
        self.ack_state.min_rtt = Duration::ZERO;
        self.ack_state.last_srtt = Duration::ZERO;
        self.ack_state.largest_acked_pkt_num = 0;
    }

    fn on_ack(
        &mut self,
        packet: &mut SentPacket,
        now: Instant,
        _app_limited: bool,
        rtt: &RttEstimator,
        bytes_in_flight: u64,
    ) {
        // Update stats.
        let sent_time = packet.time_sent;
        let acked_bytes = packet.sent_size as u64;

        self.stats.bytes_in_flight = self.stats.bytes_in_flight.saturating_sub(acked_bytes);
        self.stats.bytes_acked_in_total =
            self.stats.bytes_acked_in_total.saturating_add(acked_bytes);
        if self.in_slow_start() {
            self.stats.bytes_acked_in_slow_start = self
                .stats
                .bytes_acked_in_slow_start
                .saturating_add(acked_bytes);
        }

        self.ack_state.newly_acked_bytes =
            self.ack_state.newly_acked_bytes.saturating_add(acked_bytes);

        self.ack_state.largest_acked_pkt_num =
            self.ack_state.largest_acked_pkt_num.max(packet.pkt_num);
        self.ack_state.last_srtt = rtt.smoothed_rtt();

        // Since all the ack frames in a packet share the same ack_time and 'now_time',
        // we record only the minimum rtt and its corresponding time which will be
        // processed at the stage of end_ack.
        if self.ack_state.min_rtt.is_zero() || self.ack_state.min_rtt >= rtt.latest_rtt() {
            trace!(
                "{}. Got a smaller rtt: {}us -> {}us",
                self.name(),
                self.ack_state.min_rtt.as_micros(),
                rtt.latest_rtt().as_micros()
            );

            self.ack_state.min_rtt = rtt.latest_rtt();
        }

        trace!(
            "{}. ON_ACK. latest_rtt = {}us, srtt = {}us, newly_acked = {}, total_acked = {}",
            self.name(),
            rtt.latest_rtt().as_micros(),
            rtt.smoothed_rtt().as_micros(),
            self.ack_state.newly_acked_bytes,
            self.stats.bytes_acked_in_total,
        )
    }

    fn end_ack(&mut self) {
        self.update_round();
        trace!(
            "{}. END_ACK. round_start = {:?}, round_count = {}, end_pkt = {}, loss_rate = {}, last_acked = {}, last_lost = {}",
            self.name(),
            self.is_round_start(),
            self.round.round_count,
            self.round.round_end_pkt_num,
            self.round.loss_rate,
            self.round.last_total_acked_bytes,
            self.round.last_total_lost_bytes,
        );

        self.update_bw(self.ack_state.now);
        self.update_model();
        self.update_min_pacing_rate();
    }

    fn on_congestion_event(
        &mut self,
        now: Instant,
        packet: &SentPacket,
        in_persistent_congestion: bool,
        lost_bytes: u64,
        bytes_in_flight: u64,
    ) {
        // Statistics.
        self.stats.bytes_lost_in_total = self.stats.bytes_lost_in_total.saturating_add(lost_bytes);
        self.stats.bytes_in_flight = bytes_in_flight;

        if self.in_slow_start() {
            self.stats.bytes_lost_in_slow_start = self
                .stats
                .bytes_lost_in_slow_start
                .saturating_add(lost_bytes);
        }

        if in_persistent_congestion {
            // Hard requirement: collapse to the minimal window on persistent
            // congestion, and drop the smoothing state so recovery restarts from
            // the raw target without a slew delay.
            self.cwnd = self.config.min_cwnd;
            self.velocity.last_cwnd = self.cwnd;
            self.round_cwnd_ref = 0;
            self.smoothed_rate = 0.0;
        }
    }
}
// EVOLVE-BLOCK-END
