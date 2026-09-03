# Module 27 — Telegram alerts on BREAKOUT_READY

An open bot. Anyone can `/start` it, no ARGUS account, and gets one
message when a security enters the BREAKOUT_READY watchlist.

## Why it is not gated

ARGUS is pre-revenue and pre-track-record. There is nothing to sell
access to yet, and a paywall in front of an unvalidated signal would be
selling the thing this project exists to avoid. A paid tier gating this
later is a real, discussed direction — so the shape here is chosen to
make that an added check rather than a migration: the table answers *who
is subscribed*, which stays the right question whether or not subscribing
becomes conditional. No billing code, no fourth role, nothing that
assumes free access.

## Three parts

**The webhook service** (`telegram`, web) receives Telegram's POSTs and
answers `/start`, `/stop` and two menus. Everything else is ignored.

**The dispatch cron** (`telegram_dispatch`, `0 23 * * 1-5`) reads Module
10's transition log for the session the scanner just scanned and sends
one message per subscriber per transition.

**The command registration** (`python -m infra.deploy.telegram_commands`)
tells Telegram which commands to offer. Run by hand, not on deploy.

## The two menus

```
🔔 Alerts       manage the BREAKOUT_READY subscription
📊 Statistics   ARGUS's published track record, read from public_stats
```

Both are reachable as buttons and as commands (`/alerts`, `/stats`), so
the keyboard is a convenience rather than the only way in.

### Reply keyboard, not inline — for a security reason

An inline keyboard's buttons produce `callback_query` updates, and each
must be answered with `answerCallbackQuery` or the client spins until it
times out. A webhook response carries exactly **one** method call, so
answering the callback *and* replying needs two API calls — which means
this service would have to hold `TELEGRAM_BOT_TOKEN`, the one thing it
was built not to.

A reply keyboard's buttons send ordinary text messages, which the
existing path already handles: one update in, one reply out, no token.
The cost is cosmetic. `menus.py` maps each label back to the same command
the slash form produces, and a test asserts every label parses.

### The Alerts menu is a UI, not a second mechanism

Its status line is read from `telegram_subscribers` on each press, and
its buttons call the same `subscribers.subscribe` / `.unsubscribe` that
`/start` and `/stop` do. There is no second copy of "is this chat
subscribed" anywhere. Only the applicable action is offered — showing
both would mean one button is always a no-op, and a button that does
nothing teaches a reader the buttons are decorative.

### The Statistics menu calls `public_stats`, and computes nothing

`services/public_stats/aggregates.py` decides what ARGUS is permitted to
say about its own performance — which runs are approved, what the sample
floor is, whether a rate may be quoted at all. This module imports none
of it and calls the same HTTP API the public website calls. Two surfaces,
one source of truth; a second implementation would produce two plausible
numbers with no way to say which was wrong.

**Four states, kept four.** A real number; an honest "N of M required";
nothing published at all; and — the one this module adds — `public_stats`
could not be reached. The last matters most: telling a reader "no track
record yet" because a container was restarting would be a false statement
about the very thing the review gate exists to keep honest. A test drives
all four.

**Over the private network.** `http://public_stats.railway.internal:8080`
by default, overridable with `ARGUS_PUBLIC_STATS_URL` — Railway service
names are chosen in a dashboard and two of ARGUS's are already wrong, so
the internal hostname cannot be assumed. `ARGUS_PUBLIC_PAGE_URL`
overrides the link the message closes with.

One thing makes that hop work and would silently not: `public_stats` in
production refuses plaintext, and the private network *is* plaintext
because Railway terminates TLS at its edge. So the client sends
`X-Forwarded-Proto: https`, which `tls.py` believes only from a peer
inside `RAILWAY_PRIVATE_NETWORK`. Three tests pin it — the call
succeeds, it fails without the header, and it fails from an address
outside that range.

**v1 shows `win_rate` and `cumulative_performance`**, plus whether the
review gate has decided anything. `regime_breakdown`,
`excursion_distribution` and `top_performers` are deferred: each is a
*distribution*, and a distribution flattened into three text lines
misleads by omission. They belong on the page, and the message links
there. No ASCII chart — a curve drawn in monospace on a phone invites
reading a shape into resolution that is not there.

## The public service holds no bot token

The reply to `/start` is sent by *answering the webhook*: Telegram reads
a JSON body of `{"method": "sendMessage", …}` on the webhook response and
performs that call itself. So the internet-facing service needs no
outbound credential at all — only the cron holds `TELEGRAM_BOT_TOKEN`,
and a compromise of the web service cannot send messages as the bot.

The cost is that Telegram does not report back whether that reply landed,
so a failed confirmation is silent. A confirmation is the cheapest
message in the system to lose.

## The webhook is authenticated, and refuses to run otherwise

Telegram publishes no source addresses to allowlist, so the only thing
separating a real update from a forged one is a shared secret. Without
one, anyone who learned the URL could subscribe arbitrary chat ids —
making ARGUS's bot message strangers — or unsubscribe real ones.

`setWebhook` takes a `secret_token`; Telegram then sends it back on every
delivery as `X-Telegram-Bot-Api-Secret-Token`, and the service compares
it with `hmac.compare_digest`. In staging and production the service
**refuses to start** without `TELEGRAM_WEBHOOK_SECRET`. In development an
unset secret produces a *random* one, so nothing authenticates rather
than everything doing so — which keeps "every service boots with only a
connection string" true without the failure mode being an open endpoint.

The token is deliberately *not* in the URL path, which is the common
trick for making a webhook unguessable: a path containing the bot token
writes the credential into every access log, proxy log and browser
history that sees it.

## One-time operator setup

Two secrets on Railway, then one call to Telegram. None of it happens at
app startup — a redeploy must not re-register a webhook.

**1. Create the bot.** Message [@BotFather](https://t.me/BotFather),
`/newbot`, and keep the token it gives you. It is the only copy.

**2. Set the variables.**

| Service | Variable | Value |
|---|---|---|
| `telegram_dispatch` | `TELEGRAM_BOT_TOKEN` | BotFather's token |
| `telegram` | `TELEGRAM_WEBHOOK_SECRET` | any 1–256 chars of `A-Z a-z 0-9 _ -` |

Generate the webhook secret with `python -c "import secrets;
print(secrets.token_urlsafe(32))"`. The bot token goes **only** on the
cron service; the webhook secret goes **only** on the web service.
Neither belongs in `.env`, in this repository, or in a chat.

**3. Register the command menu — once.**

```
python -m infra.deploy.telegram_commands
```

Run against the `telegram_dispatch` service (the one holding the token),
from a Railway shell or any environment where `TELEGRAM_BOT_TOKEN`
resolves. The list lives in `services/telegram/menus.BOT_COMMANDS`, so
the registered menu cannot drift from the parser — a test asserts every
registered command is one the bot answers. Deliberately not run on app
startup: a registration that re-runs on every deploy is one a rollback
silently undoes.

**4. Register the webhook — once, by hand.**

```
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
        "url": "https://<telegram-service>.up.railway.app/telegram/webhook",
        "secret_token": "<TELEGRAM_WEBHOOK_SECRET>",
        "allowed_updates": ["message", "channel_post"],
        "drop_pending_updates": true
      }'
```

`allowed_updates` narrows what Telegram sends to the two containers this
bot reads; everything else would be delivered, ignored, and paid for in
requests. `drop_pending_updates` discards anything queued before the
webhook existed.

Check it with
`curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"` — it
reports the URL, whether a secret token is set, and
`last_error_message`, which is where a wrong URL or a 401 shows up.

To move or remove it: call `setWebhook` again with the new URL, or
`deleteWebhook` to stop delivery entirely.

## The message, and what it will not say

```
AAPL — Apple Inc. entered BREAKOUT_READY.
Session: 2026-03-09

A state change, not a recommendation. ARGUS has no validated track record.
```

Two facts and a caveat. No score, no probability, no target, no "buy",
"opportunity" or "signal to act". A push notification is the least
supervised surface in the project — Module 20 gates aggregate performance
claims behind a named human's approval precisely because such a claim
should not be publishable by accident, and a sentence that would need
that gate on the public page does not become acceptable because it fits
in a phone banner. A test asserts the forbidden words are absent.

The caveat is on every alert rather than only in the welcome text: a
subscriber from six months ago sees only the alert, and a disclaimer read
once against a claim read daily is not a disclaimer.

## Idempotency

`telegram_alerts_sent` is unique on `(chat_id, transition_id)` and the
run reads it before sending. A rerun — a cron that fired twice, an
operator retrying — finds every pair present and sends nothing.

Keyed on the transition rather than `(security_id, scan_date)` because
the transition *is* the announced event: a security that enters
BREAKOUT_READY, falls back and enters again is two changes and two
messages, which a security/date key would silently collapse.

A **failed** send is not recorded, so a rerun of the same session retries
it. Across sessions it is not retried: the next evening's run asks about
the next session, and an alert about a two-day-old state change is worse
than no alert. This is a notification, not a record — the record is
`market_state_transitions`, which none of this touches.

## Failures during a run

- **403, or a 400 saying the chat is gone** — the user blocked the bot or
  the chat no longer exists. Permanent, so the subscriber is deactivated
  and the run continues. Not a failed run: a subscriber left, and that
  was handled.
- **429** — Telegram's own `retry_after` is obeyed once, then the message
  is dropped rather than stalling every subscriber behind it.
- **Anything else** — logged, counted, run continues, and the process
  exits 1 so a cron that could not do its job does not report success.

The alternative — one unreachable chat aborting the run — would mean the
first person to block the bot silences it for everyone.

## Where this sits in the evening

```
21:00 UTC  ingestion          full-universe OHLCV
22:30 UTC  scanner            classifies, writes market_state_transitions
23:00 UTC  telegram_dispatch  reads them, sends
```

Thirty minutes after the scanner. Its worst case is four attempts with
exponential backoff plus the scan itself, so thirty minutes covers a bad
evening; and not longer, because an alert's whole value is being fresh —
23:00 UTC is early evening in the Americas.

Being wrong about this margin is survivable in a way being wrong about
ingestion's is not: a dispatch that runs too early finds no transitions,
sends nothing, and exits 0.

**Note the standing blocker.** `docs/architecture/KNOWN_ISSUES.md` G1:
the scanner cannot currently see the bars ingestion writes, so it records
no transitions, so this bot has nothing to send. Fixing G1 is what turns
this module on.

## Known gaps, flagged not fixed

- **No `getWebhookInfo` health check.** Nothing notices if Telegram
  stopped delivering — a wrong URL after a domain change, or a webhook
  someone deleted. The bot would simply go quiet and the only symptom
  would be no new subscribers. A daily check against `getWebhookInfo`'s
  `last_error_message` belongs in Module 23's health surface.
- **No message for a security leaving BREAKOUT_READY.** Alerting on one
  state is the scope, and a "no longer ready" message is arguably the
  more useful half. It needs its own copy decision — that sentence is
  much closer to advice than the entry one — so it is not smuggled in
  here.
- **No client-side cache on the Statistics menu.** Every press is a live
  call to `public_stats`, so a burst of presses spends that service's
  Module 24 rate-limit budget from one internal address. Harmless at the
  current subscriber count (zero) and a ~15-line TTL cache when it is
  not; not built, because a cache is state and this one has no users to
  justify it yet.
- **`telegram_alerts_sent` grows as subscribers × transitions** and no
  retention policy covers it. It is prunable (not append-only guarded);
  a policy must only prune rows older than the dispatch window, or
  pruning becomes re-sending.
