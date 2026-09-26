# Local patch to evdev 0.13.2

Upstream source: https://crates.io/crates/evdev/0.13.2
Licenses: retained MIT and Apache-2.0 license files.

`src/sys.rs` fixes the Linux `UI_SET_PHYS` ioctl request number. Linux defines
this ioctl with `_IOW(UINPUT_IOCTL_BASE, 108, char*)`, so the encoded argument
size must be `sizeof(char*)`, although the syscall's argument remains a pointer
to a character buffer. Upstream's `ioctl_write_ptr!(..., libc::c_char)` instead
encodes one byte and the running Linux kernel rejects it with `EINVAL`.

This patch uses nix's `ioctl_write_ptr_bad!` with the explicit pointer-sized
request code while retaining the original `*const c_char` function signature.
No other upstream behavior is changed. The application's isolated kernel tests
exercise successful physical-path setup and verify the marker round trips
before emitting events.

Original crates.io archive SHA-256:
`25b686663ba7f08d92880ff6ba22170f1df4e83629341cba34cf82cd65ebea99`
