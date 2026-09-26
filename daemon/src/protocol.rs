//! Bounded, nonblocking control channel. Never waits in the input loop.
use serde::Deserialize;
use serde_json::Value;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::time::Instant;

pub const MAX_LINE: usize = 4096;
const MAX_WRITE: usize = 16384;

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Message {
    Hello {
        version: u32,
        session: String,
        seat: String,
        epoch: u64,
        enabled: bool,
        locked: bool,
        focus: String,
    },
    State {
        epoch: u64,
        enabled: bool,
        locked: bool,
        focus: String,
        session: Option<String>,
        seat: Option<String>,
    },
    Decision {
        id: u64,
        epoch: u64,
        mode: String,
        x: f64,
        y: f64,
        focus: String,
    },
}

impl Message {
    pub fn valid(&self) -> bool {
        fn token(s: &str) -> bool {
            s.len() <= 128
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || "-_:{}.".contains(c))
        }
        match self {
            Self::Hello {
                version,
                session,
                seat,
                focus,
                ..
            } => {
                *version == 2
                    && !session.is_empty()
                    && token(session)
                    && token(seat)
                    && token(focus)
            }
            Self::State {
                focus,
                session,
                seat,
                ..
            } => {
                token(focus)
                    && session.as_ref().is_none_or(|s| token(s))
                    && seat.as_ref().is_none_or(|s| token(s))
            }
            Self::Decision {
                mode, x, y, focus, ..
            } => {
                ["scroll", "native", "ignore"].contains(&mode.as_str())
                    && x.is_finite()
                    && y.is_finite()
                    && x.abs() < 100000.
                    && y.abs() < 100000.
                    && token(focus)
            }
        }
    }
}

#[derive(Clone, Debug)]
pub struct Identity {
    pub session: String,
    pub seat: String,
    pub epoch: u64,
    pub enabled: bool,
    pub locked: bool,
    pub focus: String,
}

pub struct Peer {
    pub stream: UnixStream,
    pub uid: u32,
    pub identity: Option<Identity>,
    pub last_seen: Instant,
    input: Vec<u8>,
    output: Vec<u8>,
    written: usize,
}
impl Peer {
    pub fn new(stream: UnixStream) -> io::Result<Self> {
        stream.set_nonblocking(true)?;
        let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        let result = unsafe {
            libc::getsockopt(
                stream.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                (&mut credentials as *mut libc::ucred).cast(),
                &mut len,
            )
        };
        if result < 0 || len as usize != std::mem::size_of::<libc::ucred>() {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            stream,
            uid: credentials.uid,
            identity: None,
            last_seen: Instant::now(),
            input: Vec::with_capacity(MAX_LINE),
            output: Vec::new(),
            written: 0,
        })
    }
    pub fn fd(&self) -> RawFd {
        self.stream.as_raw_fd()
    }
    pub fn wants_write(&self) -> bool {
        self.written < self.output.len()
    }
    pub fn send(&mut self, value: Value) -> io::Result<()> {
        let bytes = serde_json::to_vec(&value)?;
        if bytes.len() > MAX_LINE {
            return Err(io::Error::other("oversized outgoing message"));
        }
        if self.written > 0 {
            self.output.drain(..self.written);
            self.written = 0;
        }
        if self.output.len() + bytes.len() + 1 > MAX_WRITE {
            return Err(io::Error::other("helper is not reading"));
        }
        self.output.extend_from_slice(&bytes);
        self.output.push(b'\n');
        self.flush()
    }
    pub fn flush(&mut self) -> io::Result<()> {
        while self.written < self.output.len() {
            match self.stream.write(&self.output[self.written..]) {
                Ok(0) => {
                    return Err(io::Error::new(
                        io::ErrorKind::BrokenPipe,
                        "helper disconnected",
                    ));
                }
                Ok(n) => self.written += n,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
        if self.written == self.output.len() {
            self.output.clear();
            self.written = 0;
        }
        Ok(())
    }
    pub fn read_messages(&mut self) -> io::Result<Vec<Message>> {
        let mut messages = Vec::new();
        let mut buffer = [0u8; 1024];
        for _ in 0..8 {
            match self.stream.read(&mut buffer) {
                Ok(0) => {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "helper disconnected",
                    ));
                }
                Ok(n) => {
                    self.input.extend_from_slice(&buffer[..n]);
                    while let Some(pos) = self.input.iter().position(|&b| b == b'\n') {
                        if pos > MAX_LINE {
                            return Err(io::Error::other("oversized helper message"));
                        }
                        let m: Message = serde_json::from_slice(&self.input[..pos])?;
                        if !m.valid() {
                            return Err(io::Error::other("invalid helper message"));
                        }
                        self.input.drain(..=pos);
                        messages.push(m);
                        if messages.len() > 32 {
                            return Err(io::Error::other("helper message flood"));
                        }
                    }
                    if self.input.len() > MAX_LINE {
                        return Err(io::Error::other("unterminated helper message"));
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
        Ok(messages)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_lines_and_valid_finite_coordinates() {
        let (a, mut b) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(a).unwrap();
        b.write_all(b"{\"type\":\"decision\",\"id\":1,\"epoch\":0,\"mode\":\"native\",\"x\":1,\"y\":2,\"focus\":\"window\"}\n").unwrap();
        assert_eq!(peer.read_messages().unwrap().len(), 1);
        b.write_all(&vec![b'x'; MAX_LINE + 1]).unwrap();
        assert!(peer.read_messages().is_err());
        assert!(
            !Message::Decision {
                id: 1,
                epoch: 0,
                mode: "native".into(),
                x: f64::NAN,
                y: 0.,
                focus: String::new()
            }
            .valid()
        );
    }
    #[test]
    fn unfinished_valid_messages_do_not_block() {
        let (a, mut b) = UnixStream::pair().unwrap();
        let mut peer = Peer::new(a).unwrap();
        b.write_all(b"{\"type\":").unwrap();
        assert!(peer.read_messages().unwrap().is_empty());
        assert!(peer.read_messages().unwrap().is_empty());
    }
    #[test]
    fn rejects_unknown_version_or_mode() {
        assert!(
            !Message::Hello {
                version: 1,
                session: "2".into(),
                seat: "seat0".into(),
                epoch: 0,
                enabled: true,
                locked: false,
                focus: String::new()
            }
            .valid()
        );
        assert!(
            !Message::Decision {
                id: 0,
                epoch: 0,
                mode: "paste".into(),
                x: 0.,
                y: 0.,
                focus: String::new()
            }
            .valid()
        );
    }
}
