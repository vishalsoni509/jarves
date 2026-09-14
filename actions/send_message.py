import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.06
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

try:
    import pywhatkit
    _PYWHATKIT = True
except ImportError:
    _PYWHATKIT = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONTACTS_PATH = _base_dir() / "config" / "contacts.json"


def _get_os() -> str:
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "windows").lower()
    except Exception:
        return "windows"


def _load_contacts() -> dict[str, str]:
    """Loads name-to-phone-number mapping from config/contacts.json."""
    if not CONTACTS_PATH.exists():
        return {}
    try:
        data = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
        return {
            k.lower().strip(): v.strip()
            for k, v in data.items()
            if not k.startswith("_")
        }
    except Exception as e:
        print(f"[SendMessage] ⚠️ Failed to load contacts.json: {e}")
        return {}


def _lookup_contact(name_or_number: str) -> str | None:
    """
    Resolves a friendly contact name or raw input to an E.164 phone number.
    If the input already looks like a phone number, returns it directly.
    """
    raw = name_or_number.strip()

    # Check if raw input is already a phone number
    cleaned = re.sub(r"[^\d+]", "", raw)
    if len(cleaned) >= 7 and (raw.startswith("+") or raw.isdigit()):
        return raw

    contacts = _load_contacts()
    key = raw.lower()

    # 1. Exact match
    if key in contacts:
        return contacts[key]

    # 2. Substring match
    for contact_name, phone in contacts.items():
        if key in contact_name or contact_name in key:
            return phone

    return None


def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _paste_text(text: str) -> None:
    _require_pyautogui()

    os_name = _get_os()
    paste_hotkey = ("command", "v") if os_name == "mac" else ("ctrl", "v")

    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey(*paste_hotkey)
        time.sleep(0.1)
    else:
        pyautogui.write(text, interval=0.03)


def _clear_and_paste(text: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    select_all = ("command", "a") if os_name == "mac" else ("ctrl", "a")
    pyautogui.hotkey(*select_all)
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    _paste_text(text)


def _open_app(app_name: str) -> bool:
    _require_pyautogui()
    os_name = _get_os()

    try:
        if os_name == "windows":
            pyautogui.press("win")
            time.sleep(0.5)
            _paste_text(app_name)
            time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(2.5)
            return True

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True, text=True, timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else:
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def _open_browser_url(url: str) -> bool:
    import webbrowser
    try:
        webbrowser.open(url)
        time.sleep(4.0)
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False


def _search_in_app(query: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    search_hotkey = ("command", "f") if os_name == "mac" else ("ctrl", "f")

    pyautogui.hotkey(*search_hotkey)
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)


def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    if not _open_app(app_name):
        return f"Could not open {app_name}."

    time.sleep(1.0)
    _search_in_app(receiver)
    pyautogui.press("enter")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via {app_name}."


def _send_whatsapp(receiver: str, message: str) -> str:
    """
    Sends WhatsApp message via pywhatkit (WhatsApp Web) if phone contact is resolved,
    or falls back to opening WhatsApp Web directly, or using the desktop app.
    """
    phone = _lookup_contact(receiver)

    if phone and _PYWHATKIT:
        try:
            print(f"[SendMessage] 📱 Using pywhatkit for {receiver} ({phone})")
            pywhatkit.sendwhatmsg_instantly(
                phone_num=phone,
                message=message,
                wait_time=15,
                tab_close=True,
                close_time=3,
            )
            return f"WhatsApp message sent to {receiver} ({phone}) via WhatsApp Web."
        except Exception as e:
            print(f"[SendMessage] ⚠️ pywhatkit failed ({e}), trying fallback")

    if phone:
        clean_phone = phone.replace("+", "")
        encoded_msg = quote_plus(message)
        web_url = f"https://web.whatsapp.com/send?phone={clean_phone}&text={encoded_msg}"
        if _open_browser_url(web_url):
            time.sleep(3.0)
            if _PYAUTOGUI:
                pyautogui.press("enter")
            return f"Opened WhatsApp Web for {receiver} ({phone}) and sent message."

    return _desktop_send("WhatsApp", receiver, message)


def _call_whatsapp(receiver: str, call_type: str = "voice") -> str:
    """
    Initiates a WhatsApp voice or video call.
    Tries desktop deep link (`whatsapp://call?phone=...`), or opens WhatsApp desktop.
    """
    phone = _lookup_contact(receiver)
    clean_phone = phone.replace("+", "") if phone else ""

    print(f"[SendMessage] 📞 Initiating WhatsApp {call_type} call to {receiver}" + (f" ({phone})" if phone else ""))

    if clean_phone:
        deep_link = f"whatsapp://call?phone={clean_phone}"
        try:
            os_name = _get_os()
            if os_name == "windows":
                os.startfile(deep_link)
                time.sleep(2.0)
                return f"Initiated WhatsApp {call_type} call to {receiver} ({phone})."
            elif os_name == "mac":
                subprocess.Popen(["open", deep_link])
                time.sleep(2.0)
                return f"Initiated WhatsApp {call_type} call to {receiver} ({phone})."
            else:
                subprocess.Popen(["xdg-open", deep_link])
                time.sleep(2.0)
                return f"Initiated WhatsApp {call_type} call to {receiver} ({phone})."
        except Exception as e:
            print(f"[SendMessage] ⚠️ WhatsApp deep link failed: {e}")

    # Fallback: Open desktop app, search contact
    if _open_app("WhatsApp"):
        time.sleep(1.0)
        _search_in_app(receiver)
        pyautogui.press("enter")
        time.sleep(1.0)
        return (
            f"Opened WhatsApp conversation with {receiver}, sir. "
            f"Please click the {'call' if call_type == 'voice' else 'video call'} button to start the call."
        )

    return f"Could not initiate WhatsApp call to {receiver}. Ensure WhatsApp is installed or add contact to config/contacts.json."


def _send_telegram(receiver: str, message: str) -> str:
    return _desktop_send("Telegram", receiver, message)


def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("Signal", receiver, message)


def _send_discord(receiver: str, message: str) -> str:
    return _desktop_send("Discord", receiver, message)


def _send_instagram(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.instagram.com/direct/new/"):
        return "Could not open Instagram in browser."

    _paste_text(receiver)
    time.sleep(1.5)

    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.4)

    for _ in range(4):
        pyautogui.press("tab")
        time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(2.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Instagram."


def _send_messenger(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.messenger.com/"):
        return "Could not open Messenger in browser."

    _search_in_app(receiver)
    time.sleep(0.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Messenger."


_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"},              _send_whatsapp),
    ({"telegram", "tg"},                      _send_telegram),
    ({"instagram", "ig", "insta"},            _send_instagram),
    ({"signal"},                               _send_signal),
    ({"discord"},                              _send_discord),
    ({"messenger", "facebook", "fb"},         _send_messenger),
]


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    action       = params.get("action", "send_message").lower().strip()
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip()
    call_type    = params.get("call_type", "voice").strip()

    if action in ("open", "open_app", "open_whatsapp"):
        if _open_app("WhatsApp"):
            return "Opened WhatsApp, sir."
        return "Could not open WhatsApp desktop app, sir."

    if not receiver:
        return "Please specify a recipient."

    if action == "call" or "call" in platform.lower():
        if player:
            player.write_log(f"[call] WhatsApp → {receiver}")
        return _call_whatsapp(receiver, call_type=call_type)

    if not message_text:
        return "Please specify the message content."

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] 📨 {platform} → {receiver}: {preview}")
    if player:
        player.write_log(f"[msg] {platform} → {receiver}")

    try:
        handler = _resolve_platform(platform)
        result  = handler(receiver, message_text)
    except Exception as e:
        result = f"Could not send message: {e}"

    print(f"[SendMessage] {'✅' if 'sent' in result.lower() else '❌'} {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "send_message",
    "description": (
        "Sends text messages, opens WhatsApp, or makes calls via WhatsApp, Telegram, or other messaging platforms. "
        "Use action='send_message' (default) to send a text message to a contact. "
        "Use action='open_whatsapp' when the user says 'open WhatsApp' or 'launch WhatsApp'. "
        "Use action='call' when the user says 'call [contact] on WhatsApp' to initiate a voice or video call. "
        "Contacts are automatically resolved from config/contacts.json."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "send_message | open_whatsapp | call (default: send_message)"
            },
            "receiver": {
                "type": "STRING",
                "description": "Recipient contact name (e.g. 'Rahul', 'Mom') or phone number"
            },
            "message_text": {
                "type": "STRING",
                "description": "The message text to send (required for send_message action)"
            },
            "platform": {
                "type": "STRING",
                "description": "Platform: WhatsApp, Telegram, Instagram, Signal, Discord, Messenger (default: WhatsApp)"
            },
            "call_type": {
                "type": "STRING",
                "description": "voice | video (default: voice, for call action)"
            }
        },
        "required": []
    },
    "handler": send_message,
}
