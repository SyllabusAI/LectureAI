# Handoff: Syllabus, for Liam

Written 2026-09-26, the day both repos moved into the SyllabusAI GitHub org.
Trace built everything below with Claude Code between 2026-09-08 and today, and
most of what follows lived only in Claude's memory notes on Trace's Mac. This
file moves it into the repo. Where a note and the code disagreed, the code as
of today wins, and the disagreement is called out.

Three companion files sit next to this one on Trace's Mac and are NOT in git,
because `.gitignore` ignores `*.md` except a short allowlist: `HOME-STRETCH.md`
(the plan and the economics), `SESSIONS.md` (how work was split into
sessions), and `HANDOFF-STRIPE.md` (the P6 plan, now finished). This file is
allowlisted; add the others the same way if you want them in the repo.

---

## 1. What Syllabus is and where it runs

Syllabus records a lecture on a student's Mac, transcribes and summarizes it,
and files a study summary as a Google Doc in the right course folder in Drive,
with the transcript one level down. A Mac signed in to a Syllabus account holds
no API keys: transcription and summaries go through a metered proxy on the
account service, which also gives every Mac a panel address on the web and
holds the account's Drive grant and class schedule. The same code runs a second
profile, Sous, for the agency's client calls; Sous is internal only and is cut
from the launch.

| Surface | What it is | Where it lives | Who deploys it, and how |
|---|---|---|---|
| Syllabus.app | PyInstaller bundle of the `intake/` Python package: Flask panel on 127.0.0.1:5173, recorder, watcher, pywebview window, menu bar item, bundled static ffmpeg | `SyllabusAI/LectureAI`, built by `packaging/build.sh`, published as a DMG by `release.yml` on a `vX.Y.Z` tag | Whoever pushes the tag. Latest release: v0.4.0 (2026-09-19). Each user replaces their own copy; there is no self-update yet |
| Terminal install | The same package via `pipx install git+https://github.com/SyllabusAI/LectureAI` | Same repo; the repo is public so this line works | Nothing to deploy; users run `pipx upgrade intake` |
| Account service | Hono + TypeScript Cloudflare Worker, D1 database, one `PanelRelay` Durable Object per claimed Mac, the `/proxy/*` metered endpoints, Stripe billing | `SyllabusAI/syllabus-accounts`, live at `https://syllabusaccounts.maincoursemedia.com`, Cloudflare account `896f047d297ba60187557f9029f6fbc5`, D1 `syllabus-accounts` (`9ed6731f-6323-4999-9674-0cb898b991aa`) | A merge to `main` deploys via `deploy.yml` once `CLOUDFLARE_API_TOKEN` is set; by hand from `main`: `npm run db:migrate:remote` then `npm run deploy`. See section 2 |
| Marketing pages | `/syllabus/` (product page) and `/syllabus/download` (install walkthrough) on maincoursemedia.com; `/syllabus/panel` 302s to Trace's own panel | Private repo `Main-Course-Media/main-course-media` (Astro): `src/pages/syllabus.astro`, `src/pages/syllabus/download.astro`, `public/_redirects`, `public/_headers` | By hand with wrangler to Cloudflare Pages. A merge alone changes nothing live |
| Google Cloud | OAuth consent screen (In production, `maincoursemedia.com` verified in Search Console), the bundled Desktop client, and the Web client the service uses | Project `friendly-bazaar-507320-b7`. A second project named LectureAI holds an older Web client that nothing uses any more | Console only |
| Stripe | Products, prices, webhook, coupon, tax | Main Course Media LLC account, **sandbox only** | Dashboard, plus `wrangler secret put` and the `STRIPE_PRICE_*` vars in `wrangler.jsonc` |
| Provider keys | Groq (transcription, primary), OpenAI (transcription fallback), Anthropic (summaries) | Worker secrets on the account service, never on a Mac | `npx wrangler secret put <NAME>` |
| Apple | Developer Program enrollment for Developer ID signing and notarization | Not enrolled. Organization verification completed, payment declined 2026-09-17 | Nothing until enrolled |

Trace is granting you access to each of the third-party accounts through their
dashboards. GitHub is done: you and Trace are both org owners, and `main` in
both repos now requires a PR with green CI.

---

## 2. How to deploy each piece today

### The Mac app, and the one that runs on Trace's Mac

A `git pull` deploys nothing. The app is a frozen copy of `intake/`, so a
change in the checkout reaches nobody until an app is rebuilt.

To build locally, from `~/apps/LectureAI`:

    .venv/bin/pip install -e '.[app,build]'   # once
    packaging/build.sh                        # writes dist/Syllabus.app

`packaging/build.sh` alone deploys nothing either: it writes to `dist/`, and
`dist/` is not what runs. On Trace's Mac the live panel is
`~/Desktop/Syllabus.app`, opened from the Desktop like any app. It is NOT a
launchd agent (verified 2026-09-17: no Syllabus plist in
`~/Library/LaunchAgents`; the `application.com.maincoursemedia.syllabus.*` row
in `launchctl list` is just how macOS tracks a running GUI app). To put a new
build there:

    osascript -e 'quit app "Syllabus"'
    rm -rf ~/Desktop/Syllabus.app && cp -R dist/Syllabus.app ~/Desktop/
    open ~/Desktop/Syllabus.app

Verify by behavior on 5173, not by the process table: ask the live panel for
something only the new code answers (for example
`curl -s http://127.0.0.1:5173/api/setup | grep -c managed`). `ps aux | grep
[S]yllabus` shows the bundle path and its build time. Never start the watcher
or a recording on Trace's Mac from a session: the watcher spends money and
writes to his Drive and Notion.

To ship to everybody: bump `__version__` in `intake/__init__.py`, merge, then
tag that commit `vX.Y.Z` and push the tag. `release.yml` runs the test suites
on a macOS runner, builds the app, wraps it with `packaging/dmg.sh`, and
publishes a public GitHub Release with `packaging/release-notes.md` as the
notes. The tag must equal the version or the workflow refuses. Every running
panel polls the release list once a day (`intake/updates.py`) and shows a
banner, so a tag is user-visible within a day. `packaging/release-notes.md` is
one of the three `.md` files allowlisted in `.gitignore`; any new packaging
file that CI reads needs the same treatment, or the tag builds and then fails.

To ship a new bundled ffmpeg: bump `VERSION`, `SHA256`, and the tag in
`packaging/ffmpeg/release`, merge, dispatch `ffmpeg.yml` from `main` with
publish ticked, then pin the new sha256. `--disable-autodetect` drops
avfoundation unless `--enable-avfoundation` is passed.

For UI work, preview on the `panel-dev` launch config (port 5199, in
`.claude/launch.json`), never on 5173. Flask caches templates with debug off,
so restart the dev server after editing `intake/templates/*.html`.

### The account service

Since 2026-09-26 (syllabus-accounts #34) a merge to `main` deploys itself:
`.github/workflows/deploy.yml` waits for the CI `check` on that commit,
applies D1 migrations, then runs `wrangler deploy`. It needs the repository
secret `CLOUDFLARE_API_TOKEN` (account-scoped: Workers Scripts:Edit, D1:Edit,
Workers Routes:Edit on the maincoursemedia.com zone). Until that secret is
set, every Deploy run fails at the migration step and changes nothing live.
Confirm a deploy with `gh run list --workflow Deploy --limit 1`. The by-hand
path still works and is the fallback, from `main`, in this order:

    git checkout main && git pull --ff-only
    npm run db:migrate:remote     # only when a migration was added
    npm run deploy

Migrations first, always. The new code writes columns that do not exist yet if
you deploy first. Twelve migrations exist (`0001` to `0012_topups.sql`) and
all are applied to the remote D1 as of 2026-09-22. `npx wrangler d1 migrations
list syllabus-accounts --remote` says what is pending; on a Mac where wrangler
is logged in to more than one Cloudflare account it needs
`CLOUDFLARE_ACCOUNT_ID=896f047d297ba60187557f9029f6fbc5` in the environment,
which is why `account_id` is pinned in `wrangler.jsonc`.

A deploy takes a minute or so to reach every edge colo, so a new route can 404
briefly right after `npm run deploy`; re-check before debugging it.

Secrets are set with `npx wrangler secret put <NAME>`, never in the repo. The
eight that exist today, by name: `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`,
`DRIVE_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`,
`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`. Changing `DRIVE_KEY` makes every
stored Drive grant unreadable and everyone reconnects Drive. For local dev the
same names go in `.dev.vars` (gitignored; `.dev.vars.example` lists the first
three).

The Worker is served only on its custom domain. A `workers.dev` subdomain
(`maincoursemedia`) exists on the account because Durable Objects require one;
nothing is served there.

### The marketing pages

In `Main-Course-Media/main-course-media`. Merge the PR, then deploy by hand
with wrangler to Cloudflare Pages; the memory notes record three such deploys
on 2026-09-17 and the live page being a specific Pages deployment id, so
"merged" and "live" are different states there too. The dev server is the
`site` entry in that repo's `.claude/launch.json` on port 4321.

Both `/syllabus/` and `/syllabus/download` are unlisted in four places that
move together at launch: `noindex` on the page, the sitemap filter in
`astro.config.mjs`, an `X-Robots-Tag` block in `public/_headers`, and no link
from the nav, footer, or `llms.txt`. Each page's header comment carries the
list and how to reverse it, plus flipping the Offer schema from PreOrder to
InStock. Prices on the page are the HOME-STRETCH table ($9 / $15 / $25) and
move only when that table moves.

---

## 3. Things that will bite you

**The frozen app is not the checkout.** `~/Desktop/Syllabus.app` carries its
own copy of `intake/`, so merging to `main` and pulling changes nothing on
Trace's Mac, and nothing on any user's Mac until a release is tagged and they
replace the app. On 2026-09-15 a `git pull && launchctl kickstart` reported
"Already up to date" and the panel kept serving the morning's build with no
error anywhere. Rebuild with `packaging/build.sh`, replace the app, quit and
reopen, then verify by asking the panel for something only the new code has.

**Relay disconnects: mostly sleep, the rest was invisible.** The panel holds a
WebSocket to the service (`intake/relay.py`), and most "the connection closed"
lines in `~/.intake/syllabus/panel.log` match the Mac's own sleep and
dark-wake cycles in `pmset -g log`; they are benign and the client reconnects.
The real complaint from the 2026-09-16 audit was a 97-second gap during active
use, which was about six failed reconnect attempts that logged nothing,
because the client only printed on `on_open`. PR #67 added logging for failed
reconnects and cut the status-poll noise (93% of the log); the cause of that
gap is still unattributed, deliberately, until a day of real use is read back.
Separately, `src/panel-relay.ts` line 86 still sets a
`WebSocketRequestResponsePair("ping", "pong")` auto-response, which matches a
text message whose body is `ping`; the client sends protocol-level ping frames
(`run_forever(ping_interval=...)`), so the two never meet and the keepalive the
Worker's comment describes is not the one the client sends. Not yet fixed.
`panel.log` used to never rotate (3.7 MB and 54,000 lines when read); since
the chunk-resume PR, `intake/logfiles.py` moves it to `panel.log.1` past 2 MB
(two old copies kept), at panel start and every ten minutes, and repoints the
launchd-redirected stdout and stderr at the fresh file. `pipeline.log` is the
lecture history and is deliberately never rotated.

**`npm test` in syllabus-accounts hangs, and CI retries it six times.**
`@cloudflare/vitest-pool-workers` deadlocks at a rate that swings between
1-in-6 and 4-in-5 locally and is worse on GitHub runners; it hangs either at
startup with only the `RUN v4.x` banner, or after every test has passed. It is
content-independent (a duplicate of a passing file reproduces it), so read the
per-file counts before blaming a branch. `ci.yml` runs up to six attempts of
`timeout --signal=KILL 150` inside a 20-minute job and retries only on exit
137/124, so a real failure can never be retried into a pass. Locally, run
vitest in the background and kill it after ~120 seconds; macOS has no
`timeout`. Two other flakes in the same suite: `devices.test.ts` "limits
polling too" can exceed the 5,000 ms default on CI (needs an explicit
timeout), and `proxy.test.ts` "rate limits an account that floods it" straddles
a fixed 60-second window on a slow runner (needs a stubbed clock).

**Two Google OAuth clients, and why `drive.file` grants are per project.**
The bundled Desktop client (`intake/credentials.json`, committed on purpose;
see SECURITY.md) and the Web client the service uses
(`61659290097-hch8...` in `wrangler.jsonc`) both live in project
`friendly-bazaar-507320-b7`, deliberately: `drive.file` only reaches files
created by the same Cloud project, so a grant on the account can see the
"Lecture Notes" folder a Mac's own `intake login` created. The first version of
the service used a Web client (`521787...`) in a separate project named
LectureAI, and its Drive grant could not see the 53 files the Desktop client
had made, which is why the uploader has the `DRIVE_SOURCE=auto|account|local`
guard. That old client is unused and can be deleted; its secret still sits
unused in Trace's `~/.intake/syllabus/.env`. The Web client's authorized
redirect URIs must include the production `/oauth2/callback` and
`http://localhost:8787/oauth2/callback` for `npm run dev`. The Cloud project's
privacy policy URL still points at the agency policy, which never mentions
Syllabus (HOME-STRETCH P1).

**The allowance model, and the owner row.** The proxy meters transcription in
audio seconds and summaries in tokens, and both refuse with the same code,
`allowance_exhausted`, which is why a summary that ran out was reported for
weeks as a transcription problem. `TRIAL_ALLOWANCE` in `src/proxy.ts` (5 hours
of audio, 150,000 tokens a month) applies to any account with no row in
`allowances`. A summary reserves `chars/4 + 16,000` tokens up front and settles
to the real cost after, so an account under ~16k tokens can summarize nothing
whatever its audio balance says; `/proxy/usage` returns `recordable_seconds`
(the smaller meter, in lecture seconds) for exactly that reason. A lapsed
subscription gets a row of zeros, not the trial back. A row with `source =
'owner'` is written by hand and the Stripe webhook leaves it alone. Trace's
account (`cTtK9kx9IMlxJf6T`) has had one since 2026-09-17: 144,000 audio
seconds and 1,500,000 tokens, written with `wrangler d1 execute
syllabus-accounts --remote --command "INSERT INTO allowances ..."` against the
columns in `migrations/0005_usage.sql` (`account_id`, `audio_seconds`,
`summary_tokens`, `source`, `updated_at`); one `DELETE` reverses it. To see
where an account stands, query D1 (`SELECT kind, SUM(units) FROM usage WHERE
account_id=... AND period='YYYY-MM' GROUP BY kind`), not the panel's message.
A transcription that failed mid-upload used to re-bill every chunk, because
resume saved the transcript only after every chunk succeeded (one 73-minute
lecture was billed three times on 2026-09-17). Now each chunk's text is kept in
the resume slot's `chunks/` folder as it lands, keyed by the source file's size
and mtime, the provider, its chunk length, and the split, and a retry sends only
the chunks that never came back.

**Groq is primary, OpenAI is the fallback, and the split lives in the data.**
`/proxy/transcribe` tries Groq `whisper-large-v3` ($0.111/hr) and falls back to
OpenAI `gpt-4o-mini-transcribe` ($0.18/hr) on any Groq failure, not just a 429,
because the service spends one key for everybody and a rate limit would
otherwise cap the whole product. Every settled usage row records its provider;
an account's own share is `transcribed_by` on `GET /proxy/usage`, and the
fleet-wide table is `npm run split`. `unrecorded` means before the column, not
a third provider. Confirmed serving from Groq in production 2026-09-19. The
Groq account is on the FREE plan: 28,800 audio seconds a day across every
account together (8 hours of lecture, total), 20 requests a minute, 25 MB per
request. Every forecast in HOME-STRETCH assumes the Developer plan; adding a
card is an open dashboard item. Turbo (`whisper-large-v3-turbo`, $0.04/hr) is
registered in `intake/providers.py` and not used by the managed path; it is
one constant in `proxy.ts`, gated on a real-lecture quality benchmark nobody
has run.

**The study assistant is BYO key only and bypasses the proxy.** Shipped
2026-09-19 (LectureAI #79) in `intake/assistant.py` behind `/api/assistant`
and `/api/assistant/ask` (SSE): two-stage context (every summary for a course
as cited, cached document blocks; full transcripts only when the model calls
`fetch_transcripts` with a reason). It calls Anthropic directly on the Mac's
own `ANTHROPIC_API_KEY` from `.env`, even when the Mac is signed in and
everything else goes through the service, so it works on Trace's Mac and for
nobody else. What is still P7's work: a metered `/proxy/assistant` and a
session cap wired to the entitlement (`assistant_sessions` is already a column
on `allowances`, unenforced). `config.ASSISTANT_MODEL = "claude-sonnet-5"` is
a pricing decision with a test pinning it: Pro at $25 nets 53% on Sonnet and
29% on Opus. Every session appends JSON to `assistant.log` in the profile home;
`assistant.escalation_rate()` reads the rate the tiers are priced on (modeled
at 15%, effectively unmeasured).

**Concurrent Claude sessions share one checkout.** On 2026-09-15 a `git add
-A` from one session swept another session's uncommitted Notion edits into PR
#47 under an unrelated title, and both merged. Stage named paths, never `-A`;
before committing run `git status --short` and `git log --oneline -1`; if your
edits look already committed, check `git reflog` for another session's commit.
For two sessions in one repo, use a worktree (`.claude/worktrees/`, ignored in
both repos). The git stash stack is shared across worktrees, so never bare
`git stash`. The LectureAI `.venv` works from any worktree; syllabus-accounts
needs `npm install` in each.

**The panel's pages are tested by running them, in jsdom.** Since PR #65,
`test_panel.py` renders both pages through Flask's test client and hands them
to node, which runs their inline scripts in jsdom; assertions are in
`tests/panel/panel.test.mjs`. Locally you need `npm ci --prefix tests/panel`
first or the suite skips and exits 0; CI sets `SYLLABUS_REQUIRE_JS_TESTS=1` so
the skip fails there. jsdom was chosen on measurement: happy-dom answers
`selectedIndex 1` for any `<select>` with a `selected` attribute, and linkedom
silently no-ops `sel.value = <missing option>`, so each would pass the exact
bug the tests exist for. Do not extract the page JS to `intake/static/*.js`:
`pyproject.toml` package-data lists `static/*.png` only, so the file would be
dropped from the wheel silently. Timers in the harness are recorded and never
armed, and an unanswered fixture request raises; both are load-bearing. CI
also needs `ffmpeg` and `lsof` on the runner, and runs on Python 3.11 and
3.12. LectureAI suites can hang on stdin (a wizard prompt did, for 400
seconds), so run them under a watchdog rather than waiting.

**Stripe is complete and entirely in a sandbox.** P6 shipped 2026-09-22 as
syllabus-accounts PRs #27 to #31: `src/tiers.ts` (one place the tiers are
written down; the hour cap binds, tokens are 30k per hour of headroom),
`src/stripe.ts` (the webhook, mounted before the auth middleware, raw-body
signature check, idempotency on event id in `stripe_events`),
`src/billing.ts` (`/billing/checkout`, `/billing/portal`, `/billing/topup`),
the 5-hour trial as a 90-day Stripe trial that `endTrialIfSpent` cuts short,
and top-ups summed at read time. Only the webhook writes an allowance. Two
dashboard items are still unset: the one-time $5 top-up Price
(`STRIPE_PRICE_TOPUP` is empty, so the button is not offered) and the Customer
portal, which `/billing/portal` needs. A sandbox shares nothing with live
mode, so going live recreates the three Prices plus the top-up Price (four
`STRIPE_PRICE_*` vars in `wrangler.jsonc`, a commit and a deploy), the webhook
endpoint and its new signing secret, the live secret key, the 100%-off coupon
and promotion code, and the Texas tax registration with the SaaS tax code.
Check each price id against its amount in the dashboard: a wrong id grants
Starter loudly, a swapped pair grants the wrong tier quietly. Known gap: a
cancellation Stripe never delivers leaves the last allowance row in place
until something sweeps `allowances` against `subscriptions`. Activating the
live side (entity details, EIN, bank account) is slow and blocks nothing in the
sandbox, so start it early.

**Signing is deferred, and the download page is the workaround.** Apple
organization verification completed and Trace declined the $99/year on
2026-09-17; the revisit trigger is the first paying student or the first
person who cannot install it, and it is needed anyway for Stage 1 per
HOME-STRETCH. No codesigning identity exists on Trace's Mac, no
`SYLLABUS_CODESIGN_IDENTITY` variable and no certificate secrets on the repo;
`build.sh` and `syllabus.spec` already read the identity variable, but
`release.yml` describes signing only in a comment and has no codesign,
notarytool, or stapler steps. Whether Apple's completed verification expires
is unknown. Meanwhile `/syllabus/download` (main-course-media PR #176) is a
real install page: four steps, HTML-and-CSS mocks of both Gatekeeper windows,
why an unsigned app is blocked, and a troubleshooting FAQ. Cut that
walkthrough when Developer ID signing lands; the page's header comment says
what to keep. There is no self-update until the build is signed.

**CSS specificity on the marketing page.** In `syllabus.astro`, blanket rules
like `.syl a`, `.syl p`, and `.syl h1, .syl h2, .syl h3` are (0,1,1) and
outrank the single-class `.syl-btn--*` and `.syl-hero__*` rules at (0,1,0).
That silently ate a button color (white on white in the CTA), then the h1's
`margin-inline: auto`, then the subhead's; each is fixed and commented at the
rule. If you add a `.syl <element>` blanket rule, check what it cancels. Two
more from the same page: the sitemap filter regex once anchored on
`/syllabus/` alone (it names both pages now), and the nav pill overflowed by 3
px at 375 px after the Sign in link was added, so measure that bar with
`getBoundingClientRect` after any nav change rather than trusting a screenshot.

**Prompt parity across the two repos, and the new required checks.** The
summary prompt exists twice, in LectureAI's `intake/schemas.py` and
syllabus-accounts' `src/prompts.ts`, and each repo carries a copy of
`prompt_parity.py` that must stay the same logic (they have drifted twice). In
syllabus-accounts the check runs on every PR and fetches LectureAI's `main`,
so a prompt reword lands in LectureAI first. In LectureAI it runs only on push
to `main`, daily, and on demand (`prompt-parity.yml`), on purpose, to avoid a
cross-repo deadlock. As of today `main` in LectureAI lists `parity` as a
required status check alongside `tests (3.11)`, `tests (3.12)`, and
`copy-standards`, but `parity` never reports on a pull request (PR #80's
checks show only the other three). If the first LectureAI PR after protection
sits at "expected", that is why: drop `parity` from the required list rather
than putting it on `pull_request`.

**Two things about the accounts flow worth keeping.** `/device/poll` answers
`slow_down`, not 429, because a 429 makes the panel's `run_claim` abandon a
claim that is still good. And do not escape `$` when quoting `.env` values:
interpolation is already off in both readers, so escaping writes a literal
backslash. One security PR from Liam's 2026-09-16 audit is still open:
device-token expiry and dropping the vulnerable `sharp` chain (`npm audit`
shows 4 high on it). Everything else from both audits is closed and verified
in production.

**Where things live on a Mac.** The profile home is `~/.intake/syllabus/`
(not the flat `~/.intake`, which is pre-2026-09-11 scratch): `.env`,
`schedule.toml`, `account.json`, `token.json`, `pipeline.log`, `panel.log`,
`assistant.log`, `inbox/`, `processed/`, `.work/` (recording state, relay
state, resume files, ffmpeg logs). Sous is `~/.intake/sous/` and is empty.
When a recording goes missing, check in order: `ps aux | grep ffmpeg`,
`.work/`, `pipeline.log`, `pmset -g log | grep -i "sleep\|wake"`, then the
flock on `.watcher.lock`. SIGINT finalizes an ffmpeg file; SIGKILL destroys it.

---

## 4. Where the project stands against HOME-STRETCH.md

HOME-STRETCH.md (on Trace's Mac, not in git; see the top of this file) is the
plan, written 2026-09-15 and updated through 2026-09-22. Read it for the
economics and the security bar. The short version:

**The launch plan.** Two stages. Stage 1, the founding cohort, target Monday
**2026-10-13**: 25 invited users, free, on BYO keys, needing the signed app,
the legal and consent work, self-serve deletion, and the security bar. Stage
2, the paid launch, target Monday **2026-11-17**: managed keys, metering,
Stripe, the three tiers, and the assistant. Pricing is decided: Starter $9 (15
hr), Standard $15 (30 hr), Pro $25 (45 hr plus 15 assistant sessions),
published on the site 2026-09-19. No free tier; friends and family get a
100%-off promotion code. Trial is 5 hours with a card up front.

**Done.** P4 transcriber seam (LectureAI #50). P5 managed keys and metering
(syllabus-accounts #12, LectureAI #52). P6 Stripe and entitlements (#27 to
#31, sandbox). Groq as the transcription leg (#24, #25, LectureAI #78). The
Pro price. The assistant's model. Both audits from Liam's 2026-09-16 reports
except one hardening PR. The Cloudflare Tunnel and the panel's own sign-in,
retired. The Google Cloud project published. Releases v0.2.0 through v0.4.0
built by `release.yml`, so "it has never run" under P0 is stale.

**Half done.** P7 study assistant: the BYO single-Mac version is shipped;
`/proxy/assistant` and the entitlement cap are not.

**Open, roughly in the order the plan wants them.** P0: Apple enrollment
(deferred on cost), Developer ID signing and notarization in `build.sh` and
`release.yml`, Sparkle self-update. P1: privacy policy and terms naming Main
Course Media LLC, hosted on the account service; the Cloud project's privacy
URL; a blocking "I have permission to record" consent; self-serve account
deletion; a no-training statement; 18+; lawyer review. P2: the security bar
as defined in the plan (threat model, rate limiting on unauthenticated
endpoints, ASVS pass, CSRF confirmation, a `DRIVE_KEY` rotation procedure).
P3: opt-in crash reporting, D1 activation analytics, the invite path. P8:
un-hide the pages, announce. Off the code path: the insurance broker call, the
lawyer, a card on the Groq account, the top-up Price and Customer portal in
Stripe, and measuring the escalation rate with real study weeks.

Note the tension the plan itself names: Stage 1 requires the signed app, and
signing is the one phase deferred. Enrolling Apple is the first thing on the
critical path.

---

## 5. Trace's personal seats that you do not inherit

**His Syllabus login, `hugahound23@gmail.com`.** A personal Gmail, not the
Groundbreaker address. It is the Google identity behind Trace's account on the
service (`cTtK9kx9IMlxJf6T`), the owner of his Drive grant, his class
schedule, and the hand-written owner allowance row. Sign in with your own
Google account; the service creates an account on first sign-in, keyed by the
Google `sub`. If you want an owner allowance, write your own row the way
section 3 describes; the trial default applies until you do.

**His Mac as a panel device.** Device `3XwJMPZ3_2RREYbt` (claimed as
"Joshs-MacBook-Pro-2755") is the only Mac on the service, and
`maincoursemedia.com/syllabus/panel` 302s to its relay address, which only
his account can open. Your panel is your own Mac, signed in to your own
account, at its own `/p/<device>/` address. Never point a public sign-in at
`/syllabus/panel`; the public entry is the service root, which serves the
Google sign-in when signed out and your own account page when signed in.

**The BYO keys in `~/.intake/syllabus/.env`.** Trace's Mac carries his own
`ANTHROPIC_API_KEY` (which the study assistant spends), an unused secret for
the old LectureAI-project Web client, and whatever else the Setup page saved.
None of it is in either repo and none of it is meant to be shared. A signed-in
Mac needs no keys for transcription or summaries; the assistant needs an
Anthropic key of your own until `/proxy/assistant` exists.

**Notion.** The pipeline's Notion step files action items into Trace's own
weekly to-do pages through an internal integration secret in his `.env`
(`NOTION_TOKEN`, `NOTION_DATABASE`). It is optional and skipped when unset.
Your own Notion, if you want the step, is your own integration.

**Claude's memory notes.** The source of this document is a directory of
memory files under `~/.claude/projects/` on Trace's Mac. They are not in git,
they are dated point-in-time observations, and several are already stale (the
deploy workflow that does not exist, the "no branch protection" note, the
v0.2.0 release as the latest). This file supersedes them; from here on, the
repo is where operational knowledge goes.
