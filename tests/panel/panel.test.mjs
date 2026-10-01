/**
 * What the panel's pages do, checked by running them.
 *
 * Driven by test_panel.py, which renders the pages through Flask and passes
 * the directory it wrote them to. Each test loads a page, feeds it the JSON
 * its own routes would return, calls the page's own functions, and asserts
 * against the resulting DOM.
 *
 * The fixtures start as what the live routes actually returned this run, and
 * a test overrides only what it is about. Nothing here hard-codes a payload
 * shape that could drift away from the route it stands for.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { assert, equal, loadPage, runner } from "./panel.mjs";

const DIR = process.argv[2];
const REAL = JSON.parse(readFileSync(path.join(DIR, "routes.json"), "utf8"));
const { run, finish } = runner();

/** A deep copy of a captured payload, with `changes` laid over the top. */
function like(route, changes = {}) {
  return { ...structuredClone(REAL[route]), ...changes };
}

/** The dashboard calls these on load whatever a test is about. */
function indexRoutes(extra = {}) {
  return {
    "/api/status": like("/api/status"),
    "/api/allowance": like("/api/allowance"),
    "/api/assistant": like("/api/assistant"),
    ...extra,
  };
}

/** The setup page calls these on load whatever a test is about. */
function setupRoutes(extra = {}) {
  return {
    "/api/setup": like("/api/setup"),
    "/api/doctor": like("/api/doctor"),
    "/api/account": like("/api/account"),
    "/api/login-item": like("/api/login-item"),
    "/api/calendar": like("/api/calendar"),
    ...extra,
  };
}

// --- The behavior two of the findings turn on ------------------------------

await run("a <select> whose value no option carries reads back empty", async () => {
  // Not a test of the app: a test of the harness, kept because the value of
  // everything below depends on it. Measured against Chromium, which gives
  // the same three answers.
  const page = loadPage(DIR, "setup.html", { routes: setupRoutes() });
  const { document } = page;
  const sel = document.createElement("select");
  sel.innerHTML = '<option value="6">6</option><option value="7">7</option>';
  equal([sel.value, sel.selectedIndex], ["6", 0], "markup with nothing selected takes the first");
  sel.value = "23";
  equal([sel.value, sel.selectedIndex], ["", -1], "a value no option carries reads back empty");
  await page.close();
});

// --- F9: an hour the backend accepts must survive a round trip -------------

await run("an early or late class keeps its time through setup", async () => {
  // A bare hour is what a schedule from before quarter hours sends.
  for (const [sent, want] of [["00:00", "00:00"], ["05:00", "05:00"], ["23:45", "23:45"],
                              ["14:15", "14:15"], [9, "09:00"]]) {
    const settings = like("/api/setup", {
      schedule: [{ day: "Mon", start: sent, course: "ENTR-4306" }],
    });
    const page = loadPage(DIR, "setup.html", {
      routes: setupRoutes({ "/api/setup": settings }),
    });
    await page.window.load();
    await page.settle();
    const start = page.document.querySelector("#rows select.start");
    assert(start, "the schedule table rendered no row");
    equal(start.value, want, `a class at ${sent} was moved by opening settings`);
    // And it survives being read back out the way Save reads it.
    equal(page.window.readRows()[0].start, want, `reading row back changed ${sent}`);
    await page.close();
  }
});

// --- F13: the course picker has to follow the schedule ---------------------

await run("the course picker follows the schedule when it changes", async () => {
  const first = like("/api/status", { courses: ["ACCT-4321", "ENTR-3306"] });
  const page = loadPage(DIR, "index.html", { routes: indexRoutes({ "/api/status": first }) });
  await page.window.poll();
  await page.settle();
  const picker = page.$("courseSel");
  equal([...picker.options].map((o) => o.value).filter(Boolean),
    ["ACCT-4321", "ENTR-3306"], "the picker did not render the schedule");

  // The schedule changes in another window, or syncs from the account.
  page.window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => like("/api/status", { courses: ["MATH-1100"] }),
  });
  await page.window.poll();
  await page.settle();
  equal([...picker.options].map((o) => o.value).filter(Boolean), ["MATH-1100"],
    "the picker still offers a course that left the schedule");
  await page.close();
});

await run("a course the user had picked is kept only while it exists", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": like("/api/status", { courses: ["ACCT-4321", "ENTR-3306"] }) }),
  });
  await page.window.poll();
  await page.settle();
  const picker = page.$("courseSel");
  picker.value = "ENTR-3306";

  page.window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => like("/api/status", { courses: ["ACCT-4321", "ENTR-3306"] }),
  });
  await page.window.poll();
  await page.settle();
  equal(picker.value, "ENTR-3306", "an unchanged schedule discarded the user's choice");

  page.window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => like("/api/status", { courses: ["ACCT-4321"] }),
  });
  await page.window.poll();
  await page.settle();
  equal(picker.value, "", "a course that left the schedule stayed selected");
  await page.close();
});

// --- A lecture filed under the wrong course can be moved -------------------

await run("a recent lecture can be moved to another course", async () => {
  const name = "ENTR-3306_2026-10-01_Producer-Surplus-And-Market";
  const row = {
    when: "2026-10-01T14:38:50", course: "ENTR-3306", source: "ENTR-3306_2026-10-01_1405.m4a",
    name, url: "https://docs.google.com/document/d/x/edit", error: null, warning: "",
    date: "2026-10-01", seconds: 1895, words: 2835, actions: 0, terms: 7,
  };
  const posted = [];
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/status": like("/api/status", { courses: ["ENTR-3306", "ECON-2301"], recent: [row] }),
      "/api/lecture/refile": (body) => {
        posted.push(body);
        return { ok: true, course: body.course, name: name.replace("ENTR-3306", body.course),
                 from: "ENTR-3306", url: row.url, warnings: [] };
      },
    }),
  });
  await page.window.poll();
  await page.settle();
  const move = page.$("recent").querySelector(".row-move");
  assert(move, "the lecture has no Move action");
  equal([...move.options].map((o) => o.value), ["", "ECON-2301"],
    "Move offers the course the lecture is already under, or misses one");
  move.value = "ECON-2301";
  move.dispatchEvent(new page.window.Event("change"));
  await page.settle();
  equal(posted, [{ name, course: "ECON-2301" }], "Move did not ask to refile the lecture");
  assert(/Moved to ECON-2301/.test(page.$("flash").textContent), page.$("flash").textContent);
  await page.close();
});

// --- R1: course text reaches markup, and must not carry anything with it ---

await run("a course cannot smuggle an attribute into the picker", async () => {
  // The exploit is the quote, not the tag: inside a <select> the parser
  // drops element tags anyway, so an injected <img> was never the danger.
  // A quote closes value="..." and whatever follows becomes an attribute.
  const nasty = 'X" onmouseover="steal()" data-x="';
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": like("/api/status", { courses: [nasty] }) }),
  });
  await page.window.poll();
  await page.settle();
  const option = page.$("courseSel").options[1];
  equal([...option.attributes].map((a) => a.name).sort(), ["value"],
    "the course brought attributes of its own into the option");
  equal(option.value, nasty, "the course did not survive escaping intact");
  await page.close();
});

// --- F7: refreshing the microphone list is not a reload --------------------

await run("refreshing the microphone list keeps unsaved edits", async () => {
  const settings = like("/api/setup", {
    schedule: [{ day: "Mon", start: 9, course: "ENTR-4306" }],
  });
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({ "/api/setup": settings }),
  });
  await page.window.load();
  await page.settle();

  // Type, but do not save.
  page.document.querySelector("#rows input.course").value = "QA-UNSAVED-101";
  page.$("tolerance").value = "20";

  page.$("refreshDevices").click();
  await page.settle();

  equal(page.document.querySelector("#rows input.course").value, "QA-UNSAVED-101",
    "Refresh discarded an edited course");
  equal(page.$("tolerance").value, "20", "Refresh discarded an edited tolerance");
  // And it did refresh the thing it is named after.
  assert(page.$("device").options.length > 0, "Refresh left no microphones listed");
  await page.close();
});

// --- F16: an account change has to reach the parts it governs --------------

await run("signing out brings back this Mac's own key fields", async () => {
  const managed = like("/api/setup", { managed: true, managed_email: "me@example.com" });
  const signedIn = like("/api/account", { signed_in: true, email: "me@example.com" });
  let signedOut = false;
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/setup": () => (signedOut ? like("/api/setup", { managed: false }) : managed),
      "/api/account": () => (signedOut ? like("/api/account", { signed_in: false })
                                       : signedIn),
      "/api/account/signout": () => { signedOut = true; return { ok: true }; },
    }),
  });
  await page.window.load();
  await page.window.loadAccount();
  await page.settle();
  equal(page.$("ownKeys").hidden, true, "a managed Mac should not be asked for keys");

  page.$("accountOff").click();
  await page.settle();
  await page.fire();

  equal(page.$("ownKeys").hidden, false,
    "after signing out the key fields are still hidden");
  equal(page.$("managedKeys").hidden, true,
    "after signing out the account is still shown as providing keys");
  await page.close();
});

// --- R3: the banner names what is missing ----------------------------------

await run("the setup banner names what is missing, and nothing else", async () => {
  const cases = [
    [[], null],
    [["API keys"], "API keys"],
    [["a class schedule"], "a class schedule"],
    [["API keys", "a class schedule"], "API keys and a class schedule"],
  ];
  for (const [needs, expected] of cases) {
    const page = loadPage(DIR, "index.html", {
      routes: indexRoutes({ "/api/status": like("/api/status", { configured: !needs.length, setup_needs: needs }) }),
    });
    await page.window.poll();
    await page.settle();
    const flash = page.$("setupFlash");
    if (expected === null) {
      equal(flash.style.display, "none", "a fully set up Mac was shown the setup banner");
    } else {
      equal(flash.style.display, "block", `${needs} showed no banner`);
      assert(flash.textContent.includes(expected),
        `the banner does not name what is missing: ${flash.textContent.trim()}`);
      // The sentence that sent a signed-in user looking for keys they do
      // not have, and a page that tells them there is nothing to do.
      assert(!/Nothing is set up yet/i.test(flash.textContent),
        `the banner still claims nothing is set up: ${flash.textContent.trim()}`);
      if (!needs.includes("API keys")) {
        assert(!/key/i.test(flash.textContent),
          `the banner asks for keys that are not missing: ${flash.textContent.trim()}`);
      }
    }
    await page.close();
  }
});

// --- R4: the panel does not claim Save can do what Save cannot -------------

// --- The warning that has to arrive before the button is pressed ----------

await run("the dashboard asks what is left before anything is recorded", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/allowance": {
        ok: true, period: "2026-09", source: "trial",
        audio_seconds: { used: 11915, allowance: 18000, left: 6085 },
        summary_tokens: { used: 123427, allowance: 150000, left: 26573 },
        recordable_seconds: 3524,
      },
    }),
  });
  await page.settle();
  assert(page.calls.some((c) => c.url.endsWith("/api/allowance") && c.method === "GET"),
    "the page never asked what was left, so nobody is warned until the lecture fails");
  const flash = page.$("allowanceFlash");
  assert(flash.style.display !== "none", "the page knew and said nothing");
  assert(/59 minutes/.test(flash.textContent), flash.textContent);
  // Recording is never the thing that is blocked.
  assert(!page.$("recBtn").disabled, "a low allowance must not take the button away");
  await page.close();
});

await run("the warning names the meter that is short, and only when one is", async () => {
  const page = loadPage(DIR, "index.html", { routes: indexRoutes() });
  await page.settle();
  const flash = page.$("allowanceFlash");
  const show = (over) => {
    page.window.renderAllowance({
      ok: true, period: "2026-09", source: "trial",
      audio_seconds: { used: 0, allowance: 18000, left: 18000 },
      summary_tokens: { used: 0, allowance: 150000, left: 150000 },
      recordable_seconds: 18000,
      ...over,
    });
    return flash.style.display === "none" ? "" : flash.textContent;
  };

  equal(show({}), "", "a full allowance still put a banner over the button");

  // Audio gone: the recording is kept, nothing is transcribed.
  const noAudio = show({
    audio_seconds: { used: 18000, allowance: 18000, left: 0 }, recordable_seconds: 0,
  });
  assert(/transcription allowance is used up/.test(noAudio), noAudio);
  assert(/file is kept/.test(noAudio), `it must say the recording survives: ${noAudio}`);
  assert(flash.className.includes("err"), flash.className);

  // The shape of the day this was written: audio to spare, no room to
  // summarize it. Saying "transcription" here is what sent somebody to the
  // wrong meter in the first place.
  const noSummary = show({
    summary_tokens: { used: 145000, allowance: 150000, left: 5000 }, recordable_seconds: 0,
  });
  assert(/summary allowance is used up/.test(noSummary), noSummary);
  assert(/transcription still/.test(noSummary), noSummary);

  // An answer that never came is not the same as an allowance that ran out.
  page.window.renderAllowance({ ok: false, error: "the account service answered 503" });
  equal(flash.style.display, "none", "a service it could not reach became a warning");
  await page.close();
});

await run("a fix the form cannot carry out is not rewritten into one it can", async () => {
  const doctorReport = like("/api/doctor");
  doctorReport.checks = [{
    name: "older install", ok: false,
    detail: "3 data file(s) still in /Users/x/.lectureai: pipeline.log ...",
    fix: "move them into ~/.intake/syllabus yourself. Saving this page does not "
       + "move recordings, and this install is already set up.",
    required: false, fix_is_web: true,
  }];
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({ "/api/doctor": doctorReport }),
  });
  await page.window.checkup();
  await page.settle();
  const shown = page.$("checks").textContent;
  assert(!/fill in the section above and save/.test(shown),
    `the page told the user to save, which moves nothing: ${shown.trim()}`);
  assert(/move them into/.test(shown), `the real instruction was dropped: ${shown.trim()}`);
  await page.close();
});

// --- Calendars --------------------------------------------------------------

/** The calendar payload with one destination's fields replaced. */
function calendarsWith(key, changes) {
  const out = like("/api/calendar");
  out.destinations = out.destinations.map((d) => (d.key === key ? { ...d, ...changes } : d));
  return out;
}

await run("each calendar is drawn with its state, and none is labeled not ready", async () => {
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/calendar": calendarsWith("apple_reminders", {
        enabled: true, state: "denied", detail: "Syllabus is not allowed to use Reminders.",
      }),
    }),
  });
  await page.window.loadCalendars();
  await page.settle();
  const rows = [...page.$("calendars").querySelectorAll(".cal")];
  equal(rows.map((r) => r.dataset.key), ["apple_calendar", "apple_reminders", "google_calendar"],
    "the three calendars were not all drawn, in order");
  const reminders = rows[1];
  assert(reminders.querySelector(".calOn").checked, "an enabled calendar showed as off");
  equal(reminders.querySelector(".dot").className, "dot bad", "denied access did not read as a problem");
  assert(/not allowed/.test(reminders.querySelector(".calText").textContent),
    "the reason access is missing was not shown");
  assert(!rows.some((r) => r.querySelector(".preview")), "a calendar was labeled not ready");
  equal(rows[0].querySelector(".calName").value, like("/api/calendar").destinations[0].name,
    "the calendar name was not filled in");
  await page.close();
});

await run("switching a calendar on asks the route, and a refusal is shown", async () => {
  const posted = [];
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/calendar": (body) => {
        if (body === undefined) return like("/api/calendar");
        posted.push(body);
        return { __status: 400, ok: false, error: "Syllabus is not allowed to use Calendars." };
      },
    }),
  });
  await page.window.loadCalendars();
  await page.settle();
  const box = page.$("calendars").querySelector('[data-key="apple_calendar"] .calOn');
  box.checked = true;
  box.dispatchEvent(new page.window.Event("change"));
  await page.settle();
  equal(posted, [{ destination: "apple_calendar", enabled: true }], "the toggle sent the wrong request");
  assert(/not allowed/.test(page.$("flash").textContent), "the refusal was not shown");
  // Redrawn from the route afterward, so the box goes back to what is saved.
  const again = page.$("calendars").querySelector('[data-key="apple_calendar"] .calOn');
  equal(again.checked, false, "a refused calendar still looks switched on");
  await page.close();
});

await run("Google's sign-in is watched until it finishes", async () => {
  let running = true;
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/calendar": (body) => {
        if (body !== undefined) return { ok: true, connecting: true };
        const out = like("/api/calendar");
        out.connecting = { running, error: "" };
        return out;
      },
    }),
  });
  await page.window.loadCalendars();
  await page.settle();
  const box = page.$("calendars").querySelector('[data-key="google_calendar"] .calOn');
  box.checked = true;
  box.dispatchEvent(new page.window.Event("change"));
  await page.settle();
  assert(page.timers.some((t) => t.kind === "interval"), "nothing is watching the sign-in");
  assert(/signing in to Google/.test(page.$("calendars").textContent),
    "the page does not say it is waiting on the browser");
  running = false;
  await page.fire();
  assert(/Google Calendar connected/.test(page.$("flash").textContent),
    "a finished sign-in was not announced");
  await page.close();
});

// --- Excusing a class that did not meet ------------------------------------

/** A status payload whose week holds one missed class and one called off. */
function weekWith(classes) {
  const status = like("/api/status");
  status.insights = structuredClone(status.insights);
  status.insights.week = {
    start: "2026-09-14",
    days: [{ day: "Tue", date: "2026-09-15", today: false, classes }],
  };
  return status;
}

const MISSED = {
  course: "ACCT-4321", hour: 14, recorded: false, url: "", name: "",
  now: false, missed: true, canceled: false, note: "",
};
const OFF = { ...MISSED, missed: false, canceled: true, note: "campus closed" };

await run("a class that did not meet reads as excused, not as a miss", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": weekWith([OFF]) }),
  });
  await page.window.poll();
  await page.settle();
  const slot = page.$("week").querySelector(".slot");
  assert(slot.classList.contains("canceled"), `the chip is not marked: ${slot.className}`);
  assert(!slot.classList.contains("missed"), "a class that did not meet still reads as missed");
  assert(/did not meet/.test(slot.textContent), `the chip does not say so: ${slot.textContent}`);
  assert(/campus closed/.test(slot.getAttribute("title")), slot.getAttribute("title"));
  equal(slot.querySelector(".slot-act").textContent, "Undo",
    "an excused class offers no way back");
  await page.close();
});

await run("the button excuses the class it sits on, and takes it back", async () => {
  // The route answers, and the poll that follows is fed the same week, so
  // the assertion is about what was asked for, not about a redraw.
  const posted = [];
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/status": weekWith([MISSED]),
      "/api/class/cancel": (body) => posted.push(body) && { ok: true },
      "/api/class/restore": (body) => posted.push(body) && { ok: true },
    }),
  });
  await page.window.poll();
  await page.settle();
  const btn = page.$("week").querySelector(".slot-act");
  equal(btn.textContent, "Didn't meet", "a missed class offers no way to excuse it");

  btn.click();
  await page.settle();
  equal(posted, [{ course: "ACCT-4321", date: "2026-09-15" }],
    "the button did not send the class it belongs to");
  const asked = page.calls.filter((c) => String(c.url).endsWith("/api/class/cancel"));
  equal(asked.length, 1, "the cancel was sent more than once, or not at all");
  equal(asked[0].method, "POST", "the cancel went out as a GET");

  // And the other way, from a chip the route has since marked canceled.
  page.window.renderWeek(weekWith([OFF]).insights);
  page.$("week").querySelector(".slot-act").click();
  await page.settle();
  equal(posted.length, 2, "the undo sent nothing");
  assert(page.calls.some((c) => String(c.url).endsWith("/api/class/restore")),
    "the undo did not reach the restore route");
  await page.close();
});

// --- No permission click-through before recording --------------------------
// Removed in 0.6.0: the Terms carry the user's responsibility for consent, so
// neither page asks, and Record starts on the first press.

await run("the dashboard has no permission checkbox, and Record starts on the first press", async () => {
  const posted = [];
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/record/start": (body) => { posted.push(body); return { ok: true, device: "d", planned: "p" }; },
    }),
  });
  await page.window.poll();
  await page.settle();
  for (const id of ["consentBox", "consentCheck", "consentBtn", "consentText"]) {
    equal(page.$(id), null, `the dashboard still has #${id}`);
  }
  assert(!/permission to record/i.test(page.window.document.body.textContent),
    "the dashboard still mentions permission to record");
  page.$("recBtn").click();
  await page.settle();
  equal(posted.length, 1, "Record did not ask the recorder to start");
  assert(/Recording from/.test(page.$("flash").textContent), page.$("flash").textContent);
  await page.close();
});

await run("the Setup page has no permission checkbox, and Save sends no permission field", async () => {
  const posted = [];
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/setup": (body) => {
        if (body) { posted.push(body); return { ok: true, configured: true, classes: 1 }; }
        return like("/api/setup", { schedule: [{ day: "Mon", start: 9, course: "ENTR-4306" }] });
      },
    }),
  });
  await page.window.load();
  await page.settle();
  for (const id of ["consent", "consentCard", "consentText", "consentHint"]) {
    equal(page.$(id), null, `the Setup page still has #${id}`);
  }
  assert(!/permission to record/i.test(page.window.document.body.textContent),
    "the Setup page still mentions permission to record");
  page.$("form").dispatchEvent(new page.window.Event("submit", { cancelable: true }));
  await page.settle();
  equal(posted.length, 1, "Save did not send the form");
  equal("consent" in posted[0], false, "Save still sends a permission field");
  await page.close();
});

// --- The relay road --------------------------------------------------------

await run("a relayed page writes every request under its device path", async () => {
  const page = loadPage(DIR, "index.relayed.html", {
    routes: indexRoutes(),
  });
  await page.window.poll();
  await page.settle();
  assert(page.calls.length > 0, "the relayed page made no requests at all");
  for (const call of page.calls) {
    assert(String(call.url).includes("/p/3XwJMPZ3_2RREYbt/api/"),
      `a relayed request skipped the device path: ${call.url}`);
  }
  await page.close();
});

// --- The guided tour -------------------------------------------------------

/** Run what the page scheduled until the tour has had its chance to start. */
async function drawDashboard(page) {
  await page.settle();
  await page.fire();
}

await run("the tour starts once on a configured dashboard, and Done keeps it closed", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": like("/api/status", { configured: true }) }),
  });
  await drawDashboard(page);
  const tour = page.$("tour");
  assert(!tour.hidden, "a first visit to a configured dashboard showed no tour");
  equal(page.$("tourCount").textContent.split(" of ")[0], "1", "the tour did not open on its first stop");
  equal(page.$("tourBack").hidden, true, "the first stop offers Back to nowhere");

  // Walk it to the end the way a person would.
  let guard = 0;
  while (!tour.hidden && guard++ < 20) page.$("tourNext").click();
  assert(tour.hidden, "Next never reached the end of the tour");
  equal(page.window.localStorage.getItem("syllabus-tour-seen"), "1",
    "finishing the tour did not remember it, so it comes back on every visit");

  // Later polls redraw the dashboard; none of them may bring it back.
  await page.window.poll();
  await drawDashboard(page);
  assert(tour.hidden, "a later poll started the tour again");
  await page.close();
});

await run("the tour waits for setup, and stays closed once seen", async () => {
  const unset = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": like("/api/status", { configured: false }) }),
  });
  await drawDashboard(unset);
  assert(unset.$("tour").hidden, "the tour pointed at a dashboard that cannot record yet");
  await unset.close();

  const seen = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": like("/api/status", { configured: true }) }),
  });
  // Before the first status arrives, as a returning visitor's browser would have it.
  seen.window.localStorage.setItem("syllabus-tour-seen", "1");
  await drawDashboard(seen);
  assert(seen.$("tour").hidden, "the tour came back for someone who already finished it");
  await seen.close();
});

await run("the Tour button replays it, Esc closes it, and missing cards are skipped", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": like("/api/status", { configured: true }) }),
  });
  page.window.localStorage.setItem("syllabus-tour-seen", "1");
  await drawDashboard(page);
  const full = () => Number(page.$("tourCount").textContent.split(" of ")[1]);

  page.$("tourBtn").click();
  assert(!page.$("tour").hidden, "the Tour button did not open the tour");
  const stops = full();
  page.document.dispatchEvent(new page.window.KeyboardEvent("keydown", { key: "ArrowRight" }));
  equal(page.$("tourCount").textContent, `2 of ${stops}`, "the arrow key did not move to the next stop");
  page.document.dispatchEvent(new page.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert(page.$("tour").hidden, "Esc did not close the tour");

  // A card that is not on the page is left out, not pointed at.
  page.$("assistant").remove();
  page.$("tourBtn").click();
  equal(full(), stops - 1, "the tour kept a stop for a card that is not there");
  const titles = [];
  for (let k = 0; k < stops - 1; k++) {
    titles.push(page.$("tourTitle").textContent);
    if (k < stops - 2) page.$("tourNext").click();
  }
  assert(!titles.includes("Study assistant"), `the tour still stops at the missing card: ${titles}`);
  page.$("tourBack").click();
  page.$("tourSkip").click();
  assert(page.$("tour").hidden, "Skip did not close the tour");
  await page.close();
});

// --- The lid: sleep ends a recording, and caffeinate cannot stop it --------

await run("the Setup page warns not to close the lid while recording", async () => {
  const page = loadPage(DIR, "setup.html", { routes: setupRoutes() });
  await page.window.load();
  await page.settle();
  const warning = page.$("lidWarning");
  assert(warning, "the Setup page has no lid warning");
  assert(!warning.hidden, "the lid warning is hidden");
  assert(/close the lid while recording/i.test(warning.textContent),
    `the lid warning does not say what to avoid: ${warning.textContent}`);
  await page.close();
});

finish();
