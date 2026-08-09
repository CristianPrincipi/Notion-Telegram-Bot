"""Getting an uploaded file off Telegram's CDN and into memory.

The fourth client, and the odd one out: the other three talk to services David
uses, this one talks to the transport David arrives on. It lives here anyway
because it is the same KIND of thing — a bounded, retryable network call that
returns `(value, error)` and knows nothing about what the bytes are for.

Both attachment paths (`Learn pdf` and the `Add q … / …` quote extraction) go
through `download_pdf_attachment`, so neither can regress to an unbounded
download on its own.

WHY IT DOES NOT `import telegram`. It never needs the library: `context.bot` is
handed in already built, and `tg_file.file_path` is a plain CDN URL in PTB v20+,
which `requests` fetches. So a service may import this module for its constants
without dragging python-telegram-bot into services/ — which the layering guard
would refuse.
"""

import asyncio

import requests

# --- PDF ATTACHMENT LIMITS ---
MAX_PDF_MB    = 15
MAX_PDF_BYTES = MAX_PDF_MB * 1024 * 1024
HTTP_TIMEOUT_SECONDS     = 30    # per-request cap: fail fast on a stalled socket
DOWNLOAD_TIMEOUT_SECONDS = 120   # whole-operation cap


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
        tg_file = await context.bot.get_file(doc.file_id)
        # tg_file.file_path is the full Telegram CDN URL in PTB v20+

        def _download():
            resp = requests.get(tg_file.file_path, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.content

        content = await asyncio.wait_for(
            asyncio.to_thread(_download),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return None, ("❌ Download timed out after 2 minutes.\n"
                      "Try a smaller PDF.")
    except Exception as e:
        return None, f"❌ Download error: {e}"

    # file_size is optional in the Telegram API, so re-check what actually arrived.
    if len(content) > MAX_PDF_BYTES:
        return None, (f"❌ That PDF is {len(content) / 1024 / 1024:.1f} MB. "
                      f"The limit is {MAX_PDF_MB} MB.")

    return content, None
