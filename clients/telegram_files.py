"""Getting an uploaded file off Telegram's CDN and into memory.

The fourth client, and the odd one out: the other three talk to services David
uses, this one talks to the transport David arrives on. It lives here anyway
because it is the same KIND of thing — a bounded, retryable network call that
returns `(value, error)` and knows nothing about what the bytes are for.

Both attachment paths (`Learn pdf` and the `Add q … / …` quote extraction) go
through `download_pdf_attachment`, and voice notes through `download_voice`; all
three share `_fetch`, so none can regress to an unbounded download on its own.

WHY IT DOES NOT `import telegram`. It never needs the library: `context.bot` is
handed in already built, and `tg_file.file_path` is a plain CDN URL in PTB v20+,
which `requests` fetches. So a service may import this module for its constants
without dragging python-telegram-bot into services/ — which the layering guard
would refuse.
"""

import asyncio
import re

import requests

# --- PDF ATTACHMENT LIMITS ---
MAX_PDF_MB    = 15
MAX_PDF_BYTES = MAX_PDF_MB * 1024 * 1024
HTTP_TIMEOUT_SECONDS     = 30    # per-request cap: fail fast on a stalled socket
DOWNLOAD_TIMEOUT_SECONDS = 120   # whole-operation cap

# The bot token inside a Telegram URL: `/file/bot<id>:<secret>/documents/…`.
_TOKEN_IN_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact_token(text: str) -> str:
    """`text` with any bot token replaced by `bot<redacted>`.

    The file URL `get_file` hands back carries the token, and requests quotes the
    URL in its exceptions ("404 Client Error: … for url: <URL>", "Max retries
    exceeded with url: …"). The download error goes to the chat, so it went there
    with the token in it. The rest of the URL stays: it still says which request
    failed.
    """
    return _TOKEN_IN_URL.sub("bot<redacted>", text)


def validate_pdf_attachment(doc) -> str | None:
    """Cheap local checks on an attachment. Returns an error message, or None if OK.

    Split out from the download so a caller can reject a bad file before spending
    a Notion lookup on it. Pure and network-free, so calling it twice is free.
    """
    if doc is None:
        return "❌ No file attached."

    if doc.mime_type != "application/pdf":
        return "❌ Please attach a PDF file."

    # Telegram reports file_size up front for most uploads — reject oversized
    # files before downloading them. It is optional in the API, so the real
    # size is checked again after the download.
    if doc.file_size is not None and doc.file_size > MAX_PDF_BYTES:
        return (f"❌ That PDF is {doc.file_size / 1024 / 1024:.1f} MB. "
                f"The limit is {MAX_PDF_MB} MB.")

    return None


async def _fetch(context, file_id) -> bytes:
    """One file off Telegram's CDN, bounded twice: a per-request timeout fails a
    stalled socket fast, and asyncio.wait_for (the caller's) caps the whole
    operation. Raises; the callers turn it into their own message."""
    tg_file = await context.bot.get_file(file_id)

    def _download():
        resp = requests.get(tg_file.file_path, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.content

    return await asyncio.wait_for(asyncio.to_thread(_download), timeout=DOWNLOAD_TIMEOUT_SECONDS)


# Groq's cap on one file. A voice note under VOICE_MAX_SECONDS is a few hundred
# KB; this is the backstop for one whose size Telegram did not state.
MAX_VOICE_BYTES = 25 * 1024 * 1024


def validate_voice(voice, max_seconds: int) -> str | None:
    """Cheap local checks on a voice note, before anything is downloaded."""
    if voice is None:
        return "❌ No voice note."
    if voice.duration is not None and voice.duration > max_seconds:
        return (f"❌ That voice note is {voice.duration} seconds. The limit is "
                f"{max_seconds}: say one command at a time.")
    if voice.file_size is not None and voice.file_size > MAX_VOICE_BYTES:
        return "❌ That voice note is too large to transcribe."
    return None


async def download_voice(context, voice, max_seconds: int):
    """Validate and download a voice note. Returns (ogg_bytes, error_message)."""
    err = validate_voice(voice, max_seconds)
    if err:
        return None, err
    try:
        content = await _fetch(context, voice.file_id)
    except asyncio.TimeoutError:
        return None, "❌ Downloading the voice note timed out."
    except Exception as e:
        return None, f"❌ Download error: {redact_token(str(e))}"
    if len(content) > MAX_VOICE_BYTES:
        return None, "❌ That voice note is too large to transcribe."
    return content, None


async def download_pdf_attachment(context, doc):
    """Validate and download an attached PDF. Returns (pdf_bytes, error_message).

    WHY THIS EXISTS: tg_file.download_as_bytearray() has no built-in timeout and
    can hang forever on Railway. requests.get() with a timeout fails fast if the
    download stalls, and asyncio.wait_for caps the whole operation regardless.
    Both attachment paths in handle_document go through here, so neither can
    regress to an unbounded download.
    """
    err = validate_pdf_attachment(doc)
    if err:
        return None, err

    try:
        content = await _fetch(context, doc.file_id)
    except asyncio.TimeoutError:
        return None, ("❌ Download timed out after 2 minutes.\n"
                      "Try a smaller PDF.")
    except Exception as e:
        return None, f"❌ Download error: {redact_token(str(e))}"

    # file_size is optional in the Telegram API, so re-check what actually arrived.
    if len(content) > MAX_PDF_BYTES:
        return None, (f"❌ That PDF is {len(content) / 1024 / 1024:.1f} MB. "
                      f"The limit is {MAX_PDF_MB} MB.")

    return content, None
