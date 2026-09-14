#youtube_video.py
import json
import re
import sys
import time
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import pygetwindow as gw
    _PYGETWINDOW = True
except ImportError:
    _PYGETWINDOW = False

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

try:
    import mss
    _MSS = True
except ImportError:
    _MSS = False

from config import get_os, is_windows, is_mac, is_linux


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"

# Module-level handles for the browser Jarvis opens.
# Stored so that the 'close' action can terminate only *this* browser/window
# (not every Chrome/Edge window on the system).
_browser_proc: "subprocess.Popen | None" = None
_browser_window = None


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _remember_browser_window() -> None:
    """Find and remember the browser window opened for the current URL."""
    global _browser_window
    if not _PYGETWINDOW:
        return

    try:
        # Give the OS a moment to create the browser window before we try to
        # capture it. This leaves the rest of the open logic unchanged.
        for _ in range(12):
            try:
                all_windows = gw.getAllWindows()
            except Exception:
                all_windows = []

            for win in all_windows:
                try:
                    title = (win.title or "").lower()
                    if not win.visible or win.width <= 100:
                        continue
                    if "youtube" in title or any(kw in title for kw in _BROWSER_WINDOW_KEYWORDS):
                        _browser_window = win
                        print(f"[YouTube] 📎 Stored browser window ref: '{win.title}'")
                        return
                except Exception:
                    continue
            time.sleep(0.15)
    except Exception as e:
        print(f"[YouTube] ⚠️ Could not remember browser window: {e}")


def _open_url(url: str) -> None:
    """Opens *url* in the OS default browser and stores the process handle."""
    global _browser_proc, _browser_window
    _browser_window = None
    try:
        if is_mac():
            _browser_proc = subprocess.Popen(["open", url])
        elif is_linux():
            _browser_proc = subprocess.Popen(["xdg-open", url])
        else:
            # On Windows, 'start' is a shell built-in; we use cmd /c start.
            # This spawns a short-lived cmd.exe whose job is just to hand off
            # the URL to the default browser — the *actual* browser process is
            # a child of that cmd.exe, not of _browser_proc itself.
            # We store the cmd.exe handle so we can at least get its PID tree
            # for cleanup; the fallback strategy handles the browser window.
            _browser_proc = subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        print(f"[YouTube] 📎 Stored browser proc PID: "
              f"{_browser_proc.pid if _browser_proc else 'n/a'}")
        _remember_browser_window()
    except Exception as e:
        print(f"[YouTube] ⚠️ open_url failed: {e}")

def _scrape_first_video_url(query: str) -> str | None:

    if not _REQUESTS_OK:
        return None

    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )

    try:
        r    = requests.get(search_url, headers=HEADERS, timeout=10)
        html = r.text

        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)

        seen = set()
        for vid in video_ids:
            if vid in seen:
                continue
            seen.add(vid)

            if f'/shorts/{vid}' in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"

    except Exception as e:
        print(f"[YouTube] ⚠️ scrape_first_video_url failed: {e}")

    return None

def _extract_video_id(url: str) -> str | None:
    match = re.search(
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})", url
    )
    return match.group(1) if match else None


def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or ""))


def _ask_for_url(prompt_text: str = "YouTube video URL:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        url = simpledialog.askstring("J.A.R.V.I.S", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ URL dialog failed: {e}")
        return None


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript      = None

        lang_priority = ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]

        try:
            transcript = transcript_list.find_manually_created_transcript(lang_priority)
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(lang_priority)
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        return " ".join(entry["text"] for entry in fetched)

    except Exception as e:
        print(f"[YouTube] ⚠️ Transcript fetch failed: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from google import genai as _genai
    from google.genai import types

    _client = _genai.Client(api_key=_get_api_key())
    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")
    response  = _client.models.generate_content(
        model="gemini-flash-latest",
        contents=f"Please summarize this YouTube video transcript:\n\n{truncated}",
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are JARVIS, an AI assistant. "
                "Summarize YouTube video transcripts clearly and concisely. "
                "Structure: 1-sentence overview, then 3-5 key points. "
                "Be direct. Address the user as 'sir'. "
                "Match the language of the transcript."
            )
        )
    )
    return response.text.strip()


def _save_summary(content: str, video_url: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"youtube_summary_{ts}.txt"
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    header = (
        f"JARVIS — YouTube Summary\n"
        f"{'─' * 50}\n"
        f"URL    : {video_url}\n"
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[YouTube] ⚠️ Could not open text editor: {e}")

    return str(filepath)


def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text
        info = {}

        for key, pattern in [
            ("title",    r'"title":\{"runs":\[\{"text":"([^"]+)"'),
            ("channel",  r'"ownerChannelName":"([^"]+)"'),
            ("views",    r'"viewCount":"(\d+)"'),
            ("duration", r'"lengthSeconds":"(\d+)"'),
            ("likes",    r'"label":"([0-9,]+ likes)"'),
        ]:
            match = re.search(pattern, html)
            if match:
                raw = match.group(1)
                if key == "views":
                    info[key] = f"{int(raw):,}"
                elif key == "duration":
                    secs = int(raw)
                    info[key] = f"{secs // 60}:{secs % 60:02d}"
                else:
                    info[key] = raw

        return info
    except Exception as e:
        print(f"[YouTube] ⚠️ Info scrape failed: {e}")
        return {}


def _scrape_trending(region: str = "TR", max_results: int = 8) -> list[dict]:
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text

        titles   = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', html)
        channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)

        results, seen = [], set()
        for i, title in enumerate(titles):
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            channel = channels[i] if i < len(channels) else "Unknown"
            results.append({"rank": len(results) + 1, "title": title, "channel": channel})
            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        print(f"[YouTube] ⚠️ Trending scrape failed: {e}")
        return []

def _handle_play(parameters: dict, player) -> str:
    query = parameters.get("query", "").strip()
    if not query:
        return "Please tell me what you'd like to watch, sir."

    if player:
        player.write_log(f"[YouTube] Searching: {query}")

    print(f"[YouTube] 🔍 Scraping first non-Shorts video for: {query}")

    video_url = _scrape_first_video_url(query)

    if video_url:
        print(f"[YouTube] ▶️ Opening: {video_url}")
        _open_url(video_url)
        return f"Playing: {query}"

    print(f"[YouTube] ⚠️ Scrape failed, opening filtered search page")
    fallback_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    _open_url(fallback_url)
    return f"Opened YouTube search for: {query} (manual selection required)"


def _handle_summarize(parameters: dict, player, speak) -> str:
    if not _TRANSCRIPT_OK:
        return "youtube-transcript-api is not installed. Run: pip install youtube-transcript-api"

    url = _ask_for_url("Please paste the YouTube video URL:")
    if not url:
        return "No URL provided, sir. Summary cancelled."
    if not _is_valid_youtube_url(url):
        return "That doesn't appear to be a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID from that URL, sir."

    if player:
        player.write_log(f"[YouTube] Summarizing: {url}")
    if speak:
        speak("Fetching the transcript now, sir. One moment.")

    transcript = _get_transcript(video_id)
    if not transcript:
        return "I couldn't retrieve a transcript for that video, sir."

    if speak:
        speak("Transcript retrieved. Generating summary now.")

    try:
        summary = _summarize_with_gemini(transcript, url)
    except Exception as e:
        return f"Summary generation failed, sir: {e}"

    if speak:
        speak(summary)

    if parameters.get("save", False):
        saved_path = _save_summary(summary, url)
        return f"Summary complete and saved to Desktop: {saved_path}"

    return summary


def _handle_get_info(parameters: dict, player, speak) -> str:
    url = parameters.get("url", "").strip()
    if not url:
        url = _ask_for_url("Please paste the YouTube video URL:")
    if not url or not _is_valid_youtube_url(url):
        return "Please provide a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID, sir."

    if player:
        player.write_log(f"[YouTube] Getting info: {url}")

    info = _scrape_video_info(video_id)
    if not info:
        return "Could not retrieve video information, sir."

    lines = [
        f"{key.capitalize()}: {info[key]}"
        for key in ("title", "channel", "views", "duration", "likes")
        if key in info
    ]
    result = "\n".join(lines)

    if speak:
        speak(f"Here's the video info, sir. {result.replace(chr(10), '. ')}")

    return result


def _handle_trending(parameters: dict, player, speak) -> str:
    region = parameters.get("region", "TR").upper()

    if player:
        player.write_log(f"[YouTube] Trending: {region}")

    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Could not fetch trending videos for region {region}, sir."

    lines  = [f"Top trending videos in {region}:"]
    lines += [f"{v['rank']}. {v['title']} — {v['channel']}" for v in trending]
    result = "\n".join(lines)

    if speak:
        top3   = trending[:3]
        spoken = "Here are the top trending videos, sir. " + ". ".join(
            f"Number {v['rank']}: {v['title']} by {v['channel']}" for v in top3
        )
        speak(spoken)

    return result

# ── Fullscreen helpers ───────────────────────────────────────────────────────
_BROWSER_WINDOW_KEYWORDS = (
    "youtube", "google chrome", "microsoft edge", "firefox",
    "opera", "brave", "vivaldi",
)

_SKIP_AD_REGION_KEY = "youtube_skip_ad_region"
_SKIP_AD_DEBOUNCE_SECONDS = 2.0
_LAST_SKIP_CLICK_AT = 0.0
_SKIP_AD_DEBUG = True


def _load_json_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_config(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[YouTube] ⚠️  Could not save config at {path}: {e}")


def _debug_skip_ad_capture(region_img: np.ndarray | None, region: tuple[int, int, int, int]) -> None:
    if not _SKIP_AD_DEBUG or region_img is None:
        return
    try:
        debug_path = BASE_DIR / "config" / "skip_region_debug.png"
        ok = cv2.imwrite(str(debug_path), region_img)
        print(f"[YouTube] [DEBUG] saved skip region capture -> {debug_path} ({ok}) region={region}")
    except Exception as e:
        print(f"[YouTube] [DEBUG] failed to save skip region capture: {e}")


def _get_skip_ad_region() -> tuple[int, int, int, int]:
    cfg_path = BASE_DIR / "config" / "api_keys.json"
    cfg = _load_json_config(cfg_path)
    raw = cfg.get(_SKIP_AD_REGION_KEY, [])

    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            region = tuple(int(v) for v in raw)
            if all(v >= 0 for v in region):
                print(f"[YouTube] [DEBUG] skip-ad region loaded from config: {region}")
                return region
        except Exception:
            pass

    if _PYAUTOGUI:
        try:
            w, h = pyautogui.size()
            region = (
                max(0, int(w * 0.62)),
                max(0, int(h * 0.72)),
                max(1, int(w * 0.28)),
                max(1, int(h * 0.20)),
            )
            print(f"[YouTube] [DEBUG] skip-ad region fell back to screen-based defaults: {region}")
            return region
        except Exception:
            pass

    print("[YouTube] [DEBUG] skip-ad region fallback returned minimal rectangle: (0, 0, 1, 1)")
    return (0, 0, 1, 1)


def _save_skip_ad_region(region: tuple[int, int, int, int]) -> None:
    cfg_path = BASE_DIR / "config" / "api_keys.json"
    cfg = _load_json_config(cfg_path)
    cfg[_SKIP_AD_REGION_KEY] = [int(v) for v in region]
    _save_json_config(cfg_path, cfg)


def _calibrate_skip_ad_region(region: tuple[int, int, int, int] | None = None) -> tuple[int, int, int, int]:
    if region is None:
        w, h = pyautogui.size() if _PYAUTOGUI else (1280, 720)
        region = (
            max(0, int(w * 0.62)),
            max(0, int(h * 0.72)),
            max(1, int(w * 0.28)),
            max(1, int(h * 0.20)),
        )
    _save_skip_ad_region(region)
    return region


def _capture_region_np(region: tuple[int, int, int, int]) -> np.ndarray | None:
    """Captures a specific screen region as a numpy array using mss, PIL ImageGrab, or pyautogui."""
    rx, ry, rw, rh = region
    if _MSS and _NUMPY and _CV2:
        try:
            with mss.mss() as sct:
                monitor = {"left": rx, "top": ry, "width": rw, "height": rh}
                sct_img = sct.grab(monitor)
                img = np.array(sct_img)
                return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception:
            pass

    if _NUMPY:
        try:
            from PIL import ImageGrab
            bbox = (rx, ry, rx + rw, ry + rh)
            img = ImageGrab.grab(bbox=bbox)
            arr = np.array(img)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if _CV2 else arr
        except Exception:
            pass

    if _PYAUTOGUI and _NUMPY:
        try:
            arr = np.array(pyautogui.screenshot(region=region))
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if _CV2 else arr
        except Exception:
            pass

    return None


def _locate_skip_button(region: tuple[int, int, int, int] | None = None) -> tuple[int, int] | None:
    if not _PYAUTOGUI:
        return None

    region = region or _get_skip_ad_region()
    print(f"[YouTube] [DEBUG] searching skip button in region={region}")
    region_img = _capture_region_np(region)
    if region_img is None:
        print("[YouTube] [DEBUG] skip button search aborted: region capture returned None")
        return None

    _debug_skip_ad_capture(region_img, region)

    skip_img_path = BASE_DIR / "config" / "skip_ad.png"
    if skip_img_path.exists() and _CV2 and _NUMPY:
        try:
            template = cv2.imread(str(skip_img_path))
            if template is not None:
                th, tw = template.shape[:2]
                res = cv2.matchTemplate(region_img, template, cv2.TM_CCOEFF_NORMED)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
                print(
                    f"[YouTube] [DEBUG] cv2.matchTemplate score={max_val:.4f} "
                    f"threshold=0.70 region={region} template={skip_img_path.name}"
                )
                if max_val >= 0.70:
                    cx = region[0] + max_loc[0] + (tw // 2)
                    cy = region[1] + max_loc[1] + (th // 2)
                    print(f"[YouTube] [DEBUG] skip button matched via template at ({cx}, {cy}) score={max_val:.4f}")
                    return (cx, cy)
                print(f"[YouTube] [DEBUG] skip template score below threshold: {max_val:.4f}")
        except Exception as e:
            print(f"[YouTube] ⚠️ cv2.matchTemplate search failed: {e}")

    # Fallback template match via pyautogui locateCenterOnScreen
    if skip_img_path.exists():
        try:
            pos = pyautogui.locateCenterOnScreen(str(skip_img_path), region=region, confidence=0.70)
            print(f"[YouTube] [DEBUG] pyautogui locateCenterOnScreen result={pos}")
            if pos:
                return (int(pos[0]), int(pos[1]))
        except Exception as e:
            print(f"[YouTube] [DEBUG] pyautogui locateCenterOnScreen failed: {e}")

    # High-contrast contour fallback detection for YouTube skip button (dark pill with white text)
    if _CV2 and _NUMPY:
        try:
            gray = cv2.cvtColor(region_img, cv2.COLOR_BGR2GRAY) if len(region_img.shape) == 3 else region_img
            _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            best = None
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 120:
                    continue
                x, y, w, h = cv2.boundingRect(contour)
                if w < 20 or h < 10:
                    continue
                aspect = w / max(h, 1)
                if aspect < 0.8 or aspect > 4.0:
                    continue
                if best is None or area > best[0]:
                    best = (area, x, y, w, h)

            if best:
                area, x, y, w, h = best
                print(f"[YouTube] [DEBUG] contour fallback found candidate area={area} size=({w}x{h})")
                return (
                    int(region[0] + x + (w / 2)),
                    int(region[1] + y + (h / 2)),
                )
            print("[YouTube] [DEBUG] contour fallback found no viable button candidate")
        except Exception as e:
            print(f"[YouTube] ⚠️ Skip-button fallback detection failed: {e}")

    print("[YouTube] [DEBUG] skip button detection failed for this cycle")
    return None


def is_skip_button_visible() -> bool:
    """Simple state check for the existing command layer."""
    return _locate_skip_button() is not None


def _focus_browser_window() -> bool:
    """
    Attempts to bring a browser window that contains a known keyword to the
    foreground so that a subsequent keypress lands in the right window.
    Returns True if a window was successfully focused, False otherwise.
    """
    if not _PYGETWINDOW:
        print("[YouTube] ⚠️ pygetwindow not available — cannot focus browser window")
        return False

    try:
        all_windows = gw.getAllWindows()
    except Exception as e:
        print(f"[YouTube] ⚠️ pygetwindow.getAllWindows() failed: {e}")
        return False

    # Prefer a window whose title includes "YouTube" first, then any browser
    for priority_kw in ("youtube", *_BROWSER_WINDOW_KEYWORDS[1:]):
        for win in all_windows:
            try:
                title = (win.title or "").lower()
                if priority_kw in title and win.visible and win.width > 100:
                    win.activate()
                    import time as _time
                    _time.sleep(0.3)   # give the OS time to actually focus
                    print(f"[YouTube] 🖥️  Focused window: '{win.title}'")
                    return True
            except Exception:
                continue

    print("[YouTube] ⚠️ No suitable browser window found to focus")
    return False


def _handle_fullscreen(parameters: dict, player) -> str:
    """
    Toggles YouTube fullscreen by pressing 'f'.
    YouTube's HTML5 player uses the F key (not F11) to toggle fullscreen.
    We focus the browser window first so the keypress is not lost.
    """
    if not _PYAUTOGUI:
        return "PyAutoGUI is not installed. Run: pip install pyautogui"

    if player:
        player.write_log("[YouTube] Fullscreen toggle")
    print("[YouTube] 🖥️  Toggling fullscreen")

    focused = _focus_browser_window()
    if not focused:
        # Still attempt the keypress — it may work if the window is already focused
        print("[YouTube] ⚠️  Proceeding without confirmed window focus")

    try:
        import time as _time
        _time.sleep(0.15)         # small settle time
        pyautogui.press("f")      # YouTube fullscreen toggle key
        return "Fullscreen toggled, sir."
    except Exception as e:
        return f"Could not send fullscreen key: {e}"


def _handle_skip_ad(parameters: dict, player) -> str:
    """
    Attempts to skip the current YouTube ad by detecting the fixed skip button
    region first, then clicking the button. Falls back to a best-effort
    keyboard path only if the button is not visible.
    """
    if not _PYAUTOGUI:
        return "PyAutoGUI is not installed."

    if player:
        player.write_log("[YouTube] Skip ad requested")

    global _LAST_SKIP_CLICK_AT

    _focus_browser_window()
    import time as _time
    _time.sleep(0.2)

    region = _get_skip_ad_region()
    pos = _locate_skip_button(region)

    if pos:
        now = time.monotonic()
        if now - _LAST_SKIP_CLICK_AT < _SKIP_AD_DEBOUNCE_SECONDS:
            print(f"[YouTube] [DEBUG] skip debounce active: last click {now - _LAST_SKIP_CLICK_AT:.2f}s ago")
            return "Skip button already handled recently, sir."
        try:
            pyautogui.click(pos)
            _LAST_SKIP_CLICK_AT = now
            print(f"[YouTube] [DEBUG] clicked skip button at {pos}")
            return "Skipped, sir."
        except Exception as e:
            return f"Could not click the skip button: {e}"

    return "No ad or skip button is currently available, sir."


# ── Close browser helpers ─────────────────────────────────────────────────
def _close_via_proc_handle() -> bool:
    """Tier 1: terminate the exact process Jarvis launched."""
    global _browser_proc
    if _browser_proc is None:
        return False
    try:
        poll = _browser_proc.poll()
        if poll is None:  # still running
            _browser_proc.terminate()
            import time as _time
            _time.sleep(0.4)
            if _browser_proc.poll() is None:
                _browser_proc.kill()   # force-kill if terminate didn't work
        _browser_proc = None
        print("[YouTube] ✅ Browser closed via process handle")
        return True
    except Exception as e:
        print(f"[YouTube] ⚠️ proc.terminate() failed: {e}")
        _browser_proc = None
        return False


def _close_via_browser_window() -> bool:
    """Tier 2: close the specific browser window Jarvis opened."""
    global _browser_window
    if _browser_window is None:
        return False

    try:
        if getattr(_browser_window, "visible", False):
            _browser_window.close()
            print(f"[YouTube] ✅ Closed remembered browser window: '{_browser_window.title}'")
        _browser_window = None
        return True
    except Exception as e:
        print(f"[YouTube] ⚠️ remembered browser window close failed: {e}")
        _browser_window = None
        return False


def _close_via_pygetwindow() -> bool:
    """Tier 3: find a YouTube/browser window by title and close it."""
    if not _PYGETWINDOW:
        return False
    try:
        import time as _time
        all_windows = gw.getAllWindows()
        for kw in ("youtube", "google chrome", "microsoft edge",
                   "firefox", "opera", "brave", "vivaldi"):
            for win in all_windows:
                title = (win.title or "").lower()
                if kw in title and win.visible:
                    try:
                        win.close()
                        _time.sleep(0.3)
                        print(f"[YouTube] ✅ Closed window via pygetwindow: '{win.title}'")
                        return True
                    except Exception as e:
                        print(f"[YouTube] ⚠️ pygetwindow.close() failed for '{win.title}': {e}")
    except Exception as e:
        print(f"[YouTube] ⚠️ pygetwindow window scan failed: {e}")
    return False


def _close_via_taskkill(pid: int | None = None) -> bool:
    """
    Tier 3 (Windows only): taskkill by PID.
    We NEVER kill by image name (chrome.exe) as that would close ALL Chrome
    windows. We only use /PID which targets a single process.
    """
    import platform as _platform
    if _platform.system() != "Windows":
        return False
    if pid is None:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0:
            print(f"[YouTube] ✅ Closed process PID {pid} via taskkill")
            return True
        print(f"[YouTube] ⚠️ taskkill failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"[YouTube] ⚠️ taskkill exception: {e}")
    return False


def _handle_close_browser(parameters: dict, player) -> str:
    """
    Closes the browser window that Jarvis opened.
    Strategy (in order):
      1. Terminate the stored process handle (exact PID, cleanest)
      2. Find and close a YouTube/browser window via pygetwindow
      3. taskkill /PID <pid> (Windows only, still targets only one process)
    """
    global _browser_proc

    if player:
        player.write_log("[YouTube] Close browser")
    print("[YouTube] 🗑️  Attempting to close browser window")

    # Capture PID before Tier 1 clears the handle
    saved_pid = _browser_proc.pid if _browser_proc else None

    # Tier 1: close the remembered browser window if one was captured.
    if _close_via_browser_window():
        return "Browser window closed, sir."

    # Tier 2: terminate the exact process Jarvis launched.
    if _close_via_proc_handle():
        return "Browser closed, sir."

    # Tier 3: find a matching browser window by title.
    if _close_via_pygetwindow():
        return "Browser window closed, sir."

    # Tier 4 (Windows only, PID-targeted)
    if saved_pid and _close_via_taskkill(saved_pid):
        return "Browser process terminated, sir."

    return (
        "Could not close the browser automatically, sir. "
        "Please close it manually. "
        "(Tip: if Jarvis opened Chrome, it may have been opened as a child of cmd.exe "
        "and the handle may have expired.)"
    )


_ACTION_MAP = {
    "play":       _handle_play,
    "summarize":  _handle_summarize,
    "get_info":   _handle_get_info,
    "trending":   _handle_trending,
    "fullscreen": _handle_fullscreen,
    "skip_ad":    _handle_skip_ad,
    "close":      _handle_close_browser,
}


def youtube_video(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "play").lower().strip()
    if action in {"skip", "skip_this_ad", "skip_ad"}:
        action = "skip_ad"

    if player:
        player.write_log(f"[YouTube] Action: {action}")
    print(f"[YouTube] ▶️  Action: {action}  Params: {params}")

    handler = _ACTION_MAP.get(action)
    if handler is None:
        return (
            f"Unknown YouTube action: '{action}'. "
            "Available: play, summarize, get_info, trending, fullscreen, skip_ad, close."
        )

    try:
        # Handlers that only need (params, player)
        if action in ("play", "fullscreen", "skip_ad", "close"):
            return handler(params, player) or "Done."
        # Handlers that also need speak
        return handler(params, player, speak) or "Done."
    except Exception as e:
        print(f"[YouTube] ❌ Error in {action}: {e}")
        return f"YouTube {action} failed, sir: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "youtube_video",
    "description": (
        "Controls YouTube. Use for: playing videos, summarizing a video's content, "
        "getting video info, showing trending videos, toggling fullscreen on the "
        "currently playing video, skipping advertisements, or closing the browser window opened by Jarvis. "
        "Use action='fullscreen' when the user says 'full screen', 'make it full screen', "
        "'press f', 'fullscreen mode', or similar. "
        "Use action='skip_ad' when the user says 'skip ad', 'skip this ad', 'skip advertisement', "
        "or just 'skip'. "
        "Use action='close' when the user says 'close the browser', 'close this', "
        "'close the window', or 'close YouTube'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | summarize | get_info | trending | fullscreen | skip_ad | close (default: play)"
            },
            "query": {
                "type": "STRING",
                "description": "Search query for play action"
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Save summary to Notepad (summarize only)"
            },
            "region": {
                "type": "STRING",
                "description": "Country code for trending e.g. TR, US"
            },
            "url": {
                "type": "STRING",
                "description": "Video URL for get_info action"
            }
        },
        "required": []
    },
    "handler": youtube_video,
}
