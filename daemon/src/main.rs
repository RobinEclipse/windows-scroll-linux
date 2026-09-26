//! Single, supervised poll loop: physical input never waits for UI inspection.
mod config;
mod coordinator;
mod engine;
mod protocol;
mod session;

use config::Config;
use coordinator::Coordinator;
use engine::{Engine, Event, Mode};
use evdev::raw_stream::RawDevice;
use evdev::uinput::VirtualDevice;
use evdev::{AbsoluteAxisCode, AttributeSet, InputEvent, KeyCode, RelativeAxisCode};
use protocol::{Identity, Message, Peer};
use serde_json::json;
use session::Login;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const SOCKET: &str = "/run/midscroll/state.sock";
const STATUS: &str = "/run/midscroll/status.json";
const EV_KEY: u16 = 1;
const EV_REL: u16 = 2;
const MAX_MICE: usize = 16;
static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn stopping(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct DeviceIdentity {
    inode: u64,
    dev: u64,
    changed: i64,
    nanos: i64,
}
fn device_identity(path: &Path) -> io::Result<DeviceIdentity> {
    let m = path.metadata()?;
    Ok(DeviceIdentity {
        inode: m.ino(),
        dev: m.rdev(),
        changed: m.ctime(),
        nanos: m.ctime_nsec(),
    })
}

struct Mouse {
    path: PathBuf,
    identity: DeviceIdentity,
    device: RawDevice,
    virtual_device: VirtualDevice,
    engine: Engine,
    out: Vec<Event>,
    emit: Vec<InputEvent>,
    report_motion: bool,
    dropping: bool,
    last_motion: Instant,
    last_flushed_motion: Instant,
}
impl Mouse {
    fn flush(&mut self) -> io::Result<()> {
        if self.out.is_empty() {
            return Ok(());
        }
        if self
            .out
            .iter()
            .any(|e| e.kind == EV_REL && [0, 1].contains(&e.code) && e.value != 0)
        {
            self.last_flushed_motion = Instant::now();
        }
        self.emit.clear();
        for event in self.out.drain(..) {
            // libinput compares mouse-button state at SYN_REPORT. A complete
            // deferred click must not collapse its down/up into one frame.
            // Preserve simultaneous different buttons and motion; split only
            // when this frame already contains the same key's transition.
            if event.kind == EV_KEY
                && self.emit.iter().any(|previous| {
                    previous.event_type().0 == EV_KEY && previous.code() == event.code
                })
            {
                self.virtual_device.emit(&self.emit)?;
                self.emit.clear();
            }
            self.emit
                .push(InputEvent::new(event.kind, event.code, event.value));
        }
        self.virtual_device.emit(&self.emit)
    }
    fn cancel(&mut self, snap: bool) -> io::Result<()> {
        self.engine.cancel(snap, &mut self.out);
        self.flush()?;
        Ok(())
    }
}
impl Drop for Mouse {
    fn drop(&mut self) {
        self.engine.release_all(&mut self.out);
        let _ = self.flush();
        let _ = self.device.ungrab();
    }
}
struct Keyboard {
    device: RawDevice,
    identity: DeviceIdentity,
}

fn nonblocking(fd: RawFd) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
fn keyboard(d: &RawDevice) -> bool {
    d.supported_keys().is_some_and(|k| {
        [
            KeyCode::KEY_A,
            KeyCode::KEY_Z,
            KeyCode::KEY_ENTER,
            KeyCode::KEY_SPACE,
        ]
        .into_iter()
        .all(|x| k.contains(x))
    })
}
fn mouse(d: &RawDevice) -> bool {
    !keyboard(d)
        && !d.physical_path().is_some_and(|p| p.contains("midscroll"))
        && d.supported_keys()
            .is_some_and(|k| k.contains(KeyCode::BTN_MIDDLE))
        && d.supported_relative_axes().is_some_and(|r| {
            r.contains(RelativeAxisCode::REL_X) && r.contains(RelativeAxisCode::REL_Y)
        })
        && !d.supported_absolute_axes().is_some_and(|a| {
            a.contains(AbsoluteAxisCode::ABS_X) || a.contains(AbsoluteAxisCode::ABS_MT_POSITION_X)
        })
}
fn matches_spec(d: &RawDevice, path: &Path, spec: &str) -> bool {
    if spec.starts_with('/') {
        return fs::canonicalize(spec).ok().as_deref() == Some(path);
    }
    if let Some((vendor, product)) = spec.split_once(':')
        && let (Ok(v), Ok(p)) = (
            u16::from_str_radix(vendor, 16),
            u16::from_str_radix(product, 16),
        )
    {
        let id = d.input_id();
        return id.vendor() == v && id.product() == p;
    }
    d.name()
        .unwrap_or("")
        .to_lowercase()
        .contains(&spec.to_lowercase())
}
fn candidate(d: &RawDevice, path: &Path, cfg: &Config) -> bool {
    let forced = cfg.extra_devices.iter().any(|s| matches_spec(d, path, s));
    let safe_relative = !keyboard(d)
        && !d.physical_path().is_some_and(|p| p.contains("midscroll"))
        && d.supported_relative_axes().is_some_and(|r| {
            r.contains(RelativeAxisCode::REL_X) && r.contains(RelativeAxisCode::REL_Y)
        })
        && d.supported_keys()
            .is_some_and(|k| k.iter().any(|v| (272..288).contains(&v.0)))
        && !d.supported_absolute_axes().is_some_and(|a| {
            a.contains(AbsoluteAxisCode::ABS_X) || a.contains(AbsoluteAxisCode::ABS_MT_POSITION_X)
        });
    if !mouse(d) && !(forced && safe_relative) {
        return false;
    }
    !cfg.ignore_devices.iter().any(|s| matches_spec(d, path, s))
}
fn virtual_name(name: &str) -> String {
    let mut end = name.len().min(78);
    while !name.is_char_boundary(end) {
        end -= 1;
    }
    name[..end].to_owned()
}
fn attach(
    path: PathBuf,
    identity: DeviceIdentity,
    mut device: RawDevice,
    config: Config,
) -> io::Result<Mouse> {
    nonblocking(device.as_raw_fd())?;
    let mut keys = AttributeSet::<KeyCode>::new();
    if let Some(k) = device.supported_keys() {
        for code in k.iter() {
            keys.insert(code);
        }
    }
    for code in 272..288 {
        keys.insert(KeyCode(code));
    }
    let mut axes = AttributeSet::<RelativeAxisCode>::new();
    if let Some(a) = device.supported_relative_axes() {
        for code in a.iter() {
            axes.insert(code);
        }
    }
    for code in [0, 1, 6, 8, 11, 12] {
        axes.insert(RelativeAxisCode(code));
    }
    if device.get_key_state()?.iter().next().is_some() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "waiting for held buttons to release",
        ));
    }
    let name = virtual_name(device.name().unwrap_or("Mouse"));
    let virtual_device = VirtualDevice::builder()?
        .name(&name)
        .input_id(device.input_id())
        .with_phys(c"midscroll-v2")?
        .with_keys(&keys)?
        .with_relative_axes(&axes)?
        .build()?;
    if device.get_key_state()?.iter().next().is_some() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "waiting for held buttons to release",
        ));
    }
    device.grab()?;
    // The open FD may contain pre-grab reports already delivered natively.
    // Discard them; never replay a user's original press or movement.
    let mut drained = false;
    for _ in 0..256 {
        match device.fetch_events() {
            Ok(events) => for _ in events {},
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                drained = true;
                break;
            }
            Err(e) => return Err(e),
        }
    }
    if !drained {
        return Err(io::Error::other(
            "device input did not settle during attachment",
        ));
    }
    if device.get_key_state()?.iter().next().is_some() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "button changed during attachment; retrying",
        ));
    }
    let active: BTreeSet<u16> = device.get_key_state()?.iter().map(|k| k.0).collect();
    let mut m = Mouse {
        path,
        identity,
        device,
        virtual_device,
        engine: Engine::new(config),
        out: Vec::with_capacity(128),
        emit: Vec::with_capacity(128),
        report_motion: false,
        dropping: false,
        last_motion: Instant::now() - Duration::from_secs(1),
        last_flushed_motion: Instant::now(),
    };
    m.engine.resync(&active, &mut m.out);
    m.flush()?;
    eprintln!("attached mouse: {name}");
    Ok(m)
}

struct Runtime {
    config: Config,
    login: Login,
    listener: UnixListener,
    socket_path: PathBuf,
    peers: BTreeMap<u64, Peer>,
    owner: Option<u64>,
    active: Option<session::Session>,
    mice: BTreeMap<u64, Mouse>,
    keyboards: BTreeMap<PathBuf, Keyboard>,
    skipped: HashMap<PathBuf, DeviceIdentity>,
    retries: HashMap<PathBuf, Instant>,
    serial: u64,
    queries: Coordinator,
    overlay: Option<u64>,
    _lock: File,
    born: Instant,
    input_buffer: Vec<InputEvent>,
}
impl Runtime {
    fn new(config: Config) -> io::Result<Self> {
        let login = Login::new()?;
        fs::create_dir_all("/run/midscroll")?;
        let directory = fs::symlink_metadata("/run/midscroll")?;
        if !directory.is_dir() || directory.uid() != 0 || directory.mode() & 0o022 != 0 {
            return Err(io::Error::other("untrusted runtime directory"));
        }
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open("/run/midscroll/daemon.lock")?;
        let lock_meta = lock.metadata()?;
        if !lock_meta.is_file() || lock_meta.uid() != 0 || lock_meta.mode() & 0o022 != 0 {
            return Err(io::Error::other("untrusted daemon lock"));
        }
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } < 0 {
            return Err(io::Error::other(
                "another Windows Scroll Linux instance is running",
            ));
        }
        match fs::remove_file(SOCKET) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e),
        }
        let listener = UnixListener::bind(SOCKET)?;
        listener.set_nonblocking(true)?;
        fs::set_permissions(SOCKET, fs::Permissions::from_mode(0o666))?;
        Ok(Self {
            config,
            active: login.active(),
            login,
            listener,
            socket_path: PathBuf::from(SOCKET),
            peers: BTreeMap::new(),
            owner: None,
            mice: BTreeMap::new(),
            keyboards: BTreeMap::new(),
            skipped: HashMap::new(),
            retries: HashMap::new(),
            serial: 0,
            queries: Coordinator::default(),
            overlay: None,
            _lock: lock,
            born: Instant::now(),
            input_buffer: Vec::with_capacity(128),
        })
    }
    fn next_id(&mut self) -> u64 {
        self.serial = self
            .serial
            .checked_add(1)
            .expect("object sequence exhausted");
        self.serial
    }
    fn send(&mut self, id: u64, value: serde_json::Value) -> io::Result<()> {
        self.peers
            .get_mut(&id)
            .ok_or_else(|| io::Error::other("helper gone"))?
            .send(value)
    }
    fn cancel_all(&mut self, snap: bool) -> io::Result<()> {
        self.queries.cancel();
        for m in self.mice.values_mut() {
            m.cancel(snap)?;
        }
        Ok(())
    }
    fn remove_peer(&mut self, id: u64) -> io::Result<()> {
        self.peers.remove(&id);
        if self.owner == Some(id) {
            self.owner = None;
            self.cancel_all(false)?;
            self.overlay = None;
        }
        Ok(())
    }
    fn authorized(&self, id: u64) -> bool {
        match (self.active.as_ref(), self.peers.get(&id)) {
            (Some(a), Some(p)) => p
                .identity
                .as_ref()
                .is_some_and(|i| session::permits(a, p.uid, &i.session, &i.seat)),
            _ => false,
        }
    }
    fn refresh_session(&mut self) -> io::Result<()> {
        let current = self.login.active();
        if current != self.active {
            self.cancel_all(false)?;
            self.active = current;
            let ids: Vec<_> = self.peers.keys().copied().collect();
            for id in ids {
                self.remove_peer(id)?;
            }
        }
        Ok(())
    }
    fn scan(&mut self) -> io::Result<()> {
        let paths: BTreeSet<PathBuf> = fs::read_dir("/dev/input")?
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .is_some_and(|n| n.to_string_lossy().starts_with("event"))
            })
            .collect();
        let gone: Vec<u64> = self
            .mice
            .iter()
            .filter(|(_, m)| {
                !paths.contains(&m.path) || device_identity(&m.path).ok() != Some(m.identity)
            })
            .map(|(id, _)| *id)
            .collect();
        for id in gone {
            self.remove_mouse(id)?;
        }
        self.keyboards.retain(|path, k| {
            paths.contains(path) && device_identity(path).ok() == Some(k.identity)
        });
        self.skipped.retain(|path, _| paths.contains(path));
        self.retries.retain(|path, _| paths.contains(path));
        for path in paths {
            if self.mice.values().any(|m| m.path == path) || self.keyboards.contains_key(&path) {
                continue;
            }
            let Ok(identity) = device_identity(&path) else {
                continue;
            };
            if self.skipped.get(&path) == Some(&identity)
                || self
                    .retries
                    .get(&path)
                    .is_some_and(|t| t.elapsed() < Duration::from_millis(500))
            {
                continue;
            }
            let Ok(device) = RawDevice::open(&path) else {
                self.retries.insert(path, Instant::now());
                continue;
            };
            let seat = match self.login.device_seat(&path) {
                Ok(s) => s,
                Err(_) => {
                    self.retries.insert(path, Instant::now());
                    continue;
                }
            };
            if seat != "seat0"
                || device
                    .physical_path()
                    .is_some_and(|p| p.contains("midscroll"))
            {
                self.skipped.insert(path, identity);
                continue;
            }
            if keyboard(&device) {
                self.keyboards.insert(path, Keyboard { device, identity });
                continue;
            }
            if !candidate(&device, &path, &self.config) {
                self.skipped.insert(path, identity);
                continue;
            }
            if self.mice.len() >= MAX_MICE {
                self.retries.insert(path, Instant::now());
                continue;
            }
            match attach(path.clone(), identity, device, self.config.clone()) {
                Ok(m) => {
                    let id = self.next_id();
                    self.mice.insert(id, m);
                    self.retries.remove(&path);
                }
                Err(e) => {
                    if !self.retries.contains_key(&path) {
                        eprintln!("mouse attach will retry ({}): {e}", path.display());
                    }
                    self.retries.insert(path, Instant::now());
                }
            }
        }
        Ok(())
    }
    fn remove_mouse(&mut self, id: u64) -> io::Result<()> {
        if self
            .queries
            .pending
            .as_ref()
            .is_some_and(|p| p.source == id)
        {
            self.queries.cancel();
        }
        if self.overlay == Some(id) {
            self.overlay = None;
            if let Some(owner) = self.owner {
                let _ = self.send(owner, json!({"type":"stop"}));
            }
        }
        if let Some(m) = self.mice.remove(&id) {
            self.skipped.remove(&m.path);
            self.retries.remove(&m.path);
        }
        Ok(())
    }
    fn accept(&mut self) -> io::Result<()> {
        self.refresh_session()?;
        for _ in 0..8 {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if self.peers.len() >= 8 {
                        continue;
                    }
                    if let Ok(peer) = Peer::new(stream) {
                        // Only the owner of seat0's active local Wayland session may connect.
                        if !self.active.as_ref().is_some_and(|a| {
                            a.uid == peer.uid
                                && a.seat == "seat0"
                                && a.kind == "wayland"
                                && !a.remote
                        }) {
                            continue;
                        }
                        if self.peers.values().filter(|p| p.uid == peer.uid).count() >= 2 {
                            continue;
                        }
                        let id = self.next_id();
                        self.peers.insert(id, peer);
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
    fn message(&mut self, id: u64, message: Message) -> io::Result<()> {
        match message {
            Message::Hello {
                version: _,
                session,
                seat,
                epoch,
                enabled,
                locked,
                focus,
            } => {
                let uid = self.peers[&id].uid;
                let allowed = self
                    .active
                    .as_ref()
                    .is_some_and(|a| session::permits(a, uid, &session, &seat));
                if !allowed || self.peers[&id].identity.is_some() {
                    return self.remove_peer(id);
                }
                if let Some(old) = self.owner
                    && old != id
                {
                    self.remove_peer(old)?;
                }
                self.cancel_all(false)?;
                let peer = self.peers.get_mut(&id).unwrap();
                peer.identity = Some(Identity {
                    session,
                    seat,
                    epoch,
                    enabled,
                    locked,
                    focus,
                });
                peer.last_seen = Instant::now();
                self.owner = Some(id);
                self.send(id, json!({"type":"stop"}))?;
            }
            Message::State {
                epoch,
                enabled,
                locked,
                focus,
                session,
                seat,
            } => {
                if !self.authorized(id) || self.owner != Some(id) {
                    return self.remove_peer(id);
                }
                let i = self.peers[&id].identity.as_ref().unwrap();
                if epoch < i.epoch
                    || session.as_ref().is_some_and(|s| s != &i.session)
                    || seat.as_ref().is_some_and(|s| s != &i.seat)
                {
                    return self.remove_peer(id);
                }
                let changed = epoch != i.epoch
                    || enabled != i.enabled
                    || locked != i.locked
                    || focus != i.focus;
                if changed {
                    self.cancel_all(false)?;
                }
                let peer = self.peers.get_mut(&id).unwrap();
                let i = peer.identity.as_mut().unwrap();
                i.epoch = epoch;
                i.enabled = enabled;
                i.locked = locked;
                i.focus = focus;
                peer.last_seen = Instant::now();
            }
            Message::Decision {
                id: request,
                epoch,
                mode,
                x,
                y,
                focus,
            } => {
                self.refresh_session()?;
                if self.owner != Some(id) || !self.authorized(id) {
                    return self.remove_peer(id);
                }
                let i = self.peers[&id].identity.as_ref().unwrap();
                if !self
                    .queries
                    .accepts(request, epoch, id, i, &focus, Instant::now())
                {
                    return Ok(());
                }
                let pending = self.queries.pending.take().unwrap();
                if let Some(m) = self.mice.get_mut(&pending.source) {
                    let mode = match mode.as_str() {
                        "scroll" => Mode::Scroll,
                        "native" if pending.allow_native => Mode::Native,
                        _ => Mode::Ignore,
                    };
                    m.engine.resolve(mode, &mut m.out);
                    m.flush()?;
                    if m.engine.captures_motion() {
                        self.overlay = Some(pending.source);
                        self.send(id,json!({"type":"start","x":x,"y":y,"ghost":self.config.ghost_cursor,"scale":self.config.ghost_scale}))?;
                    }
                }
            }
        }
        Ok(())
    }
    fn mouse_events(&mut self, id: u64) -> io::Result<()> {
        let mut events = std::mem::take(&mut self.input_buffer);
        let fetched = self
            .mice
            .get_mut(&id)
            .unwrap()
            .device
            .fetch_events()
            .map(|iter| events.extend(iter));
        match fetched {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                self.input_buffer = events;
                return Ok(());
            }
            Err(e) => {
                self.input_buffer = events;
                eprintln!("mouse disconnected: {e}");
                return self.remove_mouse(id);
            }
        }
        for ev in events.drain(..) {
            if !self.mice.contains_key(&id) {
                break;
            }
            let event = Event {
                kind: ev.event_type().0,
                code: ev.code(),
                value: ev.value(),
            };
            if event.kind == engine::EV_SYN && event.code == engine::SYN_DROPPED {
                self.cancel_all(false)?;
                let m = self.mice.get_mut(&id).unwrap();
                m.dropping = true;
                continue;
            }
            if self.mice[&id].dropping {
                if event.kind == engine::EV_SYN && event.code == engine::SYN_REPORT {
                    let m = self.mice.get_mut(&id).unwrap();
                    match m.device.get_key_state() {
                        Ok(keys) => {
                            let active = keys.iter().map(|k| k.0).collect();
                            m.engine.resync(&active, &mut m.out);
                            m.dropping = false;
                            m.report_motion = false;
                            m.flush()?;
                        }
                        Err(_) => {
                            self.remove_mouse(id)?;
                        }
                    }
                }
                continue;
            }
            let active = self
                .mice
                .iter()
                .find(|(_, m)| m.engine.captures_motion())
                .map(|(id, _)| *id);
            // All physical wheel rotations cancel capture globally; preserve wheel itself.
            if event.kind == EV_REL && [6, 8, 11, 12].contains(&event.code) {
                self.cancel_all(true)?;
            }
            if event.kind == EV_KEY
                && (272..288).contains(&event.code)
                && event.value == 1
                && active.is_some()
            {
                self.cancel_all(true)?;
                self.mice
                    .get_mut(&id)
                    .unwrap()
                    .engine
                    .suppress_button(event.code);
                continue;
            }
            // A second mouse must not move the real pointer out of an active anchor.
            if event.kind == EV_REL
                && [0, 1].contains(&event.code)
                && let Some(capture) = active
                && capture != id
            {
                let m = self.mice.get_mut(&capture).unwrap();
                m.engine.handle(event, &mut m.out);
                if let Some(p) = self.queries.pending.as_mut() {
                    p.allow_native = false;
                }
                continue;
            }
            let another_button_held = self
                .mice
                .iter()
                .any(|(other, m)| *other != id && !m.engine.forwarded_buttons().is_empty());
            let quiet = self.mice.values().all(|m| {
                !m.report_motion
                    && m.last_motion.elapsed() >= Duration::from_millis(12)
                    && m.last_flushed_motion.elapsed() >= Duration::from_millis(12)
            });
            let m = self.mice.get_mut(&id).unwrap();
            let before_motion = m.report_motion;
            if event.kind == EV_REL && [0, 1].contains(&event.code) && event.value != 0 {
                m.report_motion = true;
                m.last_motion = Instant::now();
                if let Some(p) = self.queries.pending.as_mut() {
                    p.allow_native = false;
                }
            }
            let request = m.engine.handle(event, &mut m.out);
            if event.kind == engine::EV_SYN && event.code == engine::SYN_REPORT {
                m.report_motion = false;
                m.flush()?;
            }
            if request {
                if another_button_held {
                    m.engine.resolve(Mode::Ignore, &mut m.out);
                    m.flush()?;
                    continue;
                }
                // Commit preceding movement before querying the compositor. A click
                // sharing a motion report cannot authorize native passthrough at all.
                m.flush()?;
                self.refresh_session()?;
                if !self.mice.get(&id).is_some_and(|m| m.engine.waiting()) {
                    continue;
                }
                if let Some(owner) = self.owner
                    && self.authorized(owner)
                {
                    let ident = self.peers[&owner].identity.as_ref().unwrap();
                    if ident.enabled && !ident.locked {
                        self.queries.begin(
                            id,
                            owner,
                            ident,
                            !before_motion && quiet,
                            Instant::now(),
                        );
                        continue;
                    }
                }
                let m = self.mice.get_mut(&id).unwrap();
                m.engine.resolve(Mode::Ignore, &mut m.out);
                m.flush()?;
            }
        }
        self.input_buffer = events;
        Ok(())
    }
    fn timer(&mut self, dt: f64) -> io::Result<()> {
        let now = Instant::now();
        if self
            .queries
            .pending
            .as_ref()
            .is_some_and(|p| now >= p.deadline)
        {
            let p = self.queries.pending.take().unwrap();
            if let Some(m) = self.mice.get_mut(&p.source) {
                m.engine.resolve(Mode::Ignore, &mut m.out);
                m.flush()?;
            }
        }
        if let Some(p) = self.queries.pending.as_mut()
            && !p.sent
            && now >= p.send_after
        {
            p.sent = true;
            let peer = p.peer;
            let value =
                json!({"type":"query","id":p.id,"epoch":p.epoch,"allow_native":p.allow_native});
            if self.send(peer, value).is_err() {
                self.remove_peer(peer)?;
            }
        }
        if self.mice.values().any(|m| m.engine.captures_motion()) {
            let escape = self.keyboards.values().any(|k| {
                k.device
                    .get_key_state()
                    .is_ok_and(|keys| keys.contains(KeyCode::KEY_ESC))
            });
            if escape {
                self.cancel_all(true)?;
            }
        }
        for m in self.mice.values_mut() {
            m.engine.tick(dt, &mut m.out);
            m.flush()?;
        }
        let scrolling = self
            .mice
            .iter()
            .find(|(_, m)| m.engine.captures_motion())
            .map(|(id, m)| (*id, m.engine.dx, m.engine.dy));
        if let Some(owner) = self.owner {
            if self.overlay.is_some() && scrolling.is_none() {
                self.overlay = None;
                if self.send(owner, json!({"type":"stop"})).is_err() {
                    self.remove_peer(owner)?;
                }
            } else if let Some((id, dx, dy)) = scrolling
                && self.overlay==Some(id) && self.send(owner,json!({"type":"pos","dx":dx*self.config.ghost_scale,"dy":dy*self.config.ghost_scale})).is_err(){self.remove_peer(owner)?;}
        }
        Ok(())
    }
    fn housekeeping(&mut self) -> io::Result<()> {
        self.refresh_session()?;
        let expired: Vec<_> = self
            .peers
            .iter()
            .filter(|(_, p)| p.last_seen.elapsed() > Duration::from_secs(3))
            .map(|(id, _)| *id)
            .collect();
        for id in expired {
            self.remove_peer(id)?;
        }
        self.scan()?;
        Ok(())
    }
    fn health(&mut self) -> io::Result<()> {
        let data = json!({"version":VERSION,"backend":"rust","pid":std::process::id(),"seat":"seat0","attached_mice":self.mice.len(),"helper_connected":self.owner.is_some(),"uptime_seconds":self.born.elapsed().as_secs()});
        let temporary = "/run/midscroll/status.json.tmp";
        fs::write(temporary, serde_json::to_vec_pretty(&data)?)?;
        fs::set_permissions(temporary, fs::Permissions::from_mode(0o644))?;
        fs::rename(temporary, STATUS)?;
        if let Some(owner) = self.owner
            && self.send(owner,json!({"type":"status","version":VERSION,"backend":"rust","seat":"seat0","devices":self.mice.len(),"healthy":true})).is_err(){self.remove_peer(owner)?;}
        self.login.notify("WATCHDOG=1");
        Ok(())
    }
    fn run(&mut self) -> io::Result<()> {
        self.scan()?;
        self.health()?;
        eprintln!(
            "Windows Scroll Linux {VERSION} ready: {} mice, active local session {:?}",
            self.mice.len(),
            self.active
        );
        self.login.notify("READY=1\nSTATUS=Rust input loop running");
        let mut last_tick = Instant::now();
        let mut last_scan = last_tick;
        let mut last_health = last_tick;
        let period = Duration::from_secs_f64(1.0 / self.config.tick_hz);
        let mut polls = Vec::with_capacity(32);
        let mut tags = Vec::with_capacity(32);
        while !STOP.load(Ordering::Relaxed) {
            // At most 16 mice + 8 peers. Rebuilding this small poll array avoids
            // stale-FD registrations during hotplug and keeps ownership explicit.
            polls.clear();
            tags.clear();
            polls.push(libc::pollfd {
                fd: self.login.monitor_fd(),
                events: libc::POLLIN,
                revents: 0,
            });
            tags.push((3, 0));
            polls.push(libc::pollfd {
                fd: self.listener.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            });
            tags.push((0, 0));
            for (&id, m) in &self.mice {
                polls.push(libc::pollfd {
                    fd: m.device.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                });
                tags.push((1, id));
            }
            for (&id, p) in &self.peers {
                polls.push(libc::pollfd {
                    fd: p.fd(),
                    events: libc::POLLIN | if p.wants_write() { libc::POLLOUT } else { 0 },
                    revents: 0,
                });
                tags.push((2, id));
            }
            let until = period.saturating_sub(last_tick.elapsed());
            let timeout = until.as_nanos().div_ceil(1_000_000).min(10) as i32;
            let ready =
                unsafe { libc::poll(polls.as_mut_ptr(), polls.len() as libc::nfds_t, timeout) };
            if ready < 0 {
                let e = io::Error::last_os_error();
                if e.kind() != io::ErrorKind::Interrupted {
                    return Err(e);
                }
            }
            for (p, (kind, id)) in polls.iter().zip(tags.iter().copied()) {
                if p.revents == 0 {
                    continue;
                }
                match kind {
                    3 => {
                        self.login.flush_monitor();
                        self.refresh_session()?;
                    }
                    0 => self.accept()?,
                    1 => {
                        if self.mice.contains_key(&id) {
                            if p.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 {
                                self.remove_mouse(id)?;
                            } else {
                                self.mouse_events(id)?;
                            }
                        }
                    }
                    2 => {
                        if !self.peers.contains_key(&id) {
                            continue;
                        }
                        if p.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 {
                            self.remove_peer(id)?;
                            continue;
                        }
                        if p.revents & libc::POLLOUT != 0
                            && self.peers.get_mut(&id).unwrap().flush().is_err()
                        {
                            self.remove_peer(id)?;
                            continue;
                        }
                        if p.revents & libc::POLLIN != 0 {
                            match self.peers.get_mut(&id).unwrap().read_messages() {
                                Ok(messages) => {
                                    for msg in messages {
                                        if self.peers.contains_key(&id)
                                            && self.message(id, msg).is_err()
                                        {
                                            self.remove_peer(id)?;
                                        }
                                    }
                                }
                                Err(_) => self.remove_peer(id)?,
                            }
                        }
                    }
                    _ => unreachable!(),
                }
            }
            if last_tick.elapsed() >= period {
                let now = Instant::now();
                let dt = (now - last_tick).as_secs_f64().min(0.05);
                last_tick = now;
                self.timer(dt)?;
            }
            if last_scan.elapsed() >= Duration::from_millis(250) {
                last_scan = Instant::now();
                self.housekeeping()?;
            }
            if last_health.elapsed() >= Duration::from_millis(750) {
                last_health = Instant::now();
                self.health()?;
            }
        }
        self.cancel_all(false)?;
        self.mice.clear();
        self.login.notify("STOPPING=1");
        Ok(())
    }
}
impl Drop for Runtime {
    fn drop(&mut self) {
        let _ = self.cancel_all(false);
        self.mice.clear();
        let _ = fs::remove_file(&self.socket_path);
        if self.socket_path == Path::new(SOCKET) {
            let _ = fs::remove_file(STATUS);
        }
    }
}
fn read_config(path: &Path, trusted: bool) -> io::Result<Config> {
    let mut options = OpenOptions::new();
    options.read(true);
    if trusted {
        options.custom_flags(libc::O_NOFOLLOW);
    }
    let file = options.open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.len() > 65536 {
        return Err(io::Error::other(
            "configuration must be a regular file under 64 KiB",
        ));
    }
    if trusted && (meta.uid() != 0 || meta.mode() & 0o022 != 0) {
        return Err(io::Error::other(
            "configuration must be root owned and not writable by group/others",
        ));
    }
    let mut text = String::new();
    file.take(65537).read_to_string(&mut text)?;
    if text.len() > 65536 {
        return Err(io::Error::other("configuration too large"));
    }
    Config::parse(&text).map_err(io::Error::other)
}
fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [arg] if arg == "--version" => {
            println!("Windows Scroll Linux {VERSION} (Rust input service)");
            return Ok(());
        }
        [arg, path] if arg == "--check-config" => {
            read_config(Path::new(path), false)?;
            println!("Configuration valid");
            return Ok(());
        }
        [arg] if arg == "--status" => {
            println!("{}", fs::read_to_string(STATUS)?);
            return Ok(());
        }
        [arg] if arg == "--list-devices" => {
            let login = Login::new()?;
            for (path, d) in evdev::raw_stream::enumerate() {
                println!(
                    "{}\t{}\t{}\t{}",
                    path.display(),
                    d.name().unwrap_or("unknown"),
                    login
                        .device_seat(&path)
                        .unwrap_or_else(|_| "unknown-seat".into()),
                    if mouse(&d) {
                        "relative mouse"
                    } else if keyboard(&d) {
                        "keyboard (never grabbed)"
                    } else {
                        "unsupported"
                    }
                );
            }
            return Ok(());
        }
        [] => (),
        _ => {
            return Err(io::Error::other(
                "Windows Scroll Linux input service\nUsage: midscrolld [--version | --status | --list-devices | --check-config PATH]",
            ));
        }
    }
    if unsafe { libc::geteuid() } != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "input service must run as root",
        ));
    }
    let config = read_config(Path::new("/etc/midscroll.conf"), true)?;
    unsafe {
        libc::signal(libc::SIGTERM, stopping as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, stopping as *const () as libc::sighandler_t);
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }
    // All critical work is in run(). An error unwinds devices and exits nonzero;
    // systemd restarts, and its watchdog detects a blocked main loop.
    Runtime::new(config)?.run()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn long_unicode_device_names_cannot_panic_uinput_builder() {
        let input = "🎮".repeat(100);
        let result = virtual_name(&input);
        assert!(result.len() <= 78);
        assert!(input.starts_with(&result));
    }
    #[test]
    fn reused_path_has_new_identity() {
        let a = DeviceIdentity {
            inode: 1,
            dev: 1,
            changed: 1,
            nanos: 1,
        };
        assert_ne!(a, DeviceIdentity { inode: 2, ..a });
    }
    #[test]
    fn config_rejects_symlinks_when_trusted() {
        use std::os::unix::fs::symlink;
        let directory =
            std::env::temp_dir().join(format!("midscroll-config-test-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let target = directory.join("config");
        let link = directory.join("link");
        fs::write(&target, "DEADZONE_PX=15\n").unwrap();
        symlink(&target, &link).unwrap();
        assert_eq!(
            read_config(&link, true).unwrap_err().raw_os_error(),
            Some(libc::ELOOP)
        );
        assert!(read_config(&link, false).is_ok());
        fs::remove_file(link).unwrap();
        fs::remove_file(target).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}

#[cfg(test)]
mod runtime_tests;
