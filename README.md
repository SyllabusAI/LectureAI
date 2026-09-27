# LectureAI

Record a lecture on your Mac, or drop one in the inbox from your phone. Get a
study summary as a Google Doc in the right course folder in Drive, with the raw
transcript filed one level down.

The same pipeline now runs under two names. **Syllabus** is LectureAI: lectures,
a class schedule, study notes. **Sous** records client Zoom calls for the
account team: call notes instead of study notes, a "Client Calls" folder in
Drive, to-dos filed as database rows. Each is a *profile*: its own home under
`~/.intake`, its own keys, token, inbox, and log, so the two can never share a
recording or a credential. Choose one with the `syllabus` or `sous` command,
with `intake --profile sous`, or with `INTAKE_PROFILE=sous`; with nothing
said, `intake` is Syllabus. Everything below is written for Syllabus and holds
for Sous with the paths and names swapped.

Everything in between is automatic: the course is inferred from your class
schedule, the audio is compressed and split to clear the transcription API's
size limit, Claude writes the summary, and the original recording is deleted
once both uploads land.

The control panel is reachable from any browser or phone. Every Mac signed
in to a Syllabus account has an address on the account service,
`syllabusaccounts.maincoursemedia.com/p/<device>/`, that only the account's
owner can open, with nothing to install or configure; the panel still runs
on the Mac that does the recording, and `intake service install` is what
keeps it running there without a terminal. See "The panel on the web"
below. This repository is public so that the install line below can fetch
from it.

## How a recording flows through

```
intake panel        a local control panel over everything below
   |
   v
intake record       records from this Mac's mic into .work/, then moves the
   |                finished file into inbox/ (or sync one from your phone)
   v
inbox/lecture.m4a
   |
   |  intake watch      waits for the file to stop growing, then infers
   |                    the course and date from the recording's START time
   |                    (mtime minus duration), falling back to the filename
   v
   |  transcribe.py     compresses and splits to the limits the chosen
   |                    provider carries (providers.py), transcribes each
   |                    chunk, stitches them in order
   v
   |  summarize.py      one Claude call, response constrained to a schema:
   |                    summary, key terms, action items, topic slug
   v
   |  upload.py         ACCT-4321/ACCT-4321_2026-09-03_Job-Order-Costing  (Doc)
   |                    ACCT-4321/Transcripts/..._Job-Order-Costing.txt
   v
   |  notion_tasks      action items become dated tasks in your Notion to-do
   |                    list (skipped entirely unless Notion is set up)
   v
pipeline.log        one line per lecture: timestamp, course, source file, URL
```

Everything the pipeline reads or writes lives in one place, the profile's
home: `~/.intake/syllabus/` for Syllabus, `~/.intake/sous/` for Sous. Each
holds that profile's keys in `.env`, its schedule file (`schedule.toml` for
Syllabus, `calls.toml` for Sous), the `inbox/` and `processed/` folders, the
Drive token, the Syllabus account this Mac belongs to (`account.json`, if it
has been signed in), the classes that did not meet (`canceled.json`, if any
have been marked), and `pipeline.log`. Set `INTAKE_HOME` to put the whole `~/.intake`
root somewhere else (`LECTUREAI_HOME`, the variable's old name, still works
when the new one is unset); the profile folders move with it. The code never
keeps anything next to itself.

Local copies are staged in `processed/` before upload and removed after, so a
failed upload never costs you the transcription you already paid for.

## Install

**The Mac app.** Download the newest `Syllabus-x.y.z.dmg` from the
[releases page](https://github.com/SyllabusAI/LectureAI/releases), open it, and
drag Syllabus to Applications. Open Syllabus and it lands on its Setup page:
sign in to a Syllabus account, pick a microphone, enter your class schedule,
and connect Google Drive. A signed-in Mac holds no API key of its own; without
an account, paste one key from OpenAI and one from Anthropic instead. It runs
from the menu bar after that; nothing else to install. Apple silicon, macOS 13
or newer.

Until the app is signed with an Apple Developer ID, the first time you open
it macOS says it could not verify the app and offers only Done or Move to
Trash. Click Done, open System Settings, choose Privacy & Security, scroll to
the Security section, click Open Anyway, and confirm. Once per version.

When a newer version is out, the panel and the menu bar say so with a link
to the download. Quit Syllabus, replace the copy in Applications, open it
again; everything you set up stays.

**From Terminal instead.** The same program without the window, for anyone
who prefers a command line:

## Setup

You need a Mac and Homebrew. Sign in to a Syllabus account and that is all:
transcription and summaries are then billed to the account, and no API key
goes on your Mac at all. Without an account you can still run it on two keys
of your own, one from OpenAI and one from Anthropic. Three commands in
Terminal, then the rest happens in your browser:

```bash
brew install ffmpeg pipx
```

```bash
pipx install git+https://github.com/SyllabusAI/LectureAI
```

```bash
intake panel
```

That opens the control panel in your browser. The first time, with nothing
configured, it lands on the **Setup** page: sign in to a Syllabus account (or
paste two keys of your own), pick a microphone from the list, add a row for
every time a class meets, tick Notion if you want it, and click **Connect
Google Drive**, which opens a Google sign-in tab. Signed in, step 1 says there
is nothing to do and means it. A checkup at the bottom of the page shows what
still needs doing and turns green as you go. Everything is saved on your Mac
in `~/.intake/syllabus`; nothing is sent anywhere but to the account service
and the services it spends on.

Come back to the Setup page any time from the gear in the panel's header.
Saved keys are shown masked and a blank field keeps what is there, so
changing one thing never means retyping the rest.

The same setup works from Terminal if you prefer:

```bash
intake setup
```

It asks for the two keys, shows the microphones ffmpeg can see, takes your
class schedule one line at a time (`Tue 14 ACCT-4321`, blank line when done),
asks once about Notion, asks once whether you have permission to record,
and offers to authorize Google Drive at the end.

Nothing records until that permission is confirmed, once per profile: the
Setup page has a required box for it, the dashboard asks beside the record
button, and `intake setup --consent` asks only that question. The answer is
kept in `consent.json` in the profile's home with when it was given and by
which version. The check sits in `Recorder.start` (`intake/consent.py`), so
the CLI, the record button, the menu bar item and the panel on the web all
go through it; delete the file to be asked again.

Either way, `intake doctor` is the check to run when something misbehaves.
It prints one line per thing that has to be right, with the exact fix next to
anything that is not, and it never touches the network. The Setup page's
checkup is the same list.

```
ok    Python               3.12.14
ok    ffmpeg               /opt/homebrew/bin/ffmpeg
ok    home directory       /Users/you/.intake (default)
ok    OpenAI key           sk-proj...Xk2A
ok    Anthropic key        sk-ant-...9fQ1
ok    class schedule       8 class meetings, ACCT-4321, ENTR-3306, ENTR-4306, RELI-3304, tolerance 45 min
ok    Drive OAuth client   bundled with the package (Cloud project friendly-bazaar-507320-b7)
FAIL  Drive authorization  no token.json yet
                           fix: intake login
ok    Notion               skipped in setup (optional)
ok    microphone           3 inputs, will record from MacBook Pro Microphone
```

For Drive, the Google OAuth client ships with the package, so there is no
Cloud project to create. If setup did not already do it:

```bash
intake login
```

That caches `token.json` in `~/.intake/syllabus`. The app uses the `drive.file`
scope, which only reaches files it created, so it makes its own "Lecture
Notes" folder in My Drive rather than writing into one you made by hand. Move
that folder anywhere afterward; access follows it.

If `pipx` says the command is not on your PATH, run `pipx ensurepath` and open
a new terminal. To update later: `pipx upgrade intake`.

If you installed when the command was still called `lectureai`, pipx knows the
package by that name and `upgrade` will not carry it across the rename. Run
`pipx uninstall lectureai`, then the install line above. The old command keeps
working as a second name for `intake` for now, and your data is picked up as
described under "You installed when this was called lectureai" below.

## The control panel

```bash
intake panel
```

It opens <http://127.0.0.1:5173> in your browser (add `--no-browser` to skip
that). The Sous panel is <http://127.0.0.1:5174>, so both can be open at once;
`--port` overrides either. One page to start and stop a recording,
start and stop the watcher, see what's waiting in the inbox, and open recent
lectures in Drive. It drives the same modules the CLI does, so a recording
started here is identical to one started with `intake record`.

The page is a dashboard, drawn from `pipeline.log` and the schedule, so a
line removed from the log disappears from it on the next poll. The one
thing it stores of its own is the list of classes that did not meet, below:

- **This week** is the schedule as a grid, one column per day, one chip per
  class. A chip is filled once a recording for that class is filed (click
  it to open the Doc), ringed while the class is in session, dashed once
  the class has ended with nothing filed. The lecture's date comes from the
  filed name (`ACCT-4321_2026-09-10_...`), not from when it was processed,
  so a recording synced from a phone days later still lands on the right
  day. Hovering a chip that has no recording shows a **Didn't meet**
  button: the professor was out, the campus closed, an exam took the hour.
  The chip is struck through, that class stops counting as missed, and the
  streak runs straight through it. **Undo** puts it back. What was marked
  is kept in `canceled.json` beside the schedule, one row per class
  meeting; deleting the file puts every meeting back on the books.
- **Pipeline**, right under the record button so it is never below the
  fold: the watcher as a switch, the five stages as a stepper that lights
  up while a lecture is being processed, and what is waiting in the inbox.
- **Four tiles** beside it, two by two: recorded this week against the
  classes that have met so far, lectures filed in all, the streak of
  consecutive classes recorded (classes that did not meet are skipped, and
  the tile says how many), and hours of audio filed. That last one is
  counted from a seventh field the watcher now writes to `pipeline.log`
  (seconds of audio, transcript words, to-dos and key terms, as JSON), so
  it reads as a dash until the next lecture is processed; the older lines
  have nothing to measure.
- **Lectures per week**, stacked by course for the last eight weeks, with a
  hairline per week at what the schedule expected, less any class that did
  not meet; hover a week for the
  breakdown. **Recent lectures** beside it, matched to the chart's height
  and scrolling inside the card. Clicking a course in the chart's legend,
  or in the chips over the recent list, filters the list to that course.
- **Study assistant** at the bottom is reserved space for v3: a chat that
  answers questions and builds study guides from every transcript and
  summary in Drive. Nothing in it is wired up yet, and it says so.

Each course has a color, assigned in schedule order so it does not change
from week to week. The six colors were run through a colorblind-safety
check against both card surfaces, and identity never rests on color alone:
the code is always printed beside the mark.

While a lecture is being processed the panel shows the stage it is in:
waiting for the file to finish copying, transcribing (with the part number, so
a ten-part lecture visibly advances), summarizing, uploading, adding tasks to
Notion. A 75 minute recording takes several minutes and ten API calls, and
without this it just sits in the inbox looking untouched, because
`pipeline.log` gets its line only once the whole thing is done.

The watcher writes that stage to `.work/status.json` and stamps it with its
own pid; the panel ignores a status whose pid is not the watcher currently
running, so a watcher killed mid-lecture cannot leave a stage on screen
forever.

The theme buttons in the top right switch between **system, light, and
dark**. System follows macOS; picking light or dark overrides it and is
remembered in that browser. Every color is defined once with CSS
`light-dark()`, so the two themes cannot drift apart the way a duplicated
palette does.

It binds to localhost only, and deliberately: it can start and stop processes
and read your pipeline log, none of which belongs on the network. The setup
row at the bottom reports which pieces are configured by presence alone, so no
key or token is ever sent to the browser.

`--host` can point it at another address, but only together with `--expose`,
and without that flag the panel refuses and says why: it has no login of its
own, so anyone who can reach the port can start processes and rewrite the
keys in `.env`. Reaching it from elsewhere is done differently, and without
that flag: the panel keeps listening on localhost and holds a connection out
to the account service, which puts your account's sign-in in front and
carries your requests down that connection. That is "The panel on the web"
below.

## The study assistant

The panel's assistant answers questions about the lectures you have already
filed, one course at a time. Ask it in plain words, or press one of the
starters: build me a study guide, quiz me on this week's key terms, what is
due next week.

It reads in two stages, and the second one is why it can quote a class back
to you:

1. Every **summary** filed for the course, handed over as documents Claude can
   cite. A summary is a couple of pages, so a whole term of them is still
   small, and after the first question they are served from cache: a follow-up
   costs a fraction of what the first question cost.
2. The **full transcript** of particular lectures, fetched only when the model
   asks for them by name and says why. That happens when you want something a
   summary does not carry, the exact wording of a definition, an example worked
   on the board, what the instructor said would be on the exam. The answer says
   when it went and read one.

Answers carry the lecture each point came from, and the assistant says so when
your lectures do not cover what you asked rather than filling the gap from
general knowledge.

It runs on Sonnet 5 and on this Mac's own `ANTHROPIC_API_KEY`, the one in
`.env`, even when the Mac is signed in to a Syllabus account and everything
else goes through the account service. There is no metering on this path yet,
so the guard is a cap: at most six transcripts in one escalation, and a size
ceiling above that. Nothing new is asked of your Google account, because it
reads only the files the pipeline itself created.

Every session appends a line to `assistant.log` in the profile's home: the
course, whether it escalated to transcripts, and what it cost in tokens. That
file is the measured escalation rate, which is the number the paid tiers are
priced on and which had only ever been guessed at.

## Keeping the panel running

```bash
intake service install
```

Installs the panel as a launchd agent for your login: it starts now, starts
again at every login, and is restarted if it dies. It no longer belongs to
any terminal window, so closing one, or the app you launched it from, no
longer takes the panel down. Its output goes to `panel.log` in the profile's
home. `intake service status`, `restart` (after updating the code or
changing `.env`), and `uninstall` do what they say. Each profile has its
own agent (`sous service install` for the other one).

Only the panel is kept alive. Starting the watcher stays a click in the
panel, because the watcher spends API credit and writes to Drive and Notion.

The first recording after installing asks for microphone permission on
behalf of Python, the program the agent runs. Grant it once under System
Settings > Privacy & Security > Microphone and it sticks. The red timer
described above is what you see if it was refused.

## A Syllabus account

The Setup page ends with an optional step: **Sign in to a Syllabus account**.
An account is a Google identity kept by a small web service at
`syllabusaccounts.maincoursemedia.com` (its code is the
[syllabus-accounts](https://github.com/SyllabusAI/syllabus-accounts) repo, a
Cloudflare Worker with a D1 database). Signing in ties this Mac's panel to
that identity. It is what lets the panel on the web sign you in ("The panel
on the web" below), and it is where your class schedule and your Google
Drive connection live once you connect them, so both follow you to another
Mac.

Signing in works the way a TV signs in to a streaming service. The panel
shows an eight-letter code and opens the account page in a new tab. Sign in
there with Google, enter the code, and the panel notices within a few
seconds. What it receives is a token that says "this Mac, this profile,
this account", saved as `account.json` in the profile's home with the same
permissions as your keys. The token is never sent to the browser, and your
Google session never reaches the panel. The account page lists every Mac on
the account and can remove one; the next time that Mac's Setup page loads,
it shows as signed out. **Sign out** on the Setup page does the same from
this end.

Once a Mac is signed in, **its class schedule belongs to the account too**.
Saving the Setup page pushes `schedule.toml` to the account, and starting
the panel pulls the account's copy when it is newer, so a second Mac signed
in to the same account gets the schedule without retyping it. The file on
this Mac stays what the pipeline reads; the account holds the text as
written, comments and all, with a version. When both sides changed since
they last agreed, the newer copy wins and the other is kept next to the
schedule as `schedule.toml.<stamp>.bak`. A copy from the account is parsed
before it replaces the file, so a broken schedule there cannot break this
Mac. The Setup page's account step says what the last sync did and when;
`.work/sync.json` remembers the version this Mac last agreed with.

**Google Drive can belong to the account as well.** On the Setup page,
with this Mac signed in, **Connect Google Drive through your account** opens
the account page, where Google's consent runs once for the `drive.file`
scope. The refresh token stays on the account service, encrypted; each Mac
asks it for an hour-long access token when a lecture is ready to upload.
Every Mac signed in to the account then files to the same Drive, and none of
them needs `intake login`. The uploader prefers the account's grant whenever
there is one and falls back to this Mac's own `token.json` otherwise, so a
Mac that already had Drive connected keeps working the day it signs in.
The account service's Web client lives in the same Google Cloud project as
the bundled Desktop client on purpose: `drive.file` only reaches files made
by the same project, so this is what lets the account's grant see the
"Lecture Notes" folder that `intake login` created. As a safety net, the
uploader still checks that the grant can see this Mac's existing folder
and keeps using this Mac's own token when it cannot, and `intake doctor`
says so. `DRIVE_SOURCE=account` in `.env` skips that check and files under
the account regardless; `DRIVE_SOURCE=local` never uses the grant. If
the account service cannot be reached, the local token is used when there
is one; without one the upload fails with a message saying so and the
recording waits in the inbox for the retry. Disconnecting on the account
page revokes the grant at Google for every Mac at once.

Nothing requires an account. A panel with no `account.json` is exactly what
it was, and `intake doctor` reports the account as an optional line. To hide
the step altogether, put `ACCOUNTS_URL=off` in `.env`; to test against a
service of your own, point `ACCOUNTS_URL` at it. The dashboard's status poll
reads only the file, never the network; the Setup page asks the service to
confirm the token each time it loads.

## The panel on the web

Every Mac signed in to a Syllabus account has an address:

```
https://syllabusaccounts.maincoursemedia.com/p/<device>/
```

The Setup page shows yours under the account step, with a dot that says
whether the panel is connected right now, and `intake doctor` has a "panel
on the web" line with the same. Open the address from any browser or phone,
sign in with the Google account your Mac is signed in to, and you are
looking at the panel on that Mac: the same page, the same Record button,
the same Setup page. Nothing to install beyond Syllabus, no port to open,
no hostname or tunnel to set up, no Cloudflare account.

How it works: the panel opens a WebSocket to the account service the moment
it starts with an `account.json`, keeps it open from a thread inside the
panel process (`intake/relay.py`), and reconnects on its own if it drops.
The service holds the other end and sends each browser request down it as a
small message; the panel runs the request in process and sends the answer
back up. Only what the panel serves is carried: its two pages, its API, and
its two images. Recordings, transcripts, and keys never travel this way,
because the panel never serves them.

Who may open the address is the account service's decision: the account
that owns the Mac, and nobody else. A different account is told "That
Syllabus belongs to someone else." The sign-in is the service's own Google
sign-in, so there is no login on the panel for this road. The service names
the viewer in every request it relays, and the panel believes it because the
request arrived on the socket the panel itself opened with its own device
token; a request from the local network cannot carry that mark. The header
shows who is signed in, with a sign-out link that goes to the account
service.

When the Mac is asleep, offline, or its panel is not running, there is no
socket, and the address shows a page saying so, with the Mac's name and when
it was last connected, trying again every 10 seconds. A panel that is up
but does not answer within 25 seconds gets the same treatment: the service
closes the socket as dead and the panel reconnects. Through the relay the
page asks for its status every three seconds instead of every second, and
not at all while the tab is hidden, since every request crosses the
service. A closed lid drops the socket, and the next wake reconnects it;
the panel's log (`panel.log` in the profile's home) has a line for each.

Signing this Mac out of the account closes the socket and the address stops
working; signing in again starts it. A second person's Syllabus is their own
Mac, signed in to their own account, at their own address. A panel with no
account never starts the socket and is exactly the local tool it always
was.

If the address stops working, check the pieces in order: `intake service
status` (is the panel up), then `intake doctor`, whose "panel on the web"
line says whether the panel was connected the last time it said anything,
and if not, why. "Not connected" on the address itself means exactly that:
the Mac is asleep or offline, or the panel is not running. "The account
service refused this Mac's token" means the Mac was removed from the account
page; sign in again from the Setup page.

`maincoursemedia.com/syllabus` now sends anyone to the newest release, and
`maincoursemedia.com/syllabus/panel` is the short way to this author's
address.

### The older road: a Cloudflare Tunnel, retired 2026-09-15

Before the relay, the only panel on the web was this author's, at
`syllabus.maincoursemedia.com`, through a Cloudflare Tunnel (`cloudflared`,
run as its own launch agent) to `127.0.0.1:5173`. Anyone arriving that way
signed in at the panel itself, which sent them to the account service and
traded the code it sent back for the account. The tunnel, the hostname, and
its `PANEL_PUBLIC_URL` and `PANEL_SECRET_KEY` settings were retired on
2026-09-15 once the relay had carried that panel; the code behind that road
went on 2026-09-17, so the panel now has no sign-in of its own at all. The
DNS record is gone too: `syllabus.maincoursemedia.com` no longer resolves,
and the panel's address is the relay's.

What is left of it is a line in the doctor. `PANEL_PUBLIC_URL`,
`PANEL_SECRET_KEY`, and the older `PANEL_GOOGLE_CLIENT_ID`,
`PANEL_GOOGLE_CLIENT_SECRET` and `PANEL_ALLOWED_EMAILS` are read by nothing;
`intake doctor` says so if any of them are still sitting in `.env`, so they
can be deleted.

Both the recorder and the watcher are started detached, on purpose, so
closing the panel abandons neither. A recording keeps going if the panel
quits or is killed mid-lecture; the next panel (or `intake record`) finds
it through `.work/recording.json`, shows it as live with a note that it was
picked up, and the stop button files it as usual. If nobody ever comes back,
ffmpeg stops itself at `RECORD_MAX_MINUTES` (240) and writes a valid file,
which the next panel files into the inbox on its first status poll. The
watcher likewise finishes a lecture midway through transcription on its own.

Whether the watcher is running is read from its lock file, which it holds
for exactly as long as it lives. The pid inside is only a label: a watcher
the panel started and that was later killed stays a zombie that still
answers a liveness check, and the panel once reported one as running for an
hour while nothing was processing. The lock cannot do that, and the panel
also reaps the watchers it started.

A recording that captures nothing is called out in two places. While it is
running, a file that still holds no audio a minute in turns the timer red
with a note under it, because a microphone that is open but blocked (usually
a permission never granted to whatever launched the panel) looks exactly
like a healthy one otherwise: the timer ticks either way. And when it ends,
the failure is written to `pipeline.log` and shows in the recent list as
failed, with ffmpeg's own log kept in `.work/` if it said anything. A flash
on the page and a line in the terminal are gone in seconds, and a recording
found dead during a dark wake shows nobody anything.

## Recording on this Mac

```bash
intake record
```

Records until Ctrl-C, names the file from your schedule, and drops it in
`~/.intake/syllabus/inbox/`. The first run asks for microphone permission; if it
was denied, grant it under System Settings > Privacy & Security > Microphone
and try again.

```bash
intake record --minutes 80          # stop on its own
intake record --course RELI-3304    # override the schedule
intake record --device "MacBook"    # pick a different mic
intake record --list-devices
```

Three things worth knowing:

- **The mic takes a second or two to open**, so the first moments of a lecture
  are lost. Start recording before the professor does. The finished file
  reports how much audio it actually holds, not how long you sat there.
- **Recording goes to `.work/` and only moves into `inbox/` once finalized.**
  An in-progress recording is not a valid m4a, and an 80 minute lecture written
  directly into `inbox/` would also outlast the watcher's one hour patience for
  a file that is still growing.
- **The microphone is chosen by name, not index.** avfoundation numbers devices
  in connection order, so index 0 is often a nearby iPhone rather than the
  built-in mic. `intake setup` picks by name; so does `RECORD_DEVICE` in
  `~/.intake/syllabus/.env`.
- **Press Stop before closing the lid.** Sleep ends the capture. The Mac is
  held awake for as long as ffmpeg runs (`caffeinate -w`, started alongside
  it), so a dimmed display or an idle timer cannot end a lecture early, but
  nothing can hold a closed lid open. A recording cut off by sleep is still
  filed with whatever it captured up to that point.

Stopping with Ctrl-C is the supported way to end a recording: it lets ffmpeg
write the file's trailer. Killing the process instead leaves an m4a nothing can
open.

## Running it

```bash
intake watch
```

Watches the inbox until Ctrl-C. It never dies on a bad file: the error goes to
`pipeline.log`, the recording stays in `inbox/` for a retry, and the next one
still gets processed. A lock file stops two watchers from racing on the same
inbox. To stop a stray one:

```bash
pkill -f 'intake.*watch'
```

One file at a time, without the watcher:

```bash
intake watch --once ~/.intake/syllabus/inbox/lecture.m4a
```

## The class schedule

`~/.intake/syllabus/schedule.toml` maps each class meeting to a course code. One
row per meeting: the day, the hour it starts on a 24-hour clock, and the code.
`intake setup` writes it; editing it by hand is just as good, and is the
only thing you need to touch each semester.

```toml
classes = [
  { day = "Mon", start =  9, course = "ENTR-4306" },
  { day = "Tue", start = 12, course = "ENTR-3306" },
  { day = "Tue", start = 14, course = "ACCT-4321" },
]

tolerance_minutes = 45
```

Two details that matter:

- The match runs against when the recording **started**, not the file's mtime.
  An 80 minute class ends closer to the next class on the calendar than to its
  own, so backing out the duration is what keeps back-to-back classes apart.
- `tolerance_minutes` must stay well under the gap between consecutive
  classes. With a 12:00 and a 14:00 on the same day, anything near 60 makes
  the two windows touch and the wrong course wins. The file carries this
  warning next to the value.

If the timestamp matches nothing, the filename is tried next: a recording named
`9-1-26-acct-4321-pt2.m4a` still files correctly. Only codes already in the
schedule are accepted, so a stray number in a filename cannot invent a course
folder. Failing both, it goes to `UNKNOWN/`.

A missing or broken schedule stops `record`, `watch`, and `panel` with one
line saying which row is wrong and pointing at `intake setup`.

## Things worth knowing when it misbehaves

**Start with `intake doctor`.** It checks every piece the pipeline depends
on and prints the fix for the ones that are missing.

**A summary comes back thin or oddly formatted.** The transcript is probably
truncated. The `gpt-4o-mini-transcribe` model caps output near 2000 tokens and
truncates silently rather than erroring, which is why audio is split on
duration as well as size. Lower that provider's `max_chunk_seconds` in
`providers.py` if you see truncation warnings. Models with no output cap,
Whisper and Deepgram among them, cannot hit this at all.

**Uploads suddenly fail with an auth error.** Before 2026-09-14 the Google
Cloud project behind the bundled client was in Testing, so refresh tokens
expired every 7 days; a token from then may still need one more
`intake login`. If this Mac is signed in to a Syllabus account whose Drive
is connected, the account's grant is used instead and the local token
does not matter.

**A recording was filed under the wrong course.** Rename it with the course
code in the filename and drop it back in `inbox/`. The filename fallback will
catch it.

**The panel opens on the Setup page instead of the controls.** Both keys and
at least one class are needed before anything can be recorded and filed. Fill
them in and the panel is one click away in the header.

**The timer ran for the whole lecture and nothing reached the inbox.** The
microphone was open but delivering nothing, and the recent list shows the
lecture as failed with the reason. On macOS that is almost always the
microphone permission for whatever launched the panel or `intake record`:
the terminal app, or the app whose terminal it was. Grant it under System
Settings > Privacy & Security > Microphone. The panel now turns the timer red
about a minute in when this is happening, so check the page after you press
Record. ffmpeg's own log, if it said anything, is in `~/.intake/syllabus/.work/`.

**The panel says the watcher is running but nothing gets processed.** Restart
the panel; older versions judged the watcher by its pid, which a watcher that
had been killed kept answering until the panel that started it quit.

**You installed before profiles existed.** Your data sits flat in `~/.intake`
itself, where Syllabus no longer looks. The first `intake` or `syllabus`
command you run at a terminal offers to move your `.env`, `schedule.toml`,
Drive token, `.drive_root`, log, and any recordings in `inbox/` and
`processed/` down into `~/.intake/syllabus/`; say yes and restart any watcher
or panel that was already running. Only Syllabus is offered that data; Sous
starts empty. Until you say yes, `intake doctor` lists what is waiting under
"older install". Scratch in `~/.intake/.work/` and the old lock file stay put.

**You installed before the home directory existed.** Your `.env`, token, and
log are still next to the code. The first `intake` command you run offers
to move them into `~/.intake/syllabus`; say yes and restart any watcher or
panel that was already running.

**You installed when this was called lectureai.** Your data is in
`~/.lectureai`. The first `intake` command you run at a terminal offers to
move your `.env`, `schedule.toml`, Drive token, log, and any recordings in
`inbox/` and `processed/` into `~/.intake/syllabus`; the panel does not ask,
so use `intake setup` or `intake doctor` for that first run. Until you say yes,
`intake doctor` lists what is waiting under "older install"; afterward it
reports the old folder under "older home", and you can delete it once
nothing is recording into its `.work/`. Scratch files in `.work/` are not
moved. If you had `LECTUREAI_HOME` set, nothing moves: the variable is still
read, and the doctor line for the home directory suggests renaming it.

## Re-runs, duplicates, and two-part lectures

Every uploaded file is stamped with the minute its recording started, in a
private Drive property. That stamp, not the filename, is what identifies a
lecture, which gives three useful behaviors:

- **Re-running a recording replaces its own files** rather than piling up
  copies, even if the topic slug came back different the second time and the
  filename changed with it.
- **Two recordings of one class on one day both survive.** They produce the
  same `{COURSE}_{DATE}_{Topic-Slug}`, so the second one is filed with its
  start time appended: `ACCT-4321_2026-09-01_Job-Order-Costing_1447`.
- **Nothing is overwritten unless it can be proven to be the same recording.**
  A duplicate in Drive is cheap; a lost lecture is not.

Files uploaded before this existed carry no stamp. The first time you re-run
one of those lectures, the file is claimed by name and stamped from then on.

## Action items in Notion

Every deadline a lecture mentions becomes a task in your Notion to-do
database, dated. This is optional: skip it in `intake setup` and the
pipeline behaves exactly as it did before.

**Setup**, which is mostly in the browser:

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy
   its secret.
2. Open your to-do database in Notion, and under the `...` menu choose
   **Connections** and add that integration. Without this step every request
   comes back 404, because the integration cannot see a database nobody shared
   with it.
3. Give both values to `intake setup` when it asks, or put them in
   `~/.intake/syllabus/.env` yourself:

```
NOTION_TOKEN=ntn_...
NOTION_DATABASE=https://www.notion.so/...   # just paste the database URL
```

Then confirm it can see your database and worked out the right properties:

```bash
intake notion --check
```

That prints every property in your database and which role it was matched to.

`--setup` below only applies to `NOTION_TARGET=database`; it refuses while
the target is `weekly`, since checkboxes never read those properties and
adding them would only leave clutter in your database.

If it reports that no date property matched, your database has nowhere to put
a deadline. Add what's needed:

```bash
intake notion --setup --dry-run   # see what it would add
intake notion --setup
```

That adds `Due` (date), `Course` (select, pre-filled with the codes in your
schedule), and `Source` (url) **only where the role isn't already covered**.
It never touches a property you already have: the request is built from
scratch and mentions only new names, because Notion deletes a property that is
sent as null. A database that already has a `Deadline` field keeps it and gets
no `Due`.

Apart from `--setup`, nothing about your database's shape is assumed. The
title is the only property Notion guarantees, and a due date, course, type,
and source link are used **only if** something suitable exists. Your `Status`
property is left alone so new tasks get whatever default your workflow already
uses. Each property serves one role only: if a single select has to cover both
course and type, course wins, since knowing the class matters more than
knowing it was a reading. If a guess is wrong, override it by exact name in
`~/.intake/syllabus/.env`:

```
NOTION_PROP_DUE=Deadline
NOTION_PROP_COURSE=Class
```

**Where tasks land.** A Notion database has two entirely separate surfaces,
and this matters more than it sounds. Database **rows** have real Due and
Course fields. Page **blocks** are where a weekly checklist actually lives: a
page per week, a column per day, checkboxes inside. A row is invisible from
the weekly page and vice versa, so a task can be filed perfectly and still be
nowhere you would ever see it. That happened.

By default (`NOTION_TARGET=weekly`) each action item becomes a **checkbox in
the day column matching its due date**, prefixed with the course in gray and
ending in a `↗` that links back to the Drive summary. Set `NOTION_TARGET=database` to create rows
with Due/Course/Source fields instead.

The weekly page is found by its date heading, e.g. `Sep 9 - Sep 13`, matched
against the task's due date. Headings carry no year, so the year is inferred
from the date being filed and checked against its neighbours, which is what
keeps the last week of December working. **If no page covers that date, the
task is skipped and reported** rather than filed into whatever page happens to
exist: a task hidden in a week you already finished is worse than one that
never arrived. Duplicate the weekly page and set its heading, and the next run
picks it up.

Within a day column, a blank checkbox from the template is filled before any
new one is appended, so the column keeps the shape you set up.

Run `intake notion --check` to see every page, the date range read from
each heading, and where the next seven days would land.

**Dates.** A deadline the instructor actually stated is used as-is, with
anything relative ("next Thursday") resolved against the lecture date. An item
with no stated deadline is dated to the **next time that class meets**,
computed from your schedule, and labeled `assumed` in the Drive summary so you
can tell the two apart. Nothing lands undated, because an undated task has no
day column to go in and gets skipped.

**Re-runs and restatements don't duplicate.** Before adding anything, what is
already filed is checked for the same errand. The match is on meaning, not on
the exact string: an instructor who brings one assignment up across three
class periods gets summarized three times and never in quite the same words,
so "Read chapter 7", "Please read Chapter Seven" and "Read ch. 7" all count as
one task. Numbers are matched exactly, so chapter 7 and chapter 8 stay
separate. A due date that moved does not create a second copy either. Nothing
is written into your database to make this work; the comparison happens here.

**Checkboxes stay short.** The model is asked for the errand alone, under ten
words and starting with a verb, with any context in a separate detail field.
On top of that the task is cleaned before it is filed: markdown is stripped
(Notion's checkbox text is a plain string, so an asterisk would show up as an
asterisk), line breaks are flattened, a polite opener like "Please make sure
to" comes off, and a deadline restated inside the task is dropped since the
day column already says it. The detail goes in the Drive summary, and in the
row's body when the target is `database`. What is left is one line: the course
set back in gray, the task, and a `↗` linking to the notes.

**Failures are contained.** Notion runs last, after Drive, and never raises. A
bad token or an outage costs you the Notion tasks for that lecture and nothing
else; the action items are still in the summary Doc.

## Development

The code is a plain Python package in `intake/`, and the `intake`
command is one console script over it. `syllabus` and `sous` are the same
entry point with the profile chosen (`lectureai` is an older name for it,
kept for now). The profiles themselves are in `profiles.py`, one dataclass
each: home folder, schedule filename, summary schema and prompt (`schemas.py`),
Drive folder, filename prefix, Notion target, panel port. Every module reads
those from `config.PROFILE` rather than spelling them out. To work on it:

```bash
git clone https://github.com/SyllabusAI/LectureAI && cd LectureAI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # installs the package editable, with the intake command
```

Dependencies are declared once, in `pyproject.toml`; `requirements.txt` just
points at it. Python 3.11 or newer, for `tomllib`.

Every module still runs on its own from the checkout, which is how to debug a
single stage. The root-level `record.py`, `watch.py`, `gui.py`, `upload.py`,
`transcribe.py`, `summarize.py`, and `notion_tasks.py` are one-line shims onto
the package, so these are the same code the CLI runs:

### Transcription providers

`TRANSCRIBE_MODEL` in `config.py` picks one. The limits that decide chunking
belong to the model, not to the audio, so they live with it in
`providers.py` and change when the model does:

| `TRANSCRIBE_MODEL` | Request cap | Chunked every | Can truncate | Speakers |
|---|---|---|---|---|
| `gpt-4o-mini-transcribe` (default) | 25MB | 8 min | yes | no |
| `gpt-4o-transcribe` | 25MB | 8 min | yes | no |
| `whisper-1` | 25MB | 20 min | no | no |
| `groq/whisper-large-v3` | 100MB | 20 min | no | no |
| `groq/whisper-large-v3-turbo` | 100MB | 20 min | no | no |
| `deepgram/nova-3` | 2GB | never | no | yes |

The gpt-4o models are the only ones that cap their output, so they are the
only ones that need 8 minute chunks and the re-split guard behind them. A
39 minute lecture is five requests on the default, two on Whisper, and one on
Deepgram. Anything not in the table is treated as an OpenAI model on the
default limits.

Price is why the Groq rows are there. An hour of lecture costs about $0.18 to
transcribe on the default and $0.111 on `groq/whisper-large-v3`, for the same
request shape; turbo is $0.04 again, at 12% WER against large-v3's 10.3% on
Groq's own figures. A Mac signed in to a Syllabus account does not choose:
the account service transcribes on Groq and falls back to OpenAI, and the
100MB cap above is Groq's developer tier, not its free tier's 25MB.

The non-OpenAI providers need their own key in `.env`, `DEEPGRAM_API_KEY` or
`GROQ_API_KEY`. Nothing prompts for them; `intake setup` only asks for the two
the default pipeline uses. To try one against a lecture without changing the
setting:

```bash
.venv/bin/python transcribe.py lecture.m4a --model whisper-1 > transcript.txt
```

```bash
.venv/bin/python record.py --list-devices
.venv/bin/python watch.py --once ~/.intake/syllabus/inbox/lecture.m4a
.venv/bin/python transcribe.py lecture.m4a > transcript.txt
.venv/bin/python summarize.py transcript.txt ACCT-4321 2026-09-03
.venv/bin/python upload.py summary.md ACCT-4321
.venv/bin/python gui.py
```

A dev checkout and a pipx install share `~/.intake` by default. To keep a
scratch install from touching your real recordings and keys, move the root:

```bash
INTAKE_HOME=/tmp/intake-scratch intake setup
```

That puts Syllabus in `/tmp/intake-scratch/syllabus/`. The root-level shims
(`python gui.py` and friends) run whichever profile `INTAKE_PROFILE` names,
Syllabus by default.

The Google OAuth client the app identifies itself with is `credentials.json`
inside the package, read by `google_client.py`. A `credentials.json` in the
home directory overrides it. The one at the repo root, if you have one from
before, stays gitignored.

That file holds a `client_secret`, and the repository is public, so it is
worth being exact about what that means. The client is a Google **Desktop**
client, and Google's own guidance is that a desktop client's secret is not
confidential: it ships inside every copy of the app and cannot be kept from
the person running it, which is why the flow does not rely on it for
security. What protects your Drive is the token in `~/.intake/syllabus`,
which never leaves your Mac, and the `drive.file` scope, which reaches only
files the app created. The exposure is that someone could put up their own
consent screen under this app's name, and with the project In production
anyone could complete it; what they would get is that person's own grant to
that person's own files, never yours. Never reuse this
client for anything server-side: a web app needs a **Web** client whose
secret stays on the server. Should its secret ever need resetting,
reset it on this same client in the Cloud console, put the new file in the
package, and the next release carries it; existing tokens keep working,
because they are tied to the client id, which does not change.

### Syllabus.app, the desktop build

`intake app` opens the same panel in a window of its own instead of a browser
tab (`intake/app.py`, a pywebview window over the Flask panel). It needs the
`app` extra: `.venv/bin/pip install -e '.[app]'`. When a panel is already
listening on the port, from `intake panel` or the launchd agent, the window
attaches to it and starts nothing, so a checkout and the live panel never
fight over one home.

The app stays running after its window is closed: closing hides the window,
and a menu bar item shows whether a recording is running, starts and stops
one, opens the window again, and quits. While the window is open the app has
a Dock tile and a menu bar, so copy and paste work in the key fields; hidden,
it lives in the menu bar alone. Launching it a second time only brings the
first copy's window forward. Setup's "Keep it running" card, and the same
line in the menu, install the launch agent from "Keeping the panel running"
above with the app as its program, started hidden at login and restarted only
after a crash, so Quit means quit until the next login. When the app is
opened on a Mac where `intake panel` or its agent already holds the port, it
becomes a window on that panel and its menu offers to take over, which moves
the agent to the app. Quitting never stops a recording or a lecture being
processed: both run detached and the next panel picks them up.

`packaging/build.sh` freezes that into `dist/Syllabus.app` with PyInstaller
(`packaging/syllabus.spec`; the `build` extra installs it). The bundle's one
binary is the app when double-clicked and the `syllabus` command when given
arguments, so `Syllabus.app/Contents/MacOS/Syllabus doctor` works. That is
also how the panel starts the watcher and how `service install` writes the
agent from inside the bundle: `config.program()` spells the command the way
this process was started, this interpreter with `-m intake.cli` from a
checkout or pipx, the binary itself when frozen. The icon
is made from `intake/static/icon.png` at build time; `packaging/build/` and
`dist/` are build output and stay out of git. Without a Developer ID identity
in the keychain the result is signed ad hoc: it runs on the Mac that built it,
and on another Mac only after the Gatekeeper steps in System Settings, Privacy
& Security.

Releases are cut by tag. Bump `__version__` in `intake/__init__.py`, merge,
then tag that commit `vX.Y.Z` and push the tag: the "Release Syllabus.app"
workflow builds the app on an Apple silicon runner, wraps it with
`packaging/dmg.sh`, and publishes the image and its checksum as a GitHub
Release with the notes in `packaging/release-notes.md`. The tag has to match
the version or the workflow refuses. Every running panel asks GitHub for the
release list once a day (`intake/updates.py`) and shows a notice with the
download link, or the `pipx upgrade` line for a Terminal install; nothing
replaces itself until the build is signed.

The bundle carries its own ffmpeg and ffprobe, so Homebrew is not needed on
a Mac that runs the app. They are static builds of a pinned ffmpeg release
made by `packaging/ffmpeg/build.sh` with the GPL and nonfree parts disabled
and only what the pipeline uses enabled: the microphone input, the AAC
encoder, the m4a and segment muxers, and decoders for the audio that lands in
the inbox. That makes them LGPL 2.1; the license texts ride along inside the
app. The "ffmpeg for Syllabus.app" workflow builds them on an Apple silicon
runner and, when run from main with publish ticked, attaches the tarball to
the GitHub Release named in `packaging/ffmpeg/release`; `packaging/build.sh`
downloads that file and checks its sha256. `intake/tools.py` is how the
pipeline finds them: the copy inside the app first, then a folder named in
`$INTAKE_FFMPEG_DIR`, then PATH, so a checkout and a pipx install still use
Homebrew's as before. Keeping the app resident and the release workflow for
the app itself are the next slices.

### Tests

Every suite is self-contained: the upload tests run against an in-memory fake
Drive, the recording tests open no microphone, the setup tests drive the
wizard with scripted answers into a temp directory. None needs network,
credentials, or an API key, none costs anything to run, and none can touch
`~/.intake`. They all run under the Syllabus profile whatever the shell says.

```bash
.venv/bin/python test_upload_collisions.py   # collisions, re-runs, two-part days
.venv/bin/python test_record.py              # device selection and naming
.venv/bin/python test_notion_tasks.py        # property mapping and de-duplication
.venv/bin/python test_tasktext.py            # cleaning a task, and telling two of them apart
.venv/bin/python test_gui.py                 # log parsing and API guard rails
.venv/bin/python test_insights.py            # the dashboard's numbers against a fake log and week
.venv/bin/python test_setup.py               # home directory, schedule file, setup wizard, doctor
.venv/bin/python test_profiles.py            # syllabus vs sous: homes, ports, folders, selection, migration
.venv/bin/python test_account.py             # claiming this Mac into a Syllabus account, against a fake service
.venv/bin/python test_sync.py                # the schedule file against the account's copy: push, pull, conflicts
.venv/bin/python test_relay.py               # the panel on the web: relayed requests, the base path, the socket loop
.venv/bin/python test_app.py                 # the desktop window: attach to a running panel or start one, the bundle's entry point
.venv/bin/python test_updates.py             # the update notice: picking the newest release, the daily check, what each install is told
```

## V2 roadmap

Everything described above is built and in daily use. These are what's left,
in rough order of how soon each one bites.

### New capability

- **Speaker diarization.** Separate the instructor from student questions.
- **Cross-lecture study guides.** Synthesize a whole unit rather than one
  lecture. The highest-value item for actually studying. The panel already
  holds the space for it: the "Study assistant" card at the bottom (v3) is
  where a chat over every transcript and summary will go, with study guides
  and quizzes as its starter prompts.

  Decided 2026-09-14: the assistant runs on **Claude Opus 5** (`claude-opus-5`)
  through the same `anthropic` SDK and `ANTHROPIC_API_KEY` the summarizer
  uses. No new provider, and no vector database. A 52-minute lecture is about
  8k tokens of transcript, so a semester of one course (roughly 30 lectures,
  about 240k tokens) fits in the 1M context window whole. The course's
  transcripts and summaries go into the system prompt as `document` blocks
  with citations enabled, cached with a one-hour TTL, so every answer quotes
  the lecture and date it came from rather than a retrieval guess. Whole
  semester questions start from the summaries and pull full transcripts only
  for the courses the question names. Opus rather than Sonnet because the job
  is synthesis across lectures and quiz writing, where the quality gap shows;
  run at low or medium effort for ordinary chat and raise it for study guides.
  Stream responses, since study guides run long. Rough cost with caching: the
  cache write is about $1.50 per study session for a full course, and each
  follow-up question about $0.15 to $0.20. The OpenAI key stays where it is,
  for transcription only.
- **A database.** State currently lives in `pipeline.log` and the filesystem.
  Not needed for the study assistant's retrieval (see above); it earns its
  place later for quiz history and progress.

Dropped: **slide OCR**, decided against on 2026-09-08 as not worth the
complexity.

### The desktop app, and sign-ins

Syllabus is meant to become a real Mac app that other people can use, with an
account behind it. The download at maincoursemedia.com/syllabus is the first
piece of that, and the rest is planned in this order:

- **Publish the Google Cloud project** (below). Done, 2026-09-14.
- **A Mac app bundle.** The panel already is the app; what is missing is the
  wrapper. Plan: a `pywebview` window around the same Flask panel, packaged
  with PyInstaller into `Syllabus.app`, signed and notarized, with ffmpeg
  bundled so Homebrew stops being a requirement. The pipx install stays as
  the path for people who prefer a terminal. First slice done 2026-09-15:
  `intake app` and `packaging/build.sh` produce an unsigned `Syllabus.app`
  that opens the panel in its own window ("Syllabus.app, the desktop build"
  above). The panel's watcher spawn and the launchd agent's command work
  from inside the bundle since the same day, and the bundle carries its own
  static LGPL ffmpeg, stays resident behind a menu bar item, has a
  start-at-login switch, ships as a DMG from a tagged release, and tells a
  person when a newer version is out. Still to do: Developer ID signing and
  notarization, then self-update.
- **Sign-ins.** Done. The account service ("A Syllabus account" above), a
  Cloudflare Worker with D1, the same stack `mcm-dashboard` is scaffolded
  on, owns the Google sign-in, lets a panel claim an identity with a device
  code, signs people in to the panel on the web, syncs the class schedule,
  and holds the Drive grant, handing each Mac short-lived access tokens.
  The panel's own Google sign-in and email allowlist were retired on
  2026-09-14. Since 2026-09-15 it also pays for transcription and summaries
  centrally: a signed-in Mac sends each audio chunk and each transcript to the
  account service, which holds the provider keys, meters what the account has
  spent, and stores neither the audio nor the transcript. Nobody has to make
  two AI accounts to use this any more.
- **The panel on the web is this Mac's panel.** It records from this Mac's
  microphone and starts processes here, so the address always reaches the
  one running on the Mac that does the recording. Done, 2026-09-14: every
  Mac signed in to an account has an address on the account service, over a
  connection the panel holds open ("The panel on the web" above). Left to
  do: list each Mac's address on the account page, move this author's panel
  off the tunnel, and retire the tunnel code.

### Loose ends

- **Publish the Google Cloud project.** Done on 2026-09-14: the
  friendly-bazaar project, which holds both the bundled Desktop client and
  the account service's Web client, is In production, with
  maincoursemedia.com verified in Search Console. `intake login` now works
  for anyone and its tokens no longer expire weekly. The LectureAI project,
  which only the panel's own fallback sign-in still uses, was published on
  2026-09-13.
- **Recover the September 3, 13:56 ACCT lecture.** Audio and transcript are
  gone, but the summary text survives in the raw JSON of the trashed Doc.
