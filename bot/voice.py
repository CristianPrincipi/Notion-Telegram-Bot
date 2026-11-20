"""Voice notes: what you SAID, transcribed, then read like a sentence — and
always confirmed before it runs.

    you:    🎤 (aggiungi spesa Carrefour dodici euro)
    David:  🎙️ Heard: «Aggiungi spesa Carrefour dodici euro.»
            Did you mean this?
            Add e Carrefour 12 f           [✅ Yes] [❌ No]

The chain is download (clients/telegram_files.py, bounded) → transcription
(clients/speech_client.py, Groq's Whisper) → the plain-sentence path
(bot/sentences.py) with `always_confirm`. Every check that path makes still
applies; this adds two, both because the words were HEARD rather than typed:

  - A VOICE NOTE NEVER SKIPS THE CONFIRMATION, not even for a read, and not even
    when the transcript is word for word a command. "D e Carrefour" misheard as
    "D e Carrefour 2" is a different row.
  - A VOICE NOTE NEVER REACHES THE REGISTRY OR A NUMBERED LIST DIRECTLY. "Due"
    transcribed as "2" while a list is waiting must not delete the second match.
    The one prompt it does answer is the interpreter's own clarifying question
    ("Quanto hai speso?"), because that answer is read by the model together
    with the sentence that raised it, and then confirmed like everything else.

What was heard is always shown first, so a misreading is visible before ✅ Yes.
"""

import asyncio
import logging

import pending_choice
from bot import sentences
from clients import speech_client, telegram_files
from config import TRANSCRIBE_TIMEOUT, VOICE_MAX_SECONDS

logger = logging.getLogger(__name__)


async def _transcribe(audio):
    try:
        return await asyncio.wait_for(asyncio.to_thread(speech_client.transcribe, audio),
                                      timeout=TRANSCRIBE_TIMEOUT)
    except asyncio.TimeoutError:
        return None, f"Groq did not answer within {TRANSCRIBE_TIMEOUT} seconds."


async def read_voice(update, context, *, catalogue, matching):
    """A voice note → a proposal awaiting ✅ Yes. Returns what `understand`
    returns — with `always_confirm`, always None: it has replied itself."""
    if not speech_client.GROQ_API_KEY:
        # Checked before the download: no reason to fetch audio nothing will hear.
        await update.message.reply_text(f"❌ {speech_client.NOT_CONFIGURED}")
        return None

    audio, err = await telegram_files.download_voice(context, update.message.voice,
                                                     VOICE_MAX_SECONDS)
    if err:
        await update.message.reply_text(err)
        return None

    text, err = await _transcribe(audio)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return None

    await update.message.reply_text(f"🎙️ Heard: «{text}»")

    earlier = None
    asked = pending_choice.peek(context.user_data, sentences.CLARIFY)
    if asked is not None:
        pending_choice.clear(context.user_data)
        earlier = (asked.sentence, asked.question)

    return await sentences.understand(update, context, text, catalogue=catalogue,
                                      matching=matching, earlier=earlier,
                                      always_confirm=True)
