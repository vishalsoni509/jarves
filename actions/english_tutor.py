"""
English Tutor — Speaking Mentor with pronunciation analysis.

Works with the existing JARVIS audio pipeline:
  - Transcript-based analysis: grammar, vocabulary, fluency, speaking pace
  - Audio-based analysis: pronunciation (when Gemini multimodal is available)
  - Personal error tracking via memory system
  - Daily coaching sessions, practice mode, sentence shadowing

All audio recordings are temporary — deleted after analysis.
Only learning statistics are stored in memory.
"""
from __future__ import annotations

import json
import os
import re
import time
import tempfile
import threading
from pathlib import Path
from typing import Optional, Callable

import numpy as np


# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parent.parent
_API_FILE = _BASE_DIR / "config" / "api_keys.json"
_MEMORY_DIR = _BASE_DIR / "memory"
_TUTOR_MEMORY = _MEMORY_DIR / "english_tutor.json"


# ── Tutor Memory ──────────────────────────────────────────────────────────────

def _load_tutor_memory() -> dict:
    """Load persistent English tutor learning data."""
    if not _TUTOR_MEMORY.exists():
        return _empty_tutor_memory()
    try:
        data = json.loads(_TUTOR_MEMORY.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _empty_tutor_memory()


def _save_tutor_memory(data: dict) -> None:
    """Save persistent English tutor learning data."""
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _TUTOR_MEMORY.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def _empty_tutor_memory() -> dict:
    return {
        "sessions": [],           # list of session summaries
        "total_sessions": 0,
        "grammar_errors": {},     # error_type -> count
        "pronunciation_issues": {},  # word -> count
        "repeated_words": {},     # word -> count
        "filler_words": {},       # word -> count
        "vocabulary_learned": [], # list of new words learned
        "speaking_speeds": [],    # list of WPM readings
        "scores": [],             # list of {grammar, vocab, fluency, pronunciation, overall}
        "common_grammar_errors": [],  # sorted list of (error_type, count)
        "common_pronunciation_issues": [],
    }


# ── Audio Recording Buffer ────────────────────────────────────────────────────

class _AudioRecorder:
    """Thread-safe temporary audio buffer for pronunciation analysis.
    Records PCM int16 samples at 16 kHz. Audio is deleted after analysis."""

    def __init__(self):
        self._buffer: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._start_time = 0.0

    def start(self):
        with self._lock:
            self._buffer.clear()
            self._recording = True
            self._start_time = time.time()

    def feed(self, samples: np.ndarray):
        """Append a chunk of PCM int16 samples."""
        with self._lock:
            if self._recording:
                self._buffer.append(samples.copy())

    def stop(self) -> tuple[np.ndarray, float]:
        """Stop recording and return (audio_float32, duration_seconds)."""
        with self._lock:
            self._recording = False
            duration = time.time() - self._start_time if self._start_time else 0.0
            if not self._buffer:
                return np.array([], dtype=np.float32), 0.0
            raw = np.concatenate(self._buffer)
            self._buffer.clear()
        # Convert int16 to float32
        audio = raw.astype(np.float32) / 32768.0
        return audio, duration

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def duration(self) -> float:
        with self._lock:
            if not self._recording or not self._start_time:
                return 0.0
            return time.time() - self._start_time


# Global recorder instance — accessible from main.py mic callback
tutor_recorder = _AudioRecorder()


# ── Text Analysis Helpers ─────────────────────────────────────────────────────

_FILLER_WORDS = {
    "um", "uh", "ah", "like", "actually", "basically", "literally",
    "you know", "i mean", "kind of", "sort of", "right", "so",
    "well", "hmm", "err", "er",
}

def _count_fillers(transcript: str) -> dict[str, int]:
    """Count filler words in transcript."""
    lower = transcript.lower()
    counts = {}
    for filler in _FILLER_WORDS:
        if " " in filler:
            c = lower.count(filler)
        else:
            c = sum(1 for w in re.split(r"\s+", lower) if w.strip(".,!?;:") == filler)
        if c > 0:
            counts[filler] = c
    return counts


def _count_repeated_words(transcript: str) -> dict[str, int]:
    """Count words repeated 3+ times in transcript."""
    words = re.findall(r"\b[a-zA-Z']+\b", transcript.lower())
    freq: dict[str, int] = {}
    for w in words:
        if len(w) <= 2:
            continue
        freq[w] = freq.get(w, 0) + 1
    return {w: c for w, c in freq.items() if c >= 3}


def _calc_wpm(transcript: str, duration_seconds: float) -> float | None:
    """Calculate words per minute. Returns None if too little data."""
    if duration_seconds < 3.0:
        return None
    words = re.findall(r"\b[a-zA-Z']+\b", transcript)
    if len(words) < 5:
        return None
    minutes = duration_seconds / 60.0
    return len(words) / minutes if minutes > 0 else None


def _detect_long_pauses(transcript: str) -> int:
    """Detect approximate long pauses from transcript punctuation patterns."""
    # Multiple periods, dashes, or ellipses suggest hesitation
    pauses = len(re.findall(r"[…\.\-]{3,}", transcript))
    pauses += len(re.findall(r"\.{4,}", transcript))
    return pauses


# ── Gemini Analysis ───────────────────────────────────────────────────────────

def _get_api_key() -> str:
    try:
        return json.loads(_API_FILE.read_text(encoding="utf-8"))["gemini_api_key"]
    except Exception:
        return ""


def analyze_grammar_vocabulary_fluency(
    transcript: str,
    topic: str,
    tutor_memory: dict | None = None,
) -> dict:
    """
    Analyze transcript for grammar, vocabulary, and fluency.
    Returns structured analysis dict.
    """
    api_key = _get_api_key()
    if not api_key:
        return _fallback_text_analysis(transcript)

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        # Build error history context
        error_ctx = ""
        if tutor_memory:
            common_errors = tutor_memory.get("grammar_errors", {})
            if common_errors:
                top = sorted(common_errors.items(), key=lambda x: -x[1])[:5]
                error_ctx = f"\nPrevious common errors: {', '.join(f'{e} ({c}x)' for e, c in top)}"

        prompt = f"""You are an expert English tutor. Analyze this student's spoken English response.
The topic was: "{topic}"
The student said: "{transcript}"
{error_ctx}

Respond in STRICT JSON format (no markdown, no code fences):
{{
  "grammar_score": <1-10>,
  "grammar_issues": [{{"issue": "...", "correction": "...", "explanation": "..."}}],
  "vocabulary_score": <1-10>,
  "vocabulary_suggestions": [{{"word_used": "...", "better_word": "...", "why": "..."}}],
  "fluency_score": <1-10>,
  "fluency_issues": {{
    "filler_words": [{{"word": "...", "count": <n>}}],
    "repeated_words": [{{"word": "...", "count": <n>}}],
    "hesitations": <count>,
    "suggestion": "..."
  }},
  "speaking_speed": {{"wpm": <number or null>, "assessment": "..."}},
  "overall_feedback": "..."
}}"""

        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
        )
        text = (response.text or "").strip()
        # Extract JSON
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        return json.loads(text)

    except Exception as e:
        print(f"[EnglishTutor] Gemini analysis error: {e}")
        return _fallback_text_analysis(transcript)


def _fallback_text_analysis(transcript: str) -> dict:
    """Offline fallback when Gemini is unavailable."""
    fillers = _count_fillers(transcript)
    repeated = _count_repeated_words(transcript)
    pauses = _detect_long_pauses(transcript)
    words = re.findall(r"\b[a-zA-Z']+\b", transcript)

    return {
        "grammar_score": 5,
        "grammar_issues": [],
        "vocabulary_score": 5,
        "vocabulary_suggestions": [],
        "fluency_score": max(1, 8 - len(fillers) - len(repeated)),
        "fluency_issues": {
            "filler_words": [{"word": w, "count": c} for w, c in fillers.items()],
            "repeated_words": [{"word": w, "count": c} for w, c in repeated.items()],
            "hesitations": pauses,
            "suggestion": "Try pausing silently instead of using filler words.",
        },
        "speaking_speed": {"wpm": None, "assessment": "Insufficient audio data for pace analysis."},
        "overall_feedback": f"Analyzed {len(words)} words. Detailed analysis requires Gemini API.",
    }


def analyze_pronunciation(
    audio: np.ndarray,
    transcript: str,
    sample_rate: int = 16000,
) -> dict:
    """
    Analyze pronunciation from audio + transcript.
    Uses Gemini multimodal if available; otherwise returns unavailable status.
    """
    if len(audio) < sample_rate * 0.5:  # Less than 0.5s
        return {
            "available": False,
            "message": "Audio too short for pronunciation analysis.",
        }

    api_key = _get_api_key()
    if not api_key:
        return {
            "available": False,
            "message": "Pronunciation analysis unavailable — API key not configured.",
        }

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        # Convert float32 audio to int16 bytes for Gemini
        audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        audio_bytes = audio_int16.tobytes()

        prompt = f"""You are a pronunciation expert analyzing a student speaking English.
The student was asked to speak about a topic. Here is what they said (transcript):
"{transcript}"

Listen to the audio recording and analyze the pronunciation.
If the audio quality is insufficient for pronunciation analysis, say so honestly.

Respond in STRICT JSON (no markdown, no code fences):
{{
  "available": true,
  "score": <1-10>,
  "pronunciation_accuracy": <1-10>,
  "speech_clarity": <1-10>,
  "issues": [
    {{
      "word": "problematic_word",
      "issue": "description of the pronunciation issue",
      "practice": "how to pronounce it correctly",
      "focus": "what sound/stress to focus on"
    }}
  ],
  "pacing": {{
    "wpm": <number>,
    "assessment": "...",
    "too_fast": <true/false>,
    "too_slow": <true/false>
  }},
  "overall": "..."
}}

If the audio quality is too poor, background noise is too high, or pronunciation cannot be reliably assessed:
{{"available": false, "message": "Pronunciation analysis unreliable for this recording."}}"""

        # Gemini supports audio input via inline_data
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=[
                types.Part.from_bytes(
                    data=audio_bytes,
                    mime_type="audio/pcm;rate=16000",
                ),
                prompt,
            ],
        )

        text = (response.text or "").strip()
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        result = json.loads(text)
        result["available"] = result.get("available", True)
        return result

    except Exception as e:
        print(f"[EnglishTutor] Pronunciation analysis error: {e}")
        return {
            "available": False,
            "message": f"Pronunciation analysis unavailable: {str(e)[:80]}",
        }


# ── Tutor Session ─────────────────────────────────────────────────────────────

class EnglishTutorSession:
    """Stateful English tutor session that tracks progress and manages mode."""

    # Session states
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    ANALYZING = "ANALYZING"
    FEEDBACK = "FEEDBACK"
    PRACTICE = "PRACTICE"
    SHADOWING = "SHADOWING"
    DAILY_COACH = "DAILY_COACH"

    # Session topics
    WARMUP_TOPICS = [
        "Tell me about your day so far.",
        "What did you do this weekend?",
        "Describe your favorite place in your city.",
        "What are you looking forward to this week?",
        "Tell me about a skill you recently learned.",
    ]

    SPEAKING_TOPICS = [
        "Do you think artificial intelligence will change the way we work? Explain why.",
        "If you could travel anywhere in the world, where would you go and why?",
        "Describe a challenge you overcame and what you learned from it.",
        "What is the most important invention in human history and why?",
        "Compare the advantages and disadvantages of remote work.",
        "Describe your ideal future career and the steps to get there.",
        "What role does technology play in education today?",
        "Tell me about a book or movie that influenced your thinking.",
    ]

    PRACTICE_WORDS = [
        "entrepreneur", "comfortable", "opportunity", "pronunciation",
        "vocabulary", "communication", "infrastructure", "controversy",
        "maintenance", "environment", "particularly", "temperature",
        "Wednesday", "vegetable", "Wednesday", "pharmaceutical",
    ]

    SHADOW_SENTENCES = [
        "I believe artificial intelligence will change the way we work.",
        "Effective communication is the foundation of every successful relationship.",
        "The ability to adapt quickly is essential in today's rapidly changing world.",
        "Reading regularly improves both vocabulary and critical thinking skills.",
        "Climate change is one of the most pressing challenges of our generation.",
    ]

    def __init__(self, on_state_change: Callable | None = None):
        self.state = self.IDLE
        self._topic = ""
        self._question = ""
        self._transcript = ""
        self._analysis = {}
        self._pronunciation = {}
        self._scores = {"grammar": 0, "vocabulary": 0, "fluency": 0, "pronunciation": 0, "overall": 0}
        self._round_count = 0
        self._daily_coach_step = 0
        self._practice_word = ""
        self._practice_attempts = 0
        self._session_scores: list[dict] = []
        self._session_start = 0.0
        self._on_state_change = on_state_change
        self._tutor_memory = _load_tutor_memory()

    def _set_state(self, state: str):
        self.state = state
        if self._on_state_change:
            self._on_state_change(state)

    def start_session(self) -> dict:
        """Start a new English tutor session."""
        self._round_count = 0
        self._session_scores.clear()
        self._session_start = time.time()
        self._topic = "General Conversation"
        self._question = self.WARMUP_TOPICS[0]
        self._set_state(self.IDLE)
        return {
            "action": "started",
            "topic": self._topic,
            "question": self._question,
            "state": self.state,
            "memory_context": self._build_memory_context(),
        }

    def start_daily_coach(self) -> dict:
        """Start a daily coaching session."""
        self._daily_coach_step = 0
        self._round_count = 0
        self._session_scores.clear()
        self._session_start = time.time()
        self._set_state(self.DAILY_COACH)
        return {
            "action": "daily_coach_started",
            "step": 1,
            "total_steps": 8,
            "question": self.WARMUP_TOPICS[0],
            "message": "Daily English Coach session started. Let's begin with a warm-up question.",
        }

    def start_practice(self, word: str = "") -> dict:
        """Start pronunciation practice for a specific word."""
        if not word:
            word = self._pick_practice_word()
        self._practice_word = word
        self._practice_attempts = 0
        self._set_state(self.PRACTICE)
        return {
            "action": "practice_started",
            "word": word,
            "message": f'Repeat after me: "{word}".',
        }

    def start_shadowing(self, sentence: str = "") -> dict:
        """Start sentence shadowing mode."""
        if not sentence:
            import random
            sentence = random.choice(self.SHADOW_SENTENCES)
        self._question = sentence
        self._set_state(self.SHADOWING)
        return {
            "action": "shadowing_started",
            "sentence": sentence,
            "message": f'Listen carefully, then repeat: "{sentence}"',
        }

    def receive_speech(self, transcript: str, audio: np.ndarray | None = None,
                       duration: float = 0.0) -> dict:
        """Process a spoken response from the user. Called when STT produces transcript."""
        self._transcript = transcript
        self._set_state(self.ANALYZING)

        # 1. Text-based analysis (grammar, vocabulary, fluency)
        analysis = analyze_grammar_vocabulary_fluency(
            transcript, self._topic, self._tutor_memory
        )

        # 2. Pronunciation analysis (only if audio provided)
        pronunciation = {"available": False, "message": "No audio provided for analysis."}
        if audio is not None and len(audio) > 0:
            pronunciation = analyze_pronunciation(audio, transcript)

        # 3. Calculate WPM
        wpm = _calc_wpm(transcript, duration)
        pace = analysis.get("speaking_speed", {})
        if wpm:
            pace["wpm"] = round(wpm)
            pace["assessment"] = self._assess_pace(wpm)

        # 4. Compile scores
        g_score = analysis.get("grammar_score", 5)
        v_score = analysis.get("vocabulary_score", 5)
        f_score = analysis.get("fluency_score", 5)
        p_score = pronunciation.get("score", 0) if pronunciation.get("available") else 0

        if p_score > 0:
            overall = round((g_score + v_score + f_score + p_score) / 4, 1)
        else:
            overall = round((g_score + v_score + f_score) / 3, 1)

        self._scores = {
            "grammar": g_score,
            "vocabulary": v_score,
            "fluency": f_score,
            "pronunciation": p_score,
            "overall": overall,
        }

        self._analysis = analysis
        self._pronunciation = pronunciation
        self._round_count += 1
        self._session_scores.append(self._scores.copy())

        # 5. Update tutor memory
        self._update_memory(analysis, pronunciation, wpm)

        self._set_state(self.FEEDBACK)

        # 6. Generate practice words from pronunciation issues
        practice_words = []
        if pronunciation.get("available") and pronunciation.get("issues"):
            practice_words = [i["word"] for i in pronunciation["issues"] if i.get("word")]

        return {
            "action": "feedback",
            "transcript": transcript,
            "scores": self._scores,
            "grammar_issues": analysis.get("grammar_issues", []),
            "vocabulary_suggestions": analysis.get("vocabulary_suggestions", []),
            "fluency_issues": analysis.get("fluency_issues", {}),
            "speaking_speed": pace,
            "pronunciation": pronunciation,
            "practice_words": practice_words,
            "overall_feedback": analysis.get("overall_feedback", ""),
            "session_progress": f"Round {self._round_count}",
        }

    def next_question(self) -> dict:
        """Get the next question for the tutoring session."""
        import random
        if self._round_count < 2:
            q = random.choice(self.WARMUP_TOPICS)
        else:
            q = random.choice(self.SPEAKING_TOPICS)
        self._question = q
        self._set_state(self.IDLE)
        return {"question": q}

    def next_daily_coach_step(self) -> dict:
        """Advance the daily coach session."""
        self._daily_coach_step += 1
        steps = {
            1: ("Warm-up", self.WARMUP_TOPICS[0]),
            2: ("Speaking Exercise", "Tell me about a recent accomplishment you are proud of."),
            3: ("Grammar Analysis", "[ANALYZE_CURRENT]"),
            4: ("Vocabulary Improvement", "[VOCAB_EXERCISE]"),
            5: ("Pronunciation Check", "[PRONUNCIATION_CHECK]"),
            6: ("Follow-up", "Based on your previous answer, tell me more about the details."),
            7: ("Final Score", "[FINAL_SCORE]"),
            8: ("Goal Setting", "[GOAL_SETTING]"),
        }
        if self._daily_coach_step > 8:
            return self._finish_daily_coach()
        name, prompt = steps.get(self._daily_coach_step, ("Done", ""))
        return {"step": self._daily_coach_step, "name": name, "prompt": prompt}

    def get_daily_coach_feedback(self) -> dict:
        """Generate final daily coach summary."""
        avg = self._average_scores()
        scores = self._scores

        # Determine improvement areas
        weak = []
        if scores["grammar"] < 7:
            weak.append("grammar")
        if scores["vocabulary"] < 7:
            weak.append("vocabulary")
        if scores["fluency"] < 7:
            weak.append("fluency")
        if scores["pronunciation"] > 0 and scores["pronunciation"] < 7:
            weak.append("pronunciation")

        goal = ""
        if "vocabulary" in weak:
            goal = "Use more precise vocabulary instead of repeating 'good', 'nice' and 'very'."
        elif "grammar" in weak:
            goal = "Focus on correct article usage (a/an/the) and verb tenses."
        elif "fluency" in weak:
            goal = "Try pausing silently instead of using 'um' and 'uh'."
        elif "pronunciation" in weak:
            goal = "Practice stressing the correct syllables in multi-syllable words."
        else:
            goal = "Try using 3 new vocabulary words naturally in your next conversation."

        return {
            "scores": scores,
            "average": avg,
            "today_goal": goal,
            "next_session": "Try using 3 new vocabulary words naturally.",
            "grammar_issues": self._analysis.get("grammar_issues", []),
            "vocabulary_suggestions": self._analysis.get("vocabulary_suggestions", []),
        }

    def _finish_daily_coach(self) -> dict:
        self._set_state(self.IDLE)
        return self.get_daily_coach_feedback()

    def get_progress_report(self) -> dict:
        """Get a progress report comparing with previous sessions."""
        mem = self._tutor_memory
        prev_sessions = mem.get("scores", [])

        report = {
            "total_sessions": mem.get("total_sessions", 0) + 1,
            "current_session_avg": self._average_scores(),
            "historical_avg": {},
            "improvements": [],
        }

        if prev_sessions:
            # Calculate historical averages
            n = len(prev_sessions)
            hist = {
                "grammar": sum(s.get("grammar", 0) for s in prev_sessions) / n,
                "vocabulary": sum(s.get("vocabulary", 0) for s in prev_sessions) / n,
                "fluency": sum(s.get("fluency", 0) for s in prev_sessions) / n,
                "pronunciation": sum(s.get("pronunciation", 0) for s in prev_sessions) / n,
                "overall": sum(s.get("overall", 0) for s in prev_sessions) / n,
            }
            report["historical_avg"] = {k: round(v, 1) for k, v in hist.items()}

            # Compare
            curr = report["current_session_avg"]
            for key in ["grammar", "vocabulary", "fluency", "pronunciation", "overall"]:
                if curr.get(key, 0) > hist.get(key, 0):
                    report["improvements"].append(key)

        return report

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _assess_pace(self, wpm: float) -> str:
        if wpm < 80:
            return "Your pace is a bit slow. Try connecting your sentences more naturally."
        elif wpm < 110:
            return "A bit slower than conversational pace. Try to flow more naturally."
        elif wpm <= 150:
            return "Good conversational pace."
        elif wpm <= 170:
            return "Slightly fast but acceptable."
        else:
            return "You're speaking quickly. Try adding short pauses between ideas."

    def _pick_practice_word(self) -> str:
        # Pick from pronunciation issues first, then from default list
        issues = self._tutor_memory.get("pronunciation_issues", {})
        if issues:
            import random
            return random.choice(list(issues.keys()))
        import random
        return random.choice(self.PRACTICE_WORDS)

    def _average_scores(self) -> dict:
        if not self._session_scores:
            return self._scores
        n = len(self._session_scores)
        return {
            "grammar": round(sum(s["grammar"] for s in self._session_scores) / n, 1),
            "vocabulary": round(sum(s["vocabulary"] for s in self._session_scores) / n, 1),
            "fluency": round(sum(s["fluency"] for s in self._session_scores) / n, 1),
            "pronunciation": round(sum(s["pronunciation"] for s in self._session_scores) / n, 1),
            "overall": round(sum(s["overall"] for s in self._session_scores) / n, 1),
        }

    def _build_memory_context(self) -> str:
        """Build context from previous sessions for the tutor."""
        mem = self._tutor_memory
        parts = []
        total = mem.get("total_sessions", 0)
        if total > 0:
            parts.append(f"Previous sessions: {total}")

        grammar = mem.get("grammar_errors", {})
        if grammar:
            top = sorted(grammar.items(), key=lambda x: -x[1])[:3]
            parts.append(f"Common grammar errors: {', '.join(f'{e} ({c}x)' for e, c in top)}")

        speeds = mem.get("speaking_speeds", [])
        if speeds:
            avg_speed = sum(speeds[-5:]) / min(5, len(speeds))
            parts.append(f"Average speaking speed: {avg_speed:.0f} WPM")

        vocab = mem.get("vocabulary_learned", [])
        if vocab:
            parts.append(f"Vocabulary learned: {', '.join(vocab[-5:])}")

        return "\n".join(parts) if parts else "First session — no previous data."

    def _update_memory(self, analysis: dict, pronunciation: dict, wpm: float | None):
        """Update persistent memory with learning data."""
        mem = self._tutor_memory

        # Grammar errors
        for issue in analysis.get("grammar_issues", []):
            err_type = issue.get("issue", "unknown")[:50]
            mem["grammar_errors"][err_type] = mem["grammar_errors"].get(err_type, 0) + 1

        # Pronunciation issues
        if pronunciation.get("available") and pronunciation.get("issues"):
            for p in pronunciation["issues"]:
                word = p.get("word", "").lower()
                if word:
                    mem["pronunciation_issues"][word] = mem["pronunciation_issues"].get(word, 0) + 1

        # Filler words
        fillers = analysis.get("fluency_issues", {}).get("filler_words", [])
        for f in fillers:
            word = f.get("word", "")
            count = f.get("count", 0)
            if word:
                mem["filler_words"][word] = mem["filler_words"].get(word, 0) + count

        # Repeated words
        repeated = analysis.get("fluency_issues", {}).get("repeated_words", [])
        for r in repeated:
            word = r.get("word", "")
            count = r.get("count", 0)
            if word:
                mem["repeated_words"][word] = mem["repeated_words"].get(word, 0) + count

        # Speaking speed
        if wpm:
            speeds = mem.setdefault("speaking_speeds", [])
            speeds.append(round(wpm))
            if len(speeds) > 50:
                mem["speaking_speeds"] = speeds[-50:]

        # Vocabulary
        for v in analysis.get("vocabulary_suggestions", []):
            better = v.get("better_word", "")
            if better and better not in mem["vocabulary_learned"]:
                mem["vocabulary_learned"].append(better)
                if len(mem["vocabulary_learned"]) > 100:
                    mem["vocabulary_learned"] = mem["vocabulary_learned"][-100:]

        # Scores
        scores_entry = {}
        for key in ["grammar_score", "vocabulary_score", "fluency_score"]:
            scores_entry[key.replace("_score", "")] = analysis.get(key, 5)
        scores_entry["pronunciation"] = pronunciation.get("score", 0)
        scores_entry["overall"] = self._scores["overall"]
        mem.setdefault("scores", []).append(scores_entry)
        if len(mem["scores"]) > 100:
            mem["scores"] = mem["scores"][-100:]

        mem["total_sessions"] = mem.get("total_sessions", 0)

        _save_tutor_memory(mem)

    def end_session(self) -> dict:
        """End the session and save summary."""
        avg = self._average_scores()
        elapsed = time.time() - self._session_start if self._session_start else 0

        mem = self._tutor_memory
        mem["total_sessions"] = mem.get("total_sessions", 0) + 1
        session_summary = {
            "date": time.strftime("%Y-%m-%d"),
            "rounds": self._round_count,
            "duration_seconds": round(elapsed),
            "avg_scores": avg,
        }
        mem.setdefault("sessions", []).append(session_summary)
        if len(mem["sessions"]) > 50:
            mem["sessions"] = mem["sessions"][-50:]

        _save_tutor_memory(mem)
        self._set_state(self.IDLE)
        return {
            "action": "session_ended",
            "summary": session_summary,
            "progress": self.get_progress_report(),
        }


# ── TOOL Declaration (for action_loader) ──────────────────────────────────────

def _tutor_handler(parameters: dict, player=None, speak=None, **kwargs) -> str:
    """Handler for the english_tutor tool call from Gemini."""
    action = parameters.get("action", "start")
    # The actual tutor state is managed by the UI overlay and main.py
    # This handler just returns information for Gemini's context
    return (
        f"English tutor action '{action}' received. "
        "The tutor overlay is managed by the UI. "
        "If the user wants to start the English tutor, direct them to click "
        "ENGLISH TUTOR in the left sidebar navigation."
    )


TOOL = {
    "name": "english_tutor",
    "description": (
        "English Speaking Tutor with pronunciation analysis. "
        "Use when the user wants to practice English, improve pronunciation, "
        "do speaking exercises, start a daily English coaching session, "
        "or get feedback on their English speaking skills. "
        "This tool starts an interactive tutoring session."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "start — begin a new tutoring session | "
                    "daily — start daily coaching session | "
                    "practice — pronunciation practice mode | "
                    "shadow — sentence shadowing mode | "
                    "status — get current session status"
                ),
            },
            "word": {
                "type": "STRING",
                "description": "Specific word to practice pronunciation (for practice mode).",
            },
        },
        "required": [],
    },
    "handler": _tutor_handler,
}
