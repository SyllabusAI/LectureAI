"""Tests for refile.py: moving a filed lecture to another course.

Runs against an in-memory fake Drive: no network, no OAuth, and nothing
touches the real Drive. From the project root:

    .venv/bin/python test_refile.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config, gui, refile  # noqa: E402
from intake import upload as drive  # noqa: E402

FOLDER = drive.FOLDER_MIME
DOC = drive.GOOGLE_DOC_MIME
KEY = drive.RECORDING_KEY_PROPERTY

OLD = "ENTR-3306_2026-10-01_Producer-Surplus-And-Market"
NEW = "ACCT-4321_2026-10-01_Producer-Surplus-And-Market"
LECTURE = "2026-10-01T14:05"


class Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeFiles:
    def __init__(self, store):
        self.store = store

    def list(self, q="", **kw):
        name = re.search(r"name = '([^']*)'", q)
        parent = re.search(r"'([^']*)' in parents", q)
        prop = re.search(r"appProperties has \{ key='[^']*' and value='([^']*)' \}", q)
        out = []
        for fid, f in self.store.items():
            if f["trashed"] or (name and f["name"] != name.group(1)):
                continue
            if parent and parent.group(1) not in f["parents"]:
                continue
            if prop and f["appProperties"].get(KEY) != prop.group(1):
                continue
            if (FOLDER in q) != (f["mimeType"] == FOLDER):
                continue
            out.append({"id": fid, "name": f["name"], "appProperties": f["appProperties"]})
        return Req({"files": out})

    def create(self, body=None, **kw):
        fid = f"id{len(self.store) + 1}"
        self.store[fid] = {"name": body["name"], "parents": body.get("parents", []),
                           "mimeType": body.get("mimeType", "text/plain"),
                           "appProperties": {}, "trashed": False, "html": ""}
        return Req({"id": fid})

    def get(self, fileId=None, **kw):
        if fileId not in self.store:
            raise drive.HttpError(type("R", (), {"status": 404, "reason": "nf"})(), b"")
        f = self.store[fileId]
        return Req({"id": fileId, **{k: f[k] for k in
                                      ("name", "parents", "mimeType", "appProperties", "trashed")}})

    def update(self, fileId=None, body=None, addParents=None, removeParents=None,
               media_body=None, **kw):
        f = self.store[fileId]
        if body and "name" in body:
            f["name"] = body["name"]
        if removeParents:
            f["parents"] = [p for p in f["parents"] if p not in removeParents.split(",")]
        if addParents:
            f["parents"].append(addParents)
        if media_body is not None:
            f["html"] = media_body.getbytes(0, media_body.size()).decode()
        return Req({"id": fileId, "name": f["name"],
                    "webViewLink": f"https://docs.google.com/document/d/{fileId}/edit"})

    def export(self, fileId=None, mimeType=None, **kw):
        return Req(self.store[fileId]["html"].encode())


class FakeService:
    def __init__(self):
        self.store = {}

    def files(self):
        return FakeFiles(self.store)

    def add(self, fid, name, parents, mime="text/plain", key="", html=""):
        self.store[fid] = {"name": name, "parents": parents, "mimeType": mime,
                           "appProperties": {KEY: key} if key else {},
                           "trashed": False, "html": html}


def filed():
    """A Drive and a log holding one lecture filed under ENTR-3306."""
    svc = FakeService()
    svc.add("root", "Lecture Notes", [], FOLDER)
    svc.add("entr", "ENTR-3306", ["root"], FOLDER)
    svc.add("entrT", "Transcripts", ["entr"], FOLDER)
    svc.add("doc1BCymvg5yqr9O", OLD, ["entr"], DOC, LECTURE,
            "<h1><span>ENTR-3306: 2026-10-01</span></h1><p>ENTR-3306: 2026-10-01 in a quote</p>")
    svc.add("txt1iuq1SYm3DybD", f"{OLD}.txt", ["entrT"], key=LECTURE)
    drive.get_service = lambda interactive=True: svc
    drive.ensure_root_folder = lambda service: "root"
    config.LOG_FILE.write_text(
        "2026-10-01T13:42:17\tENTR-3306\tENTR-3306_2026-10-01_1245.m4a\t"
        "ENTR-3306_2026-10-01_History-Of-Innovation-Ethics\thttps://docs.google.com/document/d/other12345/edit\t\t{}\n"
        f"2026-10-01T14:38:50\tENTR-3306\tENTR-3306_2026-10-01_1405.m4a\t{OLD}\t"
        "https://docs.google.com/document/d/doc1BCymvg5yqr9O/edit?usp=drivesdk\t\t"
        '{"seconds":1895,"words":2835,"actions":0,"terms":7}\n')
    config.CALENDAR_LEDGER.unlink(missing_ok=True)
    return svc


def folder_named(svc, name, parent):
    return next(i for i, f in svc.store.items()
                if f["mimeType"] == FOLDER and f["name"] == name and parent in f["parents"])


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    print(f"ok    {label}")
    return True


results = []


def t1():
    svc = filed()
    out = refile.refile(OLD, "ACCT-4321")
    acct = folder_named(svc, "ACCT-4321", "root")
    doc = svc.store["doc1BCymvg5yqr9O"]
    assert doc["name"] == NEW and doc["parents"] == [acct], doc
    txt = svc.store["txt1iuq1SYm3DybD"]
    assert txt["name"] == f"{NEW}.txt", txt
    assert txt["parents"] == [folder_named(svc, "Transcripts", acct)], txt
    assert out["name"] == NEW and out["course"] == "ACCT-4321" and out["from"] == "ENTR-3306", out
    assert "doc1BCymvg5yqr9O" in out["url"], "the summary got a new link"
    assert out["warnings"] == [], out["warnings"]
results.append(run("both files are renamed and moved, and keep their ids", t1))


def t2():
    svc = filed()
    refile.refile(OLD, "ACCT-4321")
    html = svc.store["doc1BCymvg5yqr9O"]["html"]
    assert "<span>ACCT-4321: 2026-10-01</span>" in html, html
    assert "<p>ENTR-3306: 2026-10-01 in a quote</p>" in html, "more than the heading changed"
results.append(run("the summary's heading names the new course, and only the heading", t2))


def t3():
    filed()
    refile.refile(OLD, "ACCT-4321")
    lines = config.LOG_FILE.read_text().splitlines()
    assert len(lines) == 2, lines
    assert lines[0].split("\t")[1] == "ENTR-3306", "the other lecture was moved too"
    fields = lines[1].split("\t")
    assert fields[1] == "ACCT-4321" and fields[3] == NEW, fields
    assert fields[6].startswith('{"seconds":1895'), "the measurements were lost"
    rows = gui._log_rows()
    assert [r["course"] for r in rows] == ["ENTR-3306", "ACCT-4321"], rows
results.append(run("the log line is rewritten in place, so the dashboard follows", t3))


def t4():
    filed()
    for course, why in (("MATH-1100", "not on your schedule"),
                        ("ENTR-3306", "already filed under")):
        try:
            refile.refile(OLD, course)
        except refile.RefileError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(f"moving to {course} was allowed")
    try:
        refile.refile("ENTR-3306_2026-10-01_Nothing", "ACCT-4321")
    except refile.RefileError as exc:
        assert "no filed lecture" in str(exc), exc
    else:
        raise AssertionError("a lecture missing from the log was moved")
results.append(run("an unknown course, the same course, or an unknown lecture is refused", t4))


def t5():
    svc = filed()
    acct = "acct"
    svc.add(acct, "ACCT-4321", ["root"], FOLDER)
    svc.add("acctT", "Transcripts", [acct], FOLDER)
    svc.add("theirs", NEW, [acct], DOC, "2026-10-01T14:00")
    refile.refile(OLD, "ACCT-4321")
    assert svc.store["theirs"]["name"] == NEW, "the lecture already there was renamed"
    assert svc.store["doc1BCymvg5yqr9O"]["name"] == f"{NEW}_1405", svc.store["doc1BCymvg5yqr9O"]
    assert svc.store["txt1iuq1SYm3DybD"]["name"] == f"{NEW}_1405.txt"
results.append(run("a lecture already holding the name in the new course keeps it", t5))


def t6():
    filed()
    config.write_private(config.CALENDAR_LEDGER, json.dumps({"items": [
        {"dest": "apple_calendar", "lecture": LECTURE, "course": "ENTR-3306", "task": "t"},
        {"dest": "apple_calendar", "lecture": "2026-09-27T14:07", "course": "ENTR-3306", "task": "u"},
    ], "calendars": {}}))
    out = refile.refile(OLD, "ACCT-4321")
    items = json.loads(config.CALENDAR_LEDGER.read_text())["items"]
    assert [i["course"] for i in items] == ["ACCT-4321", "ENTR-3306"], items
    assert any("still say ENTR-3306" in w for w in out["warnings"]), out["warnings"]
results.append(run("calendar items from the lecture are reported, and the ledger follows", t6))


def t7():
    svc = filed()
    del svc.store["txt1iuq1SYm3DybD"]
    out = refile.refile(OLD, "ACCT-4321")
    assert svc.store["doc1BCymvg5yqr9O"]["name"] == NEW
    assert any("transcript was not found" in w for w in out["warnings"]), out["warnings"]
results.append(run("a missing transcript still moves the summary, and says so", t7))


def t8():
    svc = filed()
    client = gui.app.test_client()
    res = client.post("/api/lecture/refile", json={"name": OLD, "course": "ACCT-4321"})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["name"] == NEW
    assert svc.store["doc1BCymvg5yqr9O"]["name"] == NEW
    res = client.post("/api/lecture/refile", json={"name": NEW, "course": "MATH-1100"})
    assert res.status_code == 400 and "not on your schedule" in res.get_json()["error"]
    res = client.post("/api/lecture/refile", json={"name": NEW})
    assert res.status_code == 400
results.append(run("the panel route moves a lecture and refuses a bad request", t8))


def t9():
    filed()
    code = refile.main([OLD, "--course", "acct-4321"])
    assert code == 0, code
    assert config.LOG_FILE.read_text().splitlines()[1].split("\t")[3] == NEW
results.append(run("intake refile works from the command line, any letter case", t9))


passed = sum(results)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
