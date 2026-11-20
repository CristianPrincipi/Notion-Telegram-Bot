"""Voice notes: downloaded, transcribed by Groq, read as a sentence, ALWAYS confirmed.

Telegram's CDN and Groq are intercepted with `responses`; Claude is the scripted
fake from test_sentences. What is asserted is what a misheard word must never
do: run unconfirmed (not even a read), answer a numbered list, or reach Groq when
it cannot help (no key, too long). And what a failure must say: which service,
and never the bot token or the Groq key.
"""
# The fixtures are test_sentences' own, imported by name; pytest injects them as
# parameters, which ruff reads as a redefinition of the import.
# ruff: noqa: F811

import time

import pytest
import responses

import david
import pending_choice
from bot import menu, sentences, voice
from clients import speech_client, telegram_files
from conftest import FakeContext, FakeFile, FakeUpdate, run
from test_menu import REAL_SHOW_MENU
from test_router import NO_HANDLER, router  # noqa: F401 — a fixture `spies` needs
from test_sentences import claude, send, spies  # noqa: F401 — fixtures and helper

CDN_URL = "https://api.telegram.org/file/bot123456:SECRETtoken/voice/file_7.oga"
GROQ_KEY = "gsk_test_key_never_shown"
AUDIO = b"OggS fake opus audio"


class FakeVoice:
    def __init__(self, duration=4, file_size=len(AUDIO), file_id="voice-1"):
        self.duration = duration
        self.file_size = file_size
        self.file_id = file_id


@pytest.fixture
def groq_key(monkeypatch):
    monkeypatch.setattr(speech_client, "GROQ_API_KEY", GROQ_KEY)
    return GROQ_KEY


@pytest.fixture
def cdn():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, CDN_URL, body=AUDIO)
        yield mock


def groq_says(mock, text=None, status=200, headers=None, body=None):
    mock.add(responses.POST, speech_client.TRANSCRIPTIONS_URL, status=status,
             json=None if body is not None else {"text": text},
             body=body, headers=headers or {})


def speak(context, voice_note=None):
    update = FakeUpdate()
    update.message.voice = voice_note or FakeVoice()
    run(david.handle_voice(update, context))
    return update


def voice_context():
    return FakeContext(file=FakeFile(file_path=CDN_URL, content=AUDIO))


# ─── HEARD, READ, CONFIRMED ───────────────────────────────────────────────────

def test_a_voice_note_is_heard_proposed_and_runs_only_on_yes(spies, claude, groq_key, cdn):
    groq_says(cdn, "Ho speso dodici euro da Carrefour.")
    claude.says("Add e Carrefour 12 f")
    context = voice_context()

    heard = speak(context)

    assert spies.chain == NO_HANDLER
    assert heard.message.reply_texts[0] == "🎙️ Heard: «Ho speso dodici euro da Carrefour.»"
    text, kwargs = heard.message.replies[-1]
    assert "Add e Carrefour 12 f" in text
    assert kwargs["reply_markup"] == sentences.CONFIRM_KEYBOARD
    _, user_message, _ = claude.calls[0]
    assert user_message == "Ho speso dodici euro da Carrefour."

    send(context, sentences.YES_BUTTON)

    assert spies.chain == "add_Expenses"
    assert spies.parsed_args == {"name": "Carrefour", "amount": 12.0, "category": "Food"}


def test_even_a_read_waits_for_yes_when_it_was_heard(spies, claude, groq_key, cdn):
    """A typed "cosa ho domani?" runs at once; spoken, it is confirmed first."""
    groq_says(cdn, "Cosa ho domani?")
    claude.says("Agenda tr")
    context = voice_context()

    heard = speak(context)

    assert spies.chain == NO_HANDLER
    assert heard.message.replies[-1][1]["reply_markup"] == sentences.CONFIRM_KEYBOARD

    send(context, sentences.YES_BUTTON)

    assert spies.chain == "handle_agenda"


def test_a_transcript_that_is_already_a_command_is_still_read_and_confirmed(
        spies, claude, groq_key, cdn):
    """Never the registry directly: a misheard amount would be a typed command."""
    groq_says(cdn, "D e Carrefour")
    claude.says("D e Carrefour")
    context = voice_context()

    speak(context)

    assert spies.chain == NO_HANDLER
    assert len(claude.calls) == 1
    assert pending_choice.kind_of(context.user_data) == sentences.CONFIRM


def test_a_voice_note_never_answers_a_numbered_list(spies, claude, groq_key, cdn):
    """"Due" heard as "2" while `D e` waits for a number must not delete row 2 —
    and the list must still be there to answer by typing."""
    groq_says(cdn, "2")
    context = voice_context()
    waiting = sentences.Proposal(None, pending_choice.deadline())  # any record with expires_at
    pending_choice.remember(context.user_data, "expense_delete", waiting, ["a", "b"])

    heard = speak(context)

    assert spies.chain == NO_HANDLER
    assert claude.calls == []
    assert heard.message.reply_texts == [voice.STILL_WAITING]
    assert pending_choice.kind_of(context.user_data) == "expense_delete"
    assert context.bot.get_file_ids == []


def test_a_voice_note_leaves_an_open_menu_question_open(spies, claude, groq_key, cdn,
                                                        monkeypatch):
    """Found by the review of #78: the proposal overwrote the menu's question in
    the one slot, though only ❌ Cancel, 📋 Menu or the expiry may close it."""
    groq_says(cdn, "Dodici euro.")
    claude.says("Add e Carrefour 12 f")
    monkeypatch.setattr(menu, "show_menu", REAL_SHOW_MENU)   # the router spies it
    context = voice_context()
    send(context, menu.MENU_BUTTON, menu.FLOWS[0].label)
    assert pending_choice.kind_of(context.user_data) == menu.KIND

    heard = speak(context)

    assert heard.message.reply_texts == [voice.STILL_WAITING]
    assert pending_choice.kind_of(context.user_data) == menu.KIND
    assert claude.calls == []


def test_a_voice_note_still_drops_an_unanswered_proposal(spies, claude, groq_key, cdn):
    """An open ✅ Yes is not a question that must be answered: like a typed
    message, a new voice note is read, and the old proposal is not run."""
    claude.says("Add e Carrefour 12 f")
    claude.says("B")
    groq_says(cdn, "Quanto mi resta?")
    context = voice_context()
    send(context, "ho speso 12 euro da Carrefour")

    speak(context)
    send(context, sentences.YES_BUTTON)

    assert spies.chain == "budget"


def test_a_typed_answer_to_a_question_a_voice_note_raised_is_still_confirmed(
        spies, claude, groq_key, cdn):
    """Found by the review of #78: `always_confirm` was not carried through the
    clarifying question, so typing the answer ran a read unconfirmed."""
    groq_says(cdn, "Cosa ho?")
    claude.says(question="Per quale giorno?")
    claude.says("Agenda tr")
    context = voice_context()

    speak(context)
    [typed] = send(context, "domani")

    assert spies.chain == NO_HANDLER
    assert typed.message.replies[-1][1]["reply_markup"] == sentences.CONFIRM_KEYBOARD


def test_a_spoken_answer_to_the_models_question_is_read_with_the_sentence(
        spies, claude, groq_key, cdn):
    claude.says(question="Quanto hai speso?")
    claude.says("Add e Carrefour 12 f")
    groq_says(cdn, "Dodici euro.")
    context = voice_context()

    send(context, "ho fatto la spesa da Carrefour")
    speak(context)

    _, second_user_message, _ = claude.calls[1]
    assert "ho fatto la spesa da Carrefour" in second_user_message
    assert "Quanto hai speso?" in second_user_message
    assert second_user_message.endswith("Dodici euro.")
    assert pending_choice.kind_of(context.user_data) == sentences.CONFIRM


# ─── WHEN GROQ IS NOT ASKED ───────────────────────────────────────────────────

def test_without_a_key_it_says_so_and_downloads_nothing(spies, claude, monkeypatch):
    monkeypatch.setattr(speech_client, "GROQ_API_KEY", None)
    context = voice_context()

    heard = speak(context)

    assert heard.message.reply_texts == [f"❌ {speech_client.NOT_CONFIGURED}"]
    assert "GROQ_API_KEY" in speech_client.NOT_CONFIGURED
    assert context.bot.get_file_ids == []
    assert claude.calls == []


def test_a_note_longer_than_the_limit_is_refused_before_the_download(
        spies, claude, groq_key, monkeypatch):
    monkeypatch.setattr(voice, "VOICE_MAX_SECONDS", 30)
    context = voice_context()

    heard = speak(context, FakeVoice(duration=31))

    assert heard.message.replied_with("31 seconds")
    assert heard.message.replied_with("30")
    assert context.bot.get_file_ids == []
    assert claude.calls == []


def test_a_note_at_the_limit_is_transcribed(spies, claude, groq_key, cdn, monkeypatch):
    monkeypatch.setattr(voice, "VOICE_MAX_SECONDS", 30)
    groq_says(cdn, "Budget.")

    speak(voice_context(), FakeVoice(duration=30))

    assert len(claude.calls) == 1


def test_a_note_too_large_for_groq_is_refused_before_the_download(spies, claude, groq_key):
    context = voice_context()

    heard = speak(context, FakeVoice(file_size=telegram_files.MAX_VOICE_BYTES + 1))

    assert heard.message.replied_with("too large")
    assert context.bot.get_file_ids == []


# ─── FAILURES SAY WHICH SERVICE, AND NO SECRET ────────────────────────────────

def test_the_free_tier_limit_is_named_with_its_wait(spies, claude, groq_key, cdn):
    groq_says(cdn, status=429, body='{"error":"rate"}', headers={"retry-after": "17"})

    heard = speak(voice_context())

    assert heard.message.reply_texts == [
        "❌ Groq's free-tier limit is reached. Try again in 17 seconds."]
    assert claude.calls == []


def test_a_refused_key_is_named_and_never_quoted(spies, claude, groq_key, cdn, caplog):
    groq_says(cdn, status=401, body='{"error":{"message":"Invalid API Key"}}')

    heard = speak(voice_context())

    assert heard.message.replied_with("GROQ_API_KEY")
    assert GROQ_KEY not in " ".join(heard.message.reply_texts)
    assert GROQ_KEY not in caplog.text


def test_a_groq_error_is_reported_and_nothing_is_read(spies, claude, groq_key, cdn):
    groq_says(cdn, status=500, body="upstream exploded")

    heard = speak(voice_context())

    assert heard.message.replied_with("Groq 500")
    assert claude.calls == []
    assert spies.chain == NO_HANDLER


def test_silence_is_reported_not_sent_to_claude(spies, claude, groq_key, cdn):
    groq_says(cdn, "   ")

    heard = speak(voice_context())

    assert heard.message.replied_with("heard nothing")
    assert claude.calls == []


def test_groq_unreachable_is_reported(spies, claude, groq_key):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, CDN_URL, body=AUDIO)
        # No POST registered: responses raises ConnectionError for it.
        heard = speak(voice_context())

    assert heard.message.replied_with("Could not reach Groq (ConnectionError)")


def test_a_failed_download_never_shows_the_bot_token(spies, claude, groq_key):
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, CDN_URL, status=404)
        heard = speak(voice_context())

    reply = " ".join(heard.message.reply_texts)
    assert "Download error" in reply
    assert "SECRETtoken" not in reply
    assert "bot<redacted>" in reply


def test_a_slow_transcription_is_capped(spies, claude, groq_key, cdn, monkeypatch):
    monkeypatch.setattr(voice, "TRANSCRIBE_TIMEOUT", 0.05)
    monkeypatch.setattr(speech_client, "transcribe",
                        lambda audio: (time.sleep(0.3), ("late", None))[1])

    heard = speak(voice_context())

    assert heard.message.replied_with("Groq did not answer within 0.05 seconds")
    assert claude.calls == []


# ─── WHAT IS SENT TO GROQ ─────────────────────────────────────────────────────

def test_groq_gets_the_audio_the_model_the_language_and_the_key(spies, claude, groq_key, cdn):
    groq_says(cdn, "Budget.")

    speak(voice_context())

    [call] = [c for c in cdn.calls if c.request.url == speech_client.TRANSCRIPTIONS_URL]
    body = call.request.body
    assert call.request.headers["Authorization"] == f"Bearer {GROQ_KEY}"
    assert AUDIO in body
    for value in (b"whisper-large-v3-turbo", b'name="language"\r\n\r\nit'):
        assert value in body


def test_the_transcription_runs_off_the_event_loop(spies, claude, groq_key, cdn, monkeypatch):
    """A blocking Groq call on the loop would freeze every other chat update."""
    import threading
    loop_thread = []
    real = speech_client.transcribe

    def recording(audio):
        loop_thread.append(threading.current_thread() is threading.main_thread())
        return real(audio)

    monkeypatch.setattr(speech_client, "transcribe", recording)
    groq_says(cdn, "Budget.")

    speak(voice_context())

    assert loop_thread == [False]
