"""Resolve this user's active, local graphical session on seat0 via libsystemd."""
import ctypes
import os

class SessionIdentity:
    def __init__(self, uid=None):
        self.uid = os.getuid() if uid is None else uid
        self.lib = ctypes.CDLL('libsystemd.so.0')
        self.libc = ctypes.CDLL(None)
        self.libc.free.argtypes = [ctypes.c_void_p]
        pointer = ctypes.POINTER(ctypes.c_void_p)
        self.lib.sd_seat_get_active.argtypes = [ctypes.c_char_p, pointer, ctypes.POINTER(ctypes.c_uint)]
        for name in ('sd_session_get_type', 'sd_session_get_seat', 'sd_session_get_class'):
            getattr(self.lib, name).argtypes = [ctypes.c_char_p, pointer]
        self.lib.sd_session_is_remote.argtypes = [ctypes.c_char_p]
        self.lib.sd_session_is_active.argtypes = [ctypes.c_char_p]

    def string(self, name, session):
        value = ctypes.c_void_p()
        result = getattr(self.lib, name)(session, ctypes.byref(value))
        try:
            return ctypes.string_at(value.value).decode() if result >= 0 and value.value else None
        finally:
            self.libc.free(value)

    def current(self):
        value = ctypes.c_void_p()
        uid = ctypes.c_uint()
        result = self.lib.sd_seat_get_active(b'seat0', ctypes.byref(value), ctypes.byref(uid))
        try:
            if result < 0 or not value.value or uid.value != self.uid:
                return None
            session = ctypes.string_at(value.value)
        finally:
            self.libc.free(value)
        if (self.lib.sd_session_is_remote(session) != 0
                or self.lib.sd_session_is_active(session) != 1
                or self.string('sd_session_get_seat', session) != 'seat0'
                or self.string('sd_session_get_type', session) not in ('x11', 'wayland')
                or self.string('sd_session_get_class', session) not in ('user', 'user-early')):
            return None
        return session.decode(), 'seat0'
