from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

# ── App version ──────────────────────────────────────────────────────────────
# One constant, read by the window title, the header badge and the PROTOCOL
# panel. Deriving the protocol from the name means a release bump is this one line.
APP_VERSION  = "System-Assist"
APP_PROTOCOL = APP_VERSION.split()[-1]

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_DEFAULT_W, _DEFAULT_H = 1040, 720
_MIN_W,     _MIN_H     = 880, 600
_LEFT_W  = 190
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#00060a"
    PANEL     = "#010d14"
    PANEL2    = "#010f18"
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#8ffcff"
    TEXT_DIM  = "#3a8a9a"
    TEXT_MED  = "#5ab8cc"
    WHITE     = "#d8f8ff"
    DARK      = "#000d14"
    BAR_BG    = "#011520"


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._get_gpu()
            tmp = self._get_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    """
    3D Golden-Amber Holographic AI Core Canvas for JARVIS / System-Assist.
    Features:
      - 3D rotating particle lattice intelligence with depth projection
      - Concentric 3D gimbal orbital rings with traveling energy nodes
      - Multi-layer volumetric golden-amber core with radiant corona
      - Dynamic holographic circuit traces and glowing node networks
      - Reactive audio expansion, particle ejection, and HUD equalizers
      - Holographic technical telemetry markings & Vernier scale
    """

    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 60.0
        self._tgt_halo   = 60.0
        self._last_t     = time.time()

        # 3D Rotation angles
        self._yaw   = 0.0
        self._pitch = 0.35   # ~20 degrees inclination
        self._roll  = 0.0

        # Orbital gimbal ring angles
        self._orbit_angles = [0.0, 120.0, 240.0, 60.0]
        self._scan  = 0.0
        self._scan2 = 180.0

        # Pulse waves & particle fields
        self._pulses: list[float] = [0.0, 60.0, 120.0]
        self._blink      = True
        self._blink_tick = 0
        self._burst_particles: list[list[float]] = []  # [x, y, vx, vy, life, max_life, size, col_type]
        self._ambient_particles: list[list[float]] = [] # [theta, phi, r_offset, speed, sz, alpha_phase]

        # Initialize ambient floating particle field around the sphere
        for _ in range(75):
            self._ambient_particles.append([
                random.uniform(0, 2 * math.pi),
                random.uniform(-math.pi / 2, math.pi / 2),
                random.uniform(0.85, 1.35),
                random.uniform(0.008, 0.025) * (1 if random.random() > 0.5 else -1),
                random.uniform(1.2, 3.2),
                random.uniform(0, 2 * math.pi)
            ])

        # Precompute 3D sphere lattice nodes (latitude / longitude grid)
        self._sphere_nodes: list[tuple[float, float, float, int, int]] = []
        lats, lons = 14, 24
        for i in range(lats):
            lat = -math.pi / 2 + (i + 0.5) * (math.pi / lats)
            for j in range(lons):
                lon = j * (2 * math.pi / lons)
                x = math.cos(lat) * math.cos(lon)
                y = math.sin(lat)
                z = math.cos(lat) * math.sin(lon)
                self._sphere_nodes.append((x, y, z, i, j))

        # Earth continent landmass polygon outlines for 3D Globe Projection
        continents = [
            # North America
            [(70, -165), (71, -130), (70, -80), (60, -60), (45, -55), (42, -70), (30, -80), (25, -80), (18, -100), (30, -115), (48, -125), (60, -145)],
            # South America
            [(12, -75), (8, -50), (-5, -35), (-22, -40), (-40, -62), (-54, -68), (-45, -75), (-20, -70), (0, -80)],
            # Europe
            [(70, 25), (60, 30), (55, 38), (45, 30), (38, 24), (36, -6), (44, -8), (52, 5), (60, 5)],
            # Africa
            [(37, 10), (32, 32), (12, 50), (-5, 40), (-34, 25), (-34, 18), (5, 0), (15, -17), (35, -5)],
            # Asia
            [(75, 100), (70, 175), (60, 160), (40, 130), (22, 120), (10, 105), (20, 80), (35, 60), (60, 60)],
            # Australia
            [(-12, 130), (-15, 150), (-35, 150), (-38, 140), (-32, 115), (-20, 115)]
        ]
        self._land_nodes: list[tuple[float, float, float]] = []
        for poly in continents:
            for i in range(len(poly)):
                lat1, lon1 = poly[i]
                lat2, lon2 = poly[(i + 1) % len(poly)]
                steps = 6
                for s in range(steps):
                    t = s / float(steps)
                    lat = math.radians(lat1 + (lat2 - lat1) * t)
                    lon = math.radians(lon1 + (lon2 - lon1) * t)
                    x = math.cos(lat) * math.cos(lon)
                    y = math.sin(lat)
                    z = math.cos(lat) * math.sin(lon)
                    self._land_nodes.append((x, y, z))

        # Inner core shell nodes
        self._inner_nodes: list[tuple[float, float, float]] = []
        for i in range(6):
            lat = -math.pi / 2 + (i + 0.5) * (math.pi / 6)
            for j in range(12):
                lon = j * (2 * math.pi / 12)
                self._inner_nodes.append((
                    0.55 * math.cos(lat) * math.cos(lon),
                    0.55 * math.sin(lat),
                    0.55 * math.cos(lat) * math.sin(lon)
                ))

        self._face_px: QPixmap | None = None
        self._face_cache: QPixmap | None = None
        self._face_cache_sz = -1
        self._grid_cache: QPixmap | None = None
        self._grid_key = None
        self._paint_tick = 0
        self._load_face(face_path)

        # Live audio reactivity
        self._live_amp   = 0.0
        self._amp_disp   = 0.0
        self._base_scale = 1.0
        self._base_halo  = 60.0

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def set_audio_level(self, level: float) -> None:
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        lv = max(0.0, min(1.0, lv))
        if lv > self._live_amp:
            self._live_amp = lv

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None
        self._face_cache    = None
        self._face_cache_sz = -1

    def _make_grid(self, W: int, H: int) -> QPixmap:
        pm = QPixmap(max(1, W), max(1, H))
        pm.fill(Qt.GlobalColor.transparent)
        gp = QPainter(pm)
        gp.setPen(QPen(qcol(C.PRI_GHO, 70), 1))
        for x in range(0, W, 40):
            for y in range(0, H, 40):
                gp.drawPoint(x, y)
        gp.end()
        return pm

    def _step(self):
        self._tick += 1
        now = time.time()

        # Decay audio amplitude towards silence
        self._live_amp *= 0.88
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.42
        amp = self._amp_disp

        # Dynamic breathing target
        if now - self._last_t > (0.10 if self.speaking else 0.45):
            if self.speaking:
                self._base_scale = 1.05
                self._base_halo  = 130.0
            elif self.muted:
                self._base_scale = random.uniform(0.995, 1.002)
                self._base_halo  = random.uniform(20, 32)
            else:
                self._base_scale = random.uniform(1.002, 1.012)
                self._base_halo  = random.uniform(55, 75)
            self._last_t = now

        # Live audio amplitude lifts target scale & glow halo
        if self.muted:
            self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
        elif self.speaking:
            self._tgt_scale = self._base_scale + amp * 0.16
            self._tgt_halo  = self._base_halo  + amp * 110.0
        else:
            self._tgt_scale = self._base_scale + amp * 0.08
            self._tgt_halo  = self._base_halo  + amp * 80.0

        sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.16)
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        # 3D Sphere rotations
        rot_spd = (0.038 if self.speaking else (0.028 if amp > 0.05 else 0.015)) * (1.0 + amp * 1.8)
        self._yaw = (self._yaw + rot_spd) % (2 * math.pi)
        self._pitch = 0.35 + 0.08 * math.sin(self._tick * 0.025)
        self._roll  = 0.04 * math.sin(self._tick * 0.018)

        # Orbital gimbal rings
        boost = 1.0 + amp * 2.0
        self._orbit_angles[0] = (self._orbit_angles[0] + (2.2 if self.speaking else 1.0) * boost) % 360
        self._orbit_angles[1] = (self._orbit_angles[1] - (1.7 if self.speaking else 0.7) * boost) % 360
        self._orbit_angles[2] = (self._orbit_angles[2] + (2.8 if self.speaking else 1.3) * boost) % 360
        self._orbit_angles[3] = (self._orbit_angles[3] - (1.2 if self.speaking else 0.5) * boost) % 360

        self._scan  = (self._scan  + (3.5 if self.speaking else 1.4) * boost) % 360
        self._scan2 = (self._scan2 - (2.4 if self.speaking else 0.9) * boost) % 360

        # Outer expanding pulse rings
        fw  = min(self.width(), self.height())
        lim = fw * 0.78
        pspd = 4.5 if self.speaking else 2.2
        self._pulses = [r + pspd for r in self._pulses if r + pspd < lim]
        if len(self._pulses) < 4 and random.random() < (0.08 if self.speaking else 0.028):
            self._pulses.append(0.0)

        # Ambient floating particle updates
        for p in self._ambient_particles:
            p[0] = (p[0] + p[3] * (1.5 if self.speaking else 1.0)) % (2 * math.pi)
            p[5] = (p[5] + 0.04) % (2 * math.pi)

        # Audio burst particles when speaking or receiving voice input
        if (self.speaking or amp > 0.04) and random.random() < (0.45 if self.speaking else 0.25):
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.26 * self._scale
            spd_val = random.uniform(1.2, 3.6) + amp * 3.5
            self._burst_particles.append([
                cx + math.cos(ang) * r_s,
                cy + math.sin(ang) * r_s,
                math.cos(ang) * spd_val,
                math.sin(ang) * spd_val,
                1.0,                           # life
                random.uniform(0.02, 0.04),    # decay rate
                random.uniform(1.5, 3.8),      # size
                random.choice([0, 1, 2])       # color: 0=gold, 1=amber, 2=hot-white
            ])

        # Step and clean up burst particles
        alive_burst = []
        for p in self._burst_particles:
            p[0] += p[2]
            p[1] += p[3]
            p[2] *= 0.96
            p[3] *= 0.96
            p[4] -= p[5]
            if p[4] > 0:
                alive_burst.append(p)
        self._burst_particles = alive_burst

        # Blink flag
        self._blink_tick += 1
        if self._blink_tick >= 36:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Throttled repaint
        self._paint_tick = (self._paint_tick + 1) % 3
        active = (self.speaking or amp > 0.02 or self.state in ("THINKING", "PROCESSING", "LISTENING"))
        if active or _blinked or self._paint_tick == 0:
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)
        amp = self._amp_disp

        # Background — deep sci-fi navy obsidian
        p.fillRect(self.rect(), QColor("#01060e"))

        # Cached grid dots
        _gkey = (W, H, C.PRI_GHO)
        if self._grid_cache is None or self._grid_key != _gkey:
            self._grid_cache = self._make_grid(W, H)
            self._grid_key   = _gkey
        p.drawPixmap(0, 0, self._grid_cache)

        # ── COLOR DEFINITIONS (GOLDEN-AMBER HOLOGRAPHIC SUITE) ──────────────────
        if self.muted:
            c_hot    = QColor("#ffcccc")
            c_bright = QColor("#ff4466")
            c_main   = QColor("#cc1133")
            c_warm   = QColor("#880011")
            c_deep   = QColor("#440008")
            c_cyan   = QColor("#ff6688")
        else:
            c_hot    = QColor("#fffdf2")   # Hot white-gold
            c_bright = QColor("#ffe277")   # 24K Radiant gold
            c_main   = QColor("#ffb300")   # Golden amber
            c_warm   = QColor("#ff7700")   # Solar orange-amber
            c_deep   = QColor("#b34400")   # Deep bronze-amber
            c_cyan   = QColor("#00d4ff")   # Secondary telemetry cyan

        # ── 1. OUTER HUD RETICLE & TECHNICAL TELEMETRY ─────────────────────────
        # Corner Targeting Brackets
        bl = 26
        bc = QColor(c_main); bc.setAlpha(120)
        hl, hr = cx - fw * 0.46, cx + fw * 0.46
        ht, hb = cy - fw * 0.46, cy + fw * 0.46
        p.setPen(QPen(bc, 1.5))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # Concentric guide circles
        for r_frac, a_val, stroke_w in [(0.47, 45, 1.0), (0.42, 35, 1.0), (0.36, 50, 1.0)]:
            r_c = fw * r_frac
            col = QColor(c_main); col.setAlpha(a_val)
            p.setPen(QPen(col, stroke_w, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r_c, cy - r_c, r_c * 2, r_c * 2))

        # Vernier Compass Circular Ticks (360 degrees)
        r_tick_out = fw * 0.465
        r_tick_in  = fw * 0.450
        r_tick_maj = fw * 0.440
        for deg in range(0, 360, 5):
            rad = math.radians(deg)
            is_maj = (deg % 30 == 0)
            is_med = (deg % 15 == 0)
            inn = r_tick_maj if is_maj else (r_tick_in if is_med else r_tick_in + 2)
            alpha_tick = 160 if is_maj else (100 if is_med else 45)
            col_tick = QColor(c_bright if is_maj else (c_cyan if is_med else c_main))
            col_tick.setAlpha(alpha_tick)
            p.setPen(QPen(col_tick, 1.5 if is_maj else 1.0))
            p.drawLine(
                QPointF(cx + r_tick_out * math.cos(rad), cy - r_tick_out * math.sin(rad)),
                QPointF(cx + inn * math.cos(rad), cy - inn * math.sin(rad))
            )

        # Rotating Outer Scanning Arcs
        r_scan_a = fw * 0.455
        sa1 = min(255, int(self._halo * 1.4))
        p.setPen(QPen(QColor(c_main.red(), c_main.green(), c_main.blue(), sa1), 2.0))
        p.drawArc(QRectF(cx - r_scan_a, cy - r_scan_a, r_scan_a * 2, r_scan_a * 2),
                  int(self._scan * 16), int((85 if self.speaking else 55) * 16))
        p.setPen(QPen(QColor(c_cyan.red(), c_cyan.green(), c_cyan.blue(), sa1 // 2), 1.5))
        p.drawArc(QRectF(cx - r_scan_a, cy - r_scan_a, r_scan_a * 2, r_scan_a * 2),
                  int(self._scan2 * 16), int((70 if self.speaking else 40) * 16))

        # Telemetry Labels
        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(QColor(c_bright.red(), c_bright.green(), c_bright.blue(), 130), 1))
        p.drawText(QRectF(cx - fw * 0.45, cy - fw * 0.45, 120, 14), Qt.AlignmentFlag.AlignLeft, "◈ AI CORE // ONLINE")
        p.drawText(QRectF(cx + fw * 0.45 - 120, cy - fw * 0.45, 120, 14), Qt.AlignmentFlag.AlignRight, "HOLO MATRIX v4.2")
        p.drawText(QRectF(cx - fw * 0.45, cy + fw * 0.43, 120, 14), Qt.AlignmentFlag.AlignLeft, "QUANTUM SYNC // LOCK")
        p.drawText(QRectF(cx + fw * 0.45 - 120, cy + fw * 0.43, 120, 14), Qt.AlignmentFlag.AlignRight, f"FLUX // {int(98.5 + amp*1.4)}%")

        # ── 1B. FLOATING HOLOGRAPHIC DATA SCREENS (SIDE WINGS) ────────────────
        if fw > 360:
            # 1. Top-Left Floating Screen: Flat 2D World Map Projection
            scr_w, scr_h = 110, 75
            scr_lx = cx - fw * 0.46
            scr_ly = cy - fw * 0.36

            p.setBrush(QBrush(QColor(2, 10, 18, 160)))
            p.setPen(QPen(QColor(c_cyan.red(), c_cyan.green(), c_cyan.blue(), 100), 1))
            p.drawRoundedRect(QRectF(scr_lx, scr_ly, scr_w, scr_h), 4, 4)

            p.setFont(QFont("Courier New", 5, QFont.Weight.Bold))
            p.setPen(QPen(c_cyan, 1))
            p.drawText(QRectF(scr_lx + 4, scr_ly + 3, scr_w - 8, 10), Qt.AlignmentFlag.AlignLeft, "◈ WORLD MAP // GLOBAL")

            # Mini flat 2D continent map dots
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(c_bright))
            map_pts = [
                (-35, -15), (-30, -10), (-25, -5), (-30, 5), (-32, 15), (-25, 20),
                (-5, -12), (0, -5), (5, 5), (0, 18), (5, 22),
                (15, -18), (25, -15), (35, -10), (40, 5), (30, 12), (20, 18),
                (35, 22), (40, 25)
            ]
            for m_dx, m_dy in map_pts:
                p.drawEllipse(QPointF(scr_lx + scr_w / 2 + m_dx * 1.1, scr_ly + 40 + m_dy * 0.9), 1.8, 1.8)

            # 2. Mid-Left Floating Screen: Tactical Target Radar Scope
            scr2_ly = cy - 10
            scr2_h = 95
            p.setBrush(QBrush(QColor(2, 10, 18, 160)))
            p.setPen(QPen(QColor(c_main.red(), c_main.green(), c_main.blue(), 100), 1))
            p.drawRoundedRect(QRectF(scr_lx, scr2_ly, scr_w, scr2_h), 4, 4)

            p.setFont(QFont("Courier New", 5, QFont.Weight.Bold))
            p.setPen(QPen(c_bright, 1))
            p.drawText(QRectF(scr_lx + 4, scr2_ly + 3, scr_w - 8, 10), Qt.AlignmentFlag.AlignLeft, "◈ RADAR // GYRO SCOPE")

            # Radar rings with sweeping beam
            r_cx = scr_lx + scr_w / 2
            r_cy = scr2_ly + 40
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(c_main.red(), c_main.green(), c_main.blue(), 120), 1))
            p.drawEllipse(QRectF(r_cx - 20, r_cy - 20, 40, 40))
            p.drawEllipse(QRectF(r_cx - 10, r_cy - 10, 20, 20))
            p.drawLine(QPointF(r_cx - 20, r_cy), QPointF(r_cx + 20, r_cy))
            p.drawLine(QPointF(r_cx, r_cy - 20), QPointF(r_cx, r_cy + 20))

            sw_rad = math.radians(self._scan)
            p.setPen(QPen(c_hot, 1.5))
            p.drawLine(QPointF(r_cx, r_cy), QPointF(r_cx + 20 * math.cos(sw_rad), r_cy + 20 * math.sin(sw_rad)))

            p.setFont(QFont("Courier New", 5))
            p.setPen(QPen(QColor(c_bright.red(), c_bright.green(), c_bright.blue(), 160), 1))
            p.drawText(QRectF(scr_lx + 4, scr2_ly + 68, scr_w - 8, 8), Qt.AlignmentFlag.AlignLeft, "TGT LOCK // 01")
            p.drawText(QRectF(scr_lx + 4, scr2_ly + 78, scr_w - 8, 8), Qt.AlignmentFlag.AlignLeft, "BRG 042° TRUE")

            # 3. Top-Right Floating Screen: 3D Orbit Globe Projection
            scr_rx = cx + fw * 0.46 - scr_w
            p.setBrush(QBrush(QColor(2, 10, 18, 160)))
            p.setPen(QPen(QColor(c_cyan.red(), c_cyan.green(), c_cyan.blue(), 100), 1))
            p.drawRoundedRect(QRectF(scr_rx, scr_ly, scr_w, scr_h), 4, 4)

            p.setFont(QFont("Courier New", 5, QFont.Weight.Bold))
            p.setPen(QPen(c_cyan, 1))
            p.drawText(QRectF(scr_rx + 4, scr_ly + 3, scr_w - 8, 10), Qt.AlignmentFlag.AlignLeft, "◈ ORBIT // TRACK")

            # Mini rotating globe/orbit inside top-right screen
            p.save()
            p.translate(scr_rx + scr_w / 2, scr_ly + 40)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(c_cyan.red(), c_cyan.green(), c_cyan.blue(), 130), 1))
            p.drawEllipse(QRectF(-18, -18, 36, 36))
            p.rotate(self._tick * 1.8)
            p.drawEllipse(QRectF(-18, -7, 36, 14))
            p.rotate(60)
            p.drawEllipse(QRectF(-18, -7, 36, 14))
            p.restore()

            # 4. Mid-Right Floating Screen: Target Telemetry Scope
            p.setBrush(QBrush(QColor(2, 10, 18, 160)))
            p.setPen(QPen(QColor(c_main.red(), c_main.green(), c_main.blue(), 100), 1))
            p.drawRoundedRect(QRectF(scr_rx, scr2_ly, scr_w, scr2_h), 4, 4)

            p.setFont(QFont("Courier New", 5, QFont.Weight.Bold))
            p.setPen(QPen(c_bright, 1))
            p.drawText(QRectF(scr_rx + 4, scr2_ly + 3, scr_w - 8, 10), Qt.AlignmentFlag.AlignLeft, "◈ TARGET // SCOPE")

            tr_cx = scr_rx + scr_w / 2
            tr_cy = scr2_ly + 40
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(c_bright.red(), c_bright.green(), c_bright.blue(), 120), 1, Qt.PenStyle.DashLine))
            p.drawEllipse(QRectF(tr_cx - 18, tr_cy - 18, 36, 36))
            p.drawEllipse(QRectF(tr_cx - 9, tr_cy - 9, 18, 18))

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(c_hot))
            p.drawEllipse(QPointF(tr_cx + 8, tr_cy - 7), 2, 2)
            p.drawEllipse(QPointF(tr_cx - 6, tr_cy + 8), 2, 2)

            p.setFont(QFont("Courier New", 5))
            p.setPen(QPen(QColor(c_bright.red(), c_bright.green(), c_bright.blue(), 160), 1))
            p.drawText(QRectF(scr_rx + 4, scr2_ly + 68, scr_w - 8, 8), Qt.AlignmentFlag.AlignLeft, "LAT 37°46'N")
            p.drawText(QRectF(scr_rx + 4, scr2_ly + 78, scr_w - 8, 8), Qt.AlignmentFlag.AlignLeft, "LON 122°25'W")

        # ── 1C. HOLOGRAPHIC BASE PEDESTAL & PROJECTOR BEAMS ──────────────────
        ped_y = cy + fw * 0.28
        ped_w = fw * 0.58
        ped_h = fw * 0.13

        # Upward holographic projection beam cone
        beam_grad = QLinearGradient(cx, ped_y, cx, cy)
        beam_c1 = QColor(c_cyan); beam_c1.setAlpha(min(90, int(self._halo * 0.6)))
        beam_c2 = QColor(c_bright); beam_c2.setAlpha(0)
        beam_grad.setColorAt(0.0, beam_c1)
        beam_grad.setColorAt(1.0, beam_c2)

        beam_path = QPainterPath()
        beam_path.moveTo(cx - ped_w * 0.38, ped_y)
        beam_path.lineTo(cx - fw * 0.22 * self._scale, cy + fw * 0.08)
        beam_path.lineTo(cx + fw * 0.22 * self._scale, cy + fw * 0.08)
        beam_path.lineTo(cx + ped_w * 0.38, ped_y)
        beam_path.closeSubpath()

        p.setBrush(QBrush(beam_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(beam_path)

        # Concentric base projector rings
        for pw_f, ph_f, p_al, p_w, p_col_base in [
            (1.0, 1.0, 150, 2.0, c_cyan),
            (0.82, 0.82, 190, 1.8, c_main),
            (0.60, 0.60, 230, 1.5, c_bright),
            (0.38, 0.38, 255, 1.2, c_hot)
        ]:
            rw, rh = ped_w * pw_f, ped_h * ph_f
            col_ped = QColor(p_col_base)
            col_ped.setAlpha(p_al)
            p.setPen(QPen(col_ped, p_w))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - rw / 2, ped_y - rh / 2, rw, rh))

        # Base emitter tick marks
        for deg in range(0, 360, 15):
            r_rad = math.radians(deg)
            ex = cx + (ped_w * 0.5) * math.cos(r_rad)
            ey = ped_y + (ped_h * 0.5) * math.sin(r_rad)
            p.setBrush(QBrush(c_hot))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(ex, ey), 1.8, 1.8)

        # Outer expanding pulse waves
        for pr in self._pulses:
            p_alpha = max(0, int(180 * (1.0 - pr / (fw * 0.78))))
            p_col = QColor(c_main); p_col.setAlpha(p_alpha)
            p.setPen(QPen(p_col, 1.2)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # ── 2. VOLUMETRIC GOLDEN CORE GLOW (INNER PLASMA SUN) ─────────────────
        core_r = fw * 0.25 * self._scale
        glow_rad = QRadialGradient(cx, cy, core_r * 2.2)
        g_center = QColor(c_hot);    g_center.setAlpha(min(255, int(self._halo * 1.9)))
        g_mid1   = QColor(c_bright); g_mid1.setAlpha(min(240, int(self._halo * 1.5)))
        g_mid2   = QColor(c_main);   g_mid2.setAlpha(min(190, int(self._halo * 1.0)))
        g_outer  = QColor(c_warm);   g_outer.setAlpha(min(100, int(self._halo * 0.5)))
        g_edge   = QColor(c_deep);   g_edge.setAlpha(0)

        glow_rad.setColorAt(0.0, g_center)
        glow_rad.setColorAt(0.25, g_mid1)
        glow_rad.setColorAt(0.55, g_mid2)
        glow_rad.setColorAt(0.82, g_outer)
        glow_rad.setColorAt(1.0, g_edge)

        p.setBrush(QBrush(glow_rad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - core_r * 2.2, cy - core_r * 2.2, core_r * 4.4, core_r * 4.4))

        # ── 3. 3D ROTATING GIMBAL ORBITAL RINGS ───────────────────────────────
        gimbal_configs = [
            (fw * 0.32 * self._scale, self._orbit_angles[0], 0.65, c_bright, 1.8),
            (fw * 0.36 * self._scale, self._orbit_angles[1], -0.45, c_main, 1.5),
            (fw * 0.40 * self._scale, self._orbit_angles[2], 0.95, c_warm, 1.2),
        ]
        for r_gimb, rot_a, tilt_val, ring_col, pen_w in gimbal_configs:
            p.save()
            p.translate(cx, cy)
            p.rotate(math.degrees(tilt_val))
            p.scale(1.0, 0.42 + 0.10 * math.sin(self._tick * 0.03))

            g_rect = QRectF(-r_gimb, -r_gimb, r_gimb * 2, r_gimb * 2)
            col_a = QColor(ring_col); col_a.setAlpha(min(220, int(self._halo * 1.1)))
            p.setPen(QPen(col_a, pen_w, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(g_rect, int(rot_a * 16), int(140 * 16))
            p.drawArc(g_rect, int((rot_a + 180) * 16), int(110 * 16))

            node_rad = math.radians(rot_a)
            nx = r_gimb * math.cos(node_rad)
            ny = r_gimb * math.sin(node_rad)
            p.setBrush(QBrush(c_hot))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(nx, ny), 3.2, 3.2)
            p.restore()

        # ── 4. 3D HOLOGRAPHIC EARTH GLOBE LATTICE & CONTINENTS ─────────────────
        cos_y, sin_y = math.cos(self._yaw), math.sin(self._yaw)
        cos_p, sin_p = math.cos(self._pitch), math.sin(self._pitch)
        cos_r, sin_r = math.cos(self._roll), math.sin(self._roll)

        dist = 3.6
        sphere_r = fw * 0.25 * self._scale

        # Project outer sphere lattice nodes
        projected_nodes: list[tuple[float, float, float, int, int]] = []
        for x, y, z, lat_idx, lon_idx in self._sphere_nodes:
            x1 = x * cos_y - z * sin_y
            z1 = x * sin_y + z * cos_y
            y2 = y * cos_p - z1 * sin_p
            z2 = y * sin_p + z1 * cos_p
            x3 = x1 * cos_r - y2 * sin_r
            y3 = x1 * sin_r + y2 * cos_r
            z3 = z2

            sc = dist / (dist - z3)
            px = cx + x3 * sphere_r * sc
            py = cy + y3 * sphere_r * sc
            projected_nodes.append((px, py, z3, lat_idx, lon_idx))

        # Project continent landmass nodes
        projected_land: list[tuple[float, float, float]] = []
        for x, y, z in self._land_nodes:
            x1 = x * cos_y - z * sin_y
            z1 = x * sin_y + z * cos_y
            y2 = y * cos_p - z1 * sin_p
            z2 = y * sin_p + z1 * cos_p
            x3 = x1 * cos_r - y2 * sin_r
            y3 = x1 * sin_r + y2 * cos_r
            z3 = z2

            sc = dist / (dist - z3)
            px = cx + x3 * sphere_r * sc
            py = cy + y3 * sphere_r * sc
            projected_land.append((px, py, z3))

        # Draw wireframe circuit connector lines between adjacent grid points
        lats_count, lons_count = 14, 24
        p.setBrush(Qt.BrushStyle.NoBrush)
        for idx, (px, py, z, i, j) in enumerate(projected_nodes):
            next_lon_idx = (j + 1) % lons_count
            neighbor_idx = i * lons_count + next_lon_idx
            if neighbor_idx < len(projected_nodes):
                n_px, n_py, n_z, _, _ = projected_nodes[neighbor_idx]
                avg_z = (z + n_z) / 2
                line_alpha = max(15, min(180, int((avg_z + 1.0) * 85 + self._halo * 0.4)))
                line_col = QColor(c_bright if avg_z > 0.2 else c_deep)
                line_col.setAlpha(line_alpha)
                p.setPen(QPen(line_col, 1.0 if avg_z > 0 else 0.7))
                p.drawLine(QPointF(px, py), QPointF(n_px, n_py))

            if i < lats_count - 1:
                lat_neighbor_idx = (i + 1) * lons_count + j
                if lat_neighbor_idx < len(projected_nodes):
                    n_px, n_py, n_z, _, _ = projected_nodes[lat_neighbor_idx]
                    avg_z = (z + n_z) / 2
                    line_alpha = max(15, min(180, int((avg_z + 1.0) * 85 + self._halo * 0.4)))
                    line_col = QColor(c_bright if avg_z > 0.2 else c_deep)
                    line_col.setAlpha(line_alpha)
                    p.setPen(QPen(line_col, 1.0 if avg_z > 0 else 0.7))
                    p.drawLine(QPointF(px, py), QPointF(n_px, n_py))

        # Draw Earth continent landmass points (Depth sorted)
        for px, py, z in sorted(projected_land, key=lambda n: n[2]):
            depth_factor = (z + 1.0) / 2.0
            if depth_factor > 0.35:
                l_sz = 2.0 + depth_factor * 2.5
                l_alpha = min(255, int(depth_factor * 220 + 35))
                l_col = QColor(c_hot if depth_factor > 0.7 else c_bright)
                l_col.setAlpha(l_alpha)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(l_col))
                p.drawEllipse(QPointF(px, py), l_sz, l_sz)

        # Draw projected sphere nodes (Depth sorted)
        for px, py, z, i, j in sorted(projected_nodes, key=lambda n: n[2]):
            depth_factor = (z + 1.0) / 2.0
            pt_sz = 1.6 + depth_factor * 2.8 + (amp * 2.0 if depth_factor > 0.6 else 0.0)
            pt_alpha = max(40, min(255, int(depth_factor * 200 + 45 + amp * 60)))

            if depth_factor > 0.75:
                pt_col = QColor(c_hot)
            elif depth_factor > 0.4:
                pt_col = QColor(c_bright)
            else:
                pt_col = QColor(c_warm)
            pt_col.setAlpha(pt_alpha)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(pt_col))
            p.drawEllipse(QPointF(px, py), pt_sz, pt_sz)

        # ── 5. AMBIENT ORBITING DUST & BURST PARTICLES ─────────────────────────
        for theta, phi, r_off, spd, sz, a_phase in self._ambient_particles:
            r_curr = sphere_r * r_off
            ax = r_curr * math.cos(phi) * math.cos(theta)
            ay = r_curr * math.sin(phi)
            az = r_curr * math.cos(phi) * math.sin(theta)

            ax1 = ax * cos_y - az * sin_y
            az1 = ax * sin_y + az * cos_y
            ay2 = ay * cos_p - az1 * sin_p
            az2 = ay * sin_p + az1 * cos_p

            sc_a = dist / (dist - az2)
            apx = cx + ax1 * sc_a
            apy = cy + ay2 * sc_a

            d_fact = (az2 + 1.0) / 2.0
            p_alpha = max(20, min(240, int((0.5 + 0.5 * math.sin(a_phase)) * (d_fact * 180 + 50))))
            p_c = QColor(c_bright if d_fact > 0.5 else c_deep)
            p_c.setAlpha(p_alpha)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(p_c))
            p.drawEllipse(QPointF(apx, apy), sz * sc_a, sz * sc_a)

        # Audio burst particles (ejected outward on voice)
        for bx, by, _, _, life, _, b_sz, col_t in self._burst_particles:
            b_alpha = max(0, min(255, int(life * 255)))
            b_col = QColor(c_hot if col_t == 2 else (c_bright if col_t == 0 else c_warm))
            b_col.setAlpha(b_alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(b_col))
            p.drawEllipse(QPointF(bx, by), b_sz * life, b_sz * life)

        # ── 6. CENTER HOLOGRAPHIC ASSISTANT IDENTITY ───────────────────────────
        if self._face_px:
            fsz = int(fw * 0.44 * self._scale)
            q_sz = max(1, (fsz // 4) * 4)
            if self._face_cache is None or self._face_cache_sz != q_sz:
                self._face_cache = self._face_px.scaled(
                    q_sz, q_sz,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._face_cache_sz = q_sz
            scaled = self._face_cache
            p.drawPixmap(int(cx - scaled.width() / 2), int(cy - scaled.height() / 2), scaled)
        else:
            # Holographic Core Hexagon/Reticle Badge
            badge_r = 38 + amp * 8
            badge_col = QColor(c_bright); badge_col.setAlpha(min(255, int(self._halo * 1.6)))
            inner_bg = QColor(8, 4, 1, 150)
            p.setBrush(QBrush(inner_bg))
            p.setPen(QPen(badge_col, 1.5, Qt.PenStyle.SolidLine))

            # Outer Hexagonal badge
            hex_path = QPainterPath()
            for k in range(6):
                ang_k = math.radians(k * 60 + self._tick * 0.3)
                hx = cx + badge_r * math.cos(ang_k)
                hy = cy + badge_r * math.sin(ang_k)
                if k == 0:
                    hex_path.moveTo(hx, hy)
                else:
                    hex_path.lineTo(hx, hy)
            hex_path.closeSubpath()
            p.drawPath(hex_path)

            # Inner concentric hexagonal rim
            inner_hex = QPainterPath()
            for k in range(6):
                ang_k = math.radians(k * 60 - self._tick * 0.3)
                hx = cx + (badge_r - 6) * math.cos(ang_k)
                hy = cy + (badge_r - 6) * math.sin(ang_k)
                if k == 0:
                    inner_hex.moveTo(hx, hy)
                else:
                    inner_hex.lineTo(hx, hy)
            inner_hex.closeSubpath()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(c_warm.red(), c_warm.green(), c_warm.blue(), 160), 1.0, Qt.PenStyle.DashLine))
            p.drawPath(inner_hex)

            # Center target pin
            p.setBrush(QBrush(c_hot))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy), 2.2, 2.2)

            # Assistant Name in Golden Holographic Typography
            txt_alpha = min(255, int(self._halo * 2.4))
            p.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
            p.setPen(QPen(QColor(c_hot.red(), c_hot.green(), c_hot.blue(), txt_alpha), 1))
            p.drawText(QRectF(cx - 90, cy - 12, 180, 24), Qt.AlignmentFlag.AlignCenter, self._assistant_name)

        # ── 7. CENTRAL CIRCULAR MICROPHONE BUTTON & FLANKING HORIZONTAL WAVES ─
        sy = cy + fw * 0.37
        mic_btn_r = 22 + amp * 4

        # Circular glowing gold microphone button (like replica image)
        mic_grad = QRadialGradient(cx, sy, mic_btn_r * 1.5)
        mic_grad.setColorAt(0.0, QColor(c_bright))
        mic_grad.setColorAt(0.7, QColor(c_main))
        mic_grad.setColorAt(1.0, QColor(c_warm))
        p.setBrush(QBrush(QColor(12, 6, 2, 220)))
        p.setPen(QPen(QColor(c_bright), 1.8))
        p.drawEllipse(QRectF(cx - mic_btn_r, sy - mic_btn_r, mic_btn_r * 2, mic_btn_r * 2))

        # Microphone Icon inside button — from mj.png sprite sheet
        mic_px = IconManager.get_pixmap("mic_active", int(mic_btn_r * 1.15))
        if mic_px and not mic_px.isNull():
            p.drawPixmap(
                int(cx - mic_px.width() / 2), int(sy - mic_px.height() / 2), mic_px
            )
        else:
            # Fallback: drawn mic glyph (only if mj.png icon sheet is unavailable)
            p.setPen(QPen(c_hot, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, sy - 4), 5, 5)
            p.drawRoundedRect(QRectF(cx - 4, sy + 1, 8, 9), 3, 3)
            p.drawLine(QPointF(cx - 7, sy + 9), QPointF(cx + 7, sy + 9))
            p.drawLine(QPointF(cx - 2, sy + 13), QPointF(cx + 2, sy + 13))

        # "Listening..." glowing label below button
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(c_bright, 1))
        stat_lbl = "Listening..." if self.state == "LISTENING" else (
            "Speaking..." if self.speaking else ("Thinking..." if self.state == "THINKING" else self.state)
        )
        p.drawText(QRectF(cx - 60, sy + mic_btn_r + 4, 120, 14), Qt.AlignmentFlag.AlignCenter, stat_lbl)

        # ── 8. HORIZONTAL EQUALIZER WAVES FLANKING THE MIC BUTTON ─────────────
        flank_n = 28
        bw = 4
        bgap = 2
        # Left horizontal equalizer wave
        for i in range(flank_n):
            dist_f = i / float(flank_n)
            env = math.sin(dist_f * math.pi) ** 0.85
            hgt = max(2, int(4 + (16.0 * env + amp * 22.0 * env) * (0.6 + 0.4 * math.sin(self._tick * 0.25 + i * 0.5))))
            bx = cx - mic_btn_r - 18 - (flank_n - i) * (bw + bgap)
            p.fillRect(QRectF(bx, sy - hgt / 2, bw, hgt), QColor(c_bright if amp > 0.04 else c_main))

        # Right horizontal equalizer wave
        for i in range(flank_n):
            dist_f = i / float(flank_n)
            env = math.sin(dist_f * math.pi) ** 0.85
            hgt = max(2, int(4 + (16.0 * env + amp * 22.0 * env) * (0.6 + 0.4 * math.sin(self._tick * 0.25 + i * 0.5))))
            bx = cx + mic_btn_r + 18 + i * (bw + bgap)
            p.fillRect(QRectF(bx, sy - hgt / 2, bw, hgt), QColor(c_bright if amp > 0.04 else c_main))

        p.end()

class IconManager:
    """Extracts, crops and caches icons from config/mj or config/mj.png (40 holographic icons)."""
    _cache: dict[tuple[str, int], QPixmap] = {}
    _raw_crops: dict[str, object] = {}
    _loaded = False

    _ALIASES = {
        "mem": "memory", "ram": "memory", "net": "network", "temp": "temperature",
        "tmp": "temperature", "mic": "mic_active", "microphone": "mic_active",
        "stop": "interrupt", "abort": "interrupt", "upload": "file_upload",
        "drop": "file_upload", "cfg": "settings", "gear": "settings",
        "bot": "ai_core", "shield": "security", "sec": "security",
        "speak": "voice", "audio": "voice"
    }

    @classmethod
    def _ensure_loaded(cls):
        if cls._loaded:
            return
        cls._loaded = True
        icon_path = CONFIG_DIR / "mj.png"
        if not icon_path.exists():
            return
        try:
            from PIL import Image
            im = Image.open(icon_path).convert("RGBA")
            W, H = im.size
            cw, ch = W / 10.0, H / 4.0
            mapping = {
                "chat": (0, 0), "search": (0, 1), "generate": (0, 2), "analyze": (0, 3), "automation": (0, 4),
                "code": (0, 5), "system": (0, 6), "ai_core": (0, 7), "security": (0, 8), "protocol": (0, 9),
                "voice": (1, 0), "vision": (1, 1), "file_upload": (1, 2), "command": (1, 3), "mic_active": (1, 4),
                "interrupt": (1, 5), "settings": (1, 6), "news": (1, 7), "user": (1, 8), "help": (1, 9),
                "cpu": (2, 0), "memory": (2, 1), "network": (2, 2), "gpu": (2, 3), "temperature": (2, 4),
                "time": (2, 5), "date": (2, 6), "up_down": (2, 7), "wifi": (2, 8), "storage": (2, 9),
                "start": (3, 0), "pause": (3, 1), "stop": (3, 2), "refresh": (3, 3), "delete": (3, 4),
                "edit": (3, 5), "folder": (3, 6), "download": (3, 7), "lock": (3, 8), "unlock": (3, 9)
            }
            for name, (r, c) in mapping.items():
                x0 = int(c * cw + cw * 0.08)
                y0 = int(r * ch + ch * 0.05)
                x1 = int((c + 1) * cw - cw * 0.08)
                y1 = int((r + 1) * ch - ch * 0.22)
                cls._raw_crops[name] = im.crop((x0, y0, x1, y1))
        except Exception as e:
            print(f"[IconManager] ⚠️ Error loading icon sheet: {e}")

    @classmethod
    def get_pixmap(cls, name: str, size: int = 32) -> QPixmap | None:
        cls._ensure_loaded()
        name_clean = (name or "").lower().strip()
        name_clean = cls._ALIASES.get(name_clean, name_clean)
        key = (name_clean, size)
        if key in cls._cache:
            return cls._cache[key]
        raw = cls._raw_crops.get(name_clean)
        if raw is None:
            return None
        try:
            import io
            from PIL import Image
            scaled = raw.resize((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            scaled.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            cls._cache[key] = px
            return px
        except Exception:
            return None


class MetricCard(QWidget):
    """
    Rich sci-fi metric card with circular glowing icon, label, numeric value,
    and live real-time sparkline area chart.
    """
    def __init__(self, label: str, color: str = C.PRI, icon_name: str = "", parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._icon_name = icon_name or label.lower()
        self._value = 0.0       # 0–100
        self._text  = "--"
        self._history: list[float] = [12.0 + random.uniform(-4, 4) for _ in range(16)]
        self.setFixedHeight(44)
        self.setMinimumWidth(150)

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        self._value = v
        self._text  = text
        self._history.append(v)
        if len(self._history) > 18:
            self._history.pop(0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # Background Card Frame
        p.setBrush(QBrush(QColor("#010e17")))
        p.setPen(QPen(QColor(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 6, 6)

        # Icon on Left (from IconManager)
        px = IconManager.get_pixmap(self._icon_name, 30)
        if px and not px.isNull():
            p.drawPixmap(6, 7, px)
        else:
            p.setBrush(QBrush(QColor(self._color)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(10, 10, 24, 24)

        # Label Text
        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(40, 6, 44, 13), Qt.AlignmentFlag.AlignLeft, self._label)

        # Numeric Value Text
        if self._value > 85:
            val_col = qcol(C.RED)
        elif self._value > 65:
            val_col = qcol(C.ACC)
        else:
            val_col = qcol(self._color)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(val_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(40, 20, 52, 17), Qt.AlignmentFlag.AlignLeft, self._text)

        # Sparkline Area Chart on Right
        chart_x = 92
        chart_w = W - chart_x - 7
        chart_y = 7
        chart_h = H - 14

        if chart_w > 18 and len(self._history) > 1:
            step = chart_w / (len(self._history) - 1)
            points = []
            for i, val in enumerate(self._history):
                norm = max(0.06, min(0.94, val / 100.0))
                pt_x = chart_x + i * step
                pt_y = chart_y + chart_h * (1.0 - norm)
                points.append(QPointF(pt_x, pt_y))

            # Filled area gradient
            path = QPainterPath()
            path.moveTo(chart_x, chart_y + chart_h)
            for pt in points:
                path.lineTo(pt)
            path.lineTo(chart_x + chart_w, chart_y + chart_h)
            path.closeSubpath()

            grad = QLinearGradient(0, chart_y, 0, chart_y + chart_h)
            fill_col = QColor(val_col); fill_col.setAlpha(85)
            base_col = QColor(val_col); base_col.setAlpha(10)
            grad.setColorAt(0.0, fill_col)
            grad.setColorAt(1.0, base_col)

            p.setBrush(QBrush(grad))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)

            # Glowing sparkline stroke
            stroke_col = QColor(val_col); stroke_col.setAlpha(220)
            p.setPen(QPen(stroke_col, 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(len(points) - 1):
                p.drawLine(points[i], points[i + 1])

        p.end()


# MetricBar alias for backward compatibility
MetricBar = MetricCard


class LeftNavButton(QPushButton):
    """Glowing sci-fi action button with glowing icon, title, subtitle, and hover states."""
    def __init__(self, title: str, subtitle: str, icon_name: str, active: bool = False, parent=None):
        super().__init__(parent)
        self.title = title
        self.subtitle = subtitle
        self.icon_name = icon_name
        self._active = active
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_active(self, act: bool):
        self._active = act
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        hovered = self.underMouse()
        if self._active:
            bg_col = QColor("#1e1402")
            border_col = QColor("#ffaa00")
            t_col = QColor("#ffd700")
            sub_col = QColor("#cc8800")
        elif hovered:
            bg_col = QColor("#001826")
            border_col = QColor(C.PRI)
            t_col = QColor(C.WHITE)
            sub_col = QColor(C.PRI_DIM)
        else:
            bg_col = QColor("#000c14")
            border_col = QColor(C.BORDER)
            t_col = QColor(C.TEXT)
            sub_col = QColor(C.TEXT_DIM)

        p.setBrush(QBrush(bg_col))
        p.setPen(QPen(border_col, 1.2 if (self._active or hovered) else 1.0))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 5, 5)

        # Draw Icon
        px = IconManager.get_pixmap(self.icon_name, 26)
        if px and not px.isNull():
            p.drawPixmap(5, 5, px)

        # Title & Subtitle
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(t_col, 1))
        p.drawText(QRectF(36, 4, W - 40, 14), Qt.AlignmentFlag.AlignLeft, self.title)

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(sub_col, 1))
        p.drawText(QRectF(36, 18, W - 40, 13), Qt.AlignmentFlag.AlignLeft, self.subtitle)

        p.end()


class StatusBadge(QWidget):
    """Glowing status indicator card with icon and active indicator dot."""
    def __init__(self, title: str, status: str, icon_name: str, color: str = C.GREEN, parent=None):
        super().__init__(parent)
        self.title = title
        self.status = status
        self.icon_name = icon_name
        self.color = color
        self.setFixedHeight(34)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(QColor("#000d14")))
        p.setPen(QPen(QColor(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        # Icon from mj.png
        px = IconManager.get_pixmap(self.icon_name, 22)
        if px and not px.isNull():
            p.drawPixmap(6, 6, px)
        else:
            # Fallback: coloured dot
            p.setBrush(QBrush(qcol(self.color, 160)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(8, 8, 18, 18)

        # Title
        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(self.color), 1))
        p.drawText(QRectF(32, 4, W - 66, 13), Qt.AlignmentFlag.AlignLeft, self.title)

        # Status text
        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(32, 17, W - 66, 13), Qt.AlignmentFlag.AlignLeft, self.status)

        # Active indicator dot (pulsing green / status colour)
        dot_col = qcol(self.color)
        p.setBrush(QBrush(dot_col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(W - 18, 10, 12, 12)
        # Inner glow
        glow_col = qcol(self.color, 90)
        p.setBrush(QBrush(glow_col))
        p.drawEllipse(W - 16, 12, 8, 8)

        p.end()

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # Cap scrollback so an hours-long session can't grow the document
        # without bound — keeps memory flat and every insert cheap. Oldest
        # lines drop off the top automatically.
        self.document().setMaximumBlockCount(600)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "jarvis"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    # category -> (mj.png icon name, colour)
    "image":   ("vision",      "#00d4ff"), "video":   ("analyze",    "#ff6b00"),
    "audio":   ("voice",       "#cc44ff"), "pdf":     ("file_upload", "#ff4444"),
    "word":    ("file_upload", "#4488ff"), "excel":   ("storage",    "#44bb44"),
    "code":    ("code",        "#ffcc00"), "archive": ("folder",     "#ff8844"),
    "pptx":    ("analyze",     "#ff6622"), "text":    ("file_upload", "#aaaaaa"),
    "data":    ("storage",     "#88ddff"), "unknown": ("folder",     "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)
    cleared       = pyqtSignal()   # the ✕ was pressed — the loaded file is gone

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering or dragging a file over the zone. When idle, skip the repaint
        # entirely instead of redrawing the whole zone 25×/s forever — that idle
        # repaint held the GIL and stole time from the audio/response threads.
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()
        self.cleared.emit()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

        p.end()

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        px = IconManager.get_pixmap("file_upload", 32)
        if px and not px.isNull():
            p.setOpacity(1.0 if hover else 0.75)
            p.drawPixmap(int(cx - 16), int(cy - 24), px)
            p.setOpacity(1.0)
        else:
            col = qcol(C.PRI_DIM if not hover else C.PRI)
            p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
            p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
            p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
            p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))

        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 28, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        px = IconManager.get_pixmap("download", 36) or IconManager.get_pixmap("file_upload", 36)
        if px and not px.isNull():
            p.drawPixmap(int(cx - 18), int(cy - 28), px)
        else:
            p.setPen(QPen(qcol(C.PRI), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 6))
            p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
            p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 14, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        # Icon from mj.png sprite sheet (fallback: text block)
        px = IconManager.get_pixmap(icon, 44)
        if px and not px.isNull():
            p.drawPixmap(int(block_x + (block_w - 44) / 2), int((H - 44) / 2), px)
        else:
            p.setFont(QFont("Courier New", 22, QFont.Weight.Bold))
            p.setPen(QPen(qcol(icon_col), 1))
            p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, "◈")

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","WINDOWS"),("mac","MACOS"),("linux","LINUX")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (small white circle)
    around the wheel to choose from ALL hues. The filled circle in the centre
    is a live preview of the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y goes down — flip to math axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # centre preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # draggable handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str)   # assistant_name, user_name, ui_color, voice
    _OW, _OH = 400, 588

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("◈ CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from memory.config_manager import AVAILABLE_VOICES, DEFAULT_VOICE
        lay.addSpacing(4)
        lay.addWidget(_lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice   = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self._on_voice_pick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        lay.addLayout(voice_row)
        self._refresh_voice_btns()

        # ── UI colour — colour wheel ─────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — live preview; MainWindow wires it

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── voice selection ──────────────────────────────────────────────────────
    def _on_voice_pick(self, name: str):
        self._sel_voice = name
        self._refresh_voice_btns()

    def _refresh_voice_btns(self):
        """Highlight the selected voice pill; dim the rest."""
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── colour flow ──────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Updates the selected colour; hex box + wheel stay in sync, theme is live-previewed."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # While dragging: update the hex box, don't apply the theme yet
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Handle released → live-preview the whole interface
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # If a preview was applied, revert to the colour from launch
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR, self._sel_voice)
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("◈ PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)
        return row

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone JARVIS listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'JARVIS can't hear me' is usually 'JARVIS is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed
    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("◈ AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)
            lay.addWidget(box)
            return box

        self._in_box  = _row("MICROPHONE — what JARVIS hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row("SPEAKERS — what JARVIS talks through",
                             "output", get_output_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything JARVIS has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel("◈ WHAT JARVIS REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"memory/long_term.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self._rebuild)


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class EnglishTutorOverlay(QWidget):
    """
    English Tutor / Speaking Mentor panel.

    Shows below/around the central AI Core when the ENGLISH TUTOR nav is active:
      - Current topic & question
      - Listening / Analyzing / Feedback state lights
      - Four separate scores (Grammar / Vocabulary / Fluency / Pronunciation)
      - Word practice + sentence shadowing buttons
      - Daily coaching mode
    Uses config/mj.png icons only — never jarvis.ico, never emoji.
    """

    # Actions emitted to the backend (JarvisLive wires these):
    #   request_speak(text)  — make JARVIS speak
    #   request_record()     — start mic capture for tutor
    #   request_feedback()   — analyze captured audio + transcript
    speak_requested    = pyqtSignal(str)
    record_requested   = pyqtSignal()
    feedback_requested = pyqtSignal()
    next_question_requested = pyqtSignal()

    # ── Thread-safe update channels ─────────────────────────────────────────
    # The tutor is driven from the asyncio loop and from worker threads (see
    # JarvisLive), and touching a QWidget — least of all its QTextDocument —
    # from a foreign thread is what produced
    #   "QObject: Cannot create children for a parent that is in a different
    #    thread. (Parent is QTextDocument…)"
    # followed by the window dying. These signals are the ONLY entry point for
    # the public setters below: emitting a signal is safe from any thread, and
    # the connected slots are guaranteed to run on the Qt main thread.
    _state_sig    = pyqtSignal(str)
    _question_sig = pyqtSignal(str, str)
    _feedback_sig = pyqtSignal(dict)
    _progress_sig = pyqtSignal(dict)
    _hearing_sig  = pyqtSignal(str)

    _W, _H = 620, 384

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            EnglishTutorOverlay {{
                background: rgba(1, 10, 18, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 8px;
            }}
        """)
        self.setFixedSize(self._W, self._H)
        self._state = "IDLE"   # IDLE | LISTENING | ANALYZING | FEEDBACK | PRACTICE | SHADOWING | DAILY_COACH

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        # ── Header ───────────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(8)
        _tutor_icon = IconManager.get_pixmap("voice", 24)
        icon_lbl = QLabel()
        if _tutor_icon and not _tutor_icon.isNull():
            icon_lbl.setPixmap(_tutor_icon)
        icon_lbl.setFixedSize(24, 24)
        hdr.addWidget(icon_lbl)

        title_col = QVBoxLayout(); title_col.setSpacing(0)
        self._title_lbl = QLabel("ENGLISH TUTOR")
        self._title_lbl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        title_col.addWidget(self._title_lbl)
        self._sub_lbl = QLabel("Speaking Mentor")
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        title_col.addWidget(self._sub_lbl)
        hdr.addLayout(title_col)
        hdr.addStretch()

        self._state_lbl = QLabel("IDLE")
        self._state_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._state_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._state_lbl)
        lay.addLayout(hdr)

        # ── Topic / Question ─────────────────────────────────────────────────
        self._topic_lbl = QLabel("CURRENT TOPIC: General Conversation")
        self._topic_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._topic_lbl.setStyleSheet(
            f"color: {C.ACC2}; background: {C.PANEL2}; border: 1px solid {C.BORDER}; "
            f"border-radius: 3px; padding: 3px 6px;"
        )
        lay.addWidget(self._topic_lbl)

        self._question_lbl = QLabel("Tell me about your day so far.")
        self._question_lbl.setFont(QFont("Courier New", 8))
        self._question_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: {C.DARK}; border: 1px solid {C.BORDER}; "
            f"border-radius: 3px; padding: 4px 6px;"
        )
        self._question_lbl.setWordWrap(True)
        lay.addWidget(self._question_lbl)

        # ── State lights row ─────────────────────────────────────────────────
        status_row = QHBoxLayout(); status_row.setSpacing(12)
        self._light_listen = self._make_light("LISTENING", C.PRI)
        self._light_analyze = self._make_light("ANALYZING", C.ACC2)
        self._light_feedback = self._make_light("FEEDBACK", C.GREEN)
        status_row.addWidget(self._light_listen)
        status_row.addWidget(self._light_analyze)
        status_row.addWidget(self._light_feedback)
        status_row.addStretch()
        lay.addLayout(status_row)

        # ── Scores row ───────────────────────────────────────────────────────
        self._scores_row = QHBoxLayout(); self._scores_row.setSpacing(8)
        self._score_grammar  = self._make_score("GRAMMAR", C.PRI)
        self._score_vocab    = self._make_score("VOCAB", C.ACC2)
        self._score_fluency  = self._make_score("FLUENCY", C.GREEN)
        self._score_prono    = self._make_score("PRONUNO", "#c054ff")
        self._score_overall  = self._make_score("OVERALL", C.ACC)
        for w in [self._score_grammar, self._score_vocab, self._score_fluency,
                  self._score_prono, self._score_overall]:
            self._scores_row.addWidget(w, stretch=1)
        lay.addLayout(self._scores_row)

        # ── Feedback area ────────────────────────────────────────────────────
        self._feedback_view = QTextEdit()
        self._feedback_view.setReadOnly(True)
        self._feedback_view.setFixedHeight(86)
        self._feedback_view.setFont(QFont("Courier New", 7))
        self._feedback_view.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 4px 6px;
            }}
        """)
        lay.addWidget(self._feedback_view)

        # ── Action buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(5)

        self._practice_btn = self._make_btn("PRACTICE", "voice")
        self._practice_btn.clicked.connect(self._on_practice)
        btn_row.addWidget(self._practice_btn)

        self._shadow_btn = self._make_btn("SHADOWING", "code")
        self._shadow_btn.clicked.connect(self._on_shadowing)
        btn_row.addWidget(self._shadow_btn)

        self._daily_btn = self._make_btn("DAILY COACH", "ai_core")
        self._daily_btn.clicked.connect(self._on_daily)
        btn_row.addWidget(self._daily_btn)

        self._hear_btn = self._make_btn("HEAR", "voice")
        self._hear_btn.clicked.connect(self._on_hear)
        btn_row.addWidget(self._hear_btn)

        self._next_btn = self._make_btn("NEXT TOPIC", "refresh")
        self._next_btn.clicked.connect(lambda: self.next_question_requested.emit())
        btn_row.addWidget(self._next_btn)

        btn_row.addStretch()
        self._close_btn = self._make_btn("CLOSE", "stop")
        self._close_btn.clicked.connect(self.hide)
        btn_row.addWidget(self._close_btn)
        lay.addLayout(btn_row)

        # Route every public setter through its signal (see the signal block for
        # why). Queued connections guarantee the slots below run on the Qt main
        # thread no matter which thread emitted.
        self._state_sig.connect(self._apply_state_ui)
        self._question_sig.connect(self._apply_question_ui)
        self._feedback_sig.connect(self._apply_feedback_ui)
        self._progress_sig.connect(self._apply_progress_ui)
        self._hearing_sig.connect(self._apply_hearing_ui)

        self.hide()

    # ── UI helpers ──────────────────────────────────────────────────────────

    def _make_light(self, label: str, color: str) -> QWidget:
        w = QWidget()
        w.setFixedSize(112, 22)
        l = QLabel(f"●  {label}")
        l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        l.setStyleSheet(f"color: {color}; background: transparent;")
        lay = QHBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(l)
        lay.addStretch()
        return w

    def _make_score(self, label: str, color: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 3px;"
        )
        v = QVBoxLayout(w); v.setContentsMargins(6, 3, 6, 3); v.setSpacing(0)
        l = QLabel(label)
        l.setFont(QFont("Courier New", 6, QFont.Weight.Bold))
        l.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        v.addWidget(l)
        val = QLabel("--")
        val.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        val.setStyleSheet(f"color: {color}; background: transparent;")
        val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(val)
        setattr(w, "_value_lbl", val)
        return w

    def _make_btn(self, text: str, icon_name: str) -> QPushButton:
        b = QPushButton(f"  {text}")
        b.setFixedHeight(26)
        b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        px = IconManager.get_pixmap(icon_name, 14)
        if px and not px.isNull():
            b.setIcon(QIcon(px))
            b.setIconSize(QSize(14, 14))
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """)
        return b

    # ── Public API (called from MainWindow / JarvisLive) ───────────────────

    def set_state(self, state: str):
        """Thread-safe. Queue a state change for the Qt main thread."""
        self._state_sig.emit(state)

    def _apply_state_ui(self, state: str):
        """Slot — runs on the Qt main thread. state: IDLE|LISTENING|ANALYZING|FEEDBACK."""
        self._state = state
        colors = {
            "IDLE": C.TEXT_DIM, "LISTENING": C.PRI,
            "ANALYZING": C.ACC2, "FEEDBACK": C.GREEN,
            "PRACTICE": C.ACC, "SHADOWING": C.ACC, "DAILY_COACH": C.ACC,
        }
        c = colors.get(state, C.TEXT_DIM)
        self._state_lbl.setText(state)
        self._state_lbl.setStyleSheet(f"color: {c}; background: transparent;")

        self._set_light(self._light_listen, state == "LISTENING")
        self._set_light(self._light_analyze, state == "ANALYZING")
        self._set_light(self._light_feedback, state == "FEEDBACK")

    def _set_light(self, widget: QWidget, on: bool):
        lbl = widget.findChild(QLabel)
        if lbl:
            cur = lbl.text().replace("●  ", "").replace("○  ", "")
            marker = "●  " if on else "○  "
            lbl.setText(f"{marker}{cur}")

    def set_question(self, question: str, topic: str = "General Conversation"):
        """Thread-safe. Queue a new question for the Qt main thread."""
        self._question_sig.emit(str(question), str(topic))

    def _apply_question_ui(self, question: str, topic: str):
        """Slot — runs on the Qt main thread."""
        self._topic_lbl.setText(f"CURRENT TOPIC: {topic}")
        self._question_lbl.setText(question)
        self._feedback_view.clear()

    def show_feedback(self, report: dict):
        """Thread-safe. Queue a report for the Qt main thread."""
        self._feedback_sig.emit(dict(report or {}))

    def _apply_feedback_ui(self, report: dict):
        """Slot — runs on the Qt main thread. Combined tutor report."""
        scores = report.get("scores", {})
        for name, key, widget in [
            ("GRAMMAR", "grammar", self._score_grammar),
            ("VOCAB", "vocabulary", self._score_vocab),
            ("FLUENCY", "fluency", self._score_fluency),
            ("PRONUNO", "pronunciation", self._score_prono),
            ("OVERALL", "overall", self._score_overall),
        ]:
            val = scores.get(key) if isinstance(scores, dict) else None
            lbl = getattr(widget, "_value_lbl")
            lbl.setText(f"{val:.0f}/10" if isinstance(val, (int, float)) else "--")

        parts = []
        gf = report.get("overall_feedback", "")
        if gf:
            parts.append(gf)

        # Fluency
        fl = report.get("fluency_issues", {})
        if isinstance(fl, dict):
            fillers = fl.get("filler_words", [])
            if fillers:
                parts.append("FILLER: " + ", ".join(f'"{f.get("word","")}"×{f.get("count",0)}' for f in fillers[:3]))
            sug = fl.get("suggestion", "")
            if sug:
                parts.append(sug)

        # Pronunciation
        pr = report.get("pronunciation", {})
        if isinstance(pr, dict):
            if not pr.get("available"):
                parts.append("PRONUNCIATION: " + pr.get("message", "Unavailable for this response."))
            else:
                for i in pr.get("issues", [])[:2]:
                    parts.append(
                        f'PRONUNCIATION "{i.get("word","")}": {i.get("issue","")} -> {i.get("practice","")}'
                    )

        # WPM
        pace = report.get("speaking_speed", {})
        if isinstance(pace, dict) and pace.get("wpm"):
            parts.append(f'SPEED: {pace.get("wpm")} WPM — {pace.get("assessment","")}')

        self._feedback_view.setPlainText("\n".join(parts) if parts else "No feedback available.")

    def show_progress(self, progress: dict):
        """Thread-safe. Queue a progress report for the Qt main thread."""
        self._progress_sig.emit(dict(progress or {}))

    def _apply_progress_ui(self, progress: dict):
        """Slot — runs on the Qt main thread. Previous vs current comparison."""
        parts = []
        imp = progress.get("improvements", [])
        if imp:
            parts.append("IMPROVEMENTS vs previous sessions:")
            for k in imp:
                parts.append(f"  + {k.upper()}")
        else:
            parts.append("No significant changes from previous sessions yet.")
        self._feedback_view.setPlainText("\n".join(parts))

    def set_hearing(self, word: str):
        """Thread-safe. Queue a practice word for the Qt main thread."""
        self._hearing_sig.emit(str(word))

    def _apply_hearing_ui(self, word: str):
        """Slot — runs on the Qt main thread. Show the practice word."""
        self._question_lbl.setText(f'TARGET: {word}\n\nYOUR TURN — repeat the word.')

    # ── Button handlers ─────────────────────────────────────────────────────

    def _on_practice(self):
        self.speak_requested.emit("START_PRACTICE")
        self.set_state("PRACTICE")

    def _on_shadowing(self):
        self.speak_requested.emit("START_SHADOWING")
        self.set_state("SHADOWING")

    def _on_daily(self):
        self.speak_requested.emit("START_DAILY_COACH")
        self.set_state("DAILY_COACH")

    def _on_hear(self):
        self.speak_requested.emit("HEAR_WORD")

    def show_overlay(self):
        self.show()
        self.raise_()


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("◈ PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self._lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings (like the 3D-printer suite) into the plugins folder and "
                "it will show up here.", 9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont("Courier New", fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Courier New", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Courier New", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config
        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)                 # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install qrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig    = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _phone_conn_sig = pyqtSignal()           # phone paired (fired from the asyncio thread)

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle    = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual    = None   # callable: () -> None — manual sleep/wake
        self.wake_get_state    = None   # callable: () -> dict {enabled, awake, ready}
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        # Footer news ticker
        self._news_tmr = QTimer(self)
        self._news_tmr.timeout.connect(self._update_news)
        self._news_tmr.start(5000)
        self._update_news()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._phone_conn_sig.connect(self._apply_phone_connected)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        # English Tutor overlay (child of central widget, positioned below HUD)
        self._tutor_overlay = EnglishTutorOverlay(self.centralWidget())
        self._tutor_active = False

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # English Tutor overlay — reposition below HUD
        if hasattr(self, '_tutor_overlay') and self._tutor_overlay.isVisible():
            self._position_tutor_overlay()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")

    def _update_news(self):
        """Rotate technical telemetry news tickers in the footer news strip."""
        try:
            if not hasattr(self, "_footer_news"):
                return
            snap = _metrics.snapshot()
            cpu = snap["cpu"]
            mem = snap["mem"]
            net = snap["net"]
            gpu = snap["gpu"]
            tmp = snap["tmp"]
            gpu_s = f"{gpu:.0f}%" if gpu >= 0 else "N/A"
            tmp_s = f"{tmp:.0f}°C" if tmp >= 0 else "N/A"
            net_s = f"{net*1024:.0f}KB/s" if net < 1.0 else f"{net:.1f}MB/s"
            msgs = [
                f"CPU LOAD ▸ {cpu:.0f}%",
                f"MEMORY ▸ {mem:.0f}%",
                f"NETWORK ▸ {net_s}",
                f"GPU ▸ {gpu_s}",
                f"TEMP ▸ {tmp_s}",
                f"AI CORE ▸ {APP_PROTOCOL} PROTOCOL ONLINE",
            ]
            tag = int(time.time()) // 5 % len(msgs)
            self._footer_news.setText(f"NEWS FEED ▸ {msgs[tag]}")
        except Exception:
            pass


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        # ── Left cluster: version badge + settings icon ────────────────────────
        left_col = QHBoxLayout()
        left_col.setSpacing(6)
        ver = QLabel(APP_VERSION)
        ver.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        ver.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        left_col.addWidget(ver)

        self._drawer_btn = QPushButton()
        self._drawer_btn.setFixedSize(26, 26)
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        _settings_px = IconManager.get_pixmap("settings", 20)
        if _settings_px and not _settings_px.isNull():
            self._drawer_btn.setIcon(QIcon(_settings_px))
            self._drawer_btn.setIconSize(QSize(20, 20))
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        left_col.addWidget(self._drawer_btn)
        lay.addLayout(left_col)
        lay.addStretch()

        # ── Center: title + subtitle ───────────────────────────────────────────
        mid = QVBoxLayout(); mid.setSpacing(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont("Courier New", 17, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = ("Just A Rather Very Intelligent System"
                     if _disp in ("JARVIS", "J.A.R.V.I.S")
                     else "Personal AI Assistant")
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        lay.addLayout(mid)
        lay.addStretch()

        # ── Right cluster: clock + protocol indicator ──────────────────────────
        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 8)
        lay.setSpacing(5)

        # ── SYS MONITOR ───────────────────────────────────────────────────────
        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 3px;")
        lay.addWidget(hdr)

        self._bar_cpu = MetricCard("CPU", C.PRI, "cpu")
        self._bar_mem = MetricCard("MEM", C.ACC2, "memory")
        self._bar_net = MetricCard("NET", C.GREEN, "network")
        self._bar_gpu = MetricCard("GPU", "#c054ff", "gpu")
        self._bar_tmp = MetricCard("TEMP", "#ff4455", "temperature")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        # ── Telemetry info box ────────────────────────────────────────────────
        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 4, 6, 4)
        ip_lay.setSpacing(2)

        self._uptime_lbl = QLabel("UP   00:00:00")
        self._uptime_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC 0")
        self._proc_lbl.setFont(QFont("Courier New", 7))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN 11", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS   {os_name}")
        os_lbl.setFont(QFont("Courier New", 7))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(2)

        # ── Action Buttons ────────────────────────────────────────────────────
        self._nav_chat = LeftNavButton("CHAT", "Talk to JARVIS", "chat", active=True)
        self._nav_search = LeftNavButton("SEARCH", "Web & News", "search")
        self._nav_gen = LeftNavButton("GENERATE", "Images / Videos", "generate")
        self._nav_analyze = LeftNavButton("ANALYZE", "Files & Data", "analyze")
        self._nav_auto = LeftNavButton("AUTOMATION", "System Tasks", "automation")
        self._nav_code = LeftNavButton("CODE", "Write & Debug", "code")
        self._nav_system = LeftNavButton("SYSTEM", "Control Device", "system")
        self._nav_tutor = LeftNavButton("TUTOR", "English Speaking", "voice")

        self._nav_chat.clicked.connect(lambda: self._on_nav_clicked("chat"))
        self._nav_search.clicked.connect(lambda: self._on_nav_clicked("search"))
        self._nav_gen.clicked.connect(lambda: self._on_nav_clicked("generate"))
        self._nav_analyze.clicked.connect(lambda: self._on_nav_clicked("analyze"))
        self._nav_auto.clicked.connect(lambda: self._on_nav_clicked("automation"))
        self._nav_code.clicked.connect(lambda: self._on_nav_clicked("code"))
        self._nav_system.clicked.connect(lambda: self._on_nav_clicked("system"))
        self._nav_tutor.clicked.connect(lambda: self._on_nav_clicked("tutor"))

        for btn in [self._nav_chat, self._nav_search, self._nav_gen,
                    self._nav_analyze, self._nav_auto, self._nav_code, self._nav_system,
                    self._nav_tutor]:
            lay.addWidget(btn)

        lay.addSpacing(2)

        # ── Status Badges ─────────────────────────────────────────────────────
        self._badge_core = StatusBadge("AI CORE", "ACTIVE", "ai_core", "#ffaa00")
        self._badge_sec = StatusBadge("SECURITY", "CLEAN", "security", C.GREEN)
        self._badge_proto = StatusBadge("PROTOCOL", "CONNECTED", "protocol", "#c054ff")
        self._badge_voice = StatusBadge("VOICE", "READY", "voice", C.ACC2)

        for badge in [self._badge_core, self._badge_sec, self._badge_proto, self._badge_voice]:
            lay.addWidget(badge)

        return w

    def _on_nav_clicked(self, key: str):
        # Update active state visually
        for b, k in [(self._nav_chat, "chat"), (self._nav_search, "search"),
                     (self._nav_gen, "generate"), (self._nav_analyze, "analyze"),
                     (self._nav_auto, "automation"), (self._nav_code, "code"),
                     (self._nav_system, "system"), (self._nav_tutor, "tutor")]:
            b.set_active(k == key)

        if key == "chat":
            self._input.setFocus()
        elif key == "search":
            self._input.setText("Search: ")
            self._input.setFocus()
        elif key == "generate":
            self._input.setText("Generate image: ")
            self._input.setFocus()
        elif key == "analyze":
            if self._current_file:
                self._send_command(f"Analyze the loaded file: {self._current_file}")
            else:
                self._drop_zone._browse()
        elif key == "automation":
            self._input.setText("Automate: ")
            self._input.setFocus()
        elif key == "code":
            self._input.setText("Write code for: ")
            self._input.setFocus()
        elif key == "system":
            self._send_command("System status report")
        elif key == "tutor":
            # Toggle tutor overlay on/off
            if self._tutor_overlay.isVisible():
                self._tutor_overlay.hide()
                self._tutor_active = False
            else:
                self._tutor_active = True
                self._position_tutor_overlay()
                self._tutor_overlay.show_overlay()
                # Request tutor start from the backend
                self._send_command("Start English Tutor session")

    def _send_command(self, cmd_text: str):
        if not cmd_text: return
        self._log.append_log(f"You: {cmd_text}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd_text,), daemon=True).start()
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        self._drop_zone.cleared.connect(self._on_file_cleared)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("  INTERRUPT  [ESC]")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _int_px = IconManager.get_pixmap("interrupt", 18)
        if _int_px and not _int_px.isNull():
            self._interrupt_btn.setIcon(QIcon(_int_px))
            self._interrupt_btn.setIconSize(QSize(18, 18))
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: #200010; border: 1px solid #ff6688;
            }}
            QPushButton:pressed {{
                background: #300018;
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _mic_px = IconManager.get_pixmap("mic_active", 18)
        if _mic_px and not _mic_px.isNull():
            self._mute_btn.setIcon(QIcon(_mic_px))
            self._mute_btn.setIconSize(QSize(18, 18))
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the settings header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        def _drawer_btn(text, icon_name, style, height=26):
            btn = QPushButton(f"  {text}")
            btn.setFixedHeight(height)
            btn.setFont(QFont("Courier New", 7))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(style)
            px = IconManager.get_pixmap(icon_name, 16)
            if px and not px.isNull():
                btn.setIcon(QIcon(px))
                btn.setIconSize(QSize(16, 16))
            return btn

        remote_btn = _drawer_btn("REMOTE CONTROL", "command", _BTN_STYLE_PRI, 30)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        fs_btn = _drawer_btn("FULLSCREEN  [F11]", "system", _BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        sc_btn = _drawer_btn("CREATE DESKTOP SHORTCUT", "folder", _BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = _drawer_btn("AUTO-START: OFF", "start", _BTN_STYLE_DIM)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = _drawer_btn("CUSTOMISE ASSISTANT", "settings", _BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        self._brief_btn = _drawer_btn("BRIEFING: OFF", "news", _BTN_STYLE_DIM)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        # ── Wake word ──────────────────────────────────────────────────────────
        self._wake_btn = _drawer_btn("WAKE WORD", "mic_active", _BTN_STYLE_DIM)
        self._wake_btn.clicked.connect(self._toggle_wake_word)
        lay.addWidget(self._wake_btn)

        self._wake_sleep_btn = _drawer_btn("SLEEP/WAKE", "voice", _BTN_STYLE_DIM)
        self._wake_sleep_btn.clicked.connect(self._tap_wake_manual)
        lay.addWidget(self._wake_sleep_btn)
        self._wake_sleep_btn.hide()

        audio_btn = _drawer_btn("AUDIO DEVICES", "voice", _BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = _drawer_btn("MEMORY", "memory", _BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = _drawer_btn("PLUGINS", "code", _BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        settings_btn = _drawer_btn("PLUGIN SETTINGS", "settings", _BTN_STYLE_DIM)
        settings_btn.clicked.connect(self._open_plugin_settings)
        lay.addWidget(settings_btn)

        w.adjustSize()
        return w

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._refresh_wake_btns()   # resolve wake state on open (lazy)
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        _W = 220
        self._quick_drawer.setFixedWidth(_W)
        self._quick_drawer.adjustSize()
        self._quick_drawer.setGeometry(12, 54, _W, self._quick_drawer.sizeHint().height())

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(34)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        # ── NEWS panel icon (from mj.png) ─────────────────────────────────────
        _news_px = IconManager.get_pixmap("news", 16)
        news_icon = QLabel()
        if _news_px and not _news_px.isNull():
            news_icon.setPixmap(_news_px)
        news_icon.setFixedSize(16, 16)
        lay.addWidget(news_icon)

        self._footer_news = QLabel("NEWS FEED ▸ No new updates")
        self._footer_news.setFont(QFont("Courier New", 7))
        self._footer_news.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._footer_news)
        lay.addStretch()
        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("By Vishal Soni", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _on_file_cleared(self) -> None:
        """The ✕ on the drop zone was pressed — forget the loaded file.

        MainWindow._current_file is the single source of truth the asyncio
        thread reads (via JarvisUI.current_file), so it has to be cleared here
        and not only inside the widget."""
        self._current_file = None
        self._file_hint.setText("No file loaded — drop or click above to upload")

    def notify_phone_connected(self) -> None:
        """Thread-safe. The dashboard calls this from the asyncio thread; the
        overlay it updates is a widget, so the work is queued to the Qt thread."""
        self._phone_conn_sig.emit()

    def _apply_phone_connected(self) -> None:
        """Slot — runs on the Qt main thread."""
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")),
                        "enabled": bool(s.get("enabled")),
                        "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled
            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, '_wake_btn'):
            return
        st = self._wake_state()
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("  WAKE WORD: DOWNLOAD")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("  WAKE WORD: ON")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("  SLEEP NOW" if st["awake"] else "  WAKE NOW")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("  WAKE WORD: OFF")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)
            def _work():
                try:
                    from core.wake_word import install_and_download
                    ok, msg = install_and_download(
                        logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)   # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)
            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice
            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _position_tutor_overlay(self):
        """Position English Tutor overlay below HUD, centered."""
        cw = self.centralWidget()
        pw = EnglishTutorOverlay._W
        ph = EnglishTutorOverlay._H
        x = (cw.width() - pw) // 2
        # Place below the HUD area (center panel top portion)
        split = self._center_split
        if split and split.isVisible():
            y = split.y() + 12
        else:
            y = 60
        # Clamp so it doesn't exceed the panel
        max_y = cw.height() - ph - 4
        y = min(y, max_y)
        self._tutor_overlay.setGeometry(x, y, pw, ph)
        self._tutor_overlay.raise_()

    @property
    def tutor_overlay(self) -> EnglishTutorOverlay:
        """Public access to the tutor overlay for direct window integrations."""
        return self._tutor_overlay

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #140006; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 3px;
                }}
            """)
        else:
            self._mute_btn.setText("  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #001f10; }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self.root = _RootShim(self._app)
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        # A plain attribute read on the window — NOT a call into the widget.
        # This is read from the asyncio thread while a tool runs, and QWidget
        # methods must never be entered from another thread.
        return self._win._current_file

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def on_wake_toggle(self):
        return self._win.on_wake_toggle

    @on_wake_toggle.setter
    def on_wake_toggle(self, cb):
        self._win.on_wake_toggle = cb

    @property
    def on_wake_manual(self):
        return self._win.on_wake_manual

    @on_wake_manual.setter
    def on_wake_manual(self, cb):
        self._win.on_wake_manual = cb

    @property
    def wake_get_state(self):
        return self._win.wake_get_state

    @wake_get_state.setter
    def wake_get_state(self, cb):
        self._win.wake_get_state = cb

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

    # ── English Tutor ────────────────────────────────────────────────────────

    @property
    def tutor_overlay(self):
        return self._win._tutor_overlay

    @property
    def tutor_active(self) -> bool:
        return getattr(self._win, '_tutor_active', False)

    @tutor_active.setter
    def tutor_active(self, v: bool):
        self._win._tutor_active = v
