//! Local-seat authentication through public libsystemd/libudev APIs.
//! Libraries own returned C strings; copy then free them immediately.
use libloading::Library;
use std::ffi::{CStr, CString};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Session {
    pub id: String,
    pub uid: u32,
    pub seat: String,
    pub kind: String,
    pub remote: bool,
}

pub fn permits(active: &Session, peer_uid: u32, claimed_session: &str, claimed_seat: &str) -> bool {
    active.uid == peer_uid
        && active.id == claimed_session
        && active.seat == claimed_seat
        && active.seat == "seat0"
        && active.kind == "wayland"
        && !active.remote
        && !active.id.is_empty()
}

pub struct Login {
    systemd: Library,
    udev: Library,
    monitor: *mut libc::c_void,
    monitor_fd: i32,
}

impl Login {
    pub fn new() -> io::Result<Self> {
        // Fixed system library names; LD_* is not inherited by the system unit.
        unsafe {
            let systemd = Library::new("libsystemd.so.0").map_err(io::Error::other)?;
            let udev = Library::new("libudev.so.1").map_err(io::Error::other)?;
            let new=systemd.get::<unsafe extern "C" fn(*const libc::c_char,*mut *mut libc::c_void)->libc::c_int>(b"sd_login_monitor_new\0").map_err(io::Error::other)?;
            let get_fd = systemd
                .get::<unsafe extern "C" fn(*mut libc::c_void) -> libc::c_int>(
                    b"sd_login_monitor_get_fd\0",
                )
                .map_err(io::Error::other)?;
            let mut monitor = std::ptr::null_mut();
            if new(std::ptr::null(), &mut monitor) < 0 {
                return Err(io::Error::other("logind monitor unavailable"));
            }
            let monitor_fd = get_fd(monitor);
            if monitor_fd < 0 {
                if let Ok(unref) =
                    systemd.get::<unsafe extern "C" fn(*mut libc::c_void) -> *mut libc::c_void>(
                        b"sd_login_monitor_unref\0",
                    )
                {
                    unref(monitor);
                }
                return Err(io::Error::other("logind monitor fd unavailable"));
            }
            Ok(Self {
                systemd,
                udev,
                monitor,
                monitor_fd,
            })
        }
    }
    pub fn monitor_fd(&self) -> i32 {
        self.monitor_fd
    }
    pub fn flush_monitor(&self) {
        unsafe {
            if let Ok(f) = self
                .systemd
                .get::<unsafe extern "C" fn(*mut libc::c_void) -> libc::c_int>(
                    b"sd_login_monitor_flush\0",
                )
            {
                f(self.monitor);
            }
        }
    }

    pub fn active(&self) -> Option<Session> {
        unsafe {
            let active = self
                .systemd
                .get::<unsafe extern "C" fn(
                    *const libc::c_char,
                    *mut *mut libc::c_char,
                    *mut libc::uid_t,
                ) -> libc::c_int>(b"sd_seat_get_active\0")
                .ok()?;
            let get_uid = self
                .systemd
                .get::<unsafe extern "C" fn(*const libc::c_char, *mut libc::uid_t) -> libc::c_int>(
                    b"sd_session_get_uid\0",
                )
                .ok()?;
            let remote = self
                .systemd
                .get::<unsafe extern "C" fn(*const libc::c_char) -> libc::c_int>(
                    b"sd_session_is_remote\0",
                )
                .ok()?;
            let mut id = std::ptr::null_mut();
            let mut uid = 0;
            if active(c"seat0".as_ptr(), &mut id, &mut uid) < 0 {
                return None;
            }
            let name = take_string(id)?;
            let c_name = CString::new(name.as_str()).ok()?;
            let mut actual_uid = 0;
            if get_uid(c_name.as_ptr(), &mut actual_uid) < 0 || uid != actual_uid {
                return None;
            }
            let r = remote(c_name.as_ptr());
            if r < 0 {
                return None;
            }
            let seat = self.session_string(b"sd_session_get_seat\0", &c_name)?;
            let kind = self.session_string(b"sd_session_get_type\0", &c_name)?;
            Some(Session {
                id: name,
                uid,
                seat,
                kind,
                remote: r != 0,
            })
        }
    }

    unsafe fn session_string(&self, name: &[u8], id: &CStr) -> Option<String> {
        unsafe {
            let f = self.systemd.get::<unsafe extern "C" fn(*const libc::c_char, *mut *mut libc::c_char) -> libc::c_int>(name).ok()?;
            let mut out = std::ptr::null_mut();
            if f(id.as_ptr(), &mut out) < 0 {
                return None;
            }
            take_string(out)
        }
    }

    pub fn device_seat(&self, path: &Path) -> io::Result<String> {
        let devnum = path.metadata()?.rdev();
        unsafe {
            let new = self
                .udev
                .get::<unsafe extern "C" fn() -> *mut libc::c_void>(b"udev_new\0")
                .map_err(io::Error::other)?;
            let unref = self
                .udev
                .get::<unsafe extern "C" fn(*mut libc::c_void) -> *mut libc::c_void>(
                    b"udev_unref\0",
                )
                .map_err(io::Error::other)?;
            let from_dev = self
                .udev
                .get::<unsafe extern "C" fn(
                    *mut libc::c_void,
                    libc::c_char,
                    libc::dev_t,
                ) -> *mut libc::c_void>(b"udev_device_new_from_devnum\0")
                .map_err(io::Error::other)?;
            let dev_unref = self
                .udev
                .get::<unsafe extern "C" fn(*mut libc::c_void) -> *mut libc::c_void>(
                    b"udev_device_unref\0",
                )
                .map_err(io::Error::other)?;
            let prop =
                self.udev
                    .get::<unsafe extern "C" fn(
                        *mut libc::c_void,
                        *const libc::c_char,
                    ) -> *const libc::c_char>(
                        b"udev_device_get_property_value\0"
                    )
                    .map_err(io::Error::other)?;
            let u = new();
            if u.is_null() {
                return Err(io::Error::other("udev context unavailable"));
            }
            let d = from_dev(u, b'c' as libc::c_char, devnum);
            if d.is_null() {
                unref(u);
                return Err(io::Error::other("udev device unavailable"));
            }
            let value = prop(d, c"ID_SEAT".as_ptr());
            let seat = if value.is_null() {
                "seat0".to_owned()
            } else {
                CStr::from_ptr(value).to_string_lossy().into_owned()
            };
            dev_unref(d);
            unref(u);
            Ok(seat)
        }
    }

    pub fn notify(&self, message: &str) {
        let Ok(message) = CString::new(message) else {
            return;
        };
        unsafe {
            if let Ok(f) = self
                .systemd
                .get::<unsafe extern "C" fn(libc::c_int, *const libc::c_char) -> libc::c_int>(
                    b"sd_notify\0",
                )
            {
                f(0, message.as_ptr());
            }
        }
    }
}

impl Drop for Login {
    fn drop(&mut self) {
        unsafe {
            if let Ok(f) = self
                .systemd
                .get::<unsafe extern "C" fn(*mut libc::c_void) -> *mut libc::c_void>(
                    b"sd_login_monitor_unref\0",
                )
            {
                f(self.monitor);
            }
        }
    }
}

unsafe fn take_string(value: *mut libc::c_char) -> Option<String> {
    if value.is_null() {
        return None;
    }
    unsafe {
        let result = CStr::from_ptr(value).to_str().ok().map(str::to_owned);
        libc::free(value.cast());
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn local() -> Session {
        Session {
            id: "2".into(),
            uid: 1000,
            seat: "seat0".into(),
            kind: "wayland".into(),
            remote: false,
        }
    }
    #[test]
    fn exact_local_graphical_session_only() {
        let s = local();
        assert!(permits(&s, 1000, "2", "seat0"));
        assert!(!permits(&s, 2000, "2", "seat0"));
        assert!(!permits(&s, 1000, "3", "seat0"));
        assert!(!permits(&s, 1000, "2", "seat1"));
    }
    #[test]
    fn remote_or_seatless_active_user_is_rejected() {
        let mut s = local();
        s.remote = true;
        assert!(!permits(&s, 1000, "2", "seat0"));
        s.remote = false;
        s.seat.clear();
        assert!(!permits(&s, 1000, "2", ""));
        s.seat = "seat0".into();
        s.kind = "tty".into();
        assert!(!permits(&s, 1000, "2", "seat0"));
    }
}
