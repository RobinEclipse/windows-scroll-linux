//! Allocation-free motion path and bounded, asynchronous-policy gesture state.
//! No I/O or synchronization events are performed here: callers commit output
//! frames, invalidate outstanding requests on cancellation, and apply results
//! only to the request that created the current Waiting phase.
use crate::config::Config;
use std::collections::BTreeSet;

pub const EV_SYN: u16 = 0;
pub const EV_KEY: u16 = 1;
pub const EV_REL: u16 = 2;
pub const SYN_REPORT: u16 = 0;
pub const SYN_DROPPED: u16 = 3;
pub const REL_X: u16 = 0;
pub const REL_Y: u16 = 1;
pub const REL_HWHEEL: u16 = 6;
pub const REL_WHEEL: u16 = 8;
pub const REL_WHEEL_HI_RES: u16 = 11;
pub const REL_HWHEEL_HI_RES: u16 = 12;
pub const BTN_MOUSE: u16 = 0x110;
#[cfg(test)]
pub const BTN_LEFT: u16 = 0x110;
#[cfg(test)]
pub const BTN_RIGHT: u16 = 0x111;
pub const BTN_MIDDLE: u16 = 0x112;
#[cfg(test)]
pub const BTN_SIDE: u16 = 0x113;
pub const BTN_JOYSTICK: u16 = 0x120;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Event {
    pub kind: u16,
    pub code: u16,
    pub value: i32,
}
impl Event {
    pub const fn new(kind: u16, code: u16, value: i32) -> Self {
        Self { kind, code, value }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Scroll,
    Native,
    Ignore,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Phase {
    Idle,
    Waiting,
    Pending,
    Held,
    Latched,
    Native,
    Ignore,
}

pub fn is_mouse_button(code: u16) -> bool {
    (BTN_MOUSE..BTN_JOYSTICK).contains(&code)
}
pub fn is_wheel(code: u16) -> bool {
    matches!(
        code,
        REL_WHEEL | REL_HWHEEL | REL_WHEEL_HI_RES | REL_HWHEEL_HI_RES
    )
}

#[derive(Debug)]
pub struct Engine {
    config: Config,
    pub phase: Phase,
    pub dx: f64,
    pub dy: f64,
    pub physical_middle: bool,
    waiting_moved: bool,
    held_moved: bool,
    acc_v: f64,
    acc_h: f64,
    notch_v: i32,
    notch_h: i32,
    forwarded: BTreeSet<u16>,
    suppressed: BTreeSet<u16>,
}

impl Engine {
    /// File loading should reject invalid configuration. Direct callers are
    /// still safe: an invalid struct is replaced with bounded defaults.
    pub fn new(config: Config) -> Self {
        let config = if config.validate().is_ok() {
            config
        } else {
            Config::default()
        };
        Self {
            config,
            phase: Phase::Idle,
            dx: 0.0,
            dy: 0.0,
            physical_middle: false,
            waiting_moved: false,
            held_moved: false,
            acc_v: 0.0,
            acc_h: 0.0,
            notch_v: 0,
            notch_h: 0,
            forwarded: BTreeSet::new(),
            suppressed: BTreeSet::new(),
        }
    }
    #[cfg(test)]
    pub fn config(&self) -> &Config {
        &self.config
    }
    pub fn captures_motion(&self) -> bool {
        matches!(
            self.phase,
            Phase::Waiting | Phase::Pending | Phase::Held | Phase::Latched
        )
    }
    pub fn scrolling(&self) -> bool {
        matches!(self.phase, Phase::Held | Phase::Latched)
    }
    pub fn waiting(&self) -> bool {
        self.phase == Phase::Waiting
    }
    pub fn forwarded_buttons(&self) -> &BTreeSet<u16> {
        &self.forwarded
    }

    fn write(&mut self, event: Event, out: &mut Vec<Event>) {
        if event.kind == EV_KEY {
            match event.value {
                1 => {
                    if !self.forwarded.insert(event.code) {
                        return;
                    }
                }
                0 => {
                    if !self.forwarded.remove(&event.code) {
                        return;
                    }
                }
                2 => {
                    if !self.forwarded.contains(&event.code) {
                        return;
                    }
                }
                _ => return,
            }
        }
        out.push(event);
    }
    fn reset_motion(&mut self) {
        self.dx = 0.0;
        self.dy = 0.0;
        self.acc_v = 0.0;
        self.acc_h = 0.0;
        self.notch_v = 0;
        self.notch_h = 0;
        self.waiting_moved = false;
        self.held_moved = false;
    }
    fn restore_motion(&mut self, out: &mut Vec<Event>) {
        for (code, value) in [(REL_X, self.dx as i32), (REL_Y, self.dy as i32)] {
            if value != 0 {
                out.push(Event::new(EV_REL, code, value));
            }
        }
    }
    /// Cancel a gesture without synthesizing a native click. A native press
    /// already delivered is balanced immediately, then its physical release is
    /// suppressed. Ordinary held buttons retain their normal release path.
    pub fn cancel(&mut self, snap: bool, out: &mut Vec<Event>) {
        let captured = self.captures_motion();
        if self.forwarded.contains(&BTN_MIDDLE) {
            self.write(Event::new(EV_KEY, BTN_MIDDLE, 0), out);
        }
        if self.physical_middle {
            self.suppressed.insert(BTN_MIDDLE);
        }
        self.phase = Phase::Idle;
        if captured && snap && self.config.snap_cursor_on_release {
            self.restore_motion(out);
        }
        self.reset_motion();
    }
    /// Suppress a cancellation press from this device when another device
    /// owns the active gesture. Never steal a previously forwarded release.
    pub fn suppress_button(&mut self, code: u16) {
        if !self.forwarded.contains(&code) {
            self.suppressed.insert(code);
        }
    }
    /// Resolve only the currently waiting request. The caller must additionally
    /// match its request token: Waiting can belong to a newer gesture.
    pub fn resolve(&mut self, mode: Mode, out: &mut Vec<Event>) {
        if !self.waiting() {
            return;
        }
        match mode {
            Mode::Native if !self.waiting_moved && self.forwarded.is_empty() => {
                self.reset_motion();
                self.write(Event::new(EV_KEY, BTN_MIDDLE, 1), out);
                if self.physical_middle {
                    self.phase = Phase::Native;
                } else {
                    self.write(Event::new(EV_KEY, BTN_MIDDLE, 0), out);
                    self.phase = Phase::Idle;
                }
            }
            Mode::Scroll => {
                if self.physical_middle {
                    self.phase = if self.held_moved {
                        Phase::Held
                    } else {
                        Phase::Pending
                    };
                } else if !self.held_moved {
                    self.phase = Phase::Latched;
                } else {
                    // A whole hold-and-drag completed before inspection. It is
                    // too late to autoscroll; restore pointer displacement only.
                    self.cancel(true, out);
                }
            }
            Mode::Ignore | Mode::Native => {
                // A delayed native verdict is unsafe after any motion. Preserve
                // the user's pointer movement, but discard the wheel click.
                self.restore_motion(out);
                self.reset_motion();
                self.phase = if self.physical_middle {
                    Phase::Ignore
                } else {
                    Phase::Idle
                };
            }
        }
    }
    /// Return true only when this event begins a new asynchronous inspection.
    pub fn handle(&mut self, event: Event, out: &mut Vec<Event>) -> bool {
        if event.kind == EV_SYN {
            return false;
        }
        if event.kind == EV_KEY && event.code == BTN_MIDDLE {
            if event.value == 1 {
                self.physical_middle = true;
            } else if event.value == 0 {
                self.physical_middle = false;
            }
        }
        if event.kind == EV_KEY && self.suppressed.contains(&event.code) {
            if event.value == 0 {
                self.suppressed.remove(&event.code);
            }
            return false;
        }
        if event.kind == EV_KEY && event.code == BTN_MIDDLE {
            match (self.phase, event.value) {
                (Phase::Idle, 1) => {
                    self.reset_motion();
                    if self.forwarded.is_empty() {
                        self.phase = Phase::Waiting;
                        return true;
                    }
                    self.phase = Phase::Ignore;
                    return false;
                }
                (Phase::Native, 0) => {
                    self.write(event, out);
                    self.phase = Phase::Idle;
                    return false;
                }
                (Phase::Native, _) => return false,
                (Phase::Ignore, 0) => {
                    self.phase = Phase::Idle;
                    return false;
                }
                (Phase::Pending, 0) => {
                    self.phase = Phase::Latched;
                    return false;
                }
                (Phase::Held, 0) => {
                    self.cancel(true, out);
                    return false;
                }
                (Phase::Waiting, 0) => return false,
                (_, 1) if self.captures_motion() => {
                    self.cancel(true, out);
                    self.suppress_button(BTN_MIDDLE);
                    return false;
                }
                _ => return false,
            }
        }
        if event.kind == EV_KEY
            && is_mouse_button(event.code)
            && event.value == 1
            && self.captures_motion()
        {
            self.cancel(true, out);
            self.suppress_button(event.code);
            return false;
        }
        if event.kind == EV_REL && matches!(event.code, REL_X | REL_Y) && self.captures_motion() {
            let offset = if event.code == REL_X {
                &mut self.dx
            } else {
                &mut self.dy
            };
            *offset = (*offset + f64::from(event.value))
                .clamp(-self.config.max_drag_px, self.config.max_drag_px);
            if self.waiting() && event.value != 0 {
                self.waiting_moved = true;
            }
            if self.physical_middle && self.dx.abs().max(self.dy.abs()) > self.config.deadzone_px {
                self.held_moved = true;
                if self.phase == Phase::Pending {
                    self.phase = Phase::Held;
                }
            }
            return false;
        }
        if event.kind == EV_REL
            && is_wheel(event.code)
            && event.value != 0
            && self.captures_motion()
        {
            self.cancel(true, out);
        }
        if matches!(event.kind, EV_REL | EV_KEY) {
            self.write(event, out);
        }
        false
    }
    fn speed(&self, offset: f64) -> f64 {
        let distance = offset.abs();
        if distance <= self.config.deadzone_px {
            return 0.0;
        }
        // Compare in log space before exponentiation. Even future increases in
        // tuning bounds cannot overflow the speed calculation before its cap.
        let log_speed = self.config.speed_mult.ln() + self.config.speed_exp * distance.ln();
        let speed = if log_speed >= self.config.max_px_per_sec.ln() {
            self.config.max_px_per_sec
        } else {
            log_speed.exp()
        };
        speed.copysign(offset)
    }
    pub fn tick(&mut self, dt: f64, out: &mut Vec<Event>) {
        if !self.scrolling() || !dt.is_finite() || dt <= 0.0 {
            return;
        }
        // A delayed process never catches up with a burst of stale scrolling.
        let dt = dt.min(0.05);
        let sign = if self.config.natural { 1.0 } else { -1.0 };
        let factor = 120.0 / self.config.px_per_notch;
        self.acc_v += sign * self.speed(self.dy) * factor * dt;
        self.acc_h += -sign * self.speed(self.dx) * factor * dt;
        for (acc, notch, hires, legacy) in [
            (
                &mut self.acc_v,
                &mut self.notch_v,
                REL_WHEEL_HI_RES,
                REL_WHEEL,
            ),
            (
                &mut self.acc_h,
                &mut self.notch_h,
                REL_HWHEEL_HI_RES,
                REL_HWHEEL,
            ),
        ] {
            let value = *acc as i32;
            if value == 0 {
                continue;
            }
            *acc -= f64::from(value);
            *notch += value;
            out.push(Event::new(EV_REL, hires, value));
            let steps = *notch / 120;
            if steps != 0 {
                *notch -= steps * 120;
                out.push(Event::new(EV_REL, legacy, steps));
            }
        }
    }
    /// Recover from SYN_DROPPED or a fresh attachment without inventing presses.
    /// This explicitly reconciles suppressed releases, fixing the old stale
    /// middle-suppression bug after a dropped release.
    pub fn resync(&mut self, active: &BTreeSet<u16>, out: &mut Vec<Event>) {
        self.physical_middle = active.contains(&BTN_MIDDLE);
        self.cancel(false, out);
        let released: Vec<_> = self.forwarded.difference(active).copied().collect();
        for code in released {
            self.write(Event::new(EV_KEY, code, 0), out);
        }
        self.suppressed.retain(|code| active.contains(code));
        for code in active {
            if !self.forwarded.contains(code) {
                self.suppressed.insert(*code);
            }
        }
    }
    pub fn release_all(&mut self, out: &mut Vec<Event>) {
        self.cancel(false, out);
        let pressed: Vec<_> = self.forwarded.iter().copied().collect();
        for code in pressed {
            self.write(Event::new(EV_KEY, code, 0), out);
        }
        self.suppressed.clear();
        self.physical_middle = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn key(code: u16, value: i32) -> Event {
        Event::new(EV_KEY, code, value)
    }
    fn rel(code: u16, value: i32) -> Event {
        Event::new(EV_REL, code, value)
    }
    fn new() -> (Engine, Vec<Event>) {
        (Engine::new(Config::default()), Vec::with_capacity(64))
    }
    fn start(e: &mut Engine, out: &mut Vec<Event>, mode: Mode) {
        assert!(e.handle(key(BTN_MIDDLE, 1), out));
        e.resolve(mode, out);
    }
    fn latch(e: &mut Engine, out: &mut Vec<Event>) {
        start(e, out, Mode::Scroll);
        e.handle(key(BTN_MIDDLE, 0), out);
    }
    #[test]
    fn quick_click_latches_without_middle_output() {
        let (mut e, mut out) = new();
        latch(&mut e, &mut out);
        assert_eq!(e.phase, Phase::Latched);
        assert!(out.is_empty());
        e.handle(rel(REL_Y, 80), &mut out);
        e.tick(0.02, &mut out);
        assert!(
            out.iter()
                .any(|v| v.code == REL_WHEEL_HI_RES && v.value < 0)
        );
        assert!(!out.iter().any(|v| v.kind == EV_KEY));
    }
    #[test]
    fn hold_drag_release_stops_and_restores_pointer() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Scroll);
        e.handle(rel(REL_Y, 80), &mut out);
        assert_eq!(e.phase, Phase::Held);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert_eq!(e.phase, Phase::Idle);
        assert_eq!(out, [rel(REL_Y, 80)]);
        assert!(!e.suppressed.contains(&BTN_MIDDLE));
    }
    #[test]
    fn every_mouse_button_cancels_and_consumes_press_release() {
        for button in BTN_MOUSE..BTN_JOYSTICK {
            let (mut e, mut out) = new();
            latch(&mut e, &mut out);
            e.handle(key(button, 1), &mut out);
            e.handle(key(button, 0), &mut out);
            assert_eq!(e.phase, Phase::Idle);
            assert!(out.is_empty(), "button {button}");
            assert!(e.suppressed.is_empty());
        }
    }
    #[test]
    fn native_click_is_balanced_and_motion_is_ordinary() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Native);
        e.handle(rel(REL_X, 42), &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert_eq!(
            out,
            [key(BTN_MIDDLE, 1), rel(REL_X, 42), key(BTN_MIDDLE, 0)]
        );
        assert_eq!(e.phase, Phase::Idle);
        assert!(e.forwarded.is_empty());
    }
    #[test]
    fn native_cancel_balances_press_and_swallows_physical_release() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Native);
        e.cancel(false, &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert_eq!(out, [key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
        assert!(e.suppressed.is_empty());
    }
    #[test]
    fn ignore_never_emits_middle_and_forwards_movement() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Ignore);
        e.handle(rel(REL_X, 30), &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert_eq!(out, [rel(REL_X, 30)]);
        assert_eq!(e.phase, Phase::Idle);
    }
    #[test]
    fn release_before_response_latches_scroll() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        e.handle(rel(REL_X, 100), &mut out);
        e.resolve(Mode::Scroll, &mut out);
        assert_eq!(e.phase, Phase::Latched);
        assert_eq!(e.dx, 100.0);
        assert!(out.is_empty());
    }
    #[test]
    fn held_motion_before_response_scrolls_when_resolved() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(rel(REL_Y, 100), &mut out);
        e.resolve(Mode::Scroll, &mut out);
        assert_eq!(e.phase, Phase::Held);
        assert!(out.is_empty());
    }
    #[test]
    fn entire_drag_before_response_does_not_latch() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(rel(REL_Y, 100), &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        e.resolve(Mode::Scroll, &mut out);
        assert_eq!(e.phase, Phase::Idle);
        assert_eq!(out, [rel(REL_Y, 100)]);
    }
    #[test]
    fn release_before_native_response_emits_one_balanced_pair() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        e.resolve(Mode::Native, &mut out);
        assert_eq!(out, [key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
        assert_eq!(e.phase, Phase::Idle);
    }
    #[test]
    fn any_pending_motion_invalidates_native_even_if_reversed() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(rel(REL_X, 1), &mut out);
        e.handle(rel(REL_X, -1), &mut out);
        e.resolve(Mode::Native, &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert!(out.is_empty());
        assert_eq!(e.phase, Phase::Idle);
    }
    #[test]
    fn ignored_response_restores_bounded_pending_motion() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(rel(REL_X, i32::MAX), &mut out);
        e.resolve(Mode::Ignore, &mut out);
        assert_eq!(out, [rel(REL_X, 1200)]);
        assert_eq!(e.phase, Phase::Ignore);
    }
    #[test]
    fn cancellation_while_waiting_rejects_late_response() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.cancel(false, &mut out);
        e.resolve(Mode::Scroll, &mut out);
        assert_eq!(e.phase, Phase::Idle);
        assert!(out.is_empty());
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
    }
    #[test]
    fn second_press_while_waiting_cancels_instead_of_restarting() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert!(!e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.resolve(Mode::Native, &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert!(out.is_empty());
    }
    #[test]
    fn waiting_cancellation_consumes_other_button_and_middle_release() {
        let (mut e, mut out) = new();
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(key(BTN_SIDE, 1), &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        e.handle(key(BTN_SIDE, 0), &mut out);
        assert!(out.is_empty());
        assert!(e.suppressed.is_empty());
        assert_eq!(e.phase, Phase::Idle);
    }
    #[test]
    fn wheel_cancels_and_preserves_wheel_event() {
        for axis in [REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES] {
            let (mut e, mut out) = new();
            latch(&mut e, &mut out);
            e.handle(rel(axis, 1), &mut out);
            assert_eq!(e.phase, Phase::Idle);
            assert_eq!(out, [rel(axis, 1)]);
        }
    }
    #[test]
    fn deadzone_motion_latches_and_has_no_scroll_speed() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Scroll);
        e.handle(rel(REL_X, 15), &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        e.tick(0.04, &mut out);
        assert_eq!(e.phase, Phase::Latched);
        assert!(out.is_empty());
    }
    #[test]
    fn overflow_inputs_and_large_ticks_remain_bounded() {
        let config = Config::parse("MAX_DRAG_PX=10000\nSPEED_MULT=1000\nSPEED_EXP=8\nMAX_PX_PER_SEC=100000\nPX_PER_NOTCH=1").unwrap();
        let mut e = Engine::new(config);
        let mut out = Vec::new();
        latch(&mut e, &mut out);
        for _ in 0..100 {
            e.handle(rel(REL_X, i32::MAX), &mut out);
            e.handle(rel(REL_Y, i32::MIN), &mut out);
            e.tick(f64::MAX, &mut out);
        }
        assert_eq!(e.dx, 10000.0);
        assert_eq!(e.dy, -10000.0);
        assert!(out.iter().all(|v| v.value.abs() <= 600_000));
        assert!(e.acc_v.is_finite() && e.acc_h.is_finite());
    }
    #[test]
    fn invalid_direct_config_uses_defaults() {
        let e = Engine::new(Config {
            speed_exp: f64::NAN,
            ..Config::default()
        });
        assert_eq!(e.config(), &Config::default());
    }
    #[test]
    fn invalid_ticks_do_not_change_accumulators() {
        let (mut e, mut out) = new();
        latch(&mut e, &mut out);
        e.handle(rel(REL_Y, 100), &mut out);
        for dt in [f64::NAN, f64::INFINITY, -1.0, 0.0] {
            e.tick(dt, &mut out);
        }
        assert!(out.is_empty());
        assert_eq!(e.acc_v, 0.0);
    }
    #[test]
    fn dropped_middle_release_does_not_swallow_next_click() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Scroll);
        e.handle(rel(REL_X, 30), &mut out);
        e.resync(&BTreeSet::new(), &mut out);
        assert!(e.suppressed.is_empty());
        assert!(!e.physical_middle);
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.resolve(Mode::Scroll, &mut out);
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert_eq!(e.phase, Phase::Latched);
    }
    #[test]
    fn resync_held_middle_is_suppressed_until_release() {
        let (mut e, mut out) = new();
        e.resync(&BTreeSet::from([BTN_MIDDLE]), &mut out);
        assert!(!e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        assert!(out.is_empty());
        assert!(e.handle(key(BTN_MIDDLE, 1), &mut out));
    }
    #[test]
    fn resync_balances_missing_releases_and_preserves_existing_hold() {
        let (mut e, mut out) = new();
        e.handle(key(BTN_LEFT, 1), &mut out);
        e.handle(key(BTN_RIGHT, 1), &mut out);
        e.resync(&BTreeSet::from([BTN_RIGHT, BTN_SIDE]), &mut out);
        assert_eq!(out, [key(BTN_LEFT, 1), key(BTN_RIGHT, 1), key(BTN_LEFT, 0)]);
        e.handle(key(BTN_RIGHT, 0), &mut out);
        e.handle(key(BTN_SIDE, 0), &mut out);
        assert_eq!(out.last(), Some(&key(BTN_RIGHT, 0)));
        assert!(e.suppressed.is_empty());
        assert!(e.forwarded.is_empty());
    }
    #[test]
    fn forwarded_button_chord_cannot_start_middle_gesture() {
        let (mut e, mut out) = new();
        e.handle(key(BTN_LEFT, 1), &mut out);
        assert!(!e.handle(key(BTN_MIDDLE, 1), &mut out));
        e.handle(key(BTN_MIDDLE, 0), &mut out);
        e.handle(key(BTN_LEFT, 0), &mut out);
        assert_eq!(out, [key(BTN_LEFT, 1), key(BTN_LEFT, 0)]);
    }
    #[test]
    fn cross_device_suppression_preserves_preexisting_forwarded_release() {
        let (mut e, mut out) = new();
        e.handle(key(BTN_LEFT, 1), &mut out);
        e.suppress_button(BTN_LEFT);
        e.handle(key(BTN_LEFT, 0), &mut out);
        e.suppress_button(BTN_SIDE);
        e.handle(key(BTN_SIDE, 1), &mut out);
        e.handle(key(BTN_SIDE, 0), &mut out);
        assert_eq!(out, [key(BTN_LEFT, 1), key(BTN_LEFT, 0)]);
        assert!(e.suppressed.is_empty());
    }
    #[test]
    fn release_all_balances_every_forwarded_button_without_snap() {
        let (mut e, mut out) = new();
        start(&mut e, &mut out, Mode::Native);
        e.handle(key(BTN_LEFT, 1), &mut out);
        e.release_all(&mut out);
        assert_eq!(
            out,
            [
                key(BTN_MIDDLE, 1),
                key(BTN_LEFT, 1),
                key(BTN_MIDDLE, 0),
                key(BTN_LEFT, 0)
            ]
        );
        assert_eq!(e.phase, Phase::Idle);
        assert!(e.forwarded.is_empty());
        assert!(e.suppressed.is_empty());
    }
    #[test]
    fn natural_direction_reverses_both_axes() {
        let mut generated = Vec::new();
        for natural in [false, true] {
            let mut e = Engine::new(Config {
                natural,
                ..Config::default()
            });
            let mut out = Vec::new();
            latch(&mut e, &mut out);
            e.handle(rel(REL_X, 100), &mut out);
            e.handle(rel(REL_Y, 100), &mut out);
            e.tick(0.02, &mut out);
            generated.push(out);
        }
        assert_eq!(generated[0].len(), generated[1].len());
        for (a, b) in generated[0].iter().zip(&generated[1]) {
            assert_eq!(a.code, b.code);
            assert_eq!(a.value, -b.value);
        }
    }
    #[test]
    fn generated_state_sequences_keep_buttons_balanced_and_memory_bounded() {
        // Deterministic property exercise: include malformed repeats, delayed
        // replies, disconnects and dropped-event recovery, not only happy paths.
        let mut rng = 0x6e21_8357_44ac_1f29_u64;
        for _ in 0..256 {
            let (mut e, mut out) = new();
            let mut delivered = BTreeSet::new();
            for _ in 0..512 {
                rng ^= rng << 13;
                rng ^= rng >> 7;
                rng ^= rng << 17;
                let choice = rng % 24;
                let mut native_permitted = false;
                match choice {
                    0..=7 => {
                        let code = [BTN_MIDDLE, BTN_LEFT, BTN_RIGHT, BTN_SIDE][choice as usize / 2];
                        e.handle(key(code, (choice % 2) as i32), &mut out);
                    }
                    8 | 9 => {
                        e.handle(
                            rel(if choice == 8 { REL_X } else { REL_Y }, (rng >> 32) as i32),
                            &mut out,
                        );
                    }
                    10 => {
                        e.handle(rel(REL_WHEEL, 1), &mut out);
                    }
                    11 => e.resolve(Mode::Scroll, &mut out),
                    12 => {
                        native_permitted =
                            e.waiting() && !e.waiting_moved && e.forwarded.is_empty();
                        e.resolve(Mode::Native, &mut out);
                    }
                    13 => e.resolve(Mode::Ignore, &mut out),
                    14 => e.cancel(true, &mut out),
                    15 => e.cancel(false, &mut out),
                    16 => e.tick(0.05, &mut out),
                    17 => e.resync(&BTreeSet::new(), &mut out),
                    18 => e.resync(&BTreeSet::from([BTN_MIDDLE, BTN_SIDE]), &mut out),
                    19 => e.release_all(&mut out),
                    20 => e.suppress_button(BTN_SIDE),
                    21 => {
                        e.handle(key(BTN_MIDDLE, 2), &mut out);
                    }
                    22 => {
                        e.handle(key(BTN_LEFT, 2), &mut out);
                    }
                    _ => {
                        e.handle(Event::new(EV_SYN, SYN_REPORT, 0), &mut out);
                    }
                }
                for event in out.drain(..) {
                    assert_ne!(event.kind, EV_SYN);
                    if event.kind != EV_KEY {
                        continue;
                    }
                    if event.value == 1 {
                        assert!(delivered.insert(event.code), "duplicate button down");
                        if event.code == BTN_MIDDLE {
                            assert!(native_permitted);
                        }
                    } else if event.value == 0 {
                        assert!(delivered.remove(&event.code), "release without press");
                    } else {
                        assert!(delivered.contains(&event.code));
                    }
                }
                assert_eq!(delivered, e.forwarded);
                assert!(e.forwarded.is_disjoint(&e.suppressed));
                assert!(e.dx.is_finite() && e.dx.abs() <= e.config.max_drag_px);
                assert!(e.dy.is_finite() && e.dy.abs() <= e.config.max_drag_px);
                assert!(e.acc_v.is_finite() && e.acc_v.abs() < 1.0);
                assert!(e.acc_h.is_finite() && e.acc_h.abs() < 1.0);
                assert!(e.notch_v.abs() < 120 && e.notch_h.abs() < 120);
            }
            e.release_all(&mut out);
            for event in out {
                if event.kind == EV_KEY {
                    assert_eq!(event.value, 0);
                    assert!(delivered.remove(&event.code));
                }
            }
            assert!(delivered.is_empty());
        }
    }
    #[test]
    fn pointer_snap_setting_does_not_change_click_latching() {
        let mut e = Engine::new(Config {
            snap_cursor_on_release: false,
            ..Config::default()
        });
        let mut out = Vec::new();
        latch(&mut e, &mut out);
        e.handle(rel(REL_X, 200), &mut out);
        e.handle(key(BTN_LEFT, 1), &mut out);
        e.handle(key(BTN_LEFT, 0), &mut out);
        assert!(out.is_empty());
        assert_eq!(e.phase, Phase::Idle);
    }
    #[test]
    fn no_sync_events_are_generated() {
        let (mut e, mut out) = new();
        e.handle(Event::new(EV_SYN, SYN_REPORT, 0), &mut out);
        start(&mut e, &mut out, Mode::Native);
        e.release_all(&mut out);
        assert!(out.iter().all(|v| v.kind != EV_SYN));
    }
}
