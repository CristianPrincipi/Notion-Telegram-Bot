"""`Implement [Page] - [Area]`.

Thin on purpose, and thinner than it looks: which of the two Implement paths
runs — the flat Manual or the Diet toggle tree — is decided inside the service,
from the area named in the same command text it already parses. Splitting that
decision up here would have meant parsing the command twice, in two layers, and
the two copies disagreeing is exactly the class of bug the registry work removed
from the router.
"""

from bot.notify import for_update
from services.implement import run_implement


async def handle_implement(update, user_text: str):
    """Bind this message's reply channels and run the command."""
    notify, notify_md = for_update(update)
    await run_implement(user_text, notify=notify, notify_md=notify_md)
