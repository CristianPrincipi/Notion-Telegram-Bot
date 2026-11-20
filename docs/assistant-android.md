# The Android assistant

Talk to David from the phone without opening Telegram. Tap a tile, say "ho speso
12 euro da Carrefour" or "cosa ho domani?", and hear the answer.

```
tap the "David" tile      → a text box opens
tap the keyboard's 🎤      → "cosa ho domani?"   → OK
the phone reads aloud     → "Tomorrow: 10:00 Dentista …"
```

- **A question** (`B`, `Agenda`, `Get`, …) is answered at once, read aloud.
- **Anything that writes** asks first: "Shall I do this? Add e Carrefour 12 f",
  **Yes / No**. Nothing is written before Yes.
- **A missing part is asked for** ("Quanto hai speso?"); answer it the same way.
- Everything it does also appears in the Telegram chat, under "🗣️ From the
  assistant", so the chat stays the full record.
- Some commands are **Telegram only**, and the assistant says so: `D e`, `U e`
  and `Cancel` (they pick from a numbered list), `undo`, the menu, `Learn` and
  `Implement` (they report minutes later).

It uses [HTTP Shortcuts](https://http-shortcuts.rmy.ch), a free, open-source
(MIT) Android app with no ads. HTTP
Shortcuts has no speech recognition of its own, so you speak through your
**keyboard's microphone** (Gboard's 🎤, or any keyboard with dictation): one
extra tap, the phone's own speech recognition, nothing to pay. You can also just
type.

**Cost:** the phone does the listening, so no Groq. Reading the sentence is the
same small Claude call as a typed sentence — about $0.00014 each, counted in
`ANTHROPIC_DAILY_BUDGET_USD`.

## 1. On Railway

1. The assistant needs David reachable from the internet. If you set up
   [Kindle quotes](setup.md#kindle-quotes-optional) you already have a domain
   (`https://….up.railway.app`); otherwise open **Settings → Networking →
   Generate Domain**.
2. Make a long random secret:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Add it to the service's variables as `ASSISTANT_TOKEN` — a different one from
   `KINDLE_TOKEN`. Railway redeploys; the log says
   `HTTP endpoint listening on port …: … Android assistant`.

## 2. On the phone

1. Install **HTTP Shortcuts** from the Play Store or F-Droid.
2. **Variables** (the `{ }` screen) → add two **Static** variables:
   - `david_url` — your domain, e.g. `https://david-production.up.railway.app`
     (no slash at the end);
   - `david_token` — the `ASSISTANT_TOKEN`; tick **Treat value as secret**.
3. Create a new shortcut of the **Scripting Shortcut** type (it runs only the
   script below, no request of its own), name it `David`, and paste this as its
   script. Cancelling any of its text boxes stops it with nothing sent.

```js
const base = getVariable('david_url');
const token = getVariable('david_token');

function post(path, payload) {
  const result = sendHttpRequest(base + path, {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (result.status === 'networkError') {
    return { say: 'David is not reachable: ' + result.networkError };
  }
  try {
    return JSON.parse(result.response.body);
  } catch (e) {
    return { say: 'David answered ' + result.response.statusCode };
  }
}

const text = prompt('David — what should I do?');
let answer = text ? post('/assistant/ask', { text: text }) : null;

// A missing part: ask it, send the answer back with the id.
while (answer && answer.ask) {
  speak(answer.say);
  const reply = prompt(answer.ask);
  answer = reply ? post('/assistant/ask', { text: reply, id: answer.id }) : null;
}

// A write: nothing happens before Yes.
if (answer && answer.confirm) {
  answer = confirm(answer.say)
    ? post('/assistant/confirm', { id: answer.id })
    : { say: 'OK, nothing done.' };
}

if (answer) {
  speak(answer.say);
  showDialog(answer.say, 'David');
}
```

4. Open the shortcut's **Trigger & Execution Settings** and turn on **Allow
   triggering via Quick Settings Tile** and, if you like, **Show as app shortcut
   on launcher**.
5. Pull down the quick settings, edit them, and drag HTTP Shortcuts' **Trigger
   shortcut** tile in. Tapping it runs `David`.

Try it with "quanto mi resta questo mese?" first: it changes nothing.

## What it cannot do

- **"Ok Google, ask David …" is not possible.** Google Assistant does not hand
  free text to a personal server without a bridge app; you start the assistant
  with a tap (the tile, or a long press on the HTTP Shortcuts icon).
- **Only the first 400 characters are read aloud** — HTTP Shortcuts' limit. The
  full answer is in the dialog and in the chat.
- `speak()` uses the phone's text-to-speech voice; David's replies are in
  English, so set the phone's TTS to a voice that reads English well, or read the
  dialog.

## If it does not work

See [Troubleshooting § The Android assistant](troubleshooting.md#the-android-assistant-answers-wrong-or-missing-token-or-nothing).
