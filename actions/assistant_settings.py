"""
Action handler for switching voice gender and persona tone.
Auto-discovered by core/action_loader.py.
"""
from memory.config_manager import (
    save_voice_gender, get_voice_gender,
    save_persona, get_persona, get_persona_instruction,
)

def assistant_settings(parameters: dict, jarvis=None, speak=None, player=None) -> str:
    action = str(parameters.get("action", "")).lower().strip()
    voice_gender = str(parameters.get("voice_gender", "")).lower().strip()
    persona = str(parameters.get("persona", "")).lower().strip()

    # Determine intent
    if voice_gender or "voice" in action or action in ("male", "female", "use_male_voice", "use_female_voice", "switch_voice"):
        target_gender = voice_gender or ("female" if "female" in action else "male")
        saved_g = save_voice_gender(target_gender)
        
        # Voice changes in Gemini Live API require starting a fresh session without the resumption handle;
        # otherwise the server restores the voice from the resumed session state.
        if jarvis is not None and hasattr(jarvis, "request_reconnect"):
            jarvis.request_reconnect(keep_context=False, reason=f"{saved_g} voice switch")
            
        return f"Voice switched to {saved_g} voice."

    if persona or "persona" in action or "act" in action or action in ("girlfriend", "wife", "assistant", "normal", "switch_persona"):
        target_persona = persona or action
        saved_p = save_persona(target_persona)

        # Trigger immediate reconnect on live session to inject updated persona system instruction
        if jarvis is not None and hasattr(jarvis, "request_reconnect"):
            jarvis.request_reconnect(keep_context=True, reason=f"{saved_p} persona switch")

        if saved_p == "girlfriend":
            return "Switched to girlfriend mode. I'm all yours babe!"
        elif saved_p == "wife":
            return "Switched to wife mode. I'm right here with you, my love."
        else:
            return "Switched to standard assistant mode."

    return "No changes made to settings."


TOOL = {
    "name": "assistant_settings",
    "description": (
        "Controls assistant settings: switch voice gender (male/female) or persona tone (girlfriend/wife/assistant). "
        "Use when user says 'use male voice', 'use female voice', 'switch to female voice', 'switch to male voice', "
        "'act as my girlfriend', 'act as my wife', 'act as my assistant', or 'normal mode'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "The requested action: switch_voice | switch_persona | male | female | girlfriend | wife | assistant"
            },
            "voice_gender": {
                "type": "STRING",
                "description": "Optional target voice gender: 'male' or 'female'"
            },
            "persona": {
                "type": "STRING",
                "description": "Optional target persona: 'assistant', 'girlfriend', or 'wife'"
            }
        },
        "required": ["action"]
    },
    "handler": assistant_settings,
}
