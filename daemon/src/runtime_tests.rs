//! Exercise the real Runtime handlers. Default tests need no input privileges.
//! Ignored kernel tests use only their own synthetic mice: both source and
//! mirrored output contain `midscroll` in phys, and the output is exclusively
//! grabbed BEFORE the source emits anything. No desktop events are injected.
use super::*;
use crate::engine::{BTN_LEFT, BTN_MIDDLE, BTN_SIDE, Phase, REL_WHEEL, REL_X, REL_Y};
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::sync::atomic::AtomicUsize;

static FIXTURE_ID: AtomicUsize = AtomicUsize::new(0);
const PEER: u64 = 1;

struct Fixture {
    runtime: Option<Runtime>,
    control: UnixStream,
    sources: BTreeMap<u64, VirtualDevice>,
    observers: BTreeMap<u64, RawDevice>,
    directory: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let sequence = FIXTURE_ID.fetch_add(1, Ordering::Relaxed);
        let directory = std::env::temp_dir().join(format!(
            "midscroll-runtime-test-{}-{sequence}",
            std::process::id()
        ));
        fs::create_dir(&directory).unwrap();
        let socket_path = directory.join("state.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let lock = File::create(directory.join("lock")).unwrap();
        let login = Login::new().unwrap();
        let active = login
            .active()
            .expect("runtime integration tests require a local desktop session");
        assert_eq!(active.kind, "wayland");
        assert_eq!(active.seat, "seat0");
        assert!(!active.remote);
        let (daemon, control) = UnixStream::pair().unwrap();
        control.set_nonblocking(true).unwrap();
        let mut peer = Peer::new(daemon).unwrap();
        // Normal invocation uses the real socket UID. For the explicitly
        // privileged kernel tests, model the separate user's helper process;
        // SO_PEERCRED acquisition itself is covered by protocol unit tests.
        if unsafe { libc::geteuid() } == 0 {
            peer.uid = active.uid;
        }
        assert_eq!(peer.uid, active.uid);
        let config = Config::default();
        let mut runtime = Runtime {
            config,
            login,
            listener,
            socket_path,
            peers: BTreeMap::from([(PEER, peer)]),
            owner: None,
            active: Some(active.clone()),
            mice: BTreeMap::new(),
            keyboards: BTreeMap::new(),
            skipped: HashMap::new(),
            retries: HashMap::new(),
            serial: PEER,
            queries: Coordinator::default(),
            overlay: None,
            _lock: lock,
            born: Instant::now(),
            input_buffer: Vec::with_capacity(128),
        };
        runtime
            .message(
                PEER,
                Message::Hello {
                    version: 2,
                    session: active.id,
                    seat: active.seat,
                    epoch: 1,
                    enabled: true,
                    locked: false,
                    focus: "test-window".into(),
                },
            )
            .unwrap();
        Self {
            runtime: Some(runtime),
            control,
            sources: BTreeMap::new(),
            observers: BTreeMap::new(),
            directory,
        }
    }
    fn rt(&mut self) -> &mut Runtime {
        self.runtime.as_mut().unwrap()
    }
    fn begin(&mut self, source: u64) -> u64 {
        let runtime = self.rt();
        let identity = runtime.peers[&PEER].identity.as_ref().unwrap().clone();
        runtime
            .queries
            .begin(source, PEER, &identity, true, Instant::now())
    }
    fn wire(&mut self, message: serde_json::Value) {
        let mut bytes = serde_json::to_vec(&message).unwrap();
        bytes.push(b'\n');
        self.control.write_all(&bytes).unwrap();
        let messages = self
            .rt()
            .peers
            .get_mut(&PEER)
            .unwrap()
            .read_messages()
            .unwrap();
        assert_eq!(messages.len(), 1);
        for message in messages {
            self.rt().message(PEER, message).unwrap();
        }
    }
    fn send_due_query(&mut self) -> u64 {
        let pending = self
            .rt()
            .queries
            .pending
            .as_mut()
            .expect("gesture should request inspection");
        pending.send_after = Instant::now() - Duration::from_millis(1);
        let id = pending.id;
        self.rt().timer(0.0).unwrap();
        assert!(self.rt().queries.pending.as_ref().unwrap().sent);
        id
    }
    fn decision(&mut self, id: u64, epoch: u64, mode: &str) {
        self.wire(json!({"type":"decision", "id":id, "epoch":epoch,
            "mode":mode, "x":100.0, "y":100.0, "focus":"test-window"}));
    }
    fn helper_output(&mut self) -> Vec<serde_json::Value> {
        let mut bytes = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            match self.control.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => bytes.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) => panic!("helper output failed: {e}"),
            }
        }
        bytes
            .split(|b| *b == b'\n')
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_slice(line).unwrap())
            .collect()
    }
    fn mouse(&mut self) -> u64 {
        let id = self.rt().next_id();
        let keys: AttributeSet<KeyCode> = (engine::BTN_MOUSE..engine::BTN_JOYSTICK)
            .map(KeyCode)
            .collect();
        let axes: AttributeSet<RelativeAxisCode> = [REL_X, REL_Y, REL_WHEEL]
            .into_iter()
            .map(RelativeAxisCode)
            .collect();
        let mut source = VirtualDevice::builder()
            .unwrap()
            .name(&format!("midscroll isolated test {id}"))
            .with_phys(c"midscroll-isolated-test-source")
            .unwrap()
            .with_keys(&keys)
            .unwrap()
            .with_relative_axes(&axes)
            .unwrap()
            .build()
            .unwrap();
        let (path, raw) = open_virtual(&mut source);
        assert_eq!(raw.physical_path(), Some("midscroll-isolated-test-source"));
        let mut mouse = attach(
            path.clone(),
            device_identity(&path).unwrap(),
            raw,
            self.rt().config.clone(),
        )
        .unwrap();
        // Nothing has been emitted. Prevent every mirrored event from reaching
        // desktop clients before exercising any runtime path below.
        let (_, mut observer) = open_virtual(&mut mouse.virtual_device);
        assert_eq!(observer.physical_path(), Some("midscroll-v2"));
        observer.grab().unwrap();
        nonblocking(observer.as_raw_fd()).unwrap();
        mouse.last_motion = Instant::now() - Duration::from_secs(1);
        mouse.last_flushed_motion = Instant::now() - Duration::from_secs(1);
        self.rt().mice.insert(id, mouse);
        self.observers.insert(id, observer);
        self.sources.insert(id, source);
        id
    }
    fn input(&mut self, id: u64, events: &[InputEvent]) {
        self.sources.get_mut(&id).unwrap().emit(events).unwrap();
        self.rt().mouse_events(id).unwrap();
    }
    fn output_frames(&mut self, id: u64) -> Vec<Vec<Event>> {
        let mut frames = Vec::new();
        let mut pending = Vec::new();
        loop {
            match self.observers.get_mut(&id).unwrap().fetch_events() {
                Ok(events) => {
                    for event in events {
                        if event.event_type().0 == engine::EV_SYN
                            && event.code() == engine::SYN_REPORT
                        {
                            frames.push(std::mem::take(&mut pending));
                        } else {
                            pending.push(Event::new(
                                event.event_type().0,
                                event.code(),
                                event.value(),
                            ));
                        }
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) => panic!("mirror frame read failed: {e}"),
            }
        }
        assert!(
            pending.is_empty(),
            "virtual output left an incomplete frame"
        );
        frames
    }
    fn output(&mut self, id: u64) -> Vec<Event> {
        let mut result = Vec::new();
        loop {
            match self.observers.get_mut(&id).unwrap().fetch_events() {
                Ok(events) => result.extend(
                    events
                        .filter(|e| e.event_type().0 != engine::EV_SYN)
                        .map(|e| Event::new(e.event_type().0, e.code(), e.value())),
                ),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) => panic!("mirror read failed: {e}"),
            }
        }
        result
    }
    fn latch(&mut self, id: u64) {
        self.input(id, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
        assert_eq!(self.rt().mice[&id].engine.phase, Phase::Waiting);
        let query = self.send_due_query();
        self.decision(query, 1, "scroll");
        assert_eq!(self.rt().mice[&id].engine.phase, Phase::Latched);
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        // Keep mirror observers grabbed until all release events have been
        // emitted and virtual outputs destroyed, including assertion failures.
        if let Some(runtime) = self.runtime.as_mut() {
            let _ = runtime.cancel_all(false);
            runtime.mice.clear();
        }
        self.sources.clear();
        self.observers.clear();
        self.runtime.take();
        let _ = fs::remove_dir_all(&self.directory);
    }
}
fn open_virtual(device: &mut VirtualDevice) -> (PathBuf, RawDevice) {
    let sysfs = device.get_syspath().unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Ok(entries) = fs::read_dir(&sysfs) {
            for entry in entries.flatten() {
                if entry.file_name().to_string_lossy().starts_with("event") {
                    let path = Path::new("/dev/input").join(entry.file_name());
                    if let Ok(raw) = RawDevice::open(&path) {
                        return (path, raw);
                    }
                }
            }
        }
        assert!(
            Instant::now() < deadline,
            "test virtual input not readable; kernel tests require /dev/input access"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn key(code: u16, value: i32) -> InputEvent {
    InputEvent::new(EV_KEY, code, value)
}
fn rel(code: u16, value: i32) -> InputEvent {
    InputEvent::new(EV_REL, code, value)
}

#[test]
#[ignore = "requires an active local Wayland session"]
fn wire_pause_invalidates_pending_and_stale_decision() {
    let mut f = Fixture::new();
    let request = f.begin(999);
    f.send_due_query();
    f.wire(
        json!({"type":"state", "epoch":2, "enabled":false, "locked":false, "focus":"test-window"}),
    );
    assert!(f.rt().queries.pending.is_none());
    f.decision(request, 1, "scroll");
    assert!(f.rt().queries.pending.is_none());
    assert!(!f.rt().peers[&PEER].identity.as_ref().unwrap().enabled);
}
#[test]
#[ignore = "requires an active local Wayland session"]
fn wire_focus_change_and_disconnect_invalidate_pending() {
    let mut f = Fixture::new();
    f.begin(999);
    f.send_due_query();
    f.wire(json!({"type":"state", "epoch":2, "enabled":true, "locked":false, "focus":"different-window"}));
    assert!(f.rt().queries.pending.is_none());
    f.begin(999);
    f.rt().remove_peer(PEER).unwrap();
    assert!(f.rt().owner.is_none());
    assert!(f.rt().queries.pending.is_none());
}
#[test]
#[ignore = "requires an active local Wayland session"]
fn wire_invalid_session_cannot_take_helper_ownership() {
    let mut f = Fixture::new();
    let (daemon, _client) = UnixStream::pair().unwrap();
    let mut peer = Peer::new(daemon).unwrap();
    peer.uid = f.rt().active.as_ref().unwrap().uid;
    f.rt().peers.insert(77, peer);
    f.rt()
        .message(
            77,
            Message::Hello {
                version: 2,
                session: "wrong-session".into(),
                seat: "seat0".into(),
                epoch: 1,
                enabled: true,
                locked: false,
                focus: "test-window".into(),
            },
        )
        .unwrap();
    assert_eq!(f.rt().owner, Some(PEER));
    assert!(!f.rt().peers.contains_key(&77));
}
#[test]
#[ignore = "requires an active local Wayland session"]
fn runtime_request_timeout_is_bounded_and_nonblocking() {
    let mut f = Fixture::new();
    f.begin(999);
    f.rt().queries.pending.as_mut().unwrap().deadline = Instant::now() - Duration::from_millis(1);
    f.rt().timer(0.0).unwrap();
    assert!(f.rt().queries.pending.is_none());
}
#[test]
#[ignore = "requires an active local Wayland session"]
fn runtime_teardown_only_removes_owned_socket() {
    let original = fs::symlink_metadata(SOCKET)
        .ok()
        .map(|m| (m.dev(), m.ino()));
    let f = Fixture::new();
    let path = f.runtime.as_ref().unwrap().socket_path.clone();
    assert!(path.exists());
    drop(f);
    assert!(!path.exists());
    assert_eq!(
        fs::symlink_metadata(SOCKET)
            .ok()
            .map(|m| (m.dev(), m.ino())),
        original
    );
}

#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_quick_click_waits_without_paste_then_scrolls_and_stops() {
    let mut f = Fixture::new();
    let id = f.mouse();
    f.latch(id);
    assert!(f.output(id).is_empty());
    let helper = f.helper_output();
    assert!(helper.iter().any(|v| v["type"] == "start"));
    f.input(id, &[rel(REL_Y, 100)]);
    f.rt().timer(0.02).unwrap();
    let out = f.output(id);
    assert!(
        out.iter()
            .any(|e| e.code == engine::REL_WHEEL_HI_RES && e.value < 0)
    );
    assert!(!out.iter().any(|e| e.kind == EV_KEY));
    f.input(id, &[key(BTN_SIDE, 1), key(BTN_SIDE, 0)]);
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Idle);
    assert!(!f.output(id).iter().any(|e| e.kind == EV_KEY));
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_native_pair_is_balanced_and_pause_releases_native_hold() {
    let mut f = Fixture::new();
    let id = f.mouse();
    f.input(id, &[key(BTN_MIDDLE, 1)]);
    let request = f.send_due_query();
    f.decision(request, 1, "native");
    assert_eq!(f.output(id), [Event::new(EV_KEY, BTN_MIDDLE, 1)]);
    f.wire(json!({"type":"state","epoch":2,"enabled":false,"locked":false,"focus":"test-window"}));
    f.input(id, &[key(BTN_MIDDLE, 0)]);
    assert_eq!(f.output(id), [Event::new(EV_KEY, BTN_MIDDLE, 0)]);
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Idle);
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_same_report_and_other_mouse_motion_veto_native() {
    let mut f = Fixture::new();
    let a = f.mouse();
    let b = f.mouse();
    f.input(a, &[rel(REL_X, 20), key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    assert!(!f.rt().queries.pending.as_ref().unwrap().allow_native);
    let request = f.send_due_query();
    f.decision(request, 1, "native");
    assert_eq!(f.output(a), [Event::new(EV_REL, REL_X, 20)]);
    f.input(b, &[rel(REL_X, 20)]);
    f.input(a, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    assert!(!f.rt().queries.pending.as_ref().unwrap().allow_native);
    let request = f.send_due_query();
    f.decision(request, 1, "native");
    assert!(f.output(a).is_empty());
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_second_mouse_chord_motion_and_wheel_are_coordinated() {
    let mut f = Fixture::new();
    let a = f.mouse();
    let b = f.mouse();
    f.input(b, &[key(BTN_LEFT, 1)]);
    f.input(a, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    assert!(f.rt().queries.pending.is_none());
    assert!(f.output(a).is_empty());
    f.input(b, &[key(BTN_LEFT, 0)]);
    f.output(b);
    f.latch(a);
    f.input(b, &[rel(REL_Y, 120)]);
    assert_eq!(f.rt().mice[&a].engine.dy, 120.0);
    assert!(f.output(b).is_empty());
    f.input(b, &[rel(REL_WHEEL, 1)]);
    assert_eq!(f.rt().mice[&a].engine.phase, Phase::Idle);
    assert_eq!(f.output(b), [Event::new(EV_REL, REL_WHEEL, 1)]);
    assert_eq!(f.output(a), [Event::new(EV_REL, REL_Y, 120)]);
    // The restored pointer movement also prevents a stale native verdict.
    f.input(b, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    assert!(!f.rt().queries.pending.as_ref().unwrap().allow_native);
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_pending_pause_and_old_request_cannot_start_new_gesture() {
    let mut f = Fixture::new();
    let id = f.mouse();
    f.input(id, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    let old = f.send_due_query();
    f.wire(json!({"type":"state","epoch":2,"enabled":false,"locked":false,"focus":"test-window"}));
    f.decision(old, 1, "scroll");
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Idle);
    f.wire(json!({"type":"state","epoch":3,"enabled":true,"locked":false,"focus":"test-window"}));
    f.input(id, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    let new = f.send_due_query();
    f.decision(old, 1, "native");
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Waiting);
    assert_eq!(f.rt().queries.pending.as_ref().unwrap().id, new);
    f.decision(new, 3, "scroll");
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Latched);
    assert!(f.output(id).is_empty());
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_disconnect_cancels_request_and_releases_forwarded_buttons() {
    let mut f = Fixture::new();
    let id = f.mouse();
    f.input(id, &[key(BTN_MIDDLE, 1)]);
    f.send_due_query();
    let path = f.rt().mice[&id].path.clone();
    let identity = f.rt().mice[&id].identity;
    f.rt().skipped.insert(path.clone(), identity);
    f.rt().retries.insert(path.clone(), Instant::now());
    // Unregister the actual synthetic source, then exercise the production
    // read-error/removal path rather than calling remove_mouse directly.
    f.sources.remove(&id);
    for _ in 0..16 {
        if !f.rt().mice.contains_key(&id) {
            break;
        }
        f.rt().mouse_events(id).unwrap();
    }
    assert!(!f.rt().mice.contains_key(&id));
    assert!(f.rt().queries.pending.is_none());
    assert!(!f.rt().retries.contains_key(&path));
    assert!(!f.rt().skipped.contains_key(&path));
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_eight_thousand_reports_preserve_events_and_button_balance() {
    let mut f = Fixture::new();
    let id = f.mouse();
    let started = Instant::now();
    let mut reports = 0;
    let mut presses = 0;
    let mut releases = 0;
    let mut motion = 0;
    for index in 0..8000 {
        let down = if index % 2 == 0 { 1 } else { 0 };
        let dx = if down == 1 { 1 } else { -1 };
        f.input(id, &[rel(REL_X, dx), key(BTN_LEFT, down)]);
        let output = f.output(id);
        assert_eq!(
            output,
            [
                Event::new(EV_REL, REL_X, dx),
                Event::new(EV_KEY, BTN_LEFT, down)
            ]
        );
        reports += 1;
        motion += dx;
        if down == 1 {
            presses += 1;
        } else {
            releases += 1;
        }
    }
    assert_eq!(reports, 8000);
    assert_eq!(presses, 4000);
    assert_eq!(releases, 4000);
    assert_eq!(motion, 0);
    assert!(f.rt().mice[&id].engine.forwarded_buttons().is_empty());
    assert!(f.rt().queries.pending.is_none());
    eprintln!(
        "Isolated kernel stress: {reports} complete reports, {presses} presses + {releases} releases, elapsed {:?}",
        started.elapsed()
    );
}
#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_real_evdev_overflow_recovers_lost_middle_release() {
    let mut f = Fixture::new();
    let id = f.mouse();
    f.input(id, &[key(BTN_MIDDLE, 1)]);
    let request = f.send_due_query();
    f.decision(request, 1, "scroll");
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Pending);
    // Intentionally overrun only this test source's kernel ring buffer. Its
    // release is lost, so SYN_DROPPED must recover physical middle-up state.
    f.sources
        .get_mut(&id)
        .unwrap()
        .emit(&[key(BTN_MIDDLE, 0)])
        .unwrap();
    for _ in 0..8000 {
        f.sources
            .get_mut(&id)
            .unwrap()
            .emit(&[rel(REL_X, 1)])
            .unwrap();
    }
    let mut drained = false;
    for _ in 0..1024 {
        let mut descriptor = libc::pollfd {
            fd: f.rt().mice[&id].device.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let ready = unsafe { libc::poll(&mut descriptor, 1, 0) };
        assert!(ready >= 0);
        if ready == 0 {
            drained = true;
            break;
        }
        f.rt().mouse_events(id).unwrap();
        f.output(id);
    }
    assert!(drained);
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Idle);
    assert!(!f.rt().mice[&id].engine.physical_middle);
    f.input(id, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Waiting);
    assert!(f.rt().queries.pending.is_some());
    assert!(f.output(id).is_empty());
}

#[test]
#[ignore = "isolated synthetic evdev devices; requires /dev/uinput and /dev/input access"]
fn kernel_deferred_native_click_preserves_distinct_press_release_frames() {
    let mut f = Fixture::new();
    let id = f.mouse();
    // The physical click finishes while accessibility inspection is pending.
    // Its eventual native replay must expose both states to libinput.
    f.input(id, &[key(BTN_MIDDLE, 1), key(BTN_MIDDLE, 0)]);
    let request = f.send_due_query();
    f.decision(request, 1, "native");
    assert_eq!(
        f.output_frames(id),
        vec![
            vec![Event::new(EV_KEY, BTN_MIDDLE, 1)],
            vec![Event::new(EV_KEY, BTN_MIDDLE, 0)],
        ]
    );
    assert_eq!(f.rt().mice[&id].engine.phase, Phase::Idle);
    assert!(f.rt().mice[&id].engine.forwarded_buttons().is_empty());
}
