"""The Telegram adapters: parse an update, call a service, send what comes back.

A module in here is allowed to know about `Update`, `ContextTypes`, captions,
`reply_text` and parse modes. It is the only layer that is.

WHAT A HANDLER IS ALLOWED TO DO
-------------------------------
  1. pull the arguments out of the update (or the regex match david's registry
     already ran)
  2. reject what it can reject locally, in the wording it already used
  3. build the `notify` / `notify_md` pair from the update
  4. call one service function and get out of the way

WHAT IT MUST NOT DO: decide anything. If a handler grows a Notion call, a
retry, a lock, or a rule about which row a command meant, that logic belongs in
`services/` — where it can be tested without a bot and reached from a job.

The handlers stay this thin so that david.py can stay a registry: a command
declares its pattern, its handler, its help and its `destructive` flag once, and
the handler it names is four lines of plumbing rather than a branch of business
logic. See the registry comment in david.py.
"""
