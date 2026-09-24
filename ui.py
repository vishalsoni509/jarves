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
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
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


class AudioWaveformVisualizer:
    """
    Dedicated audio waveform visualizer for JARVIS.
    Renders two symmetrical waveform wings flanking the central microphone control.
    Reacts to real-time audio amplitude (microphones and TTS speech output)
    with smooth interpolation, peak decay, and state-aware color schemes.
    """
    def __init__(self, bars_per_wing: int = 24):
        self.bars_n = bars_per_wing
        self._peaks_l = [0.0] * bars_per_wing
        self._peaks_r = [0.0] * bars_per_wing

    def render(
        self, p: QPainter, cx: float, sy: float,
        dest_rect: QRectF, amp: float, tick: int,
        state: str, speaking: bool, active: bool
    ) -> None:
        inner_margin = dest_rect.width() * (50.0 / 630.0)
        wing_w = dest_rect.width() * (135.0 / 630.0)

        bw = (wing_w / self.bars_n) * 0.58
        bgap = (wing_w / self.bars_n) * 0.42

        base_h = 2.5
        is_listening = (state == "LISTENING")

        amp_scale = 38.0 if speaking else (32.0 if is_listening else (18.0 if active else 3.5))
        spd = 0.28 if speaking else (0.22 if is_listening else 0.08)

        for side in (-1, 1):
            peaks = self._peaks_l if side < 0 else self._peaks_r
            for i in range(self.bars_n):
                dist_norm = i / max(1, self.bars_n - 1)
                env = math.sin(dist_norm * math.pi) ** 0.82

                phase1 = tick * spd + i * 0.46 * side
                phase2 = tick * (spd * 0.6) - i * 0.32 * side
                harmonic = 0.5 * math.sin(phase1) + 0.5 * math.cos(phase2)
                ripple = 0.35 + 0.65 * abs(harmonic)

                target_h = base_h + env * (amp * amp_scale * (0.35 + 0.65 * ripple) + (2.2 * ripple if active else 0.8))
                target_h = max(2.0, min(52.0, target_h))

                # Smooth peak decay
                if target_h > peaks[i]:
                    peaks[i] = target_h
                else:
                    peaks[i] = max(target_h, peaks[i] * 0.88)

                hgt = peaks[i]

                if side < 0:
                    bx = cx - inner_margin - (i + 1) * bw - i * bgap
                else:
                    bx = cx + inner_margin + i * (bw + bgap)

                # State-aware color palette
                grad = QLinearGradient(0, sy - hgt / 2, 0, sy + hgt / 2)
                if is_listening:
                    grad.setColorAt(0.0, QColor("#e0ffff"))
                    grad.setColorAt(0.35, QColor("#00e5ff"))
                    grad.setColorAt(0.75, QColor("#0088cc"))
                    grad.setColorAt(1.0, QColor("#004488"))
                else:
                    grad.setColorAt(0.0, QColor("#fff8d6"))
                    grad.setColorAt(0.35, QColor("#ffbb00"))
                    grad.setColorAt(0.75, QColor("#ff7700"))
                    grad.setColorAt(1.0, QColor("#ff4400"))

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(grad))
                p.drawRoundedRect(QRectF(bx, sy - hgt / 2, bw, hgt), bw / 2, bw / 2)

                # Subtle pedestal glass reflection
                ref_h = hgt * 0.38
                ref_y = sy + hgt / 2 + 1.5
                ref_grad = QLinearGradient(0, ref_y, 0, ref_y + ref_h)
                if is_listening:
                    ref_grad.setColorAt(0.0, QColor(0, 229, 255, 60 + int(amp * 50)))
                    ref_grad.setColorAt(1.0, QColor(0, 100, 200, 0))
                else:
                    ref_grad.setColorAt(0.0, QColor(255, 180, 0, 60 + int(amp * 50)))
                    ref_grad.setColorAt(1.0, QColor(255, 100, 0, 0))
                p.setBrush(QBrush(ref_grad))
                p.drawRoundedRect(QRectF(bx, ref_y, bw, ref_h), bw / 2, bw / 2)


class HudCanvas(QWidget):
    """
    Center AI Core panel — Fully Procedural Cinematic Golden Holographic JARVIS AI Core.

    No image files used. Renders in real time via Canvas/QPainter:
      - Cinematic dark navy/black environment with radial atmospheric glow
      - Futuristic vertical light panels, holographic side monitors, radar circles
      - Technical HUD markings, data grids, floating ambient particles
      - Large 3D-looking golden holographic globe:
          * Latitude/longitude sphere grid lines
          * Dynamic circuit traces & fragmented golden geometry patches
          * Concentric inner glow rings & orbital paths with traveling nodes
          * Radial scanning effects, glowing scan particles
          * Soft golden volumetric glow & warm amber/orange illumination
      - 'JARVIS' UI text rendered at globe center
      - 3D volumetric particle swarm with perspective projection & audio pulse
      - Concentric 3D gimbal orbital rings with traveling energy nodes
      - Dynamic volumetric golden breathing glow & subtle blue secondary lighting
      - Holographic platform / pedestal projector with vertical light conduits
      - Processing / analyzing laser scan sweep & HUD target reticles
      - Dedicated AudioWaveformVisualizer component with two symmetrical wings
      - Interactive central microphone control with click-to-mute support
      - Real UI typography for JARVIS and dynamic application status
    """

    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self._face_path = (face_path or "").strip()  # kept for API compat; not used
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 340)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        self._tick = 0
        self._live_amp = 0.0
        self._amp_disp = 0.0
        self._dest_rect: QRectF | None = None
        self._waveform = AudioWaveformVisualizer(bars_per_wing=24)

        # 3D Particle Field (85 volumetric particles in spherical space)
        self._particles: list[dict] = []
        random.seed(42)
        for _ in range(85):
            self._particles.append({
                "theta": random.uniform(0, math.tau),
                "phi": random.uniform(-math.pi * 0.45, math.pi * 0.45),
                "r": random.uniform(0.82, 1.34),
                "dtheta": random.uniform(0.008, 0.024) * (1 if random.random() > 0.5 else -1),
                "dphi": random.uniform(0.003, 0.012) * (1 if random.random() > 0.5 else -1),
                "sz": random.uniform(1.4, 3.2),
                "alpha": random.uniform(0.4, 1.0),
                "color_type": random.choices(["gold", "amber", "cyan", "white"], weights=[60, 25, 10, 5])[0]
            })

        # Floating ambient environment particles
        random.seed(99)
        self._env_particles: list[dict] = []
        for _ in range(45):
            self._env_particles.append({
                "x": random.uniform(0.05, 0.95),
                "y": random.uniform(0.05, 0.95),
                "vx": random.uniform(-0.0002, 0.0002),
                "vy": random.uniform(-0.0003, -0.0001),
                "sz": random.uniform(0.8, 2.2),
                "alpha": random.uniform(0.2, 0.7),
                "color": random.choices(["gold", "cyan", "blue"], weights=[55, 30, 15])[0],
            })

        # Circuit trace seeds for globe surface
        random.seed(17)
        self._circuit_seeds: list[dict] = []
        for _ in range(18):
            self._circuit_seeds.append({
                "theta0": random.uniform(0, math.tau),
                "phi0": random.uniform(-math.pi * 0.38, math.pi * 0.38),
                "len": random.randint(4, 9),
                "phase": random.uniform(0, math.tau),
            })

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

    # ── Environment Rendering ─────────────────────────────────────────────────

    def _draw_environment(self, p: QPainter, W: int, H: int, amp: float, active: bool) -> None:
        """Draw the cinematic dark futuristic control-room environment — fully procedural."""
        cx = W / 2.0

        # 1. Deep space base fill
        p.fillRect(self.rect(), QColor("#000308"))

        # 2. Radial atmospheric glow — deep navy/indigo haze
        atm = QRadialGradient(cx, H * 0.48, W * 0.72)
        atm.setColorAt(0.0, QColor(5, 18, 50, 80))
        atm.setColorAt(0.38, QColor(3, 10, 30, 50))
        atm.setColorAt(0.70, QColor(0, 5, 18, 25))
        atm.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(atm))
        p.drawEllipse(QRectF(-W * 0.1, -H * 0.1, W * 1.2, H * 1.2))

        # 3. Subtle tech-grid dot field
        p.setPen(QPen(QColor(0, 80, 140, 18), 1))
        grid_sp = 48
        for gx in range(0, W, grid_sp):
            for gy in range(0, H, grid_sp):
                p.drawPoint(gx, gy)

        # 4. Horizontal scan lines (very faint CRT effect)
        scan_col = QColor(0, 60, 120, 8)
        p.setPen(QPen(scan_col, 1))
        for gy in range(0, H, 4):
            p.drawLine(0, gy, W, gy)

        # 5. Holographic side monitor panels
        self._draw_side_monitor(p, W, H, side=-1, amp=amp, active=active)
        self._draw_side_monitor(p, W, H, side=1, amp=amp, active=active)

        # 6. Floating ambient environment particles
        self._draw_env_particles(p, W, H, amp)

        # 7. Subtle floor base gradient — warm amber glow at bottom
        floor = QLinearGradient(0, H * 0.72, 0, H)
        floor.setColorAt(0.0, QColor(0, 0, 0, 0))
        floor.setColorAt(0.55, QColor(100, 55, 0, 22))
        floor.setColorAt(1.0, QColor(60, 28, 0, 45))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(floor))
        p.drawRect(QRectF(0, H * 0.72, W, H * 0.28))

        # 8. Top edge blue ambient glow
        top_glow = QLinearGradient(0, 0, 0, H * 0.18)
        top_glow.setColorAt(0.0, QColor(0, 60, 140, 35))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(top_glow))
        p.drawRect(QRectF(0, 0, W, H * 0.18))

    def _draw_side_monitor(self, p: QPainter, W: int, H: int, side: int, amp: float, active: bool) -> None:
        """Draw holographic mini-monitor panels on left or right side."""
        t = self._tick
        if side < 0:
            mx = W * 0.06
        else:
            mx = W * 0.94

        # Radar circle monitor
        radar_cx = mx
        radar_cy = H * 0.30
        radar_r  = min(W * 0.065, H * 0.095)

        outer_col = QColor(0, 200, 255, 55 if not active else 80)
        p.setPen(QPen(outer_col, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(radar_cx - radar_r, radar_cy - radar_r, radar_r * 2, radar_r * 2))

        for scale in [0.65, 0.35]:
            rc = QColor(0, 180, 255, 30)
            p.setPen(QPen(rc, 0.8))
            rr = radar_r * scale
            p.drawEllipse(QRectF(radar_cx - rr, radar_cy - rr, rr * 2, rr * 2))

        sweep_a = (t * 1.8) % 360
        sweep_rad = math.radians(sweep_a)
        p.setPen(QPen(QColor(0, 230, 255, 90), 1.2))
        p.drawLine(QPointF(radar_cx, radar_cy),
                   QPointF(radar_cx + math.cos(sweep_rad) * radar_r,
                            radar_cy + math.sin(sweep_rad) * radar_r))

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(8):
            tail_a = sweep_a - i * 5.5
            tail_rad = math.radians(tail_a)
            tc = QColor(0, 200, 255, max(0, 35 - i * 5))
            p.setBrush(QBrush(tc))
            ex = radar_cx + math.cos(tail_rad) * radar_r * 0.92
            ey = radar_cy + math.sin(tail_rad) * radar_r * 0.92
            p.drawEllipse(QRectF(ex - 1.5, ey - 1.5, 3, 3))

        p.setPen(QPen(QColor(0, 160, 220, 40), 0.7))
        p.drawLine(QPointF(radar_cx - radar_r, radar_cy), QPointF(radar_cx + radar_r, radar_cy))
        p.drawLine(QPointF(radar_cx, radar_cy - radar_r), QPointF(radar_cx, radar_cy + radar_r))

        grid_y = H * 0.50
        grid_h = H * 0.18
        grid_w = W * 0.09
        if side < 0:
            grid_x = W * 0.01
        else:
            grid_x = W * 0.90

        p.setPen(QPen(QColor(0, 140, 200, 50), 1))
        p.setBrush(QBrush(QColor(0, 8, 20, 80)))
        p.drawRoundedRect(QRectF(grid_x, grid_y, grid_w, grid_h), 3, 3)

        bars_n = 7
        bar_w = grid_w / (bars_n * 1.6)
        bar_gap = grid_w / bars_n
        for bi in range(bars_n):
            phase = t * 0.08 + bi * 0.7 + (0 if side < 0 else 1.4)
            bh = grid_h * (0.18 + 0.62 * abs(math.sin(phase)))
            bx = grid_x + bi * bar_gap + bar_gap * 0.25
            by = grid_y + grid_h - bh - 3
            bc = QColor(255, 180, 0, 120 if active else 70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(bc))
            p.drawRect(QRectF(bx, by, bar_w, bh))

        p.setFont(QFont("Courier New", 5, QFont.Weight.Bold))
        p.setPen(QPen(QColor(0, 180, 255, 100), 1))
        lbl_text = "SYS-A" if side < 0 else "SYS-B"
        p.drawText(QRectF(grid_x, grid_y + 2, grid_w, 10), Qt.AlignmentFlag.AlignCenter, lbl_text)

        vp_x = W * 0.115 if side < 0 else W * 0.885
        vp_y_top = H * 0.12
        vp_y_bot = H * 0.75
        vp_w = max(2, W * 0.005)
        vp_grad = QLinearGradient(vp_x, vp_y_top, vp_x, vp_y_bot)
        pulse = 0.4 + 0.6 * abs(math.sin(t * 0.04 + (0 if side < 0 else 1.2)))
        vp_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        vp_grad.setColorAt(0.2, QColor(255, 180, 0, int(60 * pulse)))
        vp_grad.setColorAt(0.5, QColor(255, 210, 80, int(100 * pulse + amp * 60)))
        vp_grad.setColorAt(0.8, QColor(255, 170, 0, int(50 * pulse)))
        vp_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(vp_grad))
        p.drawRect(QRectF(vp_x - vp_w / 2, vp_y_top, vp_w, vp_y_bot - vp_y_top))

        glow_grad = QRadialGradient(vp_x, H * 0.44, W * 0.08)
        glow_grad.setColorAt(0.0, QColor(255, 160, 0, int(18 * pulse + amp * 20)))
        glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow_grad))
        p.drawEllipse(QRectF(vp_x - W * 0.08, H * 0.36, W * 0.16, H * 0.16))

    def _draw_env_particles(self, p: QPainter, W: int, H: int, amp: float) -> None:
        """Update and draw floating ambient environment particles."""
        p.setPen(Qt.PenStyle.NoPen)
        for ep in self._env_particles:
            ep["x"] = (ep["x"] + ep["vx"]) % 1.0
            ep["y"] = (ep["y"] + ep["vy"]) % 1.0
            if ep["y"] < 0:
                ep["y"] = 1.0
            px_x = ep["x"] * W
            px_y = ep["y"] * H
            sz = ep["sz"] * (1.0 + amp * 0.5)
            a = int(ep["alpha"] * (180 + amp * 60))
            if ep["color"] == "gold":
                col = QColor(255, 200, 80, a)
            elif ep["color"] == "cyan":
                col = QColor(0, 200, 255, a)
            else:
                col = QColor(30, 80, 200, a)
            p.setBrush(QBrush(col))
            p.drawEllipse(QRectF(px_x - sz / 2, px_y - sz / 2, sz, sz))

    def _step(self):
        self._tick += 1
        self._live_amp *= 0.88
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.35
        self.update()

    def _status_text(self) -> str:
        if self.muted:
            return "MICROPHONE MUTED"
        st = (self.state or "").upper()
        if self.speaking or st == "SPEAKING":
            return "SPEAKING..."
        if st in ("THINKING", "PROCESSING", "ANALYZING", "ANALYSING"):
            return "PROCESSING..."
        if st == "LISTENING":
            return "LISTENING..."
        return "IDLE"

    def _draw_energy_columns(self, p: QPainter, W: int, H: int, amp: float, active: bool) -> None:
        """Draw framing side telemetry rails strictly within panel boundaries."""
        cols = 16
        top = max(40, int(H * 0.10))
        bottom = max(top + 80, int(H * 0.72))
        span = bottom - top
        base_alpha = 115 if active else 50
        for side in (-1, 1):
            rail_x = 28 if side < 0 else W - 28
            p.setPen(QPen(QColor(255, 179, 0, 32), 1))
            p.drawLine(QPointF(rail_x, top), QPointF(rail_x, bottom))
            for i in range(cols):
                y = top + (i / max(1, cols - 1)) * span
                phase = self._tick * 0.11 + i * 0.55 + (0.7 if side > 0 else 0.0)
                energy = 0.25 + 0.75 * abs(math.sin(phase))
                length = (14 + energy * 26 + amp * 30) * side
                col = QColor("#ffb300" if i % 3 else "#ffe277")
                col.setAlpha(base_alpha + int(65 * energy))
                p.setPen(QPen(col, 1.2))
                p.drawLine(QPointF(rail_x, y), QPointF(rail_x + length, y))

    def _draw_platform(self, p: QPainter, cx: float, plat_y: float, base_r: float, amp: float, active: bool) -> None:
        """Draw concentric glowing holographic elliptical pedestal projector."""
        pw = base_r * 1.55
        ph = pw * 0.24

        # Ambient floor glow
        floor_glow = QRadialGradient(cx, plat_y, pw * 0.9)
        floor_glow.setColorAt(0.0, QColor(255, 170, 0, 75 + int(amp * 60)))
        floor_glow.setColorAt(0.45, QColor(0, 180, 255, 25))
        floor_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(floor_glow))
        p.drawEllipse(QRectF(cx - pw * 0.9, plat_y - ph * 1.6, pw * 1.8, ph * 3.2))

        # Concentric pedestal rings
        for idx, scale in enumerate([1.0, 0.78, 0.55, 0.32]):
            rw = pw * scale
            rh = ph * scale
            ring_col = QColor("#ffe277" if idx % 2 == 0 else "#00d4ff")
            ring_col.setAlpha(80 + int(amp * 80) if active else 45 + idx * 10)
            pen = QPen(ring_col, 1.3 if idx == 0 else 1.0)
            if idx == 1:
                pen.setDashPattern([3, 5])
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - rw, plat_y - rh, rw * 2, rh * 2))

        # Vertical projector light filaments rising into core
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(7):
            t = (i - 3) / 3.0
            beam_x = cx + t * pw * 0.58
            beam_w = 2.0 + abs(t) * 1.5
            grad = QLinearGradient(beam_x, plat_y, beam_x, plat_y - base_r * 0.9)
            grad.setColorAt(0.0, QColor(255, 214, 92, 90 + int(amp * 70)))
            grad.setColorAt(0.6, QColor(255, 153, 0, 30))
            grad.setColorAt(1.0, QColor(255, 153, 0, 0))
            p.fillRect(QRectF(beam_x - beam_w / 2, plat_y - base_r * 0.9, beam_w, base_r * 0.9), QBrush(grad))

    def _draw_orbital_rings(self, p: QPainter, cx: float, cy: float, base_r: float, amp: float, active: bool, processing: bool) -> None:
        """Draw dynamic 3D gimbal orbital rings with traveling energy nodes."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        speed_mult = 2.4 if processing else (1.6 if active else 1.0)

        ring_configs = [
            (1.18, 0.46, 18, 0.016, "#ffd76a", None),
            (1.06, 0.38, -32, -0.020, "#ffb800", [4, 6]),
            (1.28, 0.58, 62, 0.012, "#00e5ff", None),
            (1.36, 0.30, -8, 0.024, "#ffe599", [2, 8])
        ]

        for idx, (sx, sy, tilt_deg, spd, col_hex, dash) in enumerate(ring_configs):
            rr_x = base_r * sx * (1.0 + amp * 0.06)
            rr_y = base_r * sy * (1.0 + amp * 0.06)
            angle = (self._tick * spd * speed_mult) % math.tau

            p.save()
            p.translate(cx, cy)
            p.rotate(tilt_deg)

            col = QColor(col_hex)
            col.setAlpha(95 + int(amp * 110) if active else 55 + idx * 12)
            pen = QPen(col, 1.4 if idx == 0 else 1.1)
            if dash:
                pen.setDashPattern(dash)
            p.setPen(pen)
            p.drawEllipse(QRectF(-rr_x, -rr_y, rr_x * 2, rr_y * 2))

            node_x = math.cos(angle) * rr_x
            node_y = math.sin(angle) * rr_y

            node_glow = QColor(255, 245, 190, 220)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(node_glow))
            p.drawEllipse(QRectF(node_x - 3.0, node_y - 3.0, 6.0, 6.0))

            for s in range(1, 5):
                tail_a = angle - s * 0.08 * (1 if spd > 0 else -1)
                tx = math.cos(tail_a) * rr_x
                ty = math.sin(tail_a) * rr_y
                col_tail = QColor(col_hex)
                col_tail.setAlpha(max(0, 160 - s * 38))
                p.setBrush(QBrush(col_tail))
                p.drawEllipse(QRectF(tx - 2.0, ty - 2.0, 4.0, 4.0))

            p.restore()

    def _draw_particles(self, p: QPainter, cx: float, cy: float, base_r: float, amp: float, active: bool, processing: bool) -> None:
        """Draw dense 3D volumetric particle swarm with perspective projection."""
        p.setPen(Qt.PenStyle.NoPen)
        speed_mult = 2.2 if processing else (1.5 if active else 1.0)

        for pt in self._particles:
            pt["theta"] = (pt["theta"] + pt["dtheta"] * speed_mult) % math.tau
            pt["phi"] = math.sin(self._tick * pt["dphi"] * speed_mult) * (math.pi * 0.42)

            r_eff = base_r * pt["r"] * (1.0 + amp * 0.16)
            x3 = r_eff * math.cos(pt["phi"]) * math.sin(pt["theta"])
            y3 = r_eff * math.sin(pt["phi"])
            z3 = r_eff * math.cos(pt["phi"]) * math.cos(pt["theta"])

            pitch = 0.38
            yp = y3 * math.cos(pitch) - z3 * math.sin(pitch)
            zp = y3 * math.sin(pitch) + z3 * math.cos(pitch)

            fov = 400.0
            scale = fov / (fov + zp)
            screen_x = cx + x3 * scale
            screen_y = cy + yp * scale

            depth_f = (zp + base_r * 1.3) / (base_r * 2.6)
            depth_f = max(0.0, min(1.0, depth_f))

            sz = pt["sz"] * scale * (0.8 + 0.4 * depth_f)
            alpha = int(pt["alpha"] * (40 + 190 * depth_f + amp * 45))
            alpha = max(15, min(255, alpha))

            if pt["color_type"] == "gold":
                col = QColor(255, 215, 100, alpha)
            elif pt["color_type"] == "amber":
                col = QColor(255, 150, 20, alpha)
            elif pt["color_type"] == "cyan":
                col = QColor(0, 220, 255, alpha)
            else:
                col = QColor(255, 255, 255, alpha)

            p.setBrush(QBrush(col))
            p.drawEllipse(QRectF(screen_x - sz / 2, screen_y - sz / 2, sz, sz))

    def _draw_processing_hud(self, p: QPainter, cx: float, cy: float, base_r: float, amp: float) -> None:
        """Draw laser scan sweep and rotating HUD reticles during processing."""
        scan_offset = math.sin(self._tick * 0.08)
        scan_y = cy + scan_offset * base_r * 0.82
        scan_w = base_r * math.sqrt(max(0.1, 1.0 - (scan_offset * 0.82) ** 2)) * 1.9

        laser_col = QColor(255, 235, 140, 210)
        p.setPen(QPen(laser_col, 1.6))
        p.drawLine(QPointF(cx - scan_w, scan_y), QPointF(cx + scan_w, scan_y))

        curt_h = 24.0 * (1 if scan_offset >= 0 else -1)
        curt_grad = QLinearGradient(cx, scan_y, cx, scan_y - curt_h)
        curt_grad.setColorAt(0.0, QColor(255, 180, 0, 70))
        curt_grad.setColorAt(1.0, QColor(255, 180, 0, 0))
        p.fillRect(QRectF(cx - scan_w, min(scan_y, scan_y - curt_h), scan_w * 2, abs(curt_h)), QBrush(curt_grad))

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 212, 255, 130), 1.2))
        t_ang = (self._tick * 1.5) % 360
        for b_ang in [t_ang, t_ang + 90, t_ang + 180, t_ang + 270]:
            p.save()
            p.translate(cx, cy)
            p.rotate(b_ang)
            rad = base_r * 1.12
            p.drawLine(QPointF(rad - 12, 0), QPointF(rad, 0))
            p.drawLine(QPointF(rad, 0), QPointF(rad, 12))
            p.restore()

    def _draw_holographic_globe(
        self, p: QPainter, cx: float, cy: float, base_r: float, amp: float, active: bool, processing: bool
    ) -> None:
        """
        Draw the large procedural golden holographic globe.
        Renders: volumetric inner glow, lat/lon sphere grid, fragmented geometry,
        dynamic circuit traces, concentric rings, JARVIS text, radial scan effects.
        """
        t     = self._tick
        speed = 2.0 if processing else (1.4 if active else 0.8)
        rot   = t * 0.008 * speed
        breath = math.sin(t * 0.05) * 0.025
        R = base_r * (1.0 + breath + amp * 0.07)

        # ── Layer 1: Deep inner volumetric golden core glow ──────────────────
        core_glow = QRadialGradient(cx, cy, R * 0.62)
        core_glow.setColorAt(0.00, QColor(255, 240, 160, int(200 + amp * 55)))
        core_glow.setColorAt(0.18, QColor(255, 200, 60, int(160 + amp * 50)))
        core_glow.setColorAt(0.40, QColor(255, 150, 10, int(90 + amp * 40)))
        core_glow.setColorAt(0.70, QColor(200, 90, 0, int(40 + amp * 25)))
        core_glow.setColorAt(1.00, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(core_glow))
        p.drawEllipse(QRectF(cx - R * 0.62, cy - R * 0.62, R * 1.24, R * 1.24))

        # ── Layer 2: Outer volumetric glow halo ─────────────────────────────
        outer_glow = QRadialGradient(cx, cy, R * 1.18)
        outer_glow.setColorAt(0.0, QColor(255, 180, 20, int(55 + amp * 40)))
        outer_glow.setColorAt(0.5, QColor(255, 120, 0, int(22 + amp * 20)))
        outer_glow.setColorAt(0.80, QColor(0, 80, 200, 12))
        outer_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(outer_glow))
        p.drawEllipse(QRectF(cx - R * 1.18, cy - R * 1.18, R * 2.36, R * 2.36))

        # ── Layer 3: Latitude lines (horizontal circles as ellipses) ──────────
        lat_count = 9
        lat_alpha_base = 90 + int(amp * 60) if active else 55
        for i in range(lat_count):
            phi = -math.pi / 2 + (i + 1) * math.pi / (lat_count + 1)
            r_circle = R * math.cos(phi)
            y_offset = R * math.sin(phi)
            ell_w = r_circle * 2.0
            ell_h = r_circle * 0.38
            ex = cx - r_circle
            ey = cy + y_offset - ell_h / 2
            depth_f = (math.cos(phi) + 1.0) * 0.5
            alpha = int(lat_alpha_base * (0.30 + 0.70 * depth_f))
            col = QColor(255, 200, 60, max(8, alpha))
            p.setPen(QPen(col, 0.8 if i != lat_count // 2 else 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(ex, ey, ell_w, ell_h))

        # ── Layer 4: Longitude lines (vertical great-circle arcs as tilted ellipses) ─
        lon_count = 12
        lon_alpha = 75 + int(amp * 55) if active else 45
        p.setBrush(Qt.BrushStyle.NoBrush)
        for j in range(lon_count):
            lon_angle = rot + j * math.pi / lon_count
            facing = abs(math.cos(lon_angle)) * 0.7 + 0.3
            a = int(lon_alpha * facing)
            col = QColor(255, 180, 40, max(8, a))
            p.setPen(QPen(col, 0.7))
            p.save()
            p.translate(cx, cy)
            p.rotate(math.degrees(lon_angle))
            p.drawEllipse(QRectF(-R * 0.19, -R, R * 0.38, R * 2))
            p.restore()

        # ── Layer 5: Globe boundary ring ─────────────────────────────────
        globe_pen_col = QColor(255, 210, 80, 160 + int(amp * 80))
        p.setPen(QPen(globe_pen_col, 2.0 + amp * 0.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - R, cy - R, R * 2, R * 2))
        glow_ring_col = QColor(255, 240, 140, 50 + int(amp * 40))
        p.setPen(QPen(glow_ring_col, 6.0 + amp * 2.0))
        p.drawEllipse(QRectF(cx - R, cy - R, R * 2, R * 2))

        # ── Layer 6: Fragmented golden geometry patches ─────────────────────
        self._draw_globe_geometry(p, cx, cy, R, rot, amp, active)

        # ── Layer 7: Dynamic circuit traces ───────────────────────────────
        self._draw_circuit_traces(p, cx, cy, R, rot, amp, active)

        # ── Layer 8: Inner concentric glow rings ──────────────────────────
        for ring_f in [0.25, 0.50, 0.72, 0.90]:
            rr = R * ring_f
            ring_alpha = int((80 - ring_f * 55) + amp * 35)
            rc = QColor(255, 220, 100, max(5, ring_alpha))
            p.setPen(QPen(rc, 0.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))

        # ── Layer 9: Radial scanning glow effect ──────────────────────────
        scan_angle = (t * 0.022 * speed) % math.tau
        scan_len = R * (0.85 + amp * 0.15)
        for i in range(3):
            sa = scan_angle + i * (math.tau / 3)
            sx = cx + math.cos(sa) * scan_len
            sy = cy + math.sin(sa) * scan_len
            scan_col = QColor(255, 220, 80, max(0, 60 - int(i * 20)))
            p.setPen(QPen(scan_col, 1.0))
            p.drawLine(QPointF(cx, cy), QPointF(sx, sy))

        # ── Layer 10: Scanning node particles on surface ───────────────────
        p.setPen(Qt.PenStyle.NoPen)
        for ni in range(6):
            na = (t * 0.015 * speed + ni * math.tau / 6) % math.tau
            np_phi = math.sin(t * 0.011 + ni * 1.1) * math.pi * 0.4
            nx = cx + R * 0.92 * math.cos(np_phi) * math.cos(na)
            ny = cy + R * 0.92 * math.sin(np_phi)
            depth_sc = 0.5 + 0.5 * math.cos(na - rot)
            node_a = int(180 * depth_sc + amp * 60)
            node_sz = 2.5 + amp * 1.5 + 2.0 * depth_sc
            p.setBrush(QBrush(QColor(255, 250, 200, max(20, node_a))))
            p.drawEllipse(QRectF(nx - node_sz / 2, ny - node_sz / 2, node_sz, node_sz))

        # ── Layer 11: JARVIS text at globe center ─────────────────────────
        self._draw_globe_jarvis_text(p, cx, cy, R, amp, active)

    def _draw_globe_geometry(
        self, p: QPainter, cx: float, cy: float, R: float, rot: float, amp: float, active: bool
    ) -> None:
        """Draw fragmented golden geometry patches on the globe surface."""
        t = self._tick
        p.setPen(Qt.PenStyle.NoPen)
        geo_configs = [
            (0.22, 0.15, 0.018, 0.55), (-0.35, 0.28, -0.014, 0.45),
            (0.55, -0.12, 0.021, 0.50), (-0.18, -0.40, 0.016, 0.60),
            (0.40, 0.38, -0.019, 0.42), (-0.50, 0.08, 0.013, 0.52),
            (0.10, 0.45, 0.017, 0.48), (-0.28, -0.18, -0.022, 0.58),
        ]
        for gx_f, gy_f, spd, intensity in geo_configs:
            angle = rot + t * spd
            phi = math.asin(max(-1, min(1, gy_f)))
            theta = math.acos(max(-1, min(1, gx_f))) + angle
            x3 = R * math.cos(phi) * math.cos(theta)
            y3 = R * math.sin(phi)
            z3 = R * math.cos(phi) * math.sin(theta)
            if z3 > -R * 0.1:
                depth_f = (z3 + R) / (2 * R)
                sz = R * (0.08 + 0.04 * intensity) * (0.5 + 0.5 * depth_f)
                a = int(intensity * (90 + amp * 55) * depth_f)
                a = max(8, min(180, a))
                p.setBrush(QBrush(QColor(255, 210, 60, a)))
                p.drawEllipse(QRectF(cx + x3 - sz / 2, cy + y3 - sz / 2, sz, sz))
                node_sz = sz * 0.35
                p.setBrush(QBrush(QColor(255, 245, 190, min(255, a + 60))))
                p.drawEllipse(QRectF(cx + x3 - node_sz / 2, cy + y3 - node_sz / 2, node_sz, node_sz))

    def _draw_circuit_traces(
        self, p: QPainter, cx: float, cy: float, R: float, rot: float, amp: float, active: bool
    ) -> None:
        """Draw dynamic circuit traces / energy streams on globe surface."""
        t = self._tick
        trace_alpha = 100 + int(amp * 80) if active else 60
        p.setBrush(Qt.BrushStyle.NoBrush)
        for seed in self._circuit_seeds:
            angle = rot + seed["phase"] + t * 0.009
            phi0  = seed["phi0"] + math.sin(t * 0.007 + seed["phase"]) * 0.3
            seg_n = seed["len"]
            pts = []
            theta_step = 0.18
            phi_step   = 0.12
            for k in range(seg_n):
                theta = angle + k * theta_step
                phi   = phi0 + k * phi_step * math.sin(t * 0.005 + k * 0.4)
                phi   = max(-math.pi * 0.46, min(math.pi * 0.46, phi))
                x3 = R * math.cos(phi) * math.cos(theta)
                y3 = R * math.sin(phi)
                z3 = R * math.cos(phi) * math.sin(theta)
                if z3 > -R * 0.05:
                    depth = (z3 + R) / (2 * R)
                    pts.append((cx + x3, cy + y3, depth))
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    x1, y1, d1 = pts[i]
                    x2, y2, d2 = pts[i + 1]
                    avg_d = (d1 + d2) * 0.5
                    a = int(trace_alpha * avg_d)
                    col = QColor(255, 200, 60, max(8, a))
                    p.setPen(QPen(col, 0.9))
                    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                    if i % 2 == 0:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QBrush(QColor(255, 240, 160, max(15, int(a * 1.4)))))
                        p.drawEllipse(QRectF(x1 - 1.8, y1 - 1.8, 3.6, 3.6))
                        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_globe_jarvis_text(
        self, p: QPainter, cx: float, cy: float, R: float, amp: float, active: bool
    ) -> None:
        """Draw 'JARVIS' text subtly at the center of the globe."""
        t = self._tick
        pulse = 0.72 + 0.28 * math.sin(t * 0.06)
        font_sz = max(10, int(R * 0.155))
        font = QFont("Courier New", font_sz, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, font_sz * 0.55)
        p.setFont(font)
        base_a = int((160 + amp * 70) * pulse)
        for blur_r in [14, 9, 5, 0]:
            if blur_r > 0:
                a = max(8, int(base_a * (0.15 * (14 - blur_r) / 14)))
                col = QColor(255, 230, 120, a)
            else:
                a = max(80, min(255, base_a))
                col = QColor(255, 245, 190, a)
            p.setPen(QPen(col, 1))
            text_rect = QRectF(cx - R * 0.55, cy - R * 0.12, R * 1.1, R * 0.25)
            p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "JARVIS")

    def mouseMoveEvent(self, e) -> None:
        if self._dest_rect is not None:
            sy = self._dest_rect.top() + self._dest_rect.height() * (545.0 / 610.0)
            cx = self.width() / 2.0
            mic_r = self._dest_rect.width() * (28.0 / 630.0)
            if math.hypot(e.pos().x() - cx, e.pos().y() - sy) <= mic_r * 1.3:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._dest_rect is not None:
            sy = self._dest_rect.top() + self._dest_rect.height() * (545.0 / 610.0)
            cx = self.width() / 2.0
            mic_r = self._dest_rect.width() * (28.0 / 630.0)
            if math.hypot(e.pos().x() - cx, e.pos().y() - sy) <= mic_r * 1.3:
                win = self.window()
                if hasattr(win, "_toggle_mute"):
                    win._toggle_mute()
                else:
                    self.muted = not self.muted
                    self.update()
                return
        super().mousePressEvent(e)

    def _draw_microphone_control(
        self, p: QPainter, cx: float, sy: float, dest_rect: QRectF, amp: float, active: bool
    ) -> None:
        """Draw interactive central microphone button control on the pedestal."""
        mic_r = dest_rect.width() * (26.0 / 630.0)

        glow_r = mic_r * (1.3 + amp * 0.45)
        glow = QRadialGradient(cx, sy, glow_r)
        if self.muted:
            glow.setColorAt(0.0, QColor(255, 68, 85, int(90 + amp * 80)))
            glow.setColorAt(0.6, QColor(180, 20, 40, 35))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        elif self.state == "LISTENING":
            glow.setColorAt(0.0, QColor(0, 229, 255, int(95 + amp * 90)))
            glow.setColorAt(0.6, QColor(0, 140, 220, 40))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        else:
            glow.setColorAt(0.0, QColor(255, 235, 120, int(85 + amp * 90)))
            glow.setColorAt(0.6, QColor(255, 170, 0, int(40 + amp * 50)))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QRectF(cx - glow_r, sy - glow_r, glow_r * 2, glow_r * 2))

        p.setBrush(QBrush(QColor("#030d18")))
        ring_col = QColor("#ff4455" if self.muted else ("#00e5ff" if self.state == "LISTENING" else "#ffb800"))
        ring_col.setAlpha(220 if active else 140)
        p.setPen(QPen(ring_col, 1.4))
        p.drawEllipse(QRectF(cx - mic_r, sy - mic_r, mic_r * 2, mic_r * 2))

        mic_px = IconManager.get_pixmap("mic_active", int(mic_r * 1.35))
        if mic_px and not mic_px.isNull():
            p.drawPixmap(int(cx - mic_r * 0.675), int(sy - mic_r * 0.675), mic_px)

    def _draw_real_ui_text(
        self, p: QPainter, cx: float, core_y: float, sy: float, base_r: float, amp: float, active: bool
    ) -> None:
        """Draw real UI typography for 'JARVIS' and dynamic application status."""
        st = self._status_text()

        status_y = sy + base_r * 0.14
        font_status = QFont("Courier New", int(max(9, min(11, base_r * 0.055))), QFont.Weight.Bold)
        p.setFont(font_status)

        if self.muted:
            st_col = QColor("#ff4455")
        elif st == "LISTENING...":
            st_col = QColor("#00e5ff")
        elif st == "SPEAKING...":
            st_col = QColor("#ffbb00")
        elif st == "PROCESSING...":
            st_col = QColor("#00d4ff")
        else:
            st_col = QColor("#ffe277")

        st_text = f"◈  {st}  ◈"
        fm = p.fontMetrics()
        txt_w = fm.horizontalAdvance(st_text) + 24
        badge_rect = QRectF(cx - txt_w / 2, status_y - 9, txt_w, 18)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 5, 12, 160)))
        p.drawRoundedRect(badge_rect, 9, 9)

        p.setPen(QPen(QColor(st_col.red(), st_col.green(), st_col.blue(), 130), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(badge_rect, 9, 9)

        p.setPen(QPen(st_col, 1))
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, st_text)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        W, H = self.width(), self.height()
        cx = W / 2.0
        amp = self._amp_disp
        st = self._status_text()
        speaking = self.speaking or st == "SPEAKING..."
        processing = st == "PROCESSING..."
        active = speaking or processing or st == "LISTENING..."

        # ── 1. Cinematic procedural environment (background) ────────────────
        self._draw_environment(p, W, H, amp, active)

        # ── 2. Framing HUD border and corner markers ──────────────────────
        border_col = QColor("#1a5c7a")
        border_col.setAlpha(120)
        p.setPen(QPen(border_col, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, W - 1, H - 1))

        gold = QColor("#ffb300")
        gold.setAlpha(130 if active else 70)
        p.setPen(QPen(gold, 1.4))
        for x, y, sx, sy_c in [(10, 10, 1, 1), (W - 10, 10, -1, 1), (10, H - 10, 1, -1), (W - 10, H - 10, -1, -1)]:
            p.drawLine(QPointF(x, y), QPointF(x + sx * 28, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + sy_c * 28))

        # ── 3. Telemetry side rails ─────────────────────────────────────
        self._draw_energy_columns(p, W, H, amp, active)

        # ── 4. Layout coordinates ─────────────────────────────────────
        core_y = H * 0.45
        base_r = min(W * 0.38, H * 0.40)

        # ── 5. Holographic platform / pedestal projector ──────────────────
        plat_y = core_y + base_r * 0.82
        self._draw_platform(p, cx, plat_y, base_r, amp, active)

        # ── 6. Golden holographic globe (fully procedural) ────────────────
        self._draw_holographic_globe(p, cx, core_y, base_r, amp, active, processing)

        # ── 7. Orbital rings (layered over the globe) ────────────────────
        self._draw_orbital_rings(p, cx, core_y, base_r, amp, active, processing)

        # ── 8. Dense 3D volumetric particle cloud ───────────────────────
        self._draw_particles(p, cx, core_y, base_r, amp, active, processing)

        # ── 9. Processing scan effect ─────────────────────────────────
        if processing:
            self._draw_processing_hud(p, cx, core_y, base_r, amp)

        # ── 10. Waveform + microphone layout (anchored below globe) ─────────
        wave_w  = base_r * 2.60
        wave_y  = core_y + base_r * 0.88
        dest_rect = QRectF(cx - wave_w / 2, wave_y - base_r * 0.15, wave_w, base_r * 1.20)
        self._dest_rect = dest_rect
        mic_sy = wave_y

        # ── 11. Dual symmetrical waveform wings ─────────────────────────
        raw_state = (self.state or "").upper()
        self._waveform.render(p, cx, mic_sy, dest_rect, amp, self._tick, raw_state, speaking, active)

        # ── 12. Interactive central microphone control ───────────────────
        self._draw_microphone_control(p, cx, mic_sy, dest_rect, amp, active)

        # ── 13. Dynamic status badge ────────────────────────────────
        self._draw_real_ui_text(p, cx, core_y, mic_sy, base_r, amp, active)

        p.end()


class IconManager:
    """Extracts, crops and caches the 40 holographic UI icons from the
    config/mj-1.png sprite sheet (10 columns x 4 rows).

    NOTE: config/mj.png is the full golden AI-Core artwork and is used ONLY as
    the centre visual. config/jarvis.ico is the Windows app icon only. Neither
    is ever used for interface icons."""
    _cache: dict[tuple[str, int], QPixmap] = {}
    _raw_crops: dict[str, object] = {}
    _loaded = False

    @staticmethod
    def _detect_icon_bands(im):
        """Find the centre-line of every icon by projecting the sheet's alpha
        channel (icon badges are solid, the gaps between them are empty). Returns
        (col_centres, row_centres) and falls back to an even grid whenever the
        sheet does not look like the expected 10x4 layout."""
        W, H = im.size
        cols_n, rows_n = 10, 4
        try:
            import numpy as np
            alpha = np.asarray(im.getchannel("A"), dtype=float)
            col_prof = alpha.mean(axis=0)   # per-column mean alpha
            row_prof = alpha.mean(axis=1)   # per-row mean alpha

            def _centres(prof, count):
                thr = max(2.0, float(prof.max()) * 0.35)
                bands: list[tuple[int, int]] = []
                start = None
                for i, v in enumerate(prof):
                    if v >= thr and start is None:
                        start = i
                    elif v < thr and start is not None:
                        bands.append((start, i - 1))
                        start = None
                if start is not None:
                    bands.append((start, len(prof) - 1))
                bands = [b for b in bands if (b[1] - b[0]) > 20]
                if len(bands) != count:
                    return None
                return [(b[0] + b[1]) / 2.0 for b in bands]

            cxs = _centres(col_prof, cols_n)
            cys = _centres(row_prof, rows_n)
            if cxs and cys:
                return cxs, cys
        except Exception:
            pass
        return ([(c + 0.5) * (W / cols_n) for c in range(cols_n)],
                [(r + 0.5) * (H / rows_n) for r in range(rows_n)])

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
        icon_path = CONFIG_DIR / "mj-1.png"
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
            _cols, _rows = cls._detect_icon_bands(im)
            _half = 0.5 * min(cw, ch) * 0.94
            for name, (r, c) in mapping.items():
                bx = _cols[c] if c < len(_cols) else (c + 0.5) * cw
                by = _rows[r] if r < len(_rows) else (r + 0.5) * ch
                box = (int(round(bx - _half)), int(round(by - _half)),
                       int(round(bx + _half)), int(round(by + _half)))
                cls._raw_crops[name] = im.crop(box)
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

        # Icon from mj-1.png
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
    # category -> (mj-1.png icon name, colour)
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
        # Icon from mj-1.png sprite sheet (fallback: text block)
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


class EnglishTutorOverlay(QDialog):
    """
    English Tutor / Speaking Mentor floating dialog.
    Opens as an independent, resizable window with standard window controls (minimize, maximize, close).
    Features:
      - Current topic & question display
      - Live state indicator (IDLE, LISTENING, ANALYZING, FEEDBACK, etc.)
      - Five silent live scores (Grammar, Vocabulary, Fluency, Pronunciation, Overall)
      - Scrollable interactive transcript & coaching history
      - Action controls (Practice Word, Shadowing, Daily Coach, Hear Word, Next Topic, My Score, Close)
    """

    speak_requested         = pyqtSignal(str)
    record_requested        = pyqtSignal()
    feedback_requested      = pyqtSignal()
    next_question_requested = pyqtSignal()
    score_query_requested   = pyqtSignal()
    close_requested         = pyqtSignal()

    # Thread-safe update channels
    _state_sig    = pyqtSignal(str)
    _question_sig = pyqtSignal(str, str)
    _feedback_sig = pyqtSignal(dict)
    _progress_sig = pyqtSignal(dict)
    _hearing_sig  = pyqtSignal(str)

    _W, _H = 820, 580

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EnglishTutorDialog")
        self.setWindowTitle("JARVIS — English Speaking Mentor")
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowSystemMenuHint |
            Qt.WindowType.WindowMinMaxButtonsHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(self._W, self._H)
        self.setMinimumSize(680, 460)
        self.setStyleSheet(f"""
            QDialog#EnglishTutorDialog {{
                background: #010a12;
                border: 1px solid {C.BORDER_B};
            }}
        """)
        self._state = "IDLE"
        self._round_num = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        # ── Header ───────────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(10)
        _tutor_icon = IconManager.get_pixmap("voice", 26)
        icon_lbl = QLabel()
        if _tutor_icon and not _tutor_icon.isNull():
            icon_lbl.setPixmap(_tutor_icon)
        icon_lbl.setFixedSize(26, 26)
        hdr.addWidget(icon_lbl)

        title_col = QVBoxLayout(); title_col.setSpacing(1)
        self._title_lbl = QLabel("ENGLISH SPEAKING MENTOR")
        self._title_lbl.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        title_col.addWidget(self._title_lbl)
        self._sub_lbl = QLabel("Interactive Pronunciation & Fluency Coach")
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        title_col.addWidget(self._sub_lbl)
        hdr.addLayout(title_col)
        hdr.addStretch()

        self._round_lbl = QLabel("ROUND 0")
        self._round_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._round_lbl.setStyleSheet(
            f"color: {C.ACC2}; background: {C.PANEL2}; border: 1px solid {C.BORDER}; "
            f"border-radius: 3px; padding: 2px 8px;"
        )
        hdr.addWidget(self._round_lbl)

        self._state_lbl = QLabel("IDLE")
        self._state_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._state_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: {C.DARK}; border: 1px solid {C.BORDER}; "
            f"border-radius: 3px; padding: 2px 8px;"
        )
        hdr.addWidget(self._state_lbl)
        lay.addLayout(hdr)

        # ── Topic / Question ─────────────────────────────────────────────────
        self._topic_lbl = QLabel("CURRENT TOPIC: General Conversation")
        self._topic_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._topic_lbl.setStyleSheet(
            f"color: {C.ACC2}; background: {C.PANEL2}; border: 1px solid {C.BORDER}; "
            f"border-radius: 3px; padding: 4px 8px;"
        )
        lay.addWidget(self._topic_lbl)

        self._question_lbl = QLabel("Welcome! Tell me about yourself or what you've been working on today.")
        self._question_lbl.setFont(QFont("Courier New", 9))
        self._question_lbl.setStyleSheet(
            f"color: {C.TEXT}; background: {C.DARK}; border: 1px solid {C.BORDER_B}; "
            f"border-radius: 4px; padding: 8px 10px;"
        )
        self._question_lbl.setWordWrap(True)
        lay.addWidget(self._question_lbl)

        # ── State lights & Telemetry row ──────────────────────────────────────
        status_row = QHBoxLayout(); status_row.setSpacing(14)
        self._light_listen = self._make_light("LISTENING", C.PRI)
        self._light_analyze = self._make_light("ANALYZING", C.ACC2)
        self._light_feedback = self._make_light("FEEDBACK", C.GREEN)
        status_row.addWidget(self._light_listen)
        status_row.addWidget(self._light_analyze)
        status_row.addWidget(self._light_feedback)
        status_row.addStretch()

        self._wpm_lbl = QLabel("SPEED: -- WPM")
        self._wpm_lbl.setFont(QFont("Courier New", 8))
        self._wpm_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        status_row.addWidget(self._wpm_lbl)
        lay.addLayout(status_row)

        # ── Scores row (Silent Live Scores) ──────────────────────────────────
        self._scores_row = QHBoxLayout(); self._scores_row.setSpacing(8)
        self._score_grammar  = self._make_score("GRAMMAR", C.PRI)
        self._score_vocab    = self._make_score("VOCABULARY", C.ACC2)
        self._score_fluency  = self._make_score("FLUENCY", C.GREEN)
        self._score_prono    = self._make_score("PRONUNCIATION", "#c054ff")
        self._score_overall  = self._make_score("OVERALL", C.ACC)
        for w in [self._score_grammar, self._score_vocab, self._score_fluency,
                  self._score_prono, self._score_overall]:
            self._scores_row.addWidget(w, stretch=1)
        lay.addLayout(self._scores_row)

        # ── Interactive Transcript Area ──────────────────────────────────────
        trans_hdr = QHBoxLayout()
        trans_title = QLabel("COACHING TRANSCRIPT & TIPS")
        trans_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        trans_title.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        trans_hdr.addWidget(trans_title)
        trans_hdr.addStretch()
        lay.addLayout(trans_hdr)

        self._transcript_view = QTextEdit()
        self._transcript_view.setReadOnly(True)
        self._transcript_view.setFont(QFont("Courier New", 8))
        self._transcript_view.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px 8px;
            }}
        """)
        lay.addWidget(self._transcript_view, stretch=1)

        # ── Action buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(6)

        self._practice_btn = self._make_btn("PRACTICE", "voice")
        self._practice_btn.clicked.connect(self._on_practice)
        btn_row.addWidget(self._practice_btn)

        self._shadow_btn = self._make_btn("SHADOWING", "code")
        self._shadow_btn.clicked.connect(self._on_shadowing)
        btn_row.addWidget(self._shadow_btn)

        self._daily_btn = self._make_btn("DAILY COACH", "ai_core")
        self._daily_btn.clicked.connect(self._on_daily)
        btn_row.addWidget(self._daily_btn)

        self._hear_btn = self._make_btn("HEAR WORD", "voice")
        self._hear_btn.clicked.connect(self._on_hear)
        btn_row.addWidget(self._hear_btn)

        self._next_btn = self._make_btn("NEXT TOPIC", "refresh")
        self._next_btn.clicked.connect(lambda: self.next_question_requested.emit())
        btn_row.addWidget(self._next_btn)

        self._score_btn = self._make_btn("MY SCORE", "chart")
        self._score_btn.clicked.connect(lambda: self.score_query_requested.emit())
        btn_row.addWidget(self._score_btn)

        btn_row.addStretch()

        self._close_btn = self._make_btn("CLOSE TUTOR", "stop")
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                background: #2a0808; color: #ff6b6b;
                border: 1px solid #772222; border-radius: 3px;
                padding: 4px 10px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #4a1010; color: #ff9999; border-color: #ff4444; }}
        """)
        self._close_btn.clicked.connect(self.close)
        btn_row.addWidget(self._close_btn)

        lay.addLayout(btn_row)

        # Route setters through thread-safe Qt signals
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
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        v = QVBoxLayout(w); v.setContentsMargins(8, 4, 8, 4); v.setSpacing(1)
        l = QLabel(label)
        l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        l.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(l)

        val = QLabel("--")
        val.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        val.setStyleSheet(f"color: {color}; background: transparent;")
        val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(val)

        sub = QLabel("Live: --")
        sub.setFont(QFont("Courier New", 6))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sub)

        setattr(w, "_value_lbl", val)
        setattr(w, "_sub_lbl", sub)
        return w

    def _make_btn(self, text: str, icon_name: str) -> QPushButton:
        b = QPushButton(f"  {text}")
        b.setFixedHeight(28)
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
                padding: 2px 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """)
        return b

    # ── Public API (called from MainWindow / JarvisLive) ───────────────────

    def reset_scores(self):
        """Reset all live scores and transcript to clean slate."""
        self._round_num = 0
        self._round_lbl.setText("ROUND 0")
        for widget in [self._score_grammar, self._score_vocab, self._score_fluency,
                      self._score_prono, self._score_overall]:
            lbl = getattr(widget, "_value_lbl", None)
            if lbl: lbl.setText("--")
            sub = getattr(widget, "_sub_lbl", None)
            if sub: sub.setText("Live: --")
        self._transcript_view.clear()
        self._transcript_view.append("<span style='color: #64748b;'>[English Tutor Session initialized — scores reset to 0]</span><br>")
        self._wpm_lbl.setText("SPEED: -- WPM")

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
        self._state_lbl.setStyleSheet(f"color: {c}; background: {C.DARK}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px;")

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

    def show_feedback(self, report: dict):
        """Thread-safe. Queue a report for the Qt main thread."""
        self._feedback_sig.emit(dict(report or {}))

    def _apply_feedback_ui(self, report: dict):
        """Slot — runs on the Qt main thread. Combined tutor report."""
        self._round_num += 1
        self._round_lbl.setText(f"ROUND {self._round_num}")

        scores = report.get("scores", {})
        for name, key, widget in [
            ("GRAMMAR", "grammar", self._score_grammar),
            ("VOCABULARY", "vocabulary", self._score_vocab),
            ("FLUENCY", "fluency", self._score_fluency),
            ("PRONUNCIATION", "pronunciation", self._score_prono),
            ("OVERALL", "overall", self._score_overall),
        ]:
            val = scores.get(key) if isinstance(scores, dict) else None
            lbl = getattr(widget, "_value_lbl", None)
            if lbl:
                if isinstance(val, (int, float)) and val > 0:
                    lbl.setText(f"{val:.0f}/10")
                elif key == "pronunciation" and (val == 0 or val is None):
                    lbl.setText("N/A")
                else:
                    lbl.setText(f"{val:.0f}" if isinstance(val, (int, float)) else "--")

        # Update WPM reading
        pace = report.get("speaking_speed", {})
        if isinstance(pace, dict) and pace.get("wpm"):
            self._wpm_lbl.setText(f"SPEED: {pace.get('wpm')} WPM — {pace.get('assessment', '')}")

        # Append to transcript
        transcript = report.get("transcript", "")
        tutor_reply = report.get("tutor_reply", "")
        if transcript:
            self._transcript_view.append(f"<b style='color: {C.PRI};'>You:</b> {transcript}")
        if tutor_reply:
            self._transcript_view.append(f"<b style='color: #38bdf8;'>Jarvis:</b> {tutor_reply}")

        # Highlight tips or errors
        issues = []
        for g in report.get("grammar_issues", []):
            if isinstance(g, dict) and g.get("issue"):
                issues.append(f"Grammar tip: {g.get('issue')} → say '<i>{g.get('correction', '')}</i>'")
        pr = report.get("pronunciation", {})
        if isinstance(pr, dict):
            for p in pr.get("issues", [])[:2]:
                if isinstance(p, dict) and p.get("word"):
                    issues.append(f"Pronunciation: '{p.get('word')}' ({p.get('practice', '')})")
        if issues:
            tips_text = " | ".join(issues)
            self._transcript_view.append(f"<span style='color: #f59e0b;'>💡 {tips_text}</span>")

        self._transcript_view.append("")  # blank separator line
        sb = self._transcript_view.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

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
        self._transcript_view.append("<span style='color: #34d399;'>" + "<br>".join(parts) + "</span>")

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

    def close_tutor(self):
        """Clean close method invoked from backend or UI."""
        self.hide()

    def closeEvent(self, event):
        """Handle standard OS window close ('X' button)."""
        self.close_requested.emit()
        event.accept()

    def show_overlay(self):
        """Display the floating tutor window."""
        self.show()
        self.raise_()
        self.activateWindow()


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
        _window_icon = CONFIG_DIR / "jarvis.ico"
        if _window_icon.exists():
            self.setWindowIcon(QIcon(str(_window_icon)))
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

        # English Tutor floating dialog
        self._tutor_overlay = EnglishTutorOverlay(self)
        self._tutor_overlay.speak_requested.connect(self._send_command)
        self._tutor_overlay.next_question_requested.connect(lambda: self._send_command("START ENGLISH TUTOR SESSION"))
        self._tutor_overlay.score_query_requested.connect(lambda: self._send_command("what is my score"))
        self._tutor_overlay.close_requested.connect(self._on_tutor_close_requested)
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
            # Toggle tutor dialog on/off
            if self._tutor_overlay.isVisible():
                self._tutor_overlay.close()
            else:
                self._tutor_active = True
                self._tutor_overlay.reset_scores()
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

        # ── NEWS panel icon (from mj-1.png) ───────────────────────────────────
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
        """Center English Tutor dialog on screen or relative to main window."""
        if not self._tutor_overlay.isVisible():
            geo = self.geometry()
            x = geo.x() + (geo.width() - self._tutor_overlay.width()) // 2
            y = geo.y() + (geo.height() - self._tutor_overlay.height()) // 2
            self._tutor_overlay.move(max(30, x), max(30, y))

    def _on_tutor_close_requested(self):
        """Handle tutor window close: clean backend exit."""
        self._tutor_active = False
        self._send_command("CLOSE_TUTOR")

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
