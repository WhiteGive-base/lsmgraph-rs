#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct Morris8(u8);

#[derive(Debug, Clone, Default)]
pub(crate) struct Morris8RunStats {
    pub attempted_by_exponent: [u64; 16],
    pub applied_by_exponent: [u64; 16],
    pub saturation_events: u64,
}

impl Morris8RunStats {
    pub fn merge(&mut self, other: &Self) {
        for exponent in 0..16 {
            self.attempted_by_exponent[exponent] += other.attempted_by_exponent[exponent];
            self.applied_by_exponent[exponent] += other.applied_by_exponent[exponent];
        }
        self.saturation_events += other.saturation_events;
    }
}

impl Morris8 {
    pub const MAX_ESTIMATE: u64 = 1_015_792;

    pub fn observe(degree: u64, seed: u64, stats: &mut Morris8RunStats) -> Self {
        let exact_prefix = degree.min(16);
        stats.attempted_by_exponent[0] += exact_prefix;
        stats.applied_by_exponent[0] += exact_prefix;
        let mut counter = if exact_prefix < 16 {
            Self(exact_prefix as u8)
        } else {
            Self(0x10)
        };
        let mut rng = SplitMix64::new(seed);
        for _ in exact_prefix..degree {
            counter.increment(rng.next_u64(), stats);
        }
        counter
    }

    pub fn observe_untracked(degree: u64, seed: u64) -> Self {
        let exact_prefix = degree.min(16);
        let mut counter = if exact_prefix < 16 {
            Self(exact_prefix as u8)
        } else {
            Self(0x10)
        };
        let mut rng = SplitMix64::new(seed);
        for _ in exact_prefix..degree {
            counter.increment_untracked(rng.next_u64());
        }
        counter
    }

    #[cfg(test)]
    pub fn raw(self) -> u8 {
        self.0
    }

    pub fn exponent(self) -> u8 {
        self.0 >> 4
    }

    pub fn mantissa(self) -> u8 {
        self.0 & 0x0f
    }

    pub fn estimate(self) -> u64 {
        let scale = 1u64 << self.exponent();
        (scale - 1) * 16 + scale * u64::from(self.mantissa())
    }

    fn increment(&mut self, random: u64, stats: &mut Morris8RunStats) {
        let exponent = usize::from(self.exponent());
        stats.attempted_by_exponent[exponent] += 1;
        if !accepted_at_exponent(exponent as u8, random) {
            return;
        }
        if self.0 == u8::MAX {
            stats.saturation_events += 1;
            return;
        }

        let mantissa = self.mantissa();
        self.0 = if mantissa < 15 {
            self.0 + 1
        } else {
            (self.exponent() + 1) << 4
        };
        stats.applied_by_exponent[exponent] += 1;
    }

    fn increment_untracked(&mut self, random: u64) {
        let exponent = self.exponent();
        if !accepted_at_exponent(exponent, random) || self.0 == u8::MAX {
            return;
        }
        self.0 = if self.mantissa() < 15 {
            self.0 + 1
        } else {
            (exponent + 1) << 4
        };
    }
}

fn accepted_at_exponent(exponent: u8, random: u64) -> bool {
    exponent == 0 || random & ((1u64 << exponent) - 1) == 0
}

#[derive(Debug, Clone, Copy)]
struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut value = self.state;
        value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        value ^ (value >> 31)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn morris8_is_one_byte_and_decodes_paper_maximum() {
        assert_eq!(std::mem::size_of::<Morris8>(), 1);
        let max = Morris8(u8::MAX);
        assert_eq!(max.exponent(), 15);
        assert_eq!(max.mantissa(), 15);
        assert_eq!(max.estimate(), Morris8::MAX_ESTIMATE);
    }

    #[test]
    fn first_sixteen_updates_are_exact() {
        let mut counter = Morris8::default();
        let mut stats = Morris8RunStats::default();
        for expected in 1..=15 {
            counter.increment(u64::MAX, &mut stats);
            assert_eq!(counter.estimate(), expected);
        }
        counter.increment(u64::MAX, &mut stats);
        assert_eq!(counter.raw(), 0x10);
        assert_eq!(counter.estimate(), 16);
        assert_eq!(stats.attempted_by_exponent[0], 16);
        assert_eq!(stats.applied_by_exponent[0], 16);
    }

    #[test]
    fn fixed_seed_is_reproducible() {
        let mut left_stats = Morris8RunStats::default();
        let mut right_stats = Morris8RunStats::default();
        let left = Morris8::observe(10_000, 0xa57e_2026, &mut left_stats);
        let right = Morris8::observe(10_000, 0xa57e_2026, &mut right_stats);
        assert_eq!(left, right);
        assert_eq!(
            left_stats.attempted_by_exponent,
            right_stats.attempted_by_exponent
        );
        assert_eq!(
            left_stats.applied_by_exponent,
            right_stats.applied_by_exponent
        );
    }

    #[test]
    fn acceptance_probability_tracks_each_exponent() {
        let samples = 1_000_000u64;
        for exponent in 1..=12u8 {
            let accepted = (0..samples)
                .filter(|sample| accepted_at_exponent(exponent, *sample))
                .count() as u64;
            let expected = samples as f64 / (1u64 << exponent) as f64;
            assert!((accepted as f64 - expected).abs() <= 1.0);
        }
    }

    #[test]
    fn saturated_counter_never_wraps() {
        let mut counter = Morris8(u8::MAX);
        let mut stats = Morris8RunStats::default();
        counter.increment(0, &mut stats);
        assert_eq!(counter.raw(), u8::MAX);
        assert_eq!(counter.estimate(), Morris8::MAX_ESTIMATE);
        assert_eq!(stats.saturation_events, 1);
    }
}
