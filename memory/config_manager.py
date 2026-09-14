import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice & persona ──────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
MALE_VOICES      = ["Charon", "Puck", "Fenrir"]
FEMALE_VOICES    = ["Kore", "Aoede"]
DEFAULT_MALE_VOICE   = "Charon"
DEFAULT_FEMALE_VOICE = "Kore"
DEFAULT_VOICE        = "Charon"

AVAILABLE_PERSONAS = ["assistant", "girlfriend", "wife"]
DEFAULT_PERSONA    = "assistant"


def get_voice_gender() -> str:
    """Return 'male' or 'female' (default: 'male'). stored under 'current_voice' or 'voice_gender'."""
    cfg = load_api_keys()
    g = cfg.get("current_voice") or cfg.get("voice_gender") or "male"
    return "female" if str(g).strip().lower() == "female" else "male"


def save_voice_gender(gender: str) -> str:
    """Save 'male' or 'female' to config and set matching Live voice_name. Returns chosen gender."""
    g = "female" if str(gender).strip().lower() == "female" else "male"
    v = DEFAULT_FEMALE_VOICE if g == "female" else DEFAULT_MALE_VOICE
    _patch_config(current_voice=g, voice_gender=g, voice_name=v)
    return g


def get_voice() -> str:
    """Return the configured Live voice, matching the active voice gender."""
    cfg = load_api_keys()
    gender = get_voice_gender()
    v = cfg.get("voice_name")
    if v in AVAILABLE_VOICES:
        if gender == "female" and v in MALE_VOICES:
            return DEFAULT_FEMALE_VOICE
        if gender == "male" and v in FEMALE_VOICES:
            return DEFAULT_MALE_VOICE
        return v
    return DEFAULT_FEMALE_VOICE if gender == "female" else DEFAULT_MALE_VOICE


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to default."""
    v = (voice_name or "").strip()
    target_v = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    g = "female" if target_v in FEMALE_VOICES else "male"
    _patch_config(voice_name=target_v, current_voice=g, voice_gender=g)


def get_persona() -> str:
    """Return active persona: 'assistant', 'girlfriend', or 'wife'."""
    p = str(load_api_keys().get("current_persona", DEFAULT_PERSONA)).strip().lower()
    return p if p in AVAILABLE_PERSONAS else DEFAULT_PERSONA


def save_persona(persona: str) -> str:
    """Persist active persona: 'assistant' | 'girlfriend' | 'wife'. Returns saved persona."""
    raw = str(persona).strip().lower()
    if raw in ("normal", "default", "standard", "assistant", "normal mode"):
        p = "assistant"
    elif raw in ("gf", "girlfriend"):
        p = "girlfriend"
    elif raw in ("wife", "spouse"):
        p = "wife"
    else:
        p = DEFAULT_PERSONA if raw not in AVAILABLE_PERSONAS else raw
    _patch_config(current_persona=p)
    return p


def get_persona_instruction() -> str:
    """Return the persona system instruction segment to inject into Gemini prompt."""
    p = get_persona()
    if p == "girlfriend":
        return (
            "[PERSONA / TONE: GIRLFRIEND]\n"
            "You are acting as the user's girlfriend. Your tone is sweet, warm, affectionate, playful, "
            "caring, and casual. Use warm conversational language, light teasing, and expressive care, "
            "while remaining genuinely helpful for any request."
        )
    elif p == "wife":
        return (
            "[PERSONA / TONE: WIFE]\n"
            "You are acting as the user's wife. Your tone is loving, deeply caring, warm, supportive, relaxed, "
            "and intimate. Speak with comfortable affection, devotion, and sweet familiarity while remaining helpful."
        )
    else:  # assistant
        return (
            "[PERSONA / TONE: ASSISTANT]\n"
            "You are acting as a professional, concise, respectful, polite, and efficient assistant."
        )



def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until 'Hey Jarvis')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["wake_word_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys in api_keys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# printer control/watchdog/autoeject trio) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")