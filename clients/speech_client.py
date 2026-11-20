"""Speech to text: a voice note's audio → what was said, by Groq's Whisper.

The fifth client. Claude takes no audio — text, images and documents only — so
a voice note has to become text before the interpreter (services/interpret.py)
can read it, and this is the one place that does that. It knows nothing about
commands: it returns words.

WHY GROQ. Whisper, on a free tier generous for one person's voice notes, and an
OpenAI-compatible endpoint that one multipart POST reaches with `requests` — no
new library. CLAUDE.md: everything David uses is free, or a few cents a month.

WHAT IS NOT RETRIED. One attempt. A 429 means the free tier's limit, and its
`retry-after` is quoted to you rather than waited out while the chat sits silent;
anything else is reported as Groq's. Nothing is lost by failing: a transcription
writes nothing, and the voice note is still in the chat to send again.

THE KEY. `GROQ_API_KEY` is in config.OPTIONAL_ENV; unset, `transcribe` refuses
with a message that names it, and the rest of David is untouched. It travels in
a header only, so no URL, exception or reply here can quote it.

Synchronous — callers run it with asyncio.to_thread.
"""

import logging
import os

import requests

from config import TRANSCRIBE_LANGUAGE, TRANSCRIBE_MODEL

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
HTTP_TIMEOUT_SECONDS = (10, 50)          # connect, read

NOT_CONFIGURED = ("Voice notes are not set up: GROQ_API_KEY is not set on Railway. "
                  "See docs/setup.md.")


def transcribe(audio: bytes, filename: str = "voice.ogg"):
    """What the audio says. Returns (text, error); never raises."""
    if not GROQ_API_KEY:
        return None, NOT_CONFIGURED
    try:
        response = requests.post(
            TRANSCRIPTIONS_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, audio, "audio/ogg")},
            data={"model": TRANSCRIBE_MODEL, "language": TRANSCRIBE_LANGUAGE,
                  "response_format": "json", "temperature": "0"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        return None, f"Could not reach Groq ({type(e).__name__})."

    if response.status_code == 429:
        wait = response.headers.get("retry-after")
        when = f" Try again in {wait} seconds." if wait else " Try again later."
        return None, f"Groq's free-tier limit is reached.{when}"
    if response.status_code == 401:
        return None, "Groq refused the key (401). Check GROQ_API_KEY on Railway."
    if not response.ok:
        return None, f"Groq {response.status_code}: {response.text[:200]}"

    try:
        text = (response.json().get("text") or "").strip()
    except ValueError:
        return None, "Groq's answer was not JSON."
    if not text:
        return None, "Groq heard nothing in that voice note."

    logger.info("Transcribed %d bytes of audio: %d characters", len(audio), len(text))
    return text, None
