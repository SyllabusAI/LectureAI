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

await run("an early or late class keeps its hour through setup", async () => {
  for (const hour of [0, 5, 23]) {
    const settings = like("/api/setup", {
      schedule: [{ day: "Mon", start: hour, course: "ENTR-4306" }],
    });
    const page = loadPage(DIR, "setup.html", {
      routes: setupRoutes({ "/api/setup": settings }),
    });
    await page.window.load();
    await page.settle();
    const start = page.document.querySelector("#rows select.start");
    assert(start, "the schedule table rendered no row");
    equal(Number(start.value), hour,
      `a class at ${hour}:00 was moved by opening settings`);
    // And it survives being read back out the way Save reads it.
    equal(page.window.readRows()[0].start, hour, `reading row back changed ${hour}:00`);
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

// --- Permission to record, asked once before the first recording ------------

/** The status payload with consent `given` or not. */
function statusWithConsent(given) {
  const status = like("/api/status");
  status.consent = { ...status.consent, given };
  return status;
}

await run("the record card asks for permission to record, and only until it has it", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({ "/api/status": statusWithConsent(false) }),
  });
  await page.window.poll();
  await page.settle();
  const box = page.$("consentBox");
  equal(box.hidden, false, "a Mac with no permission on file was not asked for it");
  assert(page.$("consentText").textContent.includes("permission to record"),
    `the box does not show what is being agreed to: ${page.$("consentText").textContent}`);
  // The question sits beside the button; it does not take the button away.
  assert(!page.$("recBtn").disabled, "the record button was disabled instead of asking");

  page.window.renderRecord(statusWithConsent(true));
  equal(box.hidden, true, "the box stayed up after permission was given");
  await page.close();
});

await run("pressing record without permission asks for it instead of starting", async () => {
  const posted = [];
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/status": statusWithConsent(false),
      "/api/record/start": (body) => { posted.push(body); return { ok: true, device: "d", planned: "p" }; },
    }),
  });
  await page.window.poll();
  await page.settle();
  page.$("consentBox").hidden = true;
  page.$("recBtn").click();
  await page.settle();
  equal(posted, [], "the page asked the recorder to start without permission on file");
  equal(page.$("consentBox").hidden, false, "the page did not show where to give permission");
  assert(/permission to record/.test(page.$("flash").textContent), page.$("flash").textContent);
  await page.close();
});

await run("Confirm needs the box ticked, then sends an explicit yes", async () => {
  const posted = [];
  let given = false;
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/status": () => statusWithConsent(given),
      "/api/consent": (body) => { posted.push(body); given = true; return { ok: true, given: true }; },
    }),
  });
  await page.window.poll();
  await page.settle();
  page.$("consentBtn").click();
  await page.settle();
  equal(posted, [], "Confirm sent a yes nobody ticked");
  assert(/Tick the box/.test(page.$("flash").textContent), page.$("flash").textContent);

  page.$("consentCheck").checked = true;
  page.$("consentBtn").click();
  await page.settle();
  equal(posted, [{ agree: true }], "Confirm did not send the yes");
  equal(page.$("consentBox").hidden, true, "the box stayed up after Confirm");
  await page.close();
});

await run("a refusal from the recorder brings the question back", async () => {
  // The page's last status said permission was on file; by the time Record
  // is pressed it is not (the file was removed since). The answer is the box,
  // and the poll that follows the press must not hide it again.
  let onFile = true;
  const page = loadPage(DIR, "index.html", {
    routes: indexRoutes({
      "/api/status": () => statusWithConsent(onFile),
      "/api/record/start": () => {
        onFile = false;
        return {
          __status: 403, ok: false, consent_required: true,
          error: "Before this Mac records, confirm that you have permission to record.",
        };
      },
    }),
  });
  await page.window.poll();
  await page.settle();
  equal(page.$("consentBox").hidden, true, "the box showed with permission on file");
  page.$("recBtn").click();
  await page.settle();
  equal(page.$("consentBox").hidden, false, "a consent refusal did not bring the box back");
  await page.close();
});

await run("the Setup page requires the permission box once, then shows it given", async () => {
  const posted = [];
  const settings = (given) => {
    const s = like("/api/setup", { schedule: [{ day: "Mon", start: 9, course: "ENTR-4306" }] });
    s.consent = { ...s.consent, given, agreed_at: given ? "2026-09-26T15:00:00+00:00" : "" };
    return s;
  };
  let given = false;
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/setup": (body) => {
        if (body) { posted.push(body); given = true; return { ok: true, configured: true, classes: 1 }; }
        return settings(given);
      },
    }),
  });
  await page.window.load();
  await page.settle();
  const box = page.$("consent");
  equal([box.checked, box.disabled, box.required], [false, false, true],
    "an unconfirmed Mac must be shown an empty, required box");
  assert(page.$("consentText").textContent.includes("permission to record"),
    page.$("consentText").textContent);

  const submit = () => page.$("form").dispatchEvent(
    new page.window.Event("submit", { cancelable: true }));
  submit();
  await page.settle();
  equal(posted, [], "Save went ahead with the permission box empty");
  assert(/permission to record/.test(page.$("flash").textContent), page.$("flash").textContent);

  box.checked = true;
  submit();
  await page.settle();
  equal(posted.length, 1, "Save did not send the form once the box was ticked");
  equal(posted[0].consent, true, "the tick did not reach the route");
  equal([box.checked, box.disabled], [true, true],
    "once given, the box should read as given and not be untickable by accident");
  assert(/Confirmed/.test(page.$("consentHint").textContent), page.$("consentHint").textContent);
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

finish();
