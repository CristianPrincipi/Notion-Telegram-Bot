"""Which calendar David writes to — and refusing the one nobody can see.

David authenticates as a SERVICE ACCOUNT, and to a service account `primary` is
its own calendar: a real calendar, writable, readable, and visible to no human.
GOOGLE_CALENDAR_ID used to default to it. So a deploy that set the key and not
the ID booked every `Remind` there and confirmed it ("✅ Reminder set!"), `Agenda`
read that empty calendar and called a busy day free, and the heartbeat's
calendar probe passed — every check green, every reminder invisible.

It is refused in the client rather than at startup: the calendar is an optional
feature, and a startup refusal would take expenses, Learn and everything else
down with it. The refusal sits in `_get_service`, which every Google call goes
through, AFTER the cached-service read and BEFORE a service is built — so a
cached service always means the ID was valid.
"""

from unittest import mock

import pytest

from clients import calendar_client
from conftest import run
from services import agenda, reminder


@pytest.fixture
def service_account(monkeypatch):
    """Credentials present and a spy where Google's client would be built.

    Nothing reaches Google: the credential parse and `build` are both replaced,
    and `build` records that it was called. No service is cached on this thread
    on the way in, and none is left behind on the way out.
    """
    monkeypatch.setattr(calendar_client, "_CREDS_JSON", '{"type": "service_account"}')
    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_info",
                        lambda info, scopes: "credentials")
    build = mock.MagicMock(name="build")
    monkeypatch.setattr("googleapiclient.discovery.build", build)
    monkeypatch.delattr(calendar_client._thread_local, "service", raising=False)
    yield build
    if hasattr(calendar_client._thread_local, "service"):
        del calendar_client._thread_local.service


INVISIBLE = ["", "primary", "PRIMARY", " primary "]


@pytest.mark.parametrize("calendar_id", INVISIBLE, ids=["unset", "primary", "PRIMARY", "padded"])
def test_the_service_accounts_own_calendar_is_refused_before_anything_is_built(
        service_account, monkeypatch, calendar_id):
    monkeypatch.setattr(calendar_client, "CALENDAR_ID", calendar_id)

    service, err = calendar_client._get_service()

    assert service is None
    assert err and "GOOGLE_CALENDAR_ID" in err, err
    assert not service_account.called, "a service was built for a calendar nobody can see"


def test_a_real_calendar_id_still_gets_a_service(service_account, monkeypatch):
    """The other half — a fix that refused every calendar would pass the test above."""
    monkeypatch.setattr(calendar_client, "CALENDAR_ID", "you@gmail.com")

    service, err = calendar_client._get_service()

    assert err is None
    assert service is service_account.return_value


def test_missing_credentials_are_still_reported_first(monkeypatch):
    """No key at all is the calendar being switched off, not misconfigured: that
    message, not the calendar-ID one, is the accurate one."""
    monkeypatch.setattr(calendar_client, "_CREDS_JSON", "")
    monkeypatch.setattr(calendar_client, "CALENDAR_ID", "")
    monkeypatch.delattr(calendar_client._thread_local, "service", raising=False)

    _, err = calendar_client._get_service()

    assert "GOOGLE_CREDENTIALS_JSON" in err


def _replies(command, monkeypatch, calendar_id=""):
    monkeypatch.setattr(calendar_client, "CALENDAR_ID", calendar_id)
    said = []

    async def collect(text):
        said.append(text)

    run(command(collect))
    return said


def test_remind_refuses_out_loud_instead_of_confirming(service_account, monkeypatch):
    """THE BUG, end to end: with the ID unset, `Remind` said "Reminder set!" for
    an event created where you would never see it."""
    said = _replies(lambda notify: reminder.run_remind("Remind Dentist tr 10", notify=notify),
                    monkeypatch)

    assert not any("Reminder set" in message for message in said), said
    assert any("GOOGLE_CALENDAR_ID" in message for message in said), said


def test_agenda_refuses_out_loud_instead_of_reading_an_empty_calendar(service_account, monkeypatch):
    """The read side of the same bug: the service account's calendar is always
    empty, so a busy day was reported free."""
    said = _replies(lambda notify: agenda.run_agenda(None, notify=notify), monkeypatch)

    assert len(said) == 1, said
    assert "could not read your calendar" in said[0]
    assert "GOOGLE_CALENDAR_ID" in said[0]
