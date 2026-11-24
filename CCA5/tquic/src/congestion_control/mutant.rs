// Copyright (c) 2024 The TQUIC Authors.
#![allow(unused_variables)]

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use super::Bbr;
use super::BbrConfig;
use super::CongestionController;
use super::CongestionStats;
use super::Copa;
use super::CopaConfig;
use super::Cubic;
use super::CubicConfig;
use crate::connection::rtt::RttEstimator;
use crate::connection::space::SentPacket;
use crate::RecoveryConfig;

const ARM_COUNT: usize = 3;
const BBR_ARM_INDEX: usize = 0;
const FEATURE_DIM: usize = 14;
const DEFAULT_STEP_DURATION: Duration = Duration::from_millis(10);
const DEFAULT_ALPHA: f64 = 1.0;
const DEFAULT_ZETA: f64 = 1.0;
const DEFAULT_KAPPA: f64 = 1.0;
const DEFAULT_ADWIN_DELTA: f64 = 0.002;
const ADWIN_MIN_SUBWINDOW: usize = 5;
const ADWIN_MAX_WINDOW: usize = 1000;
const THROUGHPUT_NORMALIZATION_SCALE: f64 = 125_000_000.0;
const DELAY_NORMALIZATION_SCALE: f64 = 1.0;
const MIN_DELAY: f64 = 0.001;

#[derive(Debug)]
pub struct MutantConfig {
    bbr: BbrConfig,
    cubic: CubicConfig,
    copa: CopaConfig,
    max_datagram_size: u64,
    initial_cwnd: u64,
    min_cwnd: u64,
    initial_rtt: Duration,
    step_duration: Duration,
    alpha: f64,
    zeta: f64,
    kappa: f64,
}

impl MutantConfig {
    pub fn from(conf: &RecoveryConfig) -> Self {
        let max_datagram_size = conf.max_datagram_size as u64;
        Self {
            bbr: BbrConfig::from(conf),
            cubic: CubicConfig::from(conf),
            copa: CopaConfig::from(conf),
            max_datagram_size,
            initial_cwnd: conf
                .initial_congestion_window
                .saturating_mul(max_datagram_size),
            min_cwnd: conf.min_congestion_window.saturating_mul(max_datagram_size),
            initial_rtt: conf.initial_rtt,
            step_duration: DEFAULT_STEP_DURATION,
            alpha: DEFAULT_ALPHA,
            zeta: DEFAULT_ZETA,
            kappa: DEFAULT_KAPPA,
        }
    }
}

impl Default for MutantConfig {
    fn default() -> Self {
        let max_datagram_size = crate::DEFAULT_SEND_UDP_PAYLOAD_SIZE as u64;
        Self {
            bbr: BbrConfig::default(),
            cubic: CubicConfig::default(),
            copa: CopaConfig::default(),
            max_datagram_size,
            initial_cwnd: 80 * max_datagram_size,
            min_cwnd: 4 * max_datagram_size,
            initial_rtt: crate::INITIAL_RTT,
            step_duration: DEFAULT_STEP_DURATION,
            alpha: DEFAULT_ALPHA,
            zeta: DEFAULT_ZETA,
            kappa: DEFAULT_KAPPA,
        }
    }
}

#[derive(Debug)]
struct ControllerArm {
    controller: Box<dyn CongestionController>,
}

impl ControllerArm {
    fn new(controller: Box<dyn CongestionController>) -> Self {
        Self { controller }
    }
}

#[derive(Debug)]
pub struct Mutant {
    arms: Vec<ControllerArm>,
    active_index: usize,
    previous_index: usize,
    policy: LinUcbPolicy,
    stats: CongestionStats,

    max_datagram_size: u64,
    initial_cwnd: u64,
    min_cwnd: u64,
    step_duration: Duration,
    zeta: f64,
    kappa: f64,

    interval_start: Instant,
    interval_acked: u64,
    interval_lost: u64,
    last_event_time: Option<Instant>,

    latest_rtt: Duration,
    srtt: Duration,
    rttvar: Duration,
    min_rtt: Duration,

    max_throughput: f64,
    min_delay: f64,
    throughput_windows: Vec<AdwinWindow>,
    delay_windows: Vec<AdwinWindow>,
}

impl Mutant {
    pub fn new(config: MutantConfig) -> Self {
        let now = Instant::now();
        let initial_rtt = config.initial_rtt.max(Duration::from_micros(1));
        let arms = vec![
            ControllerArm::new(Box::new(Bbr::new(config.bbr))),
            ControllerArm::new(Box::new(Cubic::new(config.cubic))),
            ControllerArm::new(Box::new(Copa::new(config.copa))),
        ];

        Self {
            arms,
            active_index: BBR_ARM_INDEX,
            previous_index: BBR_ARM_INDEX,
            policy: LinUcbPolicy::new(ARM_COUNT, config.alpha),
            stats: Default::default(),
            max_datagram_size: config.max_datagram_size.max(1),
            initial_cwnd: config.initial_cwnd.max(config.min_cwnd),
            min_cwnd: config.min_cwnd.max(1),
            step_duration: config.step_duration,
            zeta: config.zeta,
            kappa: config.kappa,
            interval_start: now,
            interval_acked: 0,
            interval_lost: 0,
            last_event_time: None,
            latest_rtt: initial_rtt,
            srtt: initial_rtt,
            rttvar: initial_rtt / 2,
            min_rtt: initial_rtt,
            max_throughput: 1.0,
            min_delay: initial_rtt.as_secs_f64().max(MIN_DELAY),
            throughput_windows: (0..ARM_COUNT)
                .map(|_| AdwinWindow::new(DEFAULT_ADWIN_DELTA, THROUGHPUT_NORMALIZATION_SCALE))
                .collect(),
            delay_windows: (0..ARM_COUNT)
                .map(|_| AdwinWindow::new(DEFAULT_ADWIN_DELTA, DELAY_NORMALIZATION_SCALE))
                .collect(),
        }
    }

    fn active_controller(&self) -> &dyn CongestionController {
        self.arms[self.active_index].controller.as_ref()
    }

    fn active_controller_mut(&mut self) -> &mut dyn CongestionController {
        self.arms[self.active_index].controller.as_mut()
    }

    fn dispatch_sent_to_arms(
        &mut self,
        now: Instant,
        packet: &mut SentPacket,
        bytes_in_flight: u64,
    ) {
        let packet_snapshot = packet.clone();
        for (idx, arm) in self.arms.iter_mut().enumerate() {
            if idx == BBR_ARM_INDEX {
                arm.controller.on_sent(now, packet, bytes_in_flight);
            } else {
                let mut shadow = packet_snapshot.clone();
                arm.controller.on_sent(now, &mut shadow, bytes_in_flight);
            }
        }
    }

    fn dispatch_ack_to_arms(
        &mut self,
        packet: &mut SentPacket,
        now: Instant,
        app_limited: bool,
        rtt: &RttEstimator,
        bytes_in_flight: u64,
    ) {
        let packet_snapshot = packet.clone();
        for (idx, arm) in self.arms.iter_mut().enumerate() {
            if idx == BBR_ARM_INDEX {
                arm.controller
                    .on_ack(packet, now, app_limited, rtt, bytes_in_flight);
            } else {
                let mut shadow = packet_snapshot.clone();
                arm.controller
                    .on_ack(&mut shadow, now, app_limited, rtt, bytes_in_flight);
            }
        }
    }

    fn sync_active_cwnd(&mut self, next_index: usize) {
        let cwnd = self.congestion_window();
        self.previous_index = self.active_index;
        self.active_index = next_index.min(self.arms.len().saturating_sub(1));
        self.active_controller_mut().set_congestion_window(cwnd);
    }

    fn maybe_advance_policy(&mut self, now: Instant) {
        let elapsed = now.saturating_duration_since(self.interval_start);
        if elapsed < self.step_duration {
            return;
        }

        let context = self.context(elapsed);
        let reward = self.reward(elapsed);
        self.policy.update(self.active_index, &context, reward);

        let next = self.policy.select(&context);
        if next != self.active_index {
            self.sync_active_cwnd(next);
        } else {
            self.previous_index = self.active_index;
        }

        self.interval_start = now;
        self.interval_acked = 0;
        self.interval_lost = 0;
    }

    fn context(&self, elapsed: Duration) -> [f64; FEATURE_DIM] {
        let elapsed_secs = elapsed.as_secs_f64().max(0.000_001);
        let mss = self.max_datagram_size.max(1) as f64;
        let cwnd = self.congestion_window() as f64;
        let bytes_in_flight = self.stats.bytes_in_flight as f64;
        let total_interval_bytes = self.interval_acked.saturating_add(self.interval_lost);
        let loss_rate = if total_interval_bytes == 0 {
            0.0
        } else {
            self.interval_lost as f64 / total_interval_bytes as f64
        };
        let throughput = self.interval_acked as f64 / elapsed_secs;
        let arm_den = (self.arms.len().saturating_sub(1)).max(1) as f64;

        [
            1.0,
            normalize(cwnd / mss, 2000.0),
            normalize(bytes_in_flight / cwnd.max(1.0), 4.0),
            normalize_duration(self.latest_rtt),
            normalize_duration(self.srtt),
            normalize_duration(self.rttvar),
            normalize_duration(self.min_rtt),
            normalize(mss, 1500.0),
            normalize(self.interval_acked as f64 / mss, 2000.0),
            normalize(self.interval_lost as f64 / mss, 2000.0),
            normalize(throughput, THROUGHPUT_NORMALIZATION_SCALE),
            loss_rate.clamp(0.0, 1.0),
            self.active_index as f64 / arm_den,
            self.previous_index as f64 / arm_den,
        ]
    }

    fn reward(&mut self, elapsed: Duration) -> f64 {
        let elapsed_secs = elapsed.as_secs_f64().max(0.000_001);
        let throughput = self.interval_acked as f64 / elapsed_secs;
        let total_interval_bytes = self.interval_acked.saturating_add(self.interval_lost);
        let loss_rate = if total_interval_bytes == 0 {
            0.0
        } else {
            self.interval_lost as f64 / total_interval_bytes as f64
        };

        let delay = self.srtt.as_secs_f64().max(MIN_DELAY);
        self.update_reward_normalization(throughput, delay);

        let loss_penalty = (self.zeta * loss_rate).clamp(0.0, 1.0);
        let effective_throughput = throughput * (1.0 - loss_penalty);
        let kappa = self.kappa.max(0.000_001);
        let utility = effective_throughput.max(0.0).powf(kappa) / delay;
        let rmax =
            (self.max_throughput.max(1.0).powf(kappa) / self.min_delay.max(MIN_DELAY)).max(1.0);

        (utility / rmax).clamp(0.0, 1.0)
    }

    fn update_reward_normalization(&mut self, throughput: f64, delay: f64) {
        if let Some(window) = self.throughput_windows.get_mut(self.active_index) {
            window.add(throughput);
        }
        if let Some(window) = self.delay_windows.get_mut(self.active_index) {
            window.add(delay);
        }

        self.max_throughput = self
            .throughput_windows
            .iter()
            .filter_map(AdwinWindow::max)
            .fold(1.0, f64::max);

        let min_delay = self
            .delay_windows
            .iter()
            .filter_map(AdwinWindow::min)
            .fold(f64::INFINITY, f64::min);
        self.min_delay = if min_delay.is_finite() {
            min_delay.max(MIN_DELAY)
        } else {
            delay.max(MIN_DELAY)
        };
    }

    pub fn new_controller(conf: MutantConfig) -> Self {
        Self::new(conf)
    }
}

impl CongestionController for Mutant {
    fn name(&self) -> &str {
        "MUTANT"
    }

    fn on_sent(&mut self, now: Instant, packet: &mut SentPacket, bytes_in_flight: u64) {
        let sent_bytes = packet.sent_size as u64;
        self.stats.bytes_in_flight = bytes_in_flight.saturating_add(sent_bytes);
        self.stats.bytes_sent_in_total = self.stats.bytes_sent_in_total.saturating_add(sent_bytes);
        if self.in_slow_start() {
            self.stats.bytes_sent_in_slow_start = self
                .stats
                .bytes_sent_in_slow_start
                .saturating_add(sent_bytes);
        }

        self.last_event_time = Some(now);
        self.dispatch_sent_to_arms(now, packet, bytes_in_flight);
    }

    fn begin_ack(&mut self, now: Instant, bytes_in_flight: u64) {
        self.stats.bytes_in_flight = bytes_in_flight;
        self.last_event_time = Some(now);

        for arm in self.arms.iter_mut() {
            arm.controller.begin_ack(now, bytes_in_flight);
        }
    }

    fn on_ack(
        &mut self,
        packet: &mut SentPacket,
        now: Instant,
        app_limited: bool,
        rtt: &RttEstimator,
        bytes_in_flight: u64,
    ) {
        let acked_bytes = packet.sent_size as u64;
        self.stats.bytes_in_flight = bytes_in_flight;
        self.stats.bytes_acked_in_total =
            self.stats.bytes_acked_in_total.saturating_add(acked_bytes);
        if self.in_slow_start() {
            self.stats.bytes_acked_in_slow_start = self
                .stats
                .bytes_acked_in_slow_start
                .saturating_add(acked_bytes);
        }

        self.interval_acked = self.interval_acked.saturating_add(acked_bytes);
        self.latest_rtt = rtt.latest_rtt();
        self.srtt = rtt.smoothed_rtt();
        self.rttvar = rtt.rttvar();
        self.min_rtt = self.min_rtt.min(rtt.min_rtt());
        self.last_event_time = Some(now);

        self.dispatch_ack_to_arms(packet, now, app_limited, rtt, bytes_in_flight);
    }

    fn end_ack(&mut self) {
        for arm in self.arms.iter_mut() {
            arm.controller.end_ack();
        }

        let now = self.last_event_time.unwrap_or_else(Instant::now);
        self.maybe_advance_policy(now);
    }

    fn on_congestion_event(
        &mut self,
        now: Instant,
        packet: &SentPacket,
        is_persistent_congestion: bool,
        lost_bytes: u64,
        bytes_in_flight: u64,
    ) {
        self.stats.bytes_lost_in_total = self.stats.bytes_lost_in_total.saturating_add(lost_bytes);
        self.stats.bytes_in_flight = bytes_in_flight;
        self.interval_lost = self.interval_lost.saturating_add(lost_bytes);
        if self.in_slow_start() {
            self.stats.bytes_lost_in_slow_start = self
                .stats
                .bytes_lost_in_slow_start
                .saturating_add(lost_bytes);
        }

        self.last_event_time = Some(now);
        for arm in self.arms.iter_mut() {
            arm.controller.on_congestion_event(
                now,
                packet,
                is_persistent_congestion,
                lost_bytes,
                bytes_in_flight,
            );
        }

        self.maybe_advance_policy(now);
    }

    fn in_slow_start(&self) -> bool {
        self.active_controller().in_slow_start()
    }

    fn in_recovery(&self, sent_time: Instant) -> bool {
        self.active_controller().in_recovery(sent_time)
    }

    fn congestion_window(&self) -> u64 {
        self.active_controller()
            .congestion_window()
            .max(self.min_cwnd)
    }

    fn set_congestion_window(&mut self, cwnd: u64) {
        let cwnd = cwnd.max(self.min_cwnd);
        for arm in self.arms.iter_mut() {
            arm.controller.set_congestion_window(cwnd);
        }
    }

    fn pacing_rate(&self) -> Option<u64> {
        self.active_controller().pacing_rate()
    }

    fn initial_window(&self) -> u64 {
        self.initial_cwnd
    }

    fn minimal_window(&self) -> u64 {
        self.min_cwnd
    }

    fn stats(&self) -> &CongestionStats {
        &self.stats
    }

    fn min_pacing_rate(&self) -> Option<u64> {
        self.active_controller().min_pacing_rate()
    }
}

#[derive(Debug)]
struct LinUcbPolicy {
    arms: Vec<LinUcbArm>,
    alpha: f64,
}

impl LinUcbPolicy {
    fn new(arm_count: usize, alpha: f64) -> Self {
        Self {
            arms: (0..arm_count).map(|_| LinUcbArm::new()).collect(),
            alpha,
        }
    }

    fn update(&mut self, arm_index: usize, context: &[f64; FEATURE_DIM], reward: f64) {
        if let Some(arm) = self.arms.get_mut(arm_index) {
            arm.update(context, reward);
        }
    }

    fn select(&self, context: &[f64; FEATURE_DIM]) -> usize {
        for (idx, arm) in self.arms.iter().enumerate() {
            if arm.pulls == 0 {
                return idx;
            }
        }

        let mut best_idx = 0;
        let mut best_score = f64::NEG_INFINITY;
        for (idx, arm) in self.arms.iter().enumerate() {
            let theta = solve_linear(arm.a, arm.b);
            let uncertainty_vec = solve_linear(arm.a, *context);
            let mean = dot(&theta, context);
            let variance = dot(context, &uncertainty_vec).max(0.0);
            let score = mean + self.alpha * variance.sqrt();
            if score > best_score {
                best_score = score;
                best_idx = idx;
            }
        }

        best_idx
    }
}

#[derive(Debug, Clone, Copy)]
struct LinUcbArm {
    a: [[f64; FEATURE_DIM]; FEATURE_DIM],
    b: [f64; FEATURE_DIM],
    pulls: u64,
}

impl LinUcbArm {
    fn new() -> Self {
        let mut a = [[0.0; FEATURE_DIM]; FEATURE_DIM];
        for (i, row) in a.iter_mut().enumerate() {
            row[i] = 1.0;
        }

        Self {
            a,
            b: [0.0; FEATURE_DIM],
            pulls: 0,
        }
    }

    fn update(&mut self, context: &[f64; FEATURE_DIM], reward: f64) {
        let reward = reward.clamp(0.0, 1.0);
        for i in 0..FEATURE_DIM {
            self.b[i] += reward * context[i];
            for j in 0..FEATURE_DIM {
                self.a[i][j] += context[i] * context[j];
            }
        }
        self.pulls = self.pulls.saturating_add(1);
    }
}

#[derive(Debug, Clone, Copy)]
struct AdwinSample {
    raw: f64,
    normalized: f64,
}

#[derive(Debug)]
struct AdwinWindow {
    delta: f64,
    normalization_scale: f64,
    samples: VecDeque<AdwinSample>,
    normalized_sum: f64,
}

impl AdwinWindow {
    fn new(delta: f64, normalization_scale: f64) -> Self {
        Self {
            delta,
            normalization_scale,
            samples: VecDeque::new(),
            normalized_sum: 0.0,
        }
    }

    fn add(&mut self, value: f64) -> bool {
        if !value.is_finite() {
            return false;
        }

        let normalized = normalize(value, self.normalization_scale);
        self.samples.push_back(AdwinSample {
            raw: value,
            normalized,
        });
        self.normalized_sum += normalized;

        while self.samples.len() > ADWIN_MAX_WINDOW {
            self.pop_front();
        }

        let mut changed = false;
        while let Some(cut) = self.detect_cut() {
            for _ in 0..cut {
                self.pop_front();
            }
            changed = true;
        }

        changed
    }

    fn detect_cut(&self) -> Option<usize> {
        let n = self.samples.len();
        if n < ADWIN_MIN_SUBWINDOW * 2 {
            return None;
        }

        let mut prefix = Vec::with_capacity(n + 1);
        prefix.push(0.0);
        for sample in &self.samples {
            let next = prefix[prefix.len() - 1] + sample.normalized;
            prefix.push(next);
        }

        for cut in ADWIN_MIN_SUBWINDOW..=(n - ADWIN_MIN_SUBWINDOW) {
            let n0 = cut as f64;
            let n1 = (n - cut) as f64;
            let mean0 = prefix[cut] / n0;
            let mean1 = (prefix[n] - prefix[cut]) / n1;
            let harmonic_mean = 1.0 / (1.0 / n0 + 1.0 / n1);
            let delta = (self.delta / n as f64).max(f64::MIN_POSITIVE);
            let epsilon = ((1.0 / (2.0 * harmonic_mean)) * (4.0 / delta).ln()).sqrt();

            if (mean0 - mean1).abs() > epsilon {
                return Some(cut);
            }
        }

        None
    }

    fn pop_front(&mut self) {
        if let Some(sample) = self.samples.pop_front() {
            self.normalized_sum -= sample.normalized;
        }
    }

    fn max(&self) -> Option<f64> {
        self.samples
            .iter()
            .map(|sample| sample.raw)
            .reduce(f64::max)
    }

    fn min(&self) -> Option<f64> {
        self.samples
            .iter()
            .map(|sample| sample.raw)
            .reduce(f64::min)
    }
}

fn normalize(value: f64, scale: f64) -> f64 {
    if !value.is_finite() || scale <= 0.0 {
        return 0.0;
    }

    (value / scale).clamp(0.0, 1.0)
}

fn normalize_duration(duration: Duration) -> f64 {
    normalize(duration.as_secs_f64(), 1.0)
}

fn dot(lhs: &[f64; FEATURE_DIM], rhs: &[f64; FEATURE_DIM]) -> f64 {
    let mut sum = 0.0;
    for i in 0..FEATURE_DIM {
        sum += lhs[i] * rhs[i];
    }
    sum
}

fn solve_linear(
    mut a: [[f64; FEATURE_DIM]; FEATURE_DIM],
    mut b: [f64; FEATURE_DIM],
) -> [f64; FEATURE_DIM] {
    for col in 0..FEATURE_DIM {
        let mut pivot = col;
        let mut pivot_abs = a[col][col].abs();
        for row in (col + 1)..FEATURE_DIM {
            let candidate = a[row][col].abs();
            if candidate > pivot_abs {
                pivot = row;
                pivot_abs = candidate;
            }
        }

        if pivot_abs < 1e-12 {
            return [0.0; FEATURE_DIM];
        }

        if pivot != col {
            a.swap(col, pivot);
            b.swap(col, pivot);
        }

        let divisor = a[col][col];
        for j in 0..FEATURE_DIM {
            a[col][j] /= divisor;
        }
        b[col] /= divisor;

        for row in 0..FEATURE_DIM {
            if row == col {
                continue;
            }

            let factor = a[row][col];
            if factor == 0.0 {
                continue;
            }

            for j in 0..FEATURE_DIM {
                a[row][j] -= factor * a[col][j];
            }
            b[row] -= factor * b[col];
        }
    }

    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linucb_explores_each_arm_then_prefers_rewarded_arm() {
        let mut policy = LinUcbPolicy::new(ARM_COUNT, DEFAULT_ALPHA);
        let mut context = [0.0; FEATURE_DIM];
        context[0] = 1.0;

        assert_eq!(policy.select(&context), 0);
        policy.update(0, &context, 0.0);
        assert_eq!(policy.select(&context), 1);
        policy.update(1, &context, 1.0);
        assert_eq!(policy.select(&context), 2);
        policy.update(2, &context, 0.0);

        assert_eq!(policy.select(&context), 1);
    }

    #[test]
    fn reward_handles_zero_ack_and_loss_penalty() {
        let mut mutant = Mutant::new(MutantConfig::default());
        let elapsed = Duration::from_millis(10);

        mutant.interval_acked = 0;
        mutant.interval_lost = 0;
        assert_eq!(mutant.reward(elapsed), 0.0);

        mutant.interval_acked = 12_000;
        mutant.interval_lost = 0;
        let no_loss_reward = mutant.reward(elapsed);
        assert!(no_loss_reward > 0.0);

        mutant.interval_acked = 12_000;
        mutant.interval_lost = 12_000;
        let loss_reward = mutant.reward(elapsed);
        assert!(loss_reward < no_loss_reward);
    }

    #[test]
    fn adwin_drops_old_samples_after_distribution_shift() {
        let mut window = AdwinWindow::new(DEFAULT_ADWIN_DELTA, 1.0);

        for _ in 0..100 {
            window.add(0.9);
        }
        assert_eq!(window.max(), Some(0.9));

        let mut changed = false;
        for _ in 0..100 {
            changed |= window.add(0.1);
        }

        assert!(changed);
        assert_eq!(window.max(), Some(0.1));
        assert_eq!(window.min(), Some(0.1));
    }

    #[test]
    fn reward_normalization_tracks_adaptive_window() {
        let mut mutant = Mutant::new(MutantConfig::default());

        for _ in 0..100 {
            mutant.update_reward_normalization(90_000_000.0, 0.05);
        }
        assert_eq!(mutant.max_throughput, 90_000_000.0);

        for _ in 0..100 {
            mutant.update_reward_normalization(10_000_000.0, 0.05);
        }

        assert_eq!(mutant.max_throughput, 10_000_000.0);
    }

    #[test]
    fn switch_keeps_current_cwnd() {
        let mut mutant = Mutant::new(MutantConfig::default());
        let target_cwnd = mutant.congestion_window() + 8 * mutant.max_datagram_size;

        mutant.set_congestion_window(target_cwnd);
        mutant.sync_active_cwnd(1);

        assert_eq!(mutant.active_index, 1);
        assert_eq!(mutant.congestion_window(), target_cwnd);
    }

    #[test]
    fn context_has_expected_dimension() {
        let mutant = Mutant::new(MutantConfig::default());
        let context = mutant.context(Duration::from_millis(10));

        assert_eq!(context.len(), FEATURE_DIM);
        assert_eq!(context[0], 1.0);
    }
}
