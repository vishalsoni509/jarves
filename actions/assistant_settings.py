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
        
        greeting = (
            "Say a short, cheerful sentence in your new female voice confirming that you are now speaking in female voice and ask how you can help."
            if saved_g == "female"
            else "Say a short, confident sentence in your new male voice confirming that you are now speaking in male voice and ask how you can help."
        )

        # Voice changes in Gemini Live API require starting a fresh session without the resumption handle;
        # otherwise the server restores the voice from the resumed session state.
        if jarvis is not None and hasattr(jarvis, "request_reconnect"):
            jarvis.request_reconnect(keep_context=False, reason=f"{saved_g} voice switch", greeting=greeting)
            
        return f"Voice switched to {saved_g} voice."

    if persona or "persona" in action or "act" in action or any(w in action for w in ("girlfriend", "wife", "assistant", "normal", "switch_persona", "mj", "karen", "keron")):
        target_persona = persona or action
        if "mj" in target_persona or "girlfriend" in target_persona or "gf" in target_persona:
            target_persona = "girlfriend"
        elif "karen" in target_persona or "keron" in target_persona or "wife" in target_persona:
            target_persona = "wife"

        saved_p = save_persona(target_persona)

        # In girlfriend or wife mode, automatically switch to female voice
        if saved_p in ("girlfriend", "wife"):
            save_voice_gender("female")

        if saved_p == "girlfriend":
            greeting = "Greet the user warmly, sweetly, and lovingly as your boyfriend in your new voice right now. Introduce yourself as MJ, his girlfriend, and tell him you are here for him in girlfriend mode."
            msg = "Switched to girlfriend mode. Hey babe, I'm MJ! I'm all yours."
        elif saved_p == "wife":
            greeting = "Greet the user warmly, lovingly, and tenderly as your husband in your new voice right now. Introduce yourself as Karen, his wife, and tell him you are right here with him in wife mode."
            msg = "Switched to wife mode. Hello my love, I'm Karen. I'm right here with you."
        else:
            greeting = "Say a short professional confirmation sentence in your new voice that you are now in standard assistant mode and ready to help."
            msg = "Switched to standard assistant mode."

        # Trigger immediate reconnect on live session to inject updated voice & persona system instruction
        if jarvis is not None and hasattr(jarvis, "request_reconnect"):
            jarvis.request_reconnect(keep_context=False, reason=f"{saved_p} persona switch", greeting=greeting)

        return msg

    return "No changes made to settings."


TOOL = {
    "name": "assistant_settings",
    "description": (
        "Controls assistant settings: switch voice gender (male/female) or persona tone (girlfriend [MJ] / wife [Karen] / assistant). "
        "Use when user says 'use male voice', 'use female voice', 'switch to female voice', 'switch to male voice', "
        "'act as my girlfriend', 'act as MJ', 'act as my wife', 'act as Karen', 'act as my assistant', or 'normal mode'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "The requested action: switch_voice | switch_persona | male | female | girlfriend | mj | wife | karen | assistant"
            },
            "voice_gender": {
                "type": "STRING",
                "description": "Optional target voice gender: 'male' or 'female'"
            },
            "persona": {
                "type": "STRING",
                "description": "Optional target persona: 'assistant', 'girlfriend' (MJ), or 'wife' (Karen)"
            }
        },
        "required": ["action"]
    },
    "handler": assistant_settings,
}
