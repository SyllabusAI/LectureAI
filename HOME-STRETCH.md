# Home stretch: Syllabus to market

Written 2026-09-15 from Trace's answers to the 50-question planning pass.
This is the working plan. It supersedes the "V2 roadmap" section of the
README for anything commercial; the README stays the source of truth for how
the software works.

---

## The headline

Two answers in the planning pass conflict with each other, and one of them
conflicts with arithmetic. Both are addressed below rather than deferred.

1. **V3 by launch, launch by October 10, at 8 hours a week.** The remaining
   work is roughly 90 hours. Eight hours a week puts an unqualified launch in
   early December, not October. The fix is a two-stage launch, below, which
   preserves the October date, the 25-user target, and V3.
2. **Pro at $22 with the study assistant loses money.** With current Anthropic
   pricing, a full-course study session costs more than an eighth of the
   monthly subscription. The tier table below is rebuilt with real numbers.
   (RESOLVED 2026-09-19. Transcription moved to Groq and the assistant was
   recosted on Sonnet 5, which took Pro's COGS from $19.30 to $10.75. Pro is
   now **$25**, published, netting $13.22 at a 53% margin. The resolution
   depends on P7 shipping on Sonnet rather than Opus; see Tiers.)

Everything else in the answers holds and is locked.

---

## The two-stage launch

### Stage 1: founding cohort, target Monday October 13

Twenty-five users, invited, free. This is the "25 users total" win condition
from the planning pass, and it needs none of the commercial machinery.

- Runs on **BYO keys**, which already works today. No proxy, no metering, no
  Stripe, no tiers.
- What it does need: the signed and notarized app, the legal and consent work,
  self-serve deletion, and the security bar passed in full.
- Roughly 32 hours of work. Four weeks at 8 hours a week lands October 13.

Stage 1 exists to find out what breaks with people who are not you, before
any money is involved. That is worth more than three extra weeks of features.

### Stage 2: paid launch, target Monday October 12

- Managed keys, metering, Stripe, the three tiers, and V3.
- Roughly 58 further hours. Seven weeks at 8 hours a week.

**Mid-November is better timing than October 10, not worse.** A study tool
sells hardest during the ramp to finals. The week after fall break is the
deadest point in a student's semester for adopting anything new.

---

## Locked decisions

| # | Decision |
|---|---|
| Entity | Main Course Media LLC, Texas. Revisit at ~$2k MRR or first institutional contact. |
| Sous | Internal only. Never publicly listed. |
| Free tier | None. A Stripe promotion code at 100% off is how friends and family get in. |
| Trial | 5 hours of audio, card collected up front. |
| Keys | Paid only. Managed keys are the product; BYO is the founding cohort only. |
| Apple | Organization enrollment, existing D-U-N-S. Started 2026-09-15. |
| Distribution | Direct download, Sparkle self-update. No Mac App Store. |
| Landing page | Stays on maincoursemedia.com until Syllabus shows growth. |
| Support | syllabus@maincoursemedia.com (see note below). |
| Cut from launch | Diarization, Sous, multi-Mac support. |
| Non-negotiable | Passes the security bar in full before Stage 1. |

### On the entity question

Shipping a consumer app under the agency LLC is **not a hazard at 25 users**,
on two conditions:

1. **Call the insurance broker this week.** MCM's general liability and E&O
   almost certainly cover marketing services, not software products. A claim
   from a Syllabus user is likely uncovered today. This is a phone call and it
   is the highest-value hour on this entire list.
2. **The ToS must name Main Course Media LLC**, carry a limitation of
   liability, and carry an indemnity from the user for recordings they make.

The upside of staying put is real: "Media" in the name makes an agency that
also ships software credible, and the D-U-N-S already exists so Apple
organization enrollment can proceed now. Spinning up a new LLC would restart
D-U-N-S verification and Apple enrollment and cost weeks.

**Texas taxes SaaS as a data processing service**, 20% exempt, so about 80% of
each sale is taxable at the state and local rate. Turn on Stripe Tax before
the first dollar. This applies under either entity and is easy to miss.

### On the support address

Use **syllabus@maincoursemedia.com**, an alias on the already-verified domain.
It is filterable, it is what goes on the Google OAuth consent screen, and it
costs nothing. Registering a separate `maincoursemediahq.com` adds a domain to
verify, splits the brand, and reads like a lookalike domain to anyone checking.

---

## Unit economics

Verified against current published rates, 2026-09-15.

**Per hour of lecture audio**

| Line | Rate | Cost |
|---|---|---|
| Transcription (`groq/whisper-large-v3`) | $0.111/hr | $0.111 |
| Summary in (~8k tokens, Sonnet 5) | $2.00/M | $0.016 |
| Summary out (~1.5k tokens, Sonnet 5) | $10.00/M | $0.015 |
| **Total per audio hour** | | **~$0.14** |

Budget **$0.15** to cover retries. Verify the transcription line against a
real lecture in the provider benchmark before the tier caps are final.

**The $0.111 is not being paid yet, and not in a good way.** Groq never asked
for a card, and the Developer plan's limits are not visible from the account,
which means Syllabus is on the **free plan**. Confirmed limits there, across
EVERY account together, not per user:

| Free plan limit | Value | What it is in lecture terms |
|---|---|---|
| Audio seconds / day | 28,800 | **8 hours of audio a day, total** |
| Audio seconds / hour | 7,200 | 2 hours of audio an hour, total |
| Requests / minute | 20 | ~2 lectures uploading at once |
| Requests / day | 2,000 | ~220 lectures |
| Max file size | 25MB | fine: the proxy caps a chunk at 12MB |

8 hours of audio a day is the binding one. Twenty-five students recording one
lecture each blows through it before lunch, and everything past it falls back
to OpenAI at $0.18. The 20 RPM cap is nearly as tight: the Mac uploads a
lecture as 8-minute chunks, so a single 75-minute lecture is ten requests in
quick succession and two at once is at the ceiling.

**So the free plan quietly inverts the economics at exactly the point they
start to matter.** Below the cap Groq is free, which is better than $0.111.
Above it the effective rate climbs toward $0.18 and the whole exercise
delivers nothing. Today, at one user, it is free and fine. At 25 users it is
mostly fallback.

**Action: add a card to the Groq account to reach the Developer plan.** Every
Groq row in this document assumes Developer-plan rates and limits; until that
is done they describe a service Syllabus is not on. `npm run split` reports
the effective rate per month and is how this gets noticed rather than
discovered on a statement. (Note also that providers.py registers the BYO
Groq entries with a 100MB request cap, which is a Developer-plan number; a BYO
user on a free Groq key gets 25MB.)

**Transcription moved to Groq on 2026-09-19** (syllabus-accounts, /proxy
transcribes on Groq and falls back to OpenAI). It was $0.180 an hour on
`gpt-4o-mini-transcribe` and is $0.111 on `groq/whisper-large-v3`, which is
38% off the largest single line in the product. The fallback exists because
this service spends one key for everybody, so a Groq rate limit would be a
ceiling on the whole product; the number above is therefore the good case,
and a month where Groq is throttled costs more.

**Turbo is the other 64%, and it is not taken.** `whisper-large-v3-turbo` is
$0.04 an hour, which would put the audio hour at ~$0.071 and budget at $0.08.
Groq's own figures put it at 12% WER against large-v3's 10.3%. That is a
quality call on a real lecture, not a spreadsheet call, and it belongs to the
provider benchmark below. Both models are registered in providers.py so the
benchmark can weigh them; switching the managed path is one constant in
proxy.ts.

**Study assistant.** Rates are $5/$25 per M on Opus 5 and $2/$10 on Sonnet 5.
Caching is what dominates: a **5-minute** cache writes at 1.25x the input rate
and a 1-hour one at 2x, and a cache READ refreshes the TTL. A study session is
continuous by nature, so the 5-minute TTL stays warm for the whole of it at
62% of the write cost. This document originally costed the 1-hour write, which
was $0.90 a session more than it needed to be, on Opus, for no behavior change.

One session = one cache write plus eight follow-ups at ~800 output tokens.

| Session shape | Opus 5 | Sonnet 5 |
|---|---|---|
| Summaries only, ~15k tokens | $0.31 | $0.13 |
| Full course, ~240k tokens | $2.62 | $1.05 |
| **Blended at 15% escalation** | **$0.66** | **$0.26** |
| 15 sessions (a Pro month) | $9.90 | $3.96 |

**Two-stage context is still the first lever and it is already assumed above.**
Load summaries only by default; pull full transcripts for one named course
only when the question needs them or the user asks for a study guide. The 15%
escalation rate is the one number here nobody has measured. It is also the
most sensitive: at 50% escalation the Sonnet month is $8.82, not $3.96.
Instrument it in P7 from the first day, because the tier table moves with it.

Decide this before building V3, not after.

---

## Tiers

Stripe takes 2.9% + $0.30. Audio is $0.151 an hour (Groq large-v3 plus the
Sonnet summary, with retry slack); Pro's assistant line is 15 sessions.

**Where it stands today, on what is actually deployed:**

| Tier | Audio | COGS | Stripe | Net | Margin |
|---|---|---|---|---|---|
| Starter $9 | 15 hr | $2.27 | $0.56 | **$6.17** | 69% |
| Standard $15 | 30 hr | $4.53 | $0.73 | **$9.74** | 65% |
| **Pro $25 (the published price)** | 45 hr | $16.70 | $1.03 | **$7.27** | **29%** |
| Pro $29 (for reference) | 45 hr | $16.70 | $1.14 | $11.16 | 38% |

Groq fixed the audio lines. Starter and Standard are now nearly pure margin
and **Pro is almost entirely an assistant bill**: $16.70 of COGS, of which
$9.90 is the assistant and $6.75 is audio.

**Read the Pro row as the gap to close, not as the plan.** $25 is published
and the assistant it was priced on is not built. On today's deployed stack,
which would run the assistant on Opus 5, $25 is a 29% margin. Sonnet 5 is what
takes it to 53%. Nothing is being sold until October 12, so the price and the
assistant have that long to meet.

**With the assistant on Sonnet 5:**

| Tier | Audio | COGS | Stripe | Net | Margin |
|---|---|---|---|---|---|
| Starter $9 | 15 hr | $2.27 | $0.56 | **$6.17** | 69% |
| **Standard $15** | 30 hr | $4.53 | $0.73 | **$9.74** | 65% |
| **Pro $25 (decided 2026-09-19)** | 45 hr | $10.75 | $1.03 | **$13.22** | **53%** |

**This is what settled the $29 question.** Pro at **$22** nets $10.31, which
beats the $8.56 that $29 was chosen to defend, with the full 45 hours and 15
sessions intact. The original alternative, $22 with 35 hours and 8 sessions,
netted $6.60. Sonnet buys back the allowances the price cut would have cost.

### Pro at $25: DECIDED 2026-09-19

Published on maincoursemedia.com/syllabus the same day (main-course-media
PR #179). Starter and Standard are unchanged. The reasoning, on the Sonnet
assistant:

| Pro price | COGS | Stripe | Net | Margin | Extra Pro users needed to match $29 |
|---|---|---|---|---|---|
| $22 | $10.75 | $0.94 | $10.31 | 47% | 66% more |
| **$25** | $10.75 | $1.03 | **$13.22** | **53%** | **29% more** |
| $29 | $10.75 | $1.14 | $17.11 | 59% | n/a |

The last column is the only one that decides anything. A lower price is worth
taking when it converts enough extra subscribers to cover the net it gives
up. $25 needs 29% more Pro subscribers than $29 to come out level; $22 needs
66%. A 29% lift from a $4 cut on a student product is plausible. A 66% lift
from a $7 cut is a much bigger claim.

**$25 also survives being wrong about the escalation rate, and $22 does not.**
That rate is the softest number in this document, so the right test of a price
is what it does when the guess is bad:

| Escalation | Assistant/mo | Pro $22 | Pro $25 | Pro $29 |
|---|---|---|---|---|
| 10% | $3.27 | $11.00 (50%) | $13.91 (56%) | $17.80 (61%) |
| **15% (modeled)** | $3.96 | $10.31 (47%) | $13.22 (53%) | $17.11 (59%) |
| 25% | $5.34 | $8.92 (41%) | $11.83 (47%) | $15.72 (54%) |
| 50% | $8.80 | $5.46 (25%) | $8.37 (33%) | $12.26 (42%) |
| 75% | $12.26 | $2.00 (9%) | $4.91 (20%) | $8.80 (30%) |

At 50% escalation, which is not a crazy outcome for a product whose whole
pitch is studying from your own lectures, $22 falls to a 25% margin and $25
holds 33%. **$25 is the price that does not need the model to be right.**

**The condition on this price: it assumes the Sonnet assistant.** At $25 with
an Opus assistant the row is $7.27 and 29%, which is the thin margin this
document was written to avoid. The price is published and the assistant is not
built, so the two have to meet: P7 ships on Sonnet 5, or $25 gets revisited
before billing goes live. Nothing is being sold yet, which is the whole of the
slack available here.

**And with Groq turbo as well:**

| Tier | Audio | COGS | Stripe | Net | Margin |
|---|---|---|---|---|---|
| Starter $9 | 15 hr | $1.12 | $0.56 | **$7.31** | 81% |
| Standard $15 | 30 hr | $2.25 | $0.73 | **$12.02** | 80% |
| Pro $29 | 45 hr | $7.33 | $1.14 | **$20.52** | 71% |
| Pro $22 | 45 hr | $7.33 | $0.94 | **$13.72** | 62% |

---

## Profit potential

Blended net per paid user per month, at a tier mix of **50% Starter, 35%
Standard, 15% Pro at $29**. Students skew cheap; if the mix skews richer these
numbers rise.

| Scenario (Pro at $25) | Net / user / mo | 100 users | 250 | 500 | 1,000 |
|---|---|---|---|---|---|
| Today (Groq + Opus assistant) | $7.59 | $759/mo | $1,897 | $3,794 | $7,588 |
| **+ Sonnet assistant** | **$8.48** | **$848/mo** | **$2,120** | **$4,239** | **$8,479** |
| + Sonnet and Groq turbo | $10.36 | $1,036/mo | $2,589 | $5,179 | $10,359 |

Annualized at 1,000 paid users: **$91k / $102k / $124k**.

The $4 cut costs about $7k a year at 1,000 users against holding $29, before
any conversion lift. The lift needed to erase that is 29% more Pro
subscribers, which is the bet.

**Read these as contribution margin, not profit.** They are revenue minus
per-user variable cost and card fees, and nothing else. Not subtracted:
Cloudflare and D1 (small but real), the deferred $99/year Apple signing,
insurance, the $1.5k to $4k lawyer, support time, refunds, failed payments,
churn, or any of Trace's hours. The founding cohort is free, so the 25-user
row is $0 until the paid launch on October 12.

**Two things make these conservative, and one makes them optimistic.**

Conservative: every COGS figure above assumes a user consumes their **entire**
allowance every month. Most will not. A Standard user who records 12 of their
30 hours costs $1.81 rather than $4.53, so the real blended net sits above
these rows. What is modeled here is the cap, which is the right number for
"can one heavy user hurt us" and the wrong one for "what will a month look
like".

Optimistic: the 15% escalation rate in the assistant model is a guess, and Pro
is the tier it moves. At 50% escalation the Sonnet Pro row nets $8.37 rather
than $13.22. $25 still works there, which is why it was chosen over $22.

Also optimistic: every Groq figure assumes the Developer plan. The account is
on the free plan today. See Unit economics.

**What is measured and what is modeled.** Measured: every published token and
audio rate, and the $0.18 OpenAI hour from the 2026-09-15 benchmark. Modeled:
the session shape (15k/240k context, eight follow-ups, 800 output tokens), the
15% escalation rate, the 50/35/15 tier mix, and full allowance consumption.
The modeled numbers have never met a real user.

Hour caps map to courses so students can reason about them: roughly 2 courses
at Starter, 4 at Standard, all of them at Pro.

**Mechanics worth getting right up front**

- A 5-hour trial is not a Stripe concept. Grant 5 hours as an entitlement at
  signup with the card collected, set a long `trial_period_days`, and end the
  trial early through the API when the hours are consumed.
- The friends-and-family code is a promotion code at `percent_off: 100`,
  `duration: forever`, with Checkout set to `payment_method_collection:
  'if_required'` so it does not ask for a card.
- Cap behavior is a hard stop with a one-click top-up. No surprise bills for
  students, ever.

---

## Critical path

### Stage 1, to October 13

**P0. Apple and the app** (6 h, plus Apple's own clock, already running)
- Complete organization enrollment.
- Developer ID signing and notarization in `packaging/build.sh` and the
  release workflow.
- Tag v0.2.0 and run the release workflow end to end. It has never run.
- Wire Sparkle self-update.

**P1. Legal and consent** (8 h)
- Syllabus privacy policy and terms, hosted on the account service, naming
  Main Course Media LLC and Texas governing law.
- Point the Google Cloud project's privacy policy URL at the new page. It
  currently points at the agency policy, which never mentions Syllabus. That
  is a live re-review risk on a published project.
- Blocking consent acknowledgment at setup: "I have permission to record."
- Self-serve account deletion on the account page.
- An explicit "we do not train on your data" statement, with the provider
  settings confirmed to back it up.
- 18+ in the ToS.
- Lawyer review of all of the above.

**P2. Security bar** (12 h): see the definition below.

**P3. Cohort readiness** (6 h)
- Opt-in crash reporting and log upload.
- Self-hosted activation analytics on D1. No third-party pixel.
- The invite and onboarding path for 25 people.

### Stage 2, to October 12

**P4. Transcriber provider seam**. DONE 2026-09-15 (LectureAI #50)
- A `TranscriptionProvider` protocol carrying `max_bytes`,
  `max_chunk_seconds`, and `transcribe(path) -> str`, so the chunking rules
  travel with the model instead of living in `config.CHUNK_SECONDS`.
- Benchmark OpenAI, Deepgram, and Groq on a real lecture for cost and word
  error rate. This sets the tier caps and it is where diarization gets decided
  later, since Deepgram and AssemblyAI give it natively.

**P5. Managed keys and metering**. DONE 2026-09-15 (syllabus-accounts #12,
the proxy; LectureAI #52, the panel calling it). A signed-in Mac now holds no
API key at all and the Setup page says so. Audio and transcripts stream
through and are not stored; a usage row is all that persists. Verified live:
121 audio-seconds and 14,883 summary tokens metered against a real account.
Two bugs the live run found are fixed: a 15 s client timeout that lost a paid
summary, and an empty tool_use passing as a valid summary (#53, #14).
- A proxy endpoint on the Worker. Keys live only there and are never shipped
  to a Mac.
- Audio deleted immediately after transcription. A usage row and a byte count
  are all that persist.
- Per-account monthly usage, enforced server-side.

**P6. Stripe and entitlements** (14 h)
- `subscriptions` table on D1, Checkout, Billing Portal, webhook with
  signature verification and idempotency on event id.
- Stripe Tax on from day one.
- One entitlement check, enforced in the proxy. The panel UI may hide and dim;
  it must never be the enforcement point, because it runs on the user's Mac.

**P7. V3 study assistant** (20 h)
- Two-stage context, per the economics section. Build this first, not as an
  optimization later.
- ~~Opus 5~~ **Sonnet 5**, cached documents with citations, streaming.
- "Generate a study guide" and "Quiz me" as buttons. They are the demo.
- Hard session cap wired to the entitlement.

**PARTLY SHIPPED 2026-09-19**, as the single-user BYO-key version: the panel's
assistant runs against the lectures in Drive on this Mac's own Anthropic key,
on Sonnet 5, with the two-stage context, cached cited documents, streaming, and
the starter buttons. What is NOT done, and is still P7's remaining work:

- **The proxy path.** This calls Anthropic directly and bypasses the account
  service entirely, so it works for one signed-in Mac and for nobody else.
  Shipping it to accounts means a `/proxy/assistant` endpoint metered the way
  `/proxy/summarize` is.
- **The session cap wired to the entitlement.** There is no entitlement to wire
  to until P6, so the guard today is a fixed ceiling on one escalation (six
  transcripts, and a size limit above that), not an allowance.
- **Escalation is now instrumented**, which was the day-one requirement: every
  session writes to `assistant.log` with whether it escalated and what it cost.
  The 15% figure the tiers are priced on can now be replaced with a measurement
  instead of remaining a guess.

**P8. Launch** (2 h)
- Pricing page, cohort conversion, announcement.

---

## The security bar

"Passes 100%" needs a definition it can actually be measured against. This is
the proposed bar; it is a checklist you sign off, not a feeling.

1. `/security-review` clean on every PR into main from here forward.
   CI now exists in both repos (syllabus-accounts #15, LectureAI #55): tests,
   typecheck, the copy standards, and a cross-repo prompt-parity check. Note
   that NEITHER repo has branch protection, so every check reports and none of
   them blocks a merge. Turning on required checks is a settings change and is
   what would make any of this binding.
2. A written threat model covering: device token theft, relay hijack, the
   proxy as a key-exfiltration target, D1 row access across accounts, and the
   OAuth callback surface.
3. **Rate limiting on every unauthenticated and semi-authenticated endpoint.**
   There is none today on `/login`, `/device/start`, `/device/poll`,
   `/panel/exchange`, or `/relay/connect`. Cloudflare rate-limiting rules at
   minimum. This is the cheapest fix on the list and the most overdue.
4. A manual pass against OWASP ASVS Level 1, plus the Syllabus-specific
   surface: device tokens, the relay Durable Object, and the Drive grant.
5. Confirm `SameSite=Lax` is genuinely sufficient on the state-changing POSTs
   (`/device/approve`, `/drive/disconnect`), or add CSRF tokens.
6. A documented `DRIVE_KEY` rotation procedure.
7. Zero unresolved high or critical findings. Mediums triaged in writing.

Already good and worth not regressing: device tokens stored as SHA-256 only,
the Drive refresh token AES-GCM encrypted and never sent to a panel, one-time
codes with expiry and single redemption, the relay pinned to the owning
account with a path allowlist and size caps.

---

## Still open

- **Insurance.** The broker call. Blocks nothing technically; blocks launch
  judgment.
- **Lawyer.** Booked when? $1.5k to $4k budgeted.
- ~~**Pro at $29 or $22.**~~ CLOSED 2026-09-19: **$25**, live on the site
  (main-course-media PR #179). Nets $13.22 at 53% on the Sonnet assistant, and
  still holds 33% if the escalation rate turns out to be 50% rather than the
  modeled 15%, which is what ruled out $22. Full allowances kept: 45 hours and
  15 sessions. What remains is not the price, it is the assistant the price
  assumes.
- **Two-stage assistant context.** Needs a call before P7, and it is assumed
  by every Tiers row above.
- ~~**The assistant's model: Sonnet 5 rather than Opus 5.**~~ CLOSED
  2026-09-19: **Sonnet 5**, taken before P7 was written rather than after, as
  this entry asked. It is set in one place, `config.ASSISTANT_MODEL`, and a
  test holds it there, because moving it to Opus is the difference between
  Pro's 53% margin and its 29% one. The eval this entry wanted is now cheap to
  run: the assistant is answering from real course material, so the question
  is no longer hypothetical.
- **The escalation rate.** What share of study sessions need the full-course
  context rather than summaries. Modeled at 15%, and Pro's margin moves with
  it. **Instrumented 2026-09-19**, on day one of P7 as this entry asked:
  `assistant.log` records every session and `assistant.escalation_rate()`
  reads the rate off it. Still unmeasured in the sense that matters, because
  a handful of sessions is not a sample. It needs real study weeks behind it
  before the tier table is rebuilt on it.
- **Groq. Unparked and shipped 2026-09-19**, on `whisper-large-v3` at $0.111
  an hour with OpenAI as the fallback leg. The fallback is what let this go
  ahead without waiting on the two unknowns: a rate limit or a bad key now
  costs money instead of costing transcriptions, so neither can take the
  product down. Two things still need doing from inside a Groq account, and
  neither blocks the deploy:
  - **Create the account and set the secret.** `wrangler secret put
    GROQ_API_KEY`. Until it is set the proxy runs on OpenAI exactly as before,
    so the code is live and the saving is not.
  - **RESOLVED 2026-09-19, badly: the account is on the FREE plan.** Groq never
    asked for a card and the Developer limits are not visible, which is what
    being on free looks like. 28,800 audio seconds a day across every account
    together is 8 hours of lecture, total, and 20 requests a minute is two
    simultaneous uploads. Adding a card to reach the Developer plan is the
    open item; see Unit economics. `npm run split` is how the damage is
    measured in the meantime.
  - **Turbo** is a separate call, above. Data policy was good and is unchanged:
    no training on inputs, no retention by default, self-serve ZDR.
- **The trial allowance on Trace's own account is nearly spent.** 109k of 150k
  summary tokens went on benchmarking and live verification 2026-09-15, leaving
  roughly two full-lecture summaries before the proxy returns 402. It is a row
  in the D1 `allowances` table.
- ~~**Provider benchmark results.**~~ Done 2026-09-15, slice 1 (PR #50).
  `gpt-4o-mini-transcribe` measured **$0.18 per audio hour**. **Superseded
  2026-09-19**: transcription moved to Groq `whisper-large-v3` at $0.111/hr,
  so the per-hour total is now ~$0.14 and the tier caps were rebuilt. What
  that benchmark did NOT do is compare transcript quality against Groq, which
  is exactly the question turbo now turns on, so it owes a second pass on a
  real lecture: large-v3 against turbo, WER measured here rather than taken
  from Groq's figures. Deepgram Nova-3 is $0.258/hr and the only
  one of the three with **native diarization, included at no extra charge**,
  so the cut diarization item is a Deepgram item when it comes back, not an
  OpenAI one. Accuracy was not measured for Groq or Deepgram: no keys, and
  signing up was out of scope.
