#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PlayStation Stream Recorder - chiaki-ng Python Interface

Connects to a PlayStation 4/5 on the local network and records the
Remote Play stream (video + audio) to a file using FFmpeg.

Requirements (Fedora):
    sudo dnf install python3 ffmpeg
    # Build chiaki-ng first to get libchiaki.so:
    #   cd chiaki-ng && mkdir build && cd build
    #   cmake -DCMAKE_BUILD_TYPE=Release ..
    #   make -j$(nproc)
    # The library will be at build/lib/libchiaki.so

Usage:
    # 1) Discover your PlayStation on the network
    python3 ps_stream_recorder.py discover --host 192.168.1.X

    # 2) Register this device with your PlayStation (one-time)
    #    Go to PS Settings > Remote Play > Link Device to get a PIN
    python3 ps_stream_recorder.py register \\
        --host 192.168.1.X \\
        --psn-account-id <base64_account_id> \\
        --pin 12345678

    # 3) Record the stream
    python3 ps_stream_recorder.py record \\
        --host 192.168.1.X \\
        --regist-key <hex_regist_key> \\
        --morning <hex_morning> \\
        --output recording.mp4 \\
        --duration 60

    # Or use a config file
    python3 ps_stream_recorder.py record --config ps_config.json --output recording.mp4

    # 4) Stream raw H.264 to stdout (pipe to another app, low-latency)
    python3 ps_stream_recorder.py stream --config ps_config.json | \
        ffplay -fflags nobuffer -flags low_delay -framedrop -f h264 -

    # 5) Stream to a named pipe (FIFO)
    python3 ps_stream_recorder.py stream --config ps_config.json --fifo /tmp/ps_video

    # 6) Stream to a v4l2loopback virtual camera (GPU-decoded)
    sudo modprobe v4l2loopback video_nr=9
    python3 ps_stream_recorder.py stream --config ps_config.json --v4l2 /dev/video9

License: AGPL-3.0-only-OpenSSL (same as chiaki-ng)
"""

import argparse
import base64
import ctypes
import ctypes.util
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Opus decoder wrapper (for decoding audio frames to PCM)
# ---------------------------------------------------------------------------

_opus_lib = None
_opus_lib_searched = False


def _load_opus_lib():
    """Load libopus.so for Opus decoding."""
    global _opus_lib, _opus_lib_searched
    if _opus_lib_searched:
        return _opus_lib
    _opus_lib_searched = True
    for name in ["libopus.so.0", "libopus.so", "opus"]:
        try:
            _opus_lib = ctypes.CDLL(name)
            return _opus_lib
        except OSError:
            continue
    found = ctypes.util.find_library("opus")
    if found:
        try:
            _opus_lib = ctypes.CDLL(found)
            return _opus_lib
        except OSError:
            pass
    return None


class OpusDecoder:
    """Decode Opus packets to PCM int16 samples using libopus."""

    def __init__(self, sample_rate=48000, channels=2):
        self._lib = _load_opus_lib()
        if not self._lib:
            raise RuntimeError(
                "libopus.so not found. Install it with: sudo dnf install opus"
            )
        self._lib.opus_decoder_create.restype = ctypes.c_void_p
        self._lib.opus_decoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]
        self._lib.opus_decode.restype = ctypes.c_int
        self._lib.opus_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int,
        ]
        self._lib.opus_decoder_destroy.restype = None
        self._lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

        self._channels = channels
        self._max_frame_size = 5760  # max Opus frame size at 48kHz
        self._pcm_buf = (ctypes.c_int16 * (self._max_frame_size * channels))()

        error = ctypes.c_int(0)
        self._decoder = self._lib.opus_decoder_create(
            ctypes.c_int32(sample_rate), ctypes.c_int(channels),
            ctypes.byref(error),
        )
        if error.value != 0 or not self._decoder:
            raise RuntimeError(f"opus_decoder_create failed (error {error.value})")

    def decode(self, opus_data_ptr, opus_len):
        """Decode an Opus packet. Returns PCM bytes (int16 LE) or None."""
        samples = self._lib.opus_decode(
            self._decoder, opus_data_ptr, ctypes.c_int32(opus_len),
            self._pcm_buf, ctypes.c_int(self._max_frame_size), ctypes.c_int(0),
        )
        if samples <= 0:
            return None
        nbytes = samples * self._channels * 2
        return ctypes.string_at(self._pcm_buf, nbytes)

    def __del__(self):
        if hasattr(self, '_decoder') and self._decoder and hasattr(self, '_lib') and self._lib:
            try:
                self._lib.opus_decoder_destroy(self._decoder)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Controller input via evdev (DualSense / DualShock)
# ---------------------------------------------------------------------------

_evdev_available = False
try:
    import evdev
    from evdev import ecodes
    _evdev_available = True
except ImportError:
    pass


# ChiakiControllerButton bitmask values
CONTROLLER_BUTTON_CROSS      = (1 << 0)
CONTROLLER_BUTTON_MOON       = (1 << 1)
CONTROLLER_BUTTON_BOX        = (1 << 2)
CONTROLLER_BUTTON_PYRAMID    = (1 << 3)
CONTROLLER_BUTTON_DPAD_LEFT  = (1 << 4)
CONTROLLER_BUTTON_DPAD_RIGHT = (1 << 5)
CONTROLLER_BUTTON_DPAD_UP    = (1 << 6)
CONTROLLER_BUTTON_DPAD_DOWN  = (1 << 7)
CONTROLLER_BUTTON_L1         = (1 << 8)
CONTROLLER_BUTTON_R1         = (1 << 9)
CONTROLLER_BUTTON_L3         = (1 << 10)
CONTROLLER_BUTTON_R3         = (1 << 11)
CONTROLLER_BUTTON_OPTIONS    = (1 << 12)
CONTROLLER_BUTTON_SHARE      = (1 << 13)
CONTROLLER_BUTTON_TOUCHPAD   = (1 << 14)
CONTROLLER_BUTTON_PS         = (1 << 15)

# evdev button code -> chiaki button bitmask
_EVDEV_BUTTON_MAP = {}
if _evdev_available:
    _EVDEV_BUTTON_MAP = {
        ecodes.BTN_SOUTH:  CONTROLLER_BUTTON_CROSS,
        ecodes.BTN_EAST:   CONTROLLER_BUTTON_MOON,
        ecodes.BTN_NORTH:  CONTROLLER_BUTTON_PYRAMID,
        ecodes.BTN_WEST:   CONTROLLER_BUTTON_BOX,
        ecodes.BTN_TL:     CONTROLLER_BUTTON_L1,
        ecodes.BTN_TR:     CONTROLLER_BUTTON_R1,
        ecodes.BTN_THUMBL: CONTROLLER_BUTTON_L3,
        ecodes.BTN_THUMBR: CONTROLLER_BUTTON_R3,
        ecodes.BTN_START:  CONTROLLER_BUTTON_OPTIONS,
        ecodes.BTN_SELECT: CONTROLLER_BUTTON_SHARE,
        ecodes.BTN_MODE:   CONTROLLER_BUTTON_PS,
    }


class ChiakiControllerTouch(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_uint16),
        ("y", ctypes.c_uint16),
        ("id", ctypes.c_int8),
    ]

CHIAKI_CONTROLLER_TOUCHES_MAX = 2


class ChiakiControllerState(ctypes.Structure):
    _fields_ = [
        ("buttons", ctypes.c_uint32),
        ("l2_state", ctypes.c_uint8),
        ("r2_state", ctypes.c_uint8),
        ("left_x", ctypes.c_int16),
        ("left_y", ctypes.c_int16),
        ("right_x", ctypes.c_int16),
        ("right_y", ctypes.c_int16),
        ("touch_id_next", ctypes.c_uint8),
        ("_pad0", ctypes.c_uint8),
        ("touches", ChiakiControllerTouch * CHIAKI_CONTROLLER_TOUCHES_MAX),
        ("gyro_x", ctypes.c_float),
        ("gyro_y", ctypes.c_float),
        ("gyro_z", ctypes.c_float),
        ("accel_x", ctypes.c_float),
        ("accel_y", ctypes.c_float),
        ("accel_z", ctypes.c_float),
        ("orient_x", ctypes.c_float),
        ("orient_y", ctypes.c_float),
        ("orient_z", ctypes.c_float),
        ("orient_w", ctypes.c_float),
    ]


def find_dualsense_device():
    """Find the DualSense/DualShock gamepad evdev device."""
    if not _evdev_available:
        raise RuntimeError(
            "evdev not available. Install with: pip install evdev\n"
            "  or: sudo dnf install python3-evdev"
        )
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    for dev in devices:
        name_lower = dev.name.lower()
        caps = dev.capabilities(verbose=False)
        # Match DualSense/DualShock gamepad node (has EV_KEY with BTN_SOUTH)
        if ("dualsense" in name_lower or "dualshock" in name_lower
                or "wireless controller" in name_lower):
            ev_key = caps.get(ecodes.EV_KEY, [])
            if ecodes.BTN_SOUTH in ev_key:
                return dev
    # Fallback: any gamepad with BTN_SOUTH + ABS_X
    for dev in devices:
        caps = dev.capabilities(verbose=False)
        ev_key = caps.get(ecodes.EV_KEY, [])
        ev_abs = caps.get(ecodes.EV_ABS, [])
        abs_codes = [a[0] if isinstance(a, tuple) else a for a in ev_abs]
        if ecodes.BTN_SOUTH in ev_key and ecodes.ABS_X in abs_codes:
            return dev
    raise RuntimeError(
        "No gamepad found. Connect a DualSense/DualShock controller.\n"
        "  Check with: evtest"
    )


def _make_axis_converter(absinfo):
    """Return a function that converts an evdev axis value to chiaki int16
    using the real min/max reported by the device."""
    lo = absinfo.min
    hi = absinfo.max
    mid = (lo + hi) / 2.0
    half = (hi - lo) / 2.0 or 1.0  # avoid division by zero

    def convert(value):
        normalized = (value - mid) / half          # -1.0 .. +1.0
        scaled = int(normalized * 32767)
        return max(-32768, min(32767, scaled))

    return convert


class ControllerInputThread:
    """
    Reads a DualSense/DualShock controller via evdev and sends
    controller state to a chiaki session.
    """

    def __init__(self, lib, session_ptr, device_path=None, log_fn=None):
        self._lib = lib
        self._session_ptr = session_ptr
        self._log = log_fn or (lambda msg: print(msg, file=sys.stderr))
        self._stop_event = threading.Event()
        self._thread = None
        self._state = ChiakiControllerState()
        ctypes.memset(ctypes.byref(self._state), 0, ctypes.sizeof(self._state))
        for i in range(CHIAKI_CONTROLLER_TOUCHES_MAX):
            self._state.touches[i].id = -1

        # Detect controller immediately (raises on failure)
        if device_path:
            self._device = evdev.InputDevice(device_path)
        else:
            self._device = find_dualsense_device()
        self._log(f"[+] Controller: {self._device.name} ({self._device.path})")

        # Build per-axis converters from the device's real absinfo
        self._axis_conv = {}
        for axis_code in (ecodes.ABS_X, ecodes.ABS_Y,
                          ecodes.ABS_RX, ecodes.ABS_RY):
            try:
                info = self._device.absinfo(axis_code)
                self._axis_conv[axis_code] = _make_axis_converter(info)
            except (KeyError, OSError):
                pass

        # Trigger converters (0..255 → 0..255, identity)
        self._trigger_conv = {}
        for axis_code in (ecodes.ABS_Z, ecodes.ABS_RZ):
            try:
                info = self._device.absinfo(axis_code)
                lo, hi = info.min, info.max
                span = (hi - lo) or 1
                self._trigger_conv[axis_code] = lambda v, lo=lo, span=span: \
                    max(0, min(255, int((v - lo) / span * 255)))
            except (KeyError, OSError):
                pass

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _send_state(self):
        self._lib.chiaki_session_set_controller_state(
            self._session_ptr, ctypes.byref(self._state)
        )

    def _run(self):
        dev = self._device
        try:
            import select as _select
            while not self._stop_event.is_set():
                r, _, _ = _select.select([dev.fd], [], [], 0.1)
                if not r:
                    continue
                for event in dev.read():
                    if event.type == ecodes.EV_KEY:
                        chiaki_btn = _EVDEV_BUTTON_MAP.get(event.code)
                        if chiaki_btn:
                            if event.value:
                                self._state.buttons |= chiaki_btn
                            else:
                                self._state.buttons &= ~chiaki_btn
                            self._send_state()

                    elif event.type == ecodes.EV_ABS:
                        code = event.code
                        val = event.value

                        if code == ecodes.ABS_X:
                            conv = self._axis_conv.get(code)
                            self._state.left_x = conv(val) if conv else val
                        elif code == ecodes.ABS_Y:
                            conv = self._axis_conv.get(code)
                            self._state.left_y = conv(val) if conv else val
                        elif code == ecodes.ABS_RX:
                            conv = self._axis_conv.get(code)
                            self._state.right_x = conv(val) if conv else val
                        elif code == ecodes.ABS_RY:
                            conv = self._axis_conv.get(code)
                            self._state.right_y = conv(val) if conv else val
                        elif code == ecodes.ABS_Z:
                            conv = self._trigger_conv.get(code)
                            self._state.l2_state = conv(val) if conv else (val & 0xFF)
                        elif code == ecodes.ABS_RZ:
                            conv = self._trigger_conv.get(code)
                            self._state.r2_state = conv(val) if conv else (val & 0xFF)
                        elif code == ecodes.ABS_HAT0X:
                            self._state.buttons &= ~(CONTROLLER_BUTTON_DPAD_LEFT | CONTROLLER_BUTTON_DPAD_RIGHT)
                            if val < 0:
                                self._state.buttons |= CONTROLLER_BUTTON_DPAD_LEFT
                            elif val > 0:
                                self._state.buttons |= CONTROLLER_BUTTON_DPAD_RIGHT
                        elif code == ecodes.ABS_HAT0Y:
                            self._state.buttons &= ~(CONTROLLER_BUTTON_DPAD_UP | CONTROLLER_BUTTON_DPAD_DOWN)
                            if val < 0:
                                self._state.buttons |= CONTROLLER_BUTTON_DPAD_UP
                            elif val > 0:
                                self._state.buttons |= CONTROLLER_BUTTON_DPAD_DOWN
                        else:
                            continue
                        self._send_state()
        except Exception as e:
            if not self._stop_event.is_set():
                self._log(f"[!] Controller read error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Constants from chiaki-ng headers
# ---------------------------------------------------------------------------

# Discovery ports
DISCOVERY_PORT_PS4 = 987
DISCOVERY_PORT_PS5 = 9302
DISCOVERY_PROTOCOL_VERSION_PS4 = "00020020"
DISCOVERY_PROTOCOL_VERSION_PS5 = "00030010"

# Session
SESSION_PORT = 9295
SESSION_AUTH_SIZE = 0x10
PSN_ACCOUNT_ID_SIZE = 8
RPCRYPT_KEY_SIZE = 0x10

# Video codecs
CODEC_H264 = 0
CODEC_H265 = 1
CODEC_H265_HDR = 2

# Error codes
ERR_SUCCESS = 0

# Log levels
LOG_ERROR = (1 << 0)
LOG_WARNING = (1 << 1)
LOG_INFO = (1 << 2)
LOG_VERBOSE = (1 << 3)
LOG_DEBUG = (1 << 4)
LOG_ALL = (1 << 5) - 1

# Targets
TARGET_PS4_UNKNOWN = 0
TARGET_PS4_8 = 800
TARGET_PS4_9 = 900
TARGET_PS4_10 = 1000
TARGET_PS5_UNKNOWN = 1000000
TARGET_PS5_1 = 1000100

# Event types
EVENT_CONNECTED = 0
EVENT_LOGIN_PIN_REQUEST = 1
EVENT_QUIT = 9

# Quit reasons
QUIT_REASON_NONE = 0
QUIT_REASON_STOPPED = 1

# Audio/Video disable
NONE_DISABLED = 0

# Video buffer padding
VIDEO_BUFFER_PADDING_SIZE = 64


# ---------------------------------------------------------------------------
# Pure-Python Discovery (no library needed)
# ---------------------------------------------------------------------------

def discover_consoles(host, timeout=3.0):
    """
    Send PS4 and PS5 discovery packets via UDP and collect responses.
    Works without libchiaki — uses the simple HTTP-like text protocol.
    """
    results = []

    for port, proto_ver, console_type in [
        (DISCOVERY_PORT_PS4, DISCOVERY_PROTOCOL_VERSION_PS4, "PS4"),
        (DISCOVERY_PORT_PS5, DISCOVERY_PROTOCOL_VERSION_PS5, "PS5"),
    ]:
        pkt = f"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:{proto_ver}\n"

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)

            # Bind to an ephemeral port in the chiaki range (9303-9319)
            bound = False
            for bind_port in range(9303, 9320):
                try:
                    sock.bind(("", bind_port))
                    bound = True
                    break
                except OSError:
                    continue
            if not bound:
                sock.bind(("", 0))

            sock.sendto(pkt.encode("utf-8"), (host, port))

            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    response = _parse_discovery_response(data.decode("utf-8", errors="replace"), addr[0])
                    if response:
                        response["expected_type"] = console_type
                        results.append(response)
                except socket.timeout:
                    break
        except Exception as e:
            print(f"[!] Discovery error for {console_type} on port {port}: {e}", file=sys.stderr)
        finally:
            sock.close()

    return results


def _parse_discovery_response(text, addr):
    """Parse the HTTP-like discovery response from a PlayStation."""
    lines = text.strip().split("\n")
    if not lines:
        return None

    # First line: "HTTP/1.1 200 Ok" or "HTTP/1.1 620 Server Standby"
    status_line = lines[0].strip()
    parts = status_line.split(None, 2)
    if len(parts) < 2:
        return None

    try:
        status_code = int(parts[1])
    except ValueError:
        return None

    info = {
        "host_addr": addr,
        "status_code": status_code,
        "state": "ready" if status_code == 200 else ("standby" if status_code == 620 else "unknown"),
    }

    for line in lines[1:]:
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip().lower().replace("-", "_")] = value.strip()

    return info


def wakeup_console(host, regist_key_hex, ps5=False):
    """
    Send a wakeup packet to bring the console out of standby.

    :param host: IP address of the console
    :param regist_key_hex: registration key as hex string
    :param ps5: True for PS5, False for PS4
    """
    if ps5:
        port = DISCOVERY_PORT_PS5
        proto_ver = DISCOVERY_PROTOCOL_VERSION_PS5
    else:
        port = DISCOVERY_PORT_PS4
        proto_ver = DISCOVERY_PROTOCOL_VERSION_PS4

    # user_credential is the regist_key interpreted as hex
    user_credential = int(regist_key_hex, 16) if regist_key_hex else 0

    pkt = (
        "WAKEUP * HTTP/1.1\n"
        "client-type:vr\n"
        "auth-type:R\n"
        "model:w\n"
        "app-type:r\n"
        f"user-credential:{user_credential}\n"
        f"device-discovery-protocol-version:{proto_ver}\n"
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(pkt.encode("utf-8"), (host, port))
        print(f"[+] Wakeup packet sent to {host}:{port}")
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# libchiaki ctypes bindings
# ---------------------------------------------------------------------------

_lib = None
_lib_lock = threading.Lock()


def _find_libchiaki():
    """Search for libchiaki.so in common locations."""
    search_paths = [
        # Build directory (relative to this script)
        Path(__file__).resolve().parent.parent / "build" / "lib" / "libchiaki.so",
        # Flatpak / system-wide
        Path("/usr/lib64/libchiaki.so"),
        Path("/usr/lib/libchiaki.so"),
        Path("/usr/local/lib64/libchiaki.so"),
        Path("/usr/local/lib/libchiaki.so"),
    ]

    # Also check CHIAKI_LIB_PATH env var
    env_path = os.environ.get("CHIAKI_LIB_PATH")
    if env_path:
        search_paths.insert(0, Path(env_path))

    for p in search_paths:
        if p.exists():
            return str(p)

    # Try system library search
    found = ctypes.util.find_library("chiaki")
    if found:
        return found

    return None


def load_libchiaki(lib_path=None):
    """Load libchiaki.so and set up function signatures."""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib

        if lib_path is None:
            lib_path = _find_libchiaki()

        if lib_path is None:
            raise RuntimeError(
                "Could not find libchiaki.so. Build chiaki-ng first:\n"
                "  cd chiaki-ng && mkdir -p build && cd build\n"
                "  cmake -DCMAKE_BUILD_TYPE=Release ..\n"
                "  make -j$(nproc)\n"
                "Or set CHIAKI_LIB_PATH=/path/to/libchiaki.so"
            )

        print(f"[+] Loading libchiaki from: {lib_path}")
        _lib = ctypes.CDLL(lib_path)

        # chiaki_lib_init
        _lib.chiaki_lib_init.restype = ctypes.c_int
        _lib.chiaki_lib_init.argtypes = []

        # chiaki_error_string
        _lib.chiaki_error_string.restype = ctypes.c_char_p
        _lib.chiaki_error_string.argtypes = [ctypes.c_int]

        # chiaki_log_init
        _lib.chiaki_log_init.restype = None
        _lib.chiaki_log_init.argtypes = [
            ctypes.c_void_p,  # ChiakiLog*
            ctypes.c_uint32,  # level_mask
            ctypes.c_void_p,  # callback
            ctypes.c_void_p,  # user
        ]

        # chiaki_log_cb_print
        _lib.chiaki_log_cb_print.restype = None

        # Initialize the library
        err = _lib.chiaki_lib_init()
        if err != ERR_SUCCESS:
            raise RuntimeError(f"chiaki_lib_init failed: {_lib.chiaki_error_string(err).decode()}")

        # Verify session setter functions are exported (they were converted
        # from static inline to CHIAKI_EXPORT; older builds won't have them)
        required_symbols = [
            "chiaki_session_set_event_cb",
            "chiaki_session_set_video_sample_cb",
            "chiaki_session_set_audio_sink",
        ]
        for sym in required_symbols:
            if not hasattr(_lib, sym):
                raise RuntimeError(
                    f"libchiaki.so is missing '{sym}'. You need to rebuild chiaki-ng:\n"
                    f"  cd chiaki-ng/build && cmake .. && make -j$(nproc)\n"
                    f"  sudo make install"
                )

        return _lib


# ---------------------------------------------------------------------------
# Struct definitions for ctypes (matching C struct layouts on x86_64 Linux)
# ---------------------------------------------------------------------------

class ChiakiLog(ctypes.Structure):
    _fields_ = [
        ("level_mask", ctypes.c_uint32),
        ("_pad0", ctypes.c_uint8 * 4),  # padding for alignment
        ("cb", ctypes.c_void_p),        # function pointer
        ("user", ctypes.c_void_p),
    ]


class ChiakiConnectVideoProfile(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint),
        ("height", ctypes.c_uint),
        ("max_fps", ctypes.c_uint),
        ("bitrate", ctypes.c_uint),
        ("codec", ctypes.c_int),  # ChiakiCodec enum
    ]


class ChiakiConnectInfo(ctypes.Structure):
    _fields_ = [
        ("ps5", ctypes.c_bool),
        ("_pad0", ctypes.c_uint8 * 7),                     # align to pointer
        ("host", ctypes.c_char_p),
        # Use c_uint8 instead of c_char to avoid ctypes pitfall:
        # accessing a c_char array field returns a bytes COPY, so memmove
        # to it silently writes to a temporary. c_uint8 returns the actual array.
        ("regist_key", ctypes.c_uint8 * SESSION_AUTH_SIZE),
        ("morning", ctypes.c_uint8 * RPCRYPT_KEY_SIZE),
        ("video_profile", ChiakiConnectVideoProfile),
        ("video_profile_auto_downgrade", ctypes.c_bool),
        ("enable_keyboard", ctypes.c_bool),
        ("enable_dualsense", ctypes.c_bool),
        ("_pad1", ctypes.c_uint8),                          # enum padding
        ("audio_video_disabled", ctypes.c_int),
        ("auto_regist", ctypes.c_bool),
        ("_pad2", ctypes.c_uint8 * 3),                     # align to pointer
        ("holepunch_session", ctypes.c_void_p),
        ("rudp_sock", ctypes.c_void_p),
        ("psn_account_id", ctypes.c_uint8 * PSN_ACCOUNT_ID_SIZE),
        ("packet_loss_max", ctypes.c_double),
        ("enable_idr_on_fec_failure", ctypes.c_bool),
    ]


# ---------------------------------------------------------------------------
# Diagnostic: verify struct layout matches the C definition
# ---------------------------------------------------------------------------

def verify_struct_layout():
    """Print the ctypes struct layout for debugging."""
    print("[debug] ChiakiConnectInfo field offsets:")
    for name, ctype in ChiakiConnectInfo._fields_:
        field = getattr(ChiakiConnectInfo, name)
        print(f"  {name:40s} offset={field.offset:3d}  size={field.size}")
    print(f"  {'TOTAL':40s} size={ctypes.sizeof(ChiakiConnectInfo)}")
    print()


# ---------------------------------------------------------------------------
# Stream recording
# ---------------------------------------------------------------------------

class StreamRecorder:
    """
    Records a PlayStation Remote Play stream by wrapping chiaki-ng's
    libchiaki through ctypes.

    Video frames (H.264 / H.265 NAL units) are written to a temp file.
    Audio frames (Opus) are decoded to PCM via libopus and written to
    a temp file. After recording, FFmpeg muxes both into the output.
    """

    def __init__(self, host, regist_key, morning,
                 ps5=False, codec=CODEC_H264,
                 width=1920, height=1080, fps=60, bitrate=15000,
                 output="recording.mp4", duration=None,
                 psn_account_id=None, lib_path=None,
                 verbose=False):
        self.host = host
        self.regist_key = regist_key  # bytes, 16 bytes
        self.morning = morning        # bytes, 16 bytes
        self.ps5 = ps5
        self.codec = codec
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.output = output
        self.duration = duration
        self.psn_account_id = psn_account_id or bytes(PSN_ACCOUNT_ID_SIZE)
        self.verbose = verbose

        self._lib = load_libchiaki(lib_path)
        self._stop_event = threading.Event()
        self._tmpdir = None
        self._video_file = None
        self._audio_file = None
        self._video_frames = 0
        self._audio_frames = 0
        self._start_time = None
        self._log_buf = None
        self._audio_channels = 2
        self._audio_rate = 48000
        self._opus_decoder = None

        # Callback references (prevent GC)
        self._event_cb_ref = None
        self._video_cb_ref = None
        self._audio_header_cb_ref = None
        self._audio_frame_cb_ref = None

    def _setup_recording(self):
        """Create temp directory and files for raw stream data."""
        self._tmpdir = tempfile.mkdtemp(prefix="chiaki_rec_")
        codec_ext = "h264" if self.codec == CODEC_H264 else "h265"
        self._video_file = os.path.join(self._tmpdir, f"video.{codec_ext}")
        self._audio_file = os.path.join(self._tmpdir, "audio.pcm")

    def _mux_output(self):
        """Mux raw video and decoded PCM audio into the final output file."""
        codec_name = "h264" if self.codec == CODEC_H264 else "hevc"

        cmd = ["ffmpeg", "-y"]

        # Video input
        cmd.extend([
            "-f", codec_name,
            "-framerate", str(self.fps),
            "-i", self._video_file,
        ])

        # Audio input (decoded PCM, if available)
        has_audio = (self._audio_file
                     and os.path.exists(self._audio_file)
                     and os.path.getsize(self._audio_file) > 0)
        if has_audio:
            cmd.extend([
                "-f", "s16le",
                "-ar", str(self._audio_rate),
                "-ac", str(self._audio_channels),
                "-i", self._audio_file,
            ])

        # Output options
        cmd.extend(["-c:v", "copy"])
        if has_audio:
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-shortest"])
        cmd.append(self.output)

        print(f"[+] Muxing with FFmpeg: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            stdout=None if self.verbose else subprocess.PIPE,
            stderr=None if self.verbose else subprocess.PIPE,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            print(f"[!] FFmpeg muxing failed (exit {result.returncode})", file=sys.stderr)
            if stderr:
                # Show last few lines of stderr
                for line in stderr.strip().splitlines()[-10:]:
                    print(f"    {line}", file=sys.stderr)
            raise RuntimeError("FFmpeg muxing failed")

    def _cleanup(self):
        """Clean up temp files."""
        if self._opus_decoder:
            del self._opus_decoder
            self._opus_decoder = None
        for f in [self._video_file, self._audio_file]:
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
        if self._tmpdir and os.path.exists(self._tmpdir):
            try:
                os.rmdir(self._tmpdir)
            except OSError:
                pass

    def _make_event_callback(self):
        """Create the C callback for session events."""
        CALLBACK_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

        def event_cb(event_ptr, user_ptr):
            if not event_ptr:
                return
            # Read the event type (first field, uint32)
            event_type = ctypes.cast(event_ptr, ctypes.POINTER(ctypes.c_int))[0]

            if event_type == EVENT_CONNECTED:
                print("[+] Connected to PlayStation!")
                self._start_time = time.time()

            elif event_type == EVENT_LOGIN_PIN_REQUEST:
                print("[!] Login PIN requested by console. Use --pin if needed.")

            elif event_type == EVENT_QUIT:
                # ChiakiQuitEvent: { ChiakiQuitReason reason; const char *reason_str; }
                # On x86_64: reason is int (4 bytes) at offset 0, then padding to 8,
                # then reason_str pointer at offset 8
                # But the event is inside a union in ChiakiEvent, which starts
                # after the ChiakiEventType (int, 4 bytes + 4 padding = offset 8)
                quit_event_offset = 8  # after ChiakiEventType + padding
                quit_reason = ctypes.cast(
                    ctypes.c_void_p(event_ptr + quit_event_offset),
                    ctypes.POINTER(ctypes.c_int)
                )[0]

                reason_str = "unknown"
                # Try to read reason_str pointer (at offset 8 within quit event)
                reason_str_ptr = ctypes.cast(
                    ctypes.c_void_p(event_ptr + quit_event_offset + 8),
                    ctypes.POINTER(ctypes.c_void_p)
                )[0]
                if reason_str_ptr:
                    try:
                        reason_str = ctypes.cast(reason_str_ptr, ctypes.c_char_p).value.decode()
                    except Exception:
                        pass

                if reason_str == "unknown":
                    try:
                        reason_str = self._lib.chiaki_quit_reason_string(quit_reason).decode()
                    except Exception:
                        pass

                print(f"[+] Session ended: {reason_str} (quit_reason={quit_reason})")

                # Give actionable advice for common quit reasons
                # These values match the ChiakiQuitReason enum order
                QUIT_SESSION_REQUEST_UNKNOWN = 2
                QUIT_RP_IN_USE = 4
                QUIT_RP_VERSION_MISMATCH = 6
                if quit_reason == QUIT_SESSION_REQUEST_UNKNOWN:
                    print("[!] Hint: The console rejected the session. Check that:")
                    print("      1. Remote Play is enabled (Settings > System > Remote Play)")
                    print("      2. Your regist_key is correct (re-register if needed)")
                    print("      3. The console is awake and on the home screen")
                    print("      4. No other Remote Play session is active")
                    print("      5. The morning value is the rp_key from registration")
                    print("      6. Try running 'discover' first to verify console state")
                elif quit_reason == QUIT_RP_IN_USE:
                    print("[!] Hint: Another Remote Play session is already active")
                elif quit_reason == QUIT_RP_VERSION_MISMATCH:
                    print("[!] Hint: RP protocol version mismatch. Try rebuilding chiaki-ng")

                self._stop_event.set()

        self._event_cb_ref = CALLBACK_TYPE(event_cb)
        return self._event_cb_ref

    def _make_video_callback(self, video_fd):
        """Create the C callback for video samples (H.264/H.265 NAL units)."""
        CALLBACK_TYPE = ctypes.CFUNCTYPE(
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_uint8),  # buf
            ctypes.c_size_t,                   # buf_size
            ctypes.c_int32,                    # frames_lost
            ctypes.c_bool,                     # frame_recovered
            ctypes.c_void_p,                   # user
        )

        def video_cb(buf, buf_size, frames_lost, frame_recovered, user):
            try:
                data = ctypes.string_at(buf, buf_size)
                os.write(video_fd, data)
                self._video_frames += 1
                if self._video_frames % 300 == 0:
                    elapsed = time.time() - (self._start_time or time.time())
                    print(f"  [video] {self._video_frames} frames ({elapsed:.1f}s)")
                return True
            except Exception as e:
                print(f"[!] Video callback error: {e}", file=sys.stderr)
                return False

        self._video_cb_ref = CALLBACK_TYPE(video_cb)
        return self._video_cb_ref

    def _make_audio_callbacks(self, audio_fd):
        """Create the C callbacks for audio (Opus frames decoded to PCM)."""
        HEADER_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
        FRAME_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_void_p)

        def audio_header_cb(header_ptr, user):
            if header_ptr:
                channels = ctypes.cast(header_ptr, ctypes.POINTER(ctypes.c_uint8))[0]
                bits = ctypes.cast(header_ptr, ctypes.POINTER(ctypes.c_uint8))[1]
                rate_bytes = ctypes.string_at(ctypes.c_void_p(header_ptr + 4), 4)
                rate = struct.unpack("<I", rate_bytes)[0]
                print(f"  [audio] Stream info: {channels}ch, {bits}bit, {rate}Hz")
                self._audio_channels = channels if channels else 2
                self._audio_rate = rate if rate else 48000
                # Initialize Opus decoder now that we know the audio params
                try:
                    self._opus_decoder = OpusDecoder(
                        sample_rate=self._audio_rate,
                        channels=self._audio_channels,
                    )
                    print(f"  [audio] Opus decoder initialized ({self._audio_rate}Hz, {self._audio_channels}ch)")
                except Exception as e:
                    print(f"  [!] Opus decoder init failed: {e} (audio will be skipped)")
                    self._opus_decoder = None

        def audio_frame_cb(buf, buf_size, user):
            try:
                if self._opus_decoder:
                    pcm_data = self._opus_decoder.decode(buf, buf_size)
                    if pcm_data:
                        os.write(audio_fd, pcm_data)
                self._audio_frames += 1
            except Exception as e:
                print(f"[!] Audio callback error: {e}", file=sys.stderr)

        self._audio_header_cb_ref = HEADER_CB_TYPE(audio_header_cb)
        self._audio_frame_cb_ref = FRAME_CB_TYPE(audio_frame_cb)
        return self._audio_header_cb_ref, self._audio_frame_cb_ref

    def record(self):
        """
        Main recording function. Connects to the PlayStation and records
        the stream to the output file.
        """
        print(f"[+] PlayStation Stream Recorder")
        print(f"    Host: {self.host}")
        print(f"    Console: {'PS5' if self.ps5 else 'PS4'}")
        print(f"    Resolution: {self.width}x{self.height} @ {self.fps}fps")
        print(f"    Codec: {'H.265' if self.codec != CODEC_H264 else 'H.264'}")
        print(f"    Output: {self.output}")
        if self.duration:
            print(f"    Duration: {self.duration}s")

        # Show credential info to help diagnose auth failures
        rk_hex = self.regist_key.hex() if self.regist_key else "(none)"
        # Find effective length (up to first null, since C code does this)
        rk_len = len(self.regist_key)
        for i, b in enumerate(self.regist_key):
            if b == 0:
                rk_len = i
                break
        rk_ascii = self.regist_key[:rk_len].decode("ascii", errors="replace")
        print(f"    Regist key: {rk_hex} (effective {rk_len} bytes, ASCII: \"{rk_ascii}\")")

        # Show what the HTTP header will look like
        rk_as_hex_header = self.regist_key[:rk_len].hex()
        print(f"    RP-Registkey header will be: {rk_as_hex_header}")

        if self.psn_account_id and self.psn_account_id != bytes(PSN_ACCOUNT_ID_SIZE):
            print(f"    PSN Account ID: {base64.b64encode(self.psn_account_id).decode()}")
        else:
            print(f"    PSN Account ID: (not set)")

        morning_hex = bytes(self.morning).hex() if self.morning else "(none)"
        print(f"    Morning (rp_key): {morning_hex}")
        print()

        if self.verbose:
            verify_struct_layout()

        # Set up temp files for raw stream data
        self._setup_recording()

        try:
            # Open temp files for writing (no blocking, no deadlocks)
            video_fd = os.open(self._video_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            audio_fd = os.open(self._audio_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

            # --- Initialize chiaki session ---
            # Allocate a large buffer for the opaque ChiakiSession struct
            # (it's very large with many nested structs; 256KB is safe)
            session_buf = ctypes.create_string_buffer(256 * 1024)

            # Set up logging
            log = ChiakiLog()
            log_level = LOG_ALL if self.verbose else (LOG_INFO | LOG_WARNING | LOG_ERROR)
            self._lib.chiaki_log_init(
                ctypes.byref(log),
                ctypes.c_uint32(log_level),
                ctypes.cast(self._lib.chiaki_log_cb_print, ctypes.c_void_p),
                None,
            )

            # Set up connect info
            connect_info = ChiakiConnectInfo()
            ctypes.memset(ctypes.byref(connect_info), 0, ctypes.sizeof(connect_info))

            connect_info.ps5 = self.ps5
            connect_info.host = self.host.encode("utf-8")

            # regist_key: 16 bytes, null-padded
            rk = self.regist_key[:SESSION_AUTH_SIZE].ljust(SESSION_AUTH_SIZE, b'\x00')
            ctypes.memmove(connect_info.regist_key, rk, SESSION_AUTH_SIZE)

            # Verify regist_key was written correctly
            rk_in_struct = bytes(connect_info.regist_key)
            if rk_in_struct != rk:
                print(f"[!] WARNING: regist_key mismatch after write!", file=sys.stderr)
                print(f"    Expected: {rk.hex()}", file=sys.stderr)
                print(f"    Got:      {rk_in_struct.hex()}", file=sys.stderr)
            elif self.verbose:
                print(f"[debug] regist_key in struct: {rk_in_struct.hex()}")

            # morning: 16 bytes - this MUST be the rp_key from registration
            m = self.morning[:RPCRYPT_KEY_SIZE]
            ctypes.memmove(connect_info.morning, m, RPCRYPT_KEY_SIZE)

            # Video profile
            connect_info.video_profile.width = self.width
            connect_info.video_profile.height = self.height
            connect_info.video_profile.max_fps = self.fps
            connect_info.video_profile.bitrate = self.bitrate
            connect_info.video_profile.codec = self.codec if self.ps5 else CODEC_H264

            connect_info.video_profile_auto_downgrade = True
            connect_info.enable_keyboard = False
            connect_info.enable_dualsense = self.ps5
            connect_info.audio_video_disabled = NONE_DISABLED
            connect_info.auto_regist = False
            connect_info.holepunch_session = None
            connect_info.rudp_sock = None
            connect_info.packet_loss_max = 0.05
            connect_info.enable_idr_on_fec_failure = True

            # PSN Account ID
            if self.psn_account_id and len(self.psn_account_id) == PSN_ACCOUNT_ID_SIZE:
                ctypes.memmove(connect_info.psn_account_id, self.psn_account_id, PSN_ACCOUNT_ID_SIZE)

            # --- Initialize session ---
            print("[+] Initializing chiaki session...")
            err = self._lib.chiaki_session_init(
                ctypes.cast(session_buf, ctypes.c_void_p),
                ctypes.byref(connect_info),
                ctypes.byref(log),
            )
            if err != ERR_SUCCESS:
                err_str = self._lib.chiaki_error_string(err).decode()
                raise RuntimeError(f"chiaki_session_init failed: {err_str}")

            session_ptr = ctypes.cast(session_buf, ctypes.c_void_p)

            # --- Set callbacks ---
            event_cb = self._make_event_callback()
            self._lib.chiaki_session_set_event_cb(session_ptr, event_cb, None)

            video_cb = self._make_video_callback(video_fd)
            self._lib.chiaki_session_set_video_sample_cb(session_ptr, video_cb, None)

            # Audio sink struct: { void *user, header_cb, frame_cb }
            audio_header_cb, audio_frame_cb = self._make_audio_callbacks(audio_fd)
            # ChiakiAudioSink: 3 pointers
            audio_sink = (ctypes.c_void_p * 3)()
            audio_sink[0] = None  # user
            audio_sink[1] = ctypes.cast(audio_header_cb, ctypes.c_void_p)
            audio_sink[2] = ctypes.cast(audio_frame_cb, ctypes.c_void_p)
            self._lib.chiaki_session_set_audio_sink(session_ptr, ctypes.byref(audio_sink))

            # --- Start session ---
            print("[+] Starting session (connecting to console)...")
            err = self._lib.chiaki_session_start(session_ptr)
            if err != ERR_SUCCESS:
                err_str = self._lib.chiaki_error_string(err).decode()
                self._lib.chiaki_session_fini(session_ptr)
                raise RuntimeError(f"chiaki_session_start failed: {err_str}")

            print("[+] Session started. Recording... (Ctrl+C to stop)")

            # --- Wait for completion ---
            def signal_handler(sig, frame):
                print("\n[+] Stopping recording...")
                self._stop_event.set()

            old_handler = signal.signal(signal.SIGINT, signal_handler)

            try:
                if self.duration:
                    self._stop_event.wait(timeout=self.duration)
                else:
                    self._stop_event.wait()
            finally:
                signal.signal(signal.SIGINT, old_handler)

            # --- Stop session ---
            print("[+] Stopping session...")
            self._lib.chiaki_session_stop(session_ptr)
            self._lib.chiaki_session_join(session_ptr)
            self._lib.chiaki_session_fini(session_ptr)

            # Close temp files
            try:
                os.close(video_fd)
            except OSError:
                pass
            try:
                os.close(audio_fd)
            except OSError:
                pass

            elapsed = time.time() - (self._start_time or time.time())
            print(f"\n[+] Recording complete!")
            print(f"    Video frames: {self._video_frames}")
            print(f"    Audio frames: {self._audio_frames}")
            print(f"    Duration: {elapsed:.1f}s")

            # Mux raw streams into final output
            video_size = os.path.getsize(self._video_file) if os.path.exists(self._video_file) else 0
            audio_size = os.path.getsize(self._audio_file) if os.path.exists(self._audio_file) else 0
            print(f"    Raw video: {video_size / 1024 / 1024:.1f} MB")
            print(f"    Raw audio: {audio_size / 1024 / 1024:.1f} MB")

            if video_size == 0:
                print("[!] No video data recorded, skipping mux")
            else:
                self._mux_output()
                print(f"    Output: {self.output}")

        except Exception as e:
            print(f"\n[!] Error: {e}", file=sys.stderr)
            raise
        finally:
            self._cleanup()


# ---------------------------------------------------------------------------
# Real-time text detection on decoded video frames (OpenCV)
# ---------------------------------------------------------------------------

_cv2_available = False
try:
    import cv2
    import numpy as np
    _cv2_available = True
except ImportError:
    pass


class TextDetector:
    """
    Detects text (e.g. player names) on decoded video frames in real-time.

    Architecture:
      1. Receives raw H.264/H.265 NAL units via feed() (from the video callback)
      2. Pipes them to an FFmpeg subprocess that decodes to raw BGR24
      3. A background thread reads decoded frames and runs detection
      4. Only processes the latest available frame (skips if behind)
      5. Calls on_text_detected(texts, frame) callback with results

    Detection modes:
      - Template matching: load character images from a directory, fast (~1-2ms)
      - MSER + contour: no templates needed, detects text regions (~5-10ms)

    Usage:
        def on_text(texts, frame):
            for t in texts:
                print(f"Detected: {t['text']} at {t['bbox']} conf={t['confidence']:.2f}")

        detector = TextDetector(
            width=1920, height=1080, codec="h264",
            on_text_detected=on_text,
            rois=[(0.0, 0.0, 1.0, 0.15)],  # top 15% of screen
            skip_frames=5,  # process every 5th frame
        )
        detector.start()
        # ... in video callback: detector.feed(h264_data)
        detector.stop()
    """

    def __init__(self, width, height, codec="h264",
                 on_text_detected=None,
                 rois=None,
                 template_dir=None,
                 skip_frames=5,
                 min_confidence=0.7,
                 hw_decoder=None,
                 verbose=False):
        """
        Args:
            width, height: Video resolution.
            codec: "h264" or "hevc".
            on_text_detected: Callback(texts, frame). texts is a list of dicts
                with keys: text, bbox (x,y,w,h), confidence.
            rois: List of (x_frac, y_frac, w_frac, h_frac) normalized ROIs.
                  Default: full frame.
            template_dir: Path to directory with character template images
                (A.png, B.png, ..., 0.png, ..., 9.png). If None, uses MSER.
            skip_frames: Process every N-th frame (default 5 = ~12fps at 60fps input).
            min_confidence: Minimum confidence for template matching (0-1).
            hw_decoder: FFmpeg HW decoder name (e.g. "vaapi", "nvdec").
            verbose: Print debug info.
        """
        if not _cv2_available:
            raise RuntimeError(
                "OpenCV not available. Install with: pip install opencv-python\n"
                "  or: sudo dnf install python3-opencv"
            )

        self.width = width
        self.height = height
        self.codec = codec
        self.on_text_detected = on_text_detected
        self.rois = rois or [(0.0, 0.0, 1.0, 1.0)]
        self.template_dir = template_dir
        self.skip_frames = max(1, skip_frames)
        self.min_confidence = min_confidence
        self.hw_decoder = hw_decoder
        self.verbose = verbose

        self._ffmpeg_proc = None
        self._decoder_thread = None
        self._stop_event = threading.Event()
        self._frame_count = 0
        self._detect_count = 0
        self._pipe_lock = threading.Lock()

        # Pre-loaded character templates {char: grayscale_image}
        self._templates = {}
        if template_dir:
            self._load_templates(template_dir)

        # MSER detector (fallback when no templates)
        self._mser = None
        if not self._templates:
            self._mser = cv2.MSER_create()
            # Tune for game text: smaller areas, high contrast
            self._mser.setMinArea(30)
            self._mser.setMaxArea(2000)
            self._mser.setDelta(5)

    def _load_templates(self, template_dir):
        """Load character template images from a directory."""
        template_path = Path(template_dir)
        if not template_path.is_dir():
            print(f"[!] Template directory not found: {template_dir}", file=sys.stderr)
            return
        for img_file in sorted(template_path.glob("*.png")):
            char = img_file.stem  # e.g. "A" from "A.png"
            img = cv2.imread(str(img_file), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                self._templates[char] = img
        if self._templates:
            print(f"[+] TextDetector: loaded {len(self._templates)} character templates "
                  f"from {template_dir}", file=sys.stderr)

    def start(self):
        """Start the FFmpeg decoder subprocess and detection thread."""
        codec_name = "h264" if self.codec == "h264" else "hevc"

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        if self.hw_decoder:
            cmd += ["-hwaccel", self.hw_decoder]
        # Use generous probesize so FFmpeg can find SPS/PPS and start decoding.
        # The low_delay flags reduce buffering once decoding has started.
        cmd += ["-probesize", "5000000", "-analyzeduration", "2000000"]
        cmd += ["-f", codec_name, "-i", "pipe:0"]

        # Output raw BGR24 frames to stdout
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-an",
                "-vsync", "drop", "pipe:1"]

        print(f"  [ocr] FFmpeg cmd: {' '.join(cmd)}", file=sys.stderr)

        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # show FFmpeg errors/warnings on terminal
            bufsize=self.width * self.height * 3 * 2,  # buffer ~2 frames
        )

        self._stop_event.clear()
        self._decoder_thread = threading.Thread(
            target=self._detection_loop, daemon=True, name="TextDetector"
        )
        self._decoder_thread.start()
        print(f"[+] TextDetector started ({self.width}x{self.height}, "
              f"skip={self.skip_frames}, "
              f"mode={'template' if self._templates else 'MSER'})",
              file=sys.stderr)

    def stop(self):
        """Stop the detector and clean up."""
        self._stop_event.set()
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg_proc.terminate()
                self._ffmpeg_proc.wait(timeout=3)
            except Exception:
                self._ffmpeg_proc.kill()
            self._ffmpeg_proc = None
        if self._decoder_thread:
            self._decoder_thread.join(timeout=3)
            self._decoder_thread = None
        if self._detect_count > 0:
            print(f"[+] TextDetector stopped: processed {self._detect_count} frames",
                  file=sys.stderr)

    def feed(self, h264_data):
        """Feed raw H.264/H.265 data to the decoder. Non-blocking."""
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            with self._pipe_lock:
                try:
                    self._ffmpeg_proc.stdin.write(h264_data)
                    self._ffmpeg_proc.stdin.flush()
                    self._frame_count += 1
                    if self._frame_count == 1:
                        print(f"  [ocr] First NAL unit fed to decoder ({len(h264_data)} bytes)",
                              file=sys.stderr)
                    elif self._frame_count % 600 == 0:
                        print(f"  [ocr] Fed {self._frame_count} NAL units to decoder",
                              file=sys.stderr)
                except (BrokenPipeError, OSError) as e:
                    if self._frame_count <= 1:
                        print(f"  [ocr] Decoder pipe error: {e}", file=sys.stderr)

    def _detection_loop(self):
        """Background thread: read decoded frames and run text detection."""
        frame_size = self.width * self.height * 3  # BGR24
        stdout = self._ffmpeg_proc.stdout
        frame_idx = 0

        print(f"  [ocr] Waiting for first decoded frame ({frame_size} bytes = "
              f"{self.width}x{self.height} BGR24)...", file=sys.stderr)

        while not self._stop_event.is_set():
            # Read one full frame
            try:
                raw = stdout.read(frame_size)
            except Exception as e:
                print(f"  [ocr] Read error: {e}", file=sys.stderr)
                break
            if len(raw) != frame_size:
                if len(raw) > 0:
                    print(f"  [ocr] Partial frame: {len(raw)}/{frame_size} bytes", file=sys.stderr)
                else:
                    print(f"  [ocr] FFmpeg decoder EOF", file=sys.stderr)
                break  # EOF or error

            frame_idx += 1
            if frame_idx == 1:
                print(f"  [ocr] First frame decoded! Processing every {self.skip_frames}th frame.",
                      file=sys.stderr)

            # Skip frames for performance
            if frame_idx % self.skip_frames != 0:
                continue

            # Convert to numpy array
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                (self.height, self.width, 3)
            )

            # Run detection on each ROI
            all_texts = []
            for roi in self.rois:
                x = int(roi[0] * self.width)
                y = int(roi[1] * self.height)
                w = int(roi[2] * self.width)
                h = int(roi[3] * self.height)
                roi_img = frame[y:y+h, x:x+w]

                if self._templates:
                    texts = self._detect_template(roi_img, x, y)
                else:
                    texts = self._detect_mser(roi_img, x, y)
                all_texts.extend(texts)

            self._detect_count += 1

            if all_texts and self.on_text_detected:
                try:
                    self.on_text_detected(all_texts, frame)
                except Exception as e:
                    print(f"[!] TextDetector callback error: {e}", file=sys.stderr)

            if self.verbose and self._detect_count % 60 == 0:
                print(f"  [ocr] {self._detect_count} frames processed, "
                      f"last: {len(all_texts)} detections", file=sys.stderr)

    def _detect_template(self, roi_img, offset_x, offset_y):
        """Detect characters using template matching."""
        gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
        results = []

        for char, template in self._templates.items():
            th, tw = template.shape[:2]
            if th > gray.shape[0] or tw > gray.shape[1]:
                continue

            match = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            locations = np.where(match >= self.min_confidence)

            for pt_y, pt_x in zip(*locations):
                confidence = float(match[pt_y, pt_x])
                results.append({
                    "text": char,
                    "bbox": (offset_x + int(pt_x), offset_y + int(pt_y), tw, th),
                    "confidence": confidence,
                })

        # Group nearby detections into words (merge characters within ~5px vertically)
        if results:
            results = self._group_characters(results)

        return results

    def _detect_mser(self, roi_img, offset_x, offset_y):
        """Detect text regions using MSER (no templates needed)."""
        gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
        regions, _ = self._mser.detectRegions(gray)
        results = []

        for region in regions:
            x, y, w, h = cv2.boundingRect(region)
            # Filter by aspect ratio (text characters are typically taller than wide)
            aspect = h / max(w, 1)
            if 0.5 < aspect < 5.0 and w > 5 and h > 8:
                # Extract the region and compute a simple "text-ness" score
                char_img = gray[y:y+h, x:x+w]
                # High contrast regions are more likely to be text
                std_dev = float(np.std(char_img))
                if std_dev > 30:  # reasonable contrast
                    results.append({
                        "text": "?",  # MSER can't identify characters
                        "bbox": (offset_x + x, offset_y + y, w, h),
                        "confidence": min(1.0, std_dev / 100.0),
                    })

        # Merge overlapping detections
        if results:
            results = self._merge_overlapping(results)

        return results

    @staticmethod
    def _group_characters(detections):
        """Group individual character detections into words by proximity."""
        if not detections:
            return detections

        # Sort by x position
        detections.sort(key=lambda d: d["bbox"][0])

        words = []
        current_word = [detections[0]]

        for det in detections[1:]:
            prev = current_word[-1]
            prev_right = prev["bbox"][0] + prev["bbox"][2]
            curr_left = det["bbox"][0]
            # Check vertical alignment (within 5px) and horizontal proximity
            v_diff = abs(det["bbox"][1] - prev["bbox"][1])
            h_gap = curr_left - prev_right

            if v_diff < 5 and h_gap < det["bbox"][2] * 1.5:
                current_word.append(det)
            else:
                words.append(current_word)
                current_word = [det]
        words.append(current_word)

        # Build word results
        word_results = []
        for word_chars in words:
            text = "".join(d["text"] for d in word_chars)
            x_min = min(d["bbox"][0] for d in word_chars)
            y_min = min(d["bbox"][1] for d in word_chars)
            x_max = max(d["bbox"][0] + d["bbox"][2] for d in word_chars)
            y_max = max(d["bbox"][1] + d["bbox"][3] for d in word_chars)
            avg_conf = sum(d["confidence"] for d in word_chars) / len(word_chars)
            word_results.append({
                "text": text,
                "bbox": (x_min, y_min, x_max - x_min, y_max - y_min),
                "confidence": avg_conf,
            })
        return word_results

    @staticmethod
    def _merge_overlapping(detections):
        """Merge overlapping bounding boxes."""
        if len(detections) <= 1:
            return detections

        merged = []
        used = set()
        for i, d1 in enumerate(detections):
            if i in used:
                continue
            x1, y1, w1, h1 = d1["bbox"]
            group = [d1]
            for j, d2 in enumerate(detections[i+1:], i+1):
                if j in used:
                    continue
                x2, y2, w2, h2 = d2["bbox"]
                # Check overlap
                if (x1 < x2 + w2 and x1 + w1 > x2 and
                        y1 < y2 + h2 and y1 + h1 > y2):
                    group.append(d2)
                    used.add(j)
            used.add(i)
            # Merge bounding boxes
            x_min = min(d["bbox"][0] for d in group)
            y_min = min(d["bbox"][1] for d in group)
            x_max = max(d["bbox"][0] + d["bbox"][2] for d in group)
            y_max = max(d["bbox"][1] + d["bbox"][3] for d in group)
            best_conf = max(d["confidence"] for d in group)
            merged.append({
                "text": "?",
                "bbox": (x_min, y_min, x_max - x_min, y_max - y_min),
                "confidence": best_conf,
            })
        return merged


# ---------------------------------------------------------------------------
# Low-latency streaming output (pipe / FIFO / v4l2loopback)
# ---------------------------------------------------------------------------

class StreamOutput:
    """
    Streams a PlayStation Remote Play video stream with minimal latency.

    Output modes (from fastest to slowest):
      - stdout:  Raw H.264/H.265 NAL units to stdout (zero overhead)
      - fifo:    Raw H.264/H.265 to a named pipe (FIFO)
      - v4l2:    GPU-decoded YUV frames to /dev/videoN via v4l2loopback

    For stdout/fifo modes, the consumer application does the decoding,
    which avoids an extra decode+encode cycle and keeps latency minimal.
    """

    def __init__(self, host, regist_key, morning,
                 ps5=False, codec=CODEC_H264,
                 width=1920, height=1080, fps=60, bitrate=15000,
                 duration=None, psn_account_id=None, lib_path=None,
                 verbose=False,
                 # Output mode (exactly one should be set)
                 pipe_stdout=False, fifo_path=None, v4l2_device=None,
                 hw_decoder=None,
                 controller_device=None,
                 play=False,
                 # Text detection options
                 detect_text=False,
                 text_callback=None,
                 text_rois=None,
                 text_template_dir=None,
                 text_skip_frames=5,
                 text_min_confidence=0.7):
        self.host = host
        self.regist_key = regist_key
        self.morning = morning
        self.ps5 = ps5
        self.codec = codec
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.duration = duration
        self.psn_account_id = psn_account_id or bytes(PSN_ACCOUNT_ID_SIZE)
        self.verbose = verbose
        self.pipe_stdout = pipe_stdout
        self.fifo_path = fifo_path
        self.v4l2_device = v4l2_device
        self.hw_decoder = hw_decoder  # "vaapi", "nvdec", "vdpau", etc.
        self.controller_device = controller_device  # evdev path or "auto"
        self.play = play  # launch ffplay on v4l2 device after writer opens

        # Text detection
        self.detect_text = detect_text
        self.text_callback = text_callback
        self.text_rois = text_rois
        self.text_template_dir = text_template_dir
        self.text_skip_frames = text_skip_frames
        self.text_min_confidence = text_min_confidence
        self._text_detector = None

        self._lib = load_libchiaki(lib_path)
        self._stop_event = threading.Event()
        self._video_frames = 0
        self._audio_frames = 0
        self._start_time = None

        # Callback references (prevent GC)
        self._event_cb_ref = None
        self._video_cb_ref = None
        self._audio_header_cb_ref = None
        self._audio_frame_cb_ref = None

        # Output file descriptors
        self._video_fd = None
        self._ffmpeg_proc = None
        self._ffplay_proc = None
        self._v4l2_fd = None
        self._controller_input = None

    def _open_output(self):
        """Open the output destination based on mode."""
        if self.pipe_stdout:
            # Write raw H.264/H.265 directly to stdout (fd 1)
            self._video_fd = 1  # stdout
            # Redirect status messages to stderr so they don't mix with stream
            return

        if self.fifo_path:
            # Create FIFO if it doesn't exist
            if not os.path.exists(self.fifo_path):
                os.mkfifo(self.fifo_path)
                print(f"[+] Created FIFO: {self.fifo_path}", file=sys.stderr)
            elif not os.path.isfifo(self.fifo_path):
                raise RuntimeError(f"{self.fifo_path} exists but is not a FIFO")
            # Launch ffplay BEFORE opening the FIFO for writing, because
            # os.open(O_WRONLY) blocks until a reader connects.
            if self.play:
                self._launch_ffplay_on_fifo()
            print(f"[+] Opening FIFO {self.fifo_path} (waiting for reader...)", file=sys.stderr)
            self._video_fd = os.open(self.fifo_path, os.O_WRONLY)
            print(f"[+] FIFO reader connected", file=sys.stderr)
            return

        if self.v4l2_device:
            self._open_v4l2_output()
            return

    def _open_v4l2_output(self):
        """Open v4l2loopback device via FFmpeg decode+write pipeline."""
        # Validate v4l2 device exists before launching FFmpeg
        if not os.path.exists(self.v4l2_device):
            # Try to find available v4l2loopback devices
            import glob as glob_mod
            avail = glob_mod.glob("/dev/video*")
            hint = f" (available: {', '.join(sorted(avail))})" if avail else ""
            raise RuntimeError(
                f"V4L2 device {self.v4l2_device} does not exist{hint}.\n"
                f"  Load v4l2loopback first: sudo modprobe v4l2loopback video_nr=9"
            )

        codec_name = "h264" if self.codec == CODEC_H264 else "hevc"
        pix_fmt = "yuv420p"

        # Build FFmpeg command: decode H.264 → write raw YUV to v4l2 device
        # Low-latency flags: no buffering, real-time output, minimal probing
        base_flags = [
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-probesize", "32",
            "-analyzeduration", "0",
        ]

        cmd = ["ffmpeg", "-y"] + base_flags + [
               "-f", codec_name,
               "-i", "pipe:0"]

        # GPU-accelerated decoding
        if self.hw_decoder:
            if self.hw_decoder == "vaapi":
                # Auto-detect VAAPI render node
                render_node = "/dev/dri/renderD128"
                for candidate in ["/dev/dri/renderD128", "/dev/dri/renderD129"]:
                    if os.path.exists(candidate):
                        render_node = candidate
                        break
                cmd = ["ffmpeg", "-y"] + base_flags + [
                       "-hwaccel", "vaapi",
                       "-hwaccel_output_format", "vaapi",
                       "-vaapi_device", render_node,
                       "-f", codec_name, "-i", "pipe:0",
                       "-vf", "scale_vaapi=format=nv12,hwdownload,format=nv12,format=yuv420p"]
                pix_fmt = "yuv420p"
            elif self.hw_decoder == "nvdec":
                cmd = ["ffmpeg", "-y"] + base_flags + [
                       "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                       "-f", codec_name, "-i", "pipe:0",
                       "-vf", "hwdownload,format=nv12"]
                pix_fmt = "nv12"
            elif self.hw_decoder == "vdpau":
                cmd = ["ffmpeg", "-y"] + base_flags + [
                       "-hwaccel", "vdpau",
                       "-f", codec_name, "-i", "pipe:0",
                       "-vf", "format=yuv420p"]
            else:
                cmd = ["ffmpeg", "-y"] + base_flags + [
                       "-hwaccel", self.hw_decoder,
                       "-f", codec_name, "-i", "pipe:0",
                       "-vf", "format=yuv420p"]
        else:
            # Software decode: explicit format filter so FFmpeg can negotiate
            # with v4l2loopback (which may not support the decoder's native fmt)
            cmd.extend(["-vf", "format=yuv420p"])

        cmd.extend([
            "-f", "v4l2",
            "-pix_fmt", pix_fmt,
            self.v4l2_device,
        ])

        print(f"[+] v4l2 pipeline: {' '.join(cmd)}", file=sys.stderr)
        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None if self.verbose else subprocess.DEVNULL,
        )
        self._video_fd = self._ffmpeg_proc.stdin.fileno()

    def _launch_ffplay_on_fifo(self):
        """Launch ffplay reading raw H.264/H.265 from the FIFO."""
        codec_name = "h264" if self.codec == CODEC_H264 else "hevc"
        ffplay_cmd = [
            "ffplay",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-framedrop",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-vf", "setpts=0",
            "-f", codec_name,
            "-i", self.fifo_path,
        ]
        print(f"[+] ffplay: {' '.join(ffplay_cmd)}", file=sys.stderr)
        self._ffplay_proc = subprocess.Popen(
            ffplay_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Give ffplay a moment to start, then check it didn't crash
        time.sleep(0.3)
        ret = self._ffplay_proc.poll()
        if ret is not None:
            stderr_out = self._ffplay_proc.stderr.read().decode(errors="replace")
            self._ffplay_proc = None
            raise RuntimeError(f"ffplay exited immediately (code {ret}):\n{stderr_out}")

    def _close_output(self):
        """Close the output destination."""
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            self._ffmpeg_proc.wait(timeout=5)
            self._ffmpeg_proc = None
        elif self._video_fd is not None and self._video_fd > 2:
            try:
                os.close(self._video_fd)
            except OSError:
                pass
        # Clean up FIFO
        if self.fifo_path and os.path.exists(self.fifo_path):
            try:
                os.unlink(self.fifo_path)
            except OSError:
                pass
        self._video_fd = None

    def _log(self, msg):
        """Print to stderr to avoid mixing with stream data on stdout."""
        print(msg, file=sys.stderr)

    def _make_event_callback(self):
        """Create the C callback for session events."""
        CALLBACK_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

        def event_cb(event_ptr, user_ptr):
            if not event_ptr:
                return
            event_type = ctypes.cast(event_ptr, ctypes.POINTER(ctypes.c_int))[0]
            if event_type == EVENT_CONNECTED:
                self._log("[+] Connected to PlayStation!")
                self._start_time = time.time()
            elif event_type == EVENT_QUIT:
                quit_event_offset = 8
                quit_reason = ctypes.cast(
                    ctypes.c_void_p(event_ptr + quit_event_offset),
                    ctypes.POINTER(ctypes.c_int)
                )[0]
                reason_str = "unknown"
                try:
                    reason_str = self._lib.chiaki_quit_reason_string(quit_reason).decode()
                except Exception:
                    pass
                self._log(f"[+] Session ended: {reason_str}")
                self._stop_event.set()

        self._event_cb_ref = CALLBACK_TYPE(event_cb)
        return self._event_cb_ref

    def _make_video_callback(self):
        """Create zero-copy video callback that writes directly to output fd."""
        CALLBACK_TYPE = ctypes.CFUNCTYPE(
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.c_int32, ctypes.c_bool, ctypes.c_void_p,
        )

        video_fd = self._video_fd
        # Use self reference (not closure capture) so detector is found
        # even if it's created after this callback is built
        stream_self = self

        def video_cb(buf, buf_size, frames_lost, frame_recovered, user):
            try:
                data = ctypes.string_at(buf, buf_size)
                os.write(video_fd, data)
                # Feed to text detector (non-blocking)
                td = stream_self._text_detector
                if td:
                    td.feed(data)
                self._video_frames += 1
                if self._video_frames % 600 == 0:
                    elapsed = time.time() - (self._start_time or time.time())
                    self._log(f"  [stream] {self._video_frames} frames ({elapsed:.1f}s)")
                return True
            except BrokenPipeError:
                self._log("[+] Pipe closed by reader, stopping...")
                self._stop_event.set()
                return False
            except Exception as e:
                self._log(f"[!] Video callback error: {e}")
                return False

        self._video_cb_ref = CALLBACK_TYPE(video_cb)
        return self._video_cb_ref

    def _make_audio_noop_callbacks(self):
        """Create no-op audio callbacks (audio not used in stream mode)."""
        HEADER_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
        FRAME_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_void_p)

        def noop_header(header_ptr, user):
            pass

        def noop_frame(buf, buf_size, user):
            self._audio_frames += 1

        self._audio_header_cb_ref = HEADER_CB_TYPE(noop_header)
        self._audio_frame_cb_ref = FRAME_CB_TYPE(noop_frame)
        return self._audio_header_cb_ref, self._audio_frame_cb_ref

    def stream(self):
        """
        Main streaming function. Connects to PS and pipes video to output.
        """
        log = self._log
        mode = "stdout" if self.pipe_stdout else ("FIFO" if self.fifo_path else "v4l2")
        target = self.fifo_path or self.v4l2_device or "stdout"
        log(f"[+] PS Stream → {mode}: {target}")
        log(f"    {self.width}x{self.height}@{self.fps} {'H.265' if self.codec != CODEC_H264 else 'H.264'}")
        if self.hw_decoder:
            log(f"    GPU decode: {self.hw_decoder}")
        if self.duration:
            log(f"    Duration: {self.duration}s")

        # Start text detector if enabled
        if self.detect_text:
            codec_str = "h264" if self.codec == CODEC_H264 else "hevc"
            callback = self.text_callback or self._default_text_callback
            self._text_detector = TextDetector(
                width=self.width, height=self.height, codec=codec_str,
                on_text_detected=callback,
                rois=self.text_rois,
                template_dir=self.text_template_dir,
                skip_frames=self.text_skip_frames,
                min_confidence=self.text_min_confidence,
                hw_decoder=self.hw_decoder,
                verbose=self.verbose,
            )
            self._text_detector.start()

        self._open_output()

        # For v4l2 + play: launch ffplay AFTER the writer has opened the device
        if self.play and self.v4l2_device:
            time.sleep(0.5)
            ffplay_cmd = [
                "ffplay",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-framedrop",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-vf", "setpts=0",
                self.v4l2_device,
            ]
            log(f"[+] ffplay: {' '.join(ffplay_cmd)}")
            self._ffplay_proc = subprocess.Popen(
                ffplay_cmd,
                stdout=subprocess.DEVNULL,
                stderr=None if self.verbose else subprocess.DEVNULL,
            )
        # For FIFO + play: ffplay was already launched in _open_output()

        try:
            session_buf = ctypes.create_string_buffer(256 * 1024)

            chiaki_log = ChiakiLog()
            log_level = LOG_ALL if self.verbose else (LOG_WARNING | LOG_ERROR)
            self._lib.chiaki_log_init(
                ctypes.byref(chiaki_log),
                ctypes.c_uint32(log_level),
                ctypes.cast(self._lib.chiaki_log_cb_print, ctypes.c_void_p),
                None,
            )

            connect_info = ChiakiConnectInfo()
            ctypes.memset(ctypes.byref(connect_info), 0, ctypes.sizeof(connect_info))
            connect_info.ps5 = self.ps5
            connect_info.host = self.host.encode("utf-8")

            rk = self.regist_key[:SESSION_AUTH_SIZE].ljust(SESSION_AUTH_SIZE, b'\x00')
            ctypes.memmove(connect_info.regist_key, rk, SESSION_AUTH_SIZE)

            m = self.morning[:RPCRYPT_KEY_SIZE]
            ctypes.memmove(connect_info.morning, m, RPCRYPT_KEY_SIZE)

            connect_info.video_profile.width = self.width
            connect_info.video_profile.height = self.height
            connect_info.video_profile.max_fps = self.fps
            connect_info.video_profile.bitrate = self.bitrate
            connect_info.video_profile.codec = self.codec if self.ps5 else CODEC_H264
            connect_info.video_profile_auto_downgrade = True
            connect_info.enable_dualsense = self.ps5
            connect_info.audio_video_disabled = NONE_DISABLED
            connect_info.packet_loss_max = 0.05
            connect_info.enable_idr_on_fec_failure = True

            if self.psn_account_id and len(self.psn_account_id) == PSN_ACCOUNT_ID_SIZE:
                ctypes.memmove(connect_info.psn_account_id, self.psn_account_id, PSN_ACCOUNT_ID_SIZE)

            err = self._lib.chiaki_session_init(
                ctypes.cast(session_buf, ctypes.c_void_p),
                ctypes.byref(connect_info),
                ctypes.byref(chiaki_log),
            )
            if err != ERR_SUCCESS:
                raise RuntimeError(f"chiaki_session_init failed: {self._lib.chiaki_error_string(err).decode()}")

            session_ptr = ctypes.cast(session_buf, ctypes.c_void_p)

            event_cb = self._make_event_callback()
            self._lib.chiaki_session_set_event_cb(session_ptr, event_cb, None)

            video_cb = self._make_video_callback()
            self._lib.chiaki_session_set_video_sample_cb(session_ptr, video_cb, None)

            audio_header_cb, audio_frame_cb = self._make_audio_noop_callbacks()
            audio_sink = (ctypes.c_void_p * 3)()
            audio_sink[0] = None
            audio_sink[1] = ctypes.cast(audio_header_cb, ctypes.c_void_p)
            audio_sink[2] = ctypes.cast(audio_frame_cb, ctypes.c_void_p)
            self._lib.chiaki_session_set_audio_sink(session_ptr, ctypes.byref(audio_sink))

            err = self._lib.chiaki_session_start(session_ptr)
            if err != ERR_SUCCESS:
                self._lib.chiaki_session_fini(session_ptr)
                raise RuntimeError(f"chiaki_session_start failed: {self._lib.chiaki_error_string(err).decode()}")

            # Start controller input if requested
            if self.controller_device:
                dev_path = None if self.controller_device == "auto" else self.controller_device
                self._controller_input = ControllerInputThread(
                    self._lib, session_ptr, device_path=dev_path, log_fn=log
                )
                self._controller_input.start()

            log("[+] Streaming... (Ctrl+C to stop)")

            def signal_handler(sig, frame):
                log("\n[+] Stopping stream...")
                self._stop_event.set()

            old_handler = signal.signal(signal.SIGINT, signal_handler)
            try:
                if self.duration:
                    self._stop_event.wait(timeout=self.duration)
                else:
                    self._stop_event.wait()
            finally:
                signal.signal(signal.SIGINT, old_handler)

            if self._controller_input:
                self._controller_input.stop()

            self._lib.chiaki_session_stop(session_ptr)
            self._lib.chiaki_session_join(session_ptr)
            self._lib.chiaki_session_fini(session_ptr)

            elapsed = time.time() - (self._start_time or time.time())
            log(f"[+] Done: {self._video_frames} frames in {elapsed:.1f}s")

        finally:
            if self._text_detector:
                self._text_detector.stop()
                self._text_detector = None
            if self._ffplay_proc:
                self._ffplay_proc.terminate()
                try:
                    if self._ffplay_proc.stderr:
                        self._ffplay_proc.stderr.close()
                    self._ffplay_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._ffplay_proc.kill()
                self._ffplay_proc = None
            self._close_output()

    @staticmethod
    def _default_text_callback(texts, frame):
        """Default callback: print detected text to stderr."""
        for t in texts:
            x, y, w, h = t["bbox"]
            print(f"  [text] \"{t['text']}\" at ({x},{y} {w}x{h}) "
                  f"conf={t['confidence']:.2f}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Alternative: Record using chiaki-ng subprocess (simpler approach)
# ---------------------------------------------------------------------------

def record_with_chiaki_cli(host, regist_key, morning, output, duration=None,
                            ps5=False, resolution="1080p", fps=60):
    """
    Alternative recording approach: launch chiaki-ng GUI/CLI with
    environment settings and capture X11/Wayland output with FFmpeg.

    This is a fallback if ctypes binding doesn't work.
    """
    print("[+] Fallback: Recording via screen capture")
    print("    This method uses FFmpeg to capture the chiaki window.")
    print()

    # Determine display server
    wayland = os.environ.get("WAYLAND_DISPLAY")
    x11_display = os.environ.get("DISPLAY", ":0")

    if wayland:
        # PipeWire capture for Wayland
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "pipewire",
            "-framerate", str(fps),
            "-i", "default",
        ]
    else:
        # X11 capture
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-video_size", f"1920x1080",
            "-framerate", str(fps),
            "-f", "x11grab",
            "-i", x11_display,
        ]

    ffmpeg_cmd.extend([
        "-f", "pulse", "-i", "default",  # audio from PulseAudio
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
    ])

    if duration:
        ffmpeg_cmd.extend(["-t", str(duration)])

    ffmpeg_cmd.append(output)

    print(f"[+] FFmpeg command: {' '.join(ffmpeg_cmd)}")
    print("[!] Start chiaki-ng manually and connect to your PlayStation,")
    print("    then FFmpeg will capture the screen.")

    subprocess.run(ffmpeg_cmd)


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_console(host, psn_account_id_b64, pin, ps5=False, lib_path=None):
    """
    Register this device with a PlayStation console.

    This is a one-time operation. After registration you receive:
    - rp_regist_key: needed for future connections
    - rp_key: the "morning" encryption key needed for session crypto

    NOTE: Due to the complexity of the registration protocol (AES encryption,
    custom key derivation), this function requires libchiaki.so.
    """
    lib = load_libchiaki(lib_path)

    print(f"[+] Registration")
    print(f"    Host: {host}")
    print(f"    PIN: {pin}")
    print(f"    PSN Account ID: {psn_account_id_b64}")
    print()

    # Decode PSN account ID
    try:
        psn_account_id = base64.b64decode(psn_account_id_b64)
        if len(psn_account_id) != PSN_ACCOUNT_ID_SIZE:
            print(f"[!] PSN Account ID must be {PSN_ACCOUNT_ID_SIZE} bytes (got {len(psn_account_id)})")
            return None
    except Exception as e:
        print(f"[!] Invalid PSN Account ID base64: {e}")
        return None

    # Set up logging
    log = ChiakiLog()
    lib.chiaki_log_init(
        ctypes.byref(log),
        ctypes.c_uint32(LOG_ALL),
        ctypes.cast(lib.chiaki_log_cb_print, ctypes.c_void_p),
        None,
    )

    # Registration uses a complex state machine internally
    # We need ChiakiRegistInfo and ChiakiRegist structs
    # Since the structs are complex, allocate generous buffers

    target = TARGET_PS5_1 if ps5 else TARGET_PS4_10

    # ChiakiRegistInfo struct layout (approximate):
    #   int target;           // 4 bytes
    #   pad 4;
    #   char *host;           // 8 bytes
    #   bool broadcast;       // 1 byte
    #   pad 7;
    #   char *psn_online_id;  // 8 bytes
    #   uint8_t psn_account_id[8]; // 8 bytes
    #   uint32_t pin;         // 4 bytes
    #   uint32_t console_pin; // 4 bytes
    #   void *holepunch_info; // 8 bytes
    #   void *rudp;           // 8 bytes (pointer-sized, this is a pointer typedef)

    regist_info_buf = ctypes.create_string_buffer(256)
    ctypes.memset(regist_info_buf, 0, 256)

    offset = 0
    # target (int32)
    struct.pack_into("<i", regist_info_buf, offset, target)
    offset += 4
    offset += 4  # padding

    # host (char*)
    host_bytes = host.encode("utf-8") + b'\x00'
    host_buf = ctypes.create_string_buffer(host_bytes)
    host_ptr = ctypes.cast(host_buf, ctypes.c_void_p).value
    struct.pack_into("<Q", regist_info_buf, offset, host_ptr)
    offset += 8

    # broadcast (bool) - False for direct host IP, True only for broadcast addresses
    struct.pack_into("<B", regist_info_buf, offset, 0)  # broadcast = false for direct host
    offset += 1
    offset += 7  # padding

    # psn_online_id (char*) - NULL, use account_id instead
    struct.pack_into("<Q", regist_info_buf, offset, 0)
    offset += 8

    # psn_account_id[8]
    regist_info_buf[offset:offset + 8] = psn_account_id[:8]
    offset += 8

    # pin (uint32)
    struct.pack_into("<I", regist_info_buf, offset, int(pin))
    offset += 4

    # console_pin (uint32) - 0
    struct.pack_into("<I", regist_info_buf, offset, 0)
    offset += 4

    # holepunch_info (pointer) - NULL
    struct.pack_into("<Q", regist_info_buf, offset, 0)
    offset += 8

    # rudp (pointer-sized) - NULL
    struct.pack_into("<Q", regist_info_buf, offset, 0)
    offset += 8

    # Registration result storage
    result = {"success": False, "regist_key": None, "rp_key": None}
    result_event = threading.Event()

    # Callback for registration completion
    REGIST_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

    def regist_cb(event_ptr, user_ptr):
        if not event_ptr:
            return
        # ChiakiRegistEvent: { int type; ChiakiRegisteredHost *host; }
        event_type = ctypes.cast(event_ptr, ctypes.POINTER(ctypes.c_int))[0]

        if event_type == 2:  # CHIAKI_REGIST_EVENT_TYPE_FINISHED_SUCCESS
            print("[+] Registration successful!")
            # Parse the registered host data
            host_ptr_val = ctypes.cast(
                ctypes.c_void_p(event_ptr + 8),
                ctypes.POINTER(ctypes.c_void_p)
            )[0]
            if host_ptr_val:
                # ChiakiRegisteredHost layout:
                #   int target;        // +0
                #   char ap_ssid[48];  // +4
                #   char ap_bssid[32]; // +52
                #   char ap_key[80];   // +84
                #   char ap_name[32];  // +164
                #   uint8_t mac[6];    // +196
                #   char nickname[32]; // +202 (but likely aligned to +204 or similar)
                #   char regist_key[16]; // +234
                #   uint32_t rp_key_type; // +250
                #   uint8_t rp_key[16]; // +254

                # Use a simpler approach: read the raw bytes
                host_data = ctypes.string_at(host_ptr_val, 512)

                # Extract regist_key and rp_key by scanning for them
                # The offsets depend on struct packing
                # target (4) + ap_ssid(48) + ap_bssid(32) + ap_key(80) + ap_name(32) + mac(6) = 202
                # nickname(32) = offset 234
                # regist_key(16) = offset 266 (but alignment may shift this)
                # Let's use a more robust approach - read the target first
                rh_target = struct.unpack_from("<i", host_data, 0)[0]
                # ap_ssid starts at 4, 0x30=48 bytes
                # ap_bssid at 52, 0x20=32 bytes
                # ap_key at 84, 0x50=80 bytes
                # ap_name at 164, 0x20=32 bytes
                # server_mac at 196, 6 bytes
                # pad to align: 202 -> 204 maybe? No, char arrays don't need alignment
                # server_nickname at 202, 0x20=32 bytes
                nickname = host_data[202:234].split(b'\x00')[0].decode("utf-8", errors="replace")
                # rp_regist_key at 234, 16 bytes
                regist_key = host_data[234:250]
                # rp_key_type at 250, uint32
                rp_key_type = struct.unpack_from("<I", host_data, 250)[0]
                # rp_key at 254, 16 bytes
                rp_key = host_data[254:270]

                result["success"] = True
                result["nickname"] = nickname
                result["regist_key"] = regist_key
                result["rp_key_type"] = rp_key_type
                result["rp_key"] = rp_key

                print(f"    Console nickname: {nickname}")
                print(f"    Regist key (hex): {regist_key.hex()}")
                print(f"    RP key (hex): {rp_key.hex()}")
                print(f"    RP key type: {rp_key_type}")
        elif event_type == 1:  # FAILED
            print("[!] Registration FAILED")
        elif event_type == 0:  # CANCELED
            print("[!] Registration canceled")

        result_event.set()

    cb_ref = REGIST_CB_TYPE(regist_cb)

    # Allocate ChiakiRegist struct (generous buffer)
    regist_buf = ctypes.create_string_buffer(4096)

    print("[+] Starting registration (make sure PIN is entered on console)...")
    err = lib.chiaki_regist_start(
        ctypes.cast(regist_buf, ctypes.c_void_p),
        ctypes.byref(log),
        ctypes.cast(regist_info_buf, ctypes.c_void_p),
        cb_ref,
        None,
    )

    if err != ERR_SUCCESS:
        err_str = lib.chiaki_error_string(err).decode()
        print(f"[!] Registration start failed: {err_str}")
        return None

    # Wait for registration to complete
    if not result_event.wait(timeout=30):
        print("[!] Registration timed out")
        lib.chiaki_regist_stop(ctypes.cast(regist_buf, ctypes.c_void_p))

    lib.chiaki_regist_fini(ctypes.cast(regist_buf, ctypes.c_void_p))

    if result["success"]:
        return result
    return None


# ---------------------------------------------------------------------------
# Config file management
# ---------------------------------------------------------------------------

def save_config(path, host, regist_key, morning, ps5=False,
                psn_account_id=None, nickname=None):
    """Save connection parameters to a JSON config file."""
    config = {
        "host": host,
        "ps5": ps5,
        "regist_key": regist_key.hex() if isinstance(regist_key, bytes) else regist_key,
        "morning": morning.hex() if isinstance(morning, bytes) else morning,
    }
    if psn_account_id:
        if isinstance(psn_account_id, bytes):
            config["psn_account_id"] = base64.b64encode(psn_account_id).decode()
        else:
            config["psn_account_id"] = psn_account_id
    if nickname:
        config["nickname"] = nickname

    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"[+] Config saved to {path}")


def load_config(path):
    """Load connection parameters from a JSON config file."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="PlayStation Stream Recorder (chiaki-ng Python interface)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Discover consoles on the network
  %(prog)s discover --host 192.168.1.255

  # Register with a console (one-time setup)
  %(prog)s register --host 192.168.1.100 --psn-account-id <base64> --pin 12345678

  # Record a stream
  %(prog)s record --host 192.168.1.100 \\
      --regist-key <hex> --morning <hex> \\
      --output recording.mp4 --duration 60

  # Record using a saved config
  %(prog)s record --config ps_config.json --output recording.mp4

  # Stream raw H.264 to stdout (low-latency playback with ffplay)
  %(prog)s stream --config ps_config.json | \\
      ffplay -fflags nobuffer -flags low_delay -framedrop \\
             -probesize 32 -analyzeduration 0 -f h264 -

  # Stream to stdout (low-latency playback with mpv)
  %(prog)s stream --config ps_config.json | \\
      mpv --no-cache --untimed --no-demuxer-thread --profile=low-latency -

  # Stream to a named pipe (FIFO)
  %(prog)s stream --config ps_config.json --fifo /tmp/ps_video

  # Stream to v4l2loopback virtual camera (with GPU decode)
  sudo modprobe v4l2loopback video_nr=9
  %(prog)s stream --config ps_config.json --v4l2 /dev/video9 --hw-decoder vaapi

  # Stream with real-time text detection (MSER auto-detect, top 15%% of screen)
  %(prog)s stream --config ps_config.json --play \\
      --detect-text --text-roi 0.0,0.0,1.0,0.15 --text-skip 10

  # Stream with template matching (provide character images)
  %(prog)s stream --config ps_config.json --play \\
      --detect-text --text-templates ./nhl_font/ --text-confidence 0.8

  # Wake up a console from standby
  %(prog)s wakeup --host 192.168.1.100 --regist-key <hex> --ps5
""",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # --- discover ---
    discover_parser = subparsers.add_parser("discover", help="Discover PlayStation consoles on the network")
    discover_parser.add_argument("--host", required=True, help="Target IP or broadcast address (e.g. 192.168.1.255)")
    discover_parser.add_argument("--timeout", type=float, default=3.0, help="Discovery timeout in seconds (default: 3)")

    # --- wakeup ---
    wakeup_parser = subparsers.add_parser("wakeup", help="Wake up a console from standby")
    wakeup_parser.add_argument("--host", required=True, help="Console IP address")
    wakeup_parser.add_argument("--regist-key", required=True, help="Registration key (hex)")
    wakeup_parser.add_argument("--ps5", action="store_true", help="Target is PS5 (default: PS4)")

    # --- register ---
    register_parser = subparsers.add_parser("register", help="Register this device with a PlayStation (one-time)")
    register_parser.add_argument("--host", required=True, help="Console IP address")
    register_parser.add_argument("--psn-account-id", required=True, help="PSN Account ID (base64)")
    register_parser.add_argument("--pin", required=True, help="PIN from console (Settings > Remote Play > Link Device)")
    register_parser.add_argument("--ps5", action="store_true", help="Target is PS5 (default: PS4)")
    register_parser.add_argument("--lib-path", help="Path to libchiaki.so")
    register_parser.add_argument("--save-config", help="Save registration result to config file")

    # --- record ---
    record_parser = subparsers.add_parser("record", help="Connect and record the PlayStation stream")
    record_parser.add_argument("--host", help="Console IP address")
    record_parser.add_argument("--regist-key", help="Registration key (hex, 32 chars)")
    record_parser.add_argument("--morning", help="Morning/RP-key (hex, 32 chars). Must be the rp_key from registration.")
    record_parser.add_argument("--config", help="Load settings from JSON config file")
    record_parser.add_argument("--output", "-o", default="recording.mp4", help="Output file (default: recording.mp4)")
    record_parser.add_argument("--duration", "-d", type=int, help="Recording duration in seconds (default: unlimited)")
    record_parser.add_argument("--ps5", action="store_true", help="Target is PS5 (default: PS4)")
    record_parser.add_argument("--codec", choices=["h264", "h265", "h265-hdr"], default="h264", help="Video codec")
    record_parser.add_argument("--resolution", choices=["360p", "540p", "720p", "1080p"], default="1080p", help="Resolution")
    record_parser.add_argument("--fps", type=int, choices=[30, 60], default=60, help="Frames per second")
    record_parser.add_argument("--bitrate", type=int, default=15000, help="Video bitrate in kbps")
    record_parser.add_argument("--psn-account-id", help="PSN Account ID (base64)")
    record_parser.add_argument("--lib-path", help="Path to libchiaki.so")
    record_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    record_parser.add_argument("--fallback", action="store_true", help="Use screen capture fallback instead of direct")

    # --- stream ---
    stream_parser = subparsers.add_parser("stream", help="Stream raw video to stdout, FIFO, or v4l2 device")
    stream_parser.add_argument("--host", help="Console IP address")
    stream_parser.add_argument("--regist-key", help="Registration key (hex)")
    stream_parser.add_argument("--morning", help="Morning/RP-key (hex)")
    stream_parser.add_argument("--config", help="Load settings from JSON config file")
    stream_parser.add_argument("--duration", "-d", type=int, help="Duration in seconds")
    stream_parser.add_argument("--ps5", action="store_true", help="Target is PS5")
    stream_parser.add_argument("--codec", choices=["h264", "h265", "h265-hdr"], default="h264", help="Video codec")
    stream_parser.add_argument("--resolution", choices=["360p", "540p", "720p", "1080p"], default="1080p")
    stream_parser.add_argument("--fps", type=int, choices=[30, 60], default=60)
    stream_parser.add_argument("--bitrate", type=int, default=15000, help="Video bitrate in kbps")
    stream_parser.add_argument("--psn-account-id", help="PSN Account ID (base64)")
    stream_parser.add_argument("--lib-path", help="Path to libchiaki.so")
    stream_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    # Output mode (mutually exclusive)
    stream_output = stream_parser.add_mutually_exclusive_group()
    stream_output.add_argument("--fifo", metavar="PATH", help="Write raw stream to a named pipe (FIFO)")
    stream_output.add_argument("--v4l2", metavar="DEVICE", help="Write decoded frames to v4l2loopback (e.g. /dev/video9)")
    # GPU decoder for v4l2 mode
    stream_parser.add_argument("--hw-decoder", choices=["vaapi", "nvdec", "vdpau", "vulkan"],
                               help="GPU decoder for v4l2 mode (default: software)")
    stream_parser.add_argument("--controller", nargs="?", const="auto", default=None,
                               metavar="DEVICE",
                               help="Enable controller input (auto-detect or specify /dev/input/eventX)")
    stream_parser.add_argument("--play", action="store_true",
                               help="Launch ffplay with low-latency flags (auto-creates FIFO, or uses --v4l2/--fifo)")
    # Text detection options
    stream_parser.add_argument("--detect-text", action="store_true",
                               help="Enable real-time text detection on video frames (requires opencv-python)")
    stream_parser.add_argument("--text-templates", metavar="DIR",
                               help="Directory with character template images (A.png, B.png, ...) for template matching")
    stream_parser.add_argument("--text-roi", metavar="X,Y,W,H", action="append",
                               help="ROI for text detection as normalized fractions (e.g. 0.0,0.0,1.0,0.15 = top 15%%). "
                                    "Can be specified multiple times. Default: full frame.")
    stream_parser.add_argument("--text-skip", type=int, default=5,
                               help="Process every N-th frame for text detection (default: 5)")
    stream_parser.add_argument("--text-confidence", type=float, default=0.7,
                               help="Minimum confidence for text detection (0-1, default: 0.7)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # === DISCOVER ===
    if args.command == "discover":
        print(f"[+] Discovering PlayStation consoles at {args.host}...")
        results = discover_consoles(args.host, timeout=args.timeout)

        if not results:
            print("[!] No consoles found. Make sure the console is on the same network.")
            sys.exit(1)

        for i, host in enumerate(results):
            print(f"\n--- Console #{i + 1} ---")
            print(f"  Address:        {host.get('host_addr', 'unknown')}")
            print(f"  State:          {host.get('state', 'unknown')}")
            print(f"  Host Name:      {host.get('host_name', 'N/A')}")
            print(f"  Host Type:      {host.get('host_type', 'N/A')}")
            print(f"  Host ID:        {host.get('host_id', 'N/A')}")
            print(f"  System Version: {host.get('system_version', 'N/A')}")
            print(f"  Protocol:       {host.get('device_discovery_protocol_version', 'N/A')}")
            print(f"  Running App:    {host.get('running_app_name', 'N/A')} ({host.get('running_app_titleid', 'N/A')})")
            port = host.get('host_request_port', SESSION_PORT)
            print(f"  Request Port:   {port}")

    # === WAKEUP ===
    elif args.command == "wakeup":
        wakeup_console(args.host, args.regist_key, ps5=args.ps5)

    # === REGISTER ===
    elif args.command == "register":
        result = register_console(
            args.host,
            args.psn_account_id,
            args.pin,
            ps5=args.ps5,
            lib_path=args.lib_path,
        )

        if result and result["success"]:
            # FIX: Use the rp_key from registration as the morning value,
            # NOT a random value. The morning field in ChiakiConnectInfo
            # is the RP encryption key established during registration.
            morning = result["rp_key"]

            print(f"\n[+] Save these values for recording:")
            print(f"    --regist-key {result['regist_key'].hex()}")
            print(f"    --morning {morning.hex()}")
            print()
            print(f"[!] IMPORTANT: The --morning value is the RP key from registration.")
            print(f"    It is NOT random. You must use this exact value for all future sessions.")

            if args.save_config:
                save_config(
                    args.save_config,
                    host=args.host,
                    regist_key=result["regist_key"],
                    morning=morning,
                    ps5=args.ps5,
                    psn_account_id=args.psn_account_id,
                    nickname=result.get("nickname"),
                )
        else:
            print("[!] Registration failed")
            sys.exit(1)

    # === RECORD ===
    elif args.command == "record":
        host = args.host
        regist_key = None
        morning = None
        ps5 = args.ps5
        psn_account_id = None

        # Load from config if specified
        if args.config:
            config = load_config(args.config)
            host = host or config.get("host")
            ps5 = config.get("ps5", ps5)
            if config.get("regist_key"):
                regist_key = bytes.fromhex(config["regist_key"])
            if config.get("morning"):
                morning = bytes.fromhex(config["morning"])
            if config.get("psn_account_id"):
                psn_account_id = base64.b64decode(config["psn_account_id"])

        # Override with CLI args
        if args.regist_key:
            regist_key = bytes.fromhex(args.regist_key)
        if args.morning:
            morning = bytes.fromhex(args.morning)
        if args.psn_account_id:
            psn_account_id = base64.b64decode(args.psn_account_id)

        if morning is None:
            print("[!] WARNING: No morning/rp_key provided. Generating random value.")
            print("    This will likely fail! The morning must be the rp_key from registration.")
            print("    Re-register with: python3 ps_stream_recorder.py register --save-config ps_config.json ...")
            morning = os.urandom(16)

        if not host or not regist_key:
            print("[!] --host and --regist-key are required (or use --config)")
            sys.exit(1)

        if len(regist_key) != SESSION_AUTH_SIZE:
            print(f"[!] regist_key must be {SESSION_AUTH_SIZE} bytes ({SESSION_AUTH_SIZE * 2} hex chars)")
            sys.exit(1)

        # Resolution map
        res_map = {
            "360p": (640, 360, 2000),
            "540p": (960, 540, 6000),
            "720p": (1280, 720, 10000),
            "1080p": (1920, 1080, 15000),
        }
        width, height, default_bitrate = res_map[args.resolution]
        bitrate = args.bitrate if args.bitrate != 15000 else default_bitrate

        codec_map = {"h264": CODEC_H264, "h265": CODEC_H265, "h265-hdr": CODEC_H265_HDR}
        codec = codec_map[args.codec]

        if args.fallback:
            record_with_chiaki_cli(
                host, regist_key.hex(), morning.hex(), args.output,
                duration=args.duration, ps5=ps5, resolution=args.resolution, fps=args.fps
            )
        else:
            recorder = StreamRecorder(
                host=host,
                regist_key=regist_key,
                morning=morning,
                ps5=ps5,
                codec=codec,
                width=width,
                height=height,
                fps=args.fps,
                bitrate=bitrate,
                output=args.output,
                duration=args.duration,
                psn_account_id=psn_account_id,
                lib_path=args.lib_path,
                verbose=args.verbose,
            )
            recorder.record()

    # === STREAM ===
    elif args.command == "stream":
        host = args.host
        regist_key = None
        morning = None
        ps5 = args.ps5
        psn_account_id = None

        if args.config:
            config = load_config(args.config)
            host = host or config.get("host")
            ps5 = config.get("ps5", ps5)
            if config.get("regist_key"):
                regist_key = bytes.fromhex(config["regist_key"])
            if config.get("morning"):
                morning = bytes.fromhex(config["morning"])
            if config.get("psn_account_id"):
                psn_account_id = base64.b64decode(config["psn_account_id"])

        if args.regist_key:
            regist_key = bytes.fromhex(args.regist_key)
        if args.morning:
            morning = bytes.fromhex(args.morning)
        if args.psn_account_id:
            psn_account_id = base64.b64decode(args.psn_account_id)

        if morning is None:
            print("[!] No morning/rp_key. Re-register first.", file=sys.stderr)
            sys.exit(1)
        if not host or not regist_key:
            print("[!] --host and --regist-key required (or use --config)", file=sys.stderr)
            sys.exit(1)
        if len(regist_key) != SESSION_AUTH_SIZE:
            print(f"[!] regist_key must be {SESSION_AUTH_SIZE} bytes", file=sys.stderr)
            sys.exit(1)

        res_map = {
            "360p": (640, 360, 2000),
            "540p": (960, 540, 6000),
            "720p": (1280, 720, 10000),
            "1080p": (1920, 1080, 15000),
        }
        width, height, default_bitrate = res_map[args.resolution]
        bitrate = args.bitrate if args.bitrate != 15000 else default_bitrate
        codec_map = {"h264": CODEC_H264, "h265": CODEC_H265, "h265-hdr": CODEC_H265_HDR}
        codec = codec_map[args.codec]

        # Determine output mode
        if args.hw_decoder and not args.v4l2:
            print("[!] --hw-decoder only applies to --v4l2 mode", file=sys.stderr)
            sys.exit(1)

        fifo_path = args.fifo
        # --play without --v4l2/--fifo: auto-create a temp FIFO
        if args.play and not args.v4l2 and not args.fifo:
            fifo_path = os.path.join(tempfile.gettempdir(), f"chiaki_play_{os.getpid()}")

        pipe_stdout = not fifo_path and not args.v4l2

        # Parse text detection ROIs
        text_rois = None
        if hasattr(args, 'text_roi') and args.text_roi:
            text_rois = []
            for roi_str in args.text_roi:
                parts = [float(x.strip()) for x in roi_str.split(",")]
                if len(parts) != 4:
                    print(f"[!] Invalid --text-roi: {roi_str} (expected X,Y,W,H)", file=sys.stderr)
                    sys.exit(1)
                text_rois.append(tuple(parts))

        streamer = StreamOutput(
            host=host, regist_key=regist_key, morning=morning,
            ps5=ps5, codec=codec, width=width, height=height,
            fps=args.fps, bitrate=bitrate, duration=args.duration,
            psn_account_id=psn_account_id, lib_path=args.lib_path,
            verbose=args.verbose,
            pipe_stdout=pipe_stdout, fifo_path=fifo_path,
            v4l2_device=args.v4l2, hw_decoder=args.hw_decoder,
            controller_device=args.controller,
            play=args.play,
            detect_text=args.detect_text,
            text_rois=text_rois,
            text_template_dir=args.text_templates,
            text_skip_frames=args.text_skip,
            text_min_confidence=args.text_confidence,
        )
        streamer.stream()


if __name__ == "__main__":
    main()
