//! Request lifetime is independent of device reads. Cancellation invalidates all
//! outstanding decisions, including replies that arrive after pause/reconnect.
use crate::protocol::Identity;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct Request {
    pub id: u64,
    pub source: u64,
    pub peer: u64,
    pub epoch: u64,
    pub focus: String,
    pub allow_native: bool,
    pub send_after: Instant,
    pub deadline: Instant,
    pub sent: bool,
}
#[derive(Default)]
pub struct Coordinator {
    pub pending: Option<Request>,
    sequence: u64,
}
impl Coordinator {
    pub fn begin(
        &mut self,
        source: u64,
        peer: u64,
        identity: &Identity,
        allow_native: bool,
        now: Instant,
    ) -> u64 {
        self.sequence = self
            .sequence
            .checked_add(1)
            .expect("request sequence exhausted");
        self.pending = Some(Request {
            id: self.sequence,
            source,
            peer,
            epoch: identity.epoch,
            focus: identity.focus.clone(),
            allow_native,
            send_after: now + Duration::from_millis(6),
            deadline: now + Duration::from_millis(220),
            sent: false,
        });
        self.sequence
    }
    pub fn cancel(&mut self) {
        self.pending = None;
    }
    pub fn accepts(
        &self,
        id: u64,
        epoch: u64,
        peer: u64,
        identity: &Identity,
        focus: &str,
        now: Instant,
    ) -> bool {
        self.pending.as_ref().is_some_and(|p| {
            p.id == id
                && p.peer == peer
                && p.epoch == epoch
                && identity.epoch == epoch
                && identity.enabled
                && !identity.locked
                && p.focus == focus
                && identity.focus == focus
                && now <= p.deadline
                && p.sent
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn identity() -> Identity {
        Identity {
            session: "2".into(),
            seat: "seat0".into(),
            epoch: 5,
            enabled: true,
            locked: false,
            focus: "window".into(),
        }
    }
    #[test]
    fn cancelled_reply_cannot_reactivate_scroll() {
        let mut c = Coordinator::default();
        let i = identity();
        let n = Instant::now();
        let id = c.begin(1, 2, &i, true, n);
        c.pending.as_mut().unwrap().sent = true;
        assert!(c.accepts(id, 5, 2, &i, "window", n));
        c.cancel();
        assert!(!c.accepts(id, 5, 2, &i, "window", n));
    }
    #[test]
    fn wrong_peer_epoch_focus_or_deadline_never_authorizes() {
        let mut c = Coordinator::default();
        let i = identity();
        let n = Instant::now();
        let id = c.begin(1, 2, &i, false, n);
        c.pending.as_mut().unwrap().sent = true;
        assert!(!c.accepts(id, 5, 3, &i, "window", n));
        assert!(!c.accepts(id, 4, 2, &i, "window", n));
        assert!(!c.accepts(id, 5, 2, &i, "different", n));
        assert!(!c.accepts(id, 5, 2, &i, "window", n + Duration::from_millis(300)));
        let mut paused = i.clone();
        paused.enabled = false;
        assert!(!c.accepts(id, 5, 2, &paused, "window", n));
        paused.enabled = true;
        paused.epoch += 1;
        assert!(!c.accepts(id, 5, 2, &paused, "window", n));
    }
}
