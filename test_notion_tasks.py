"""Tests for notion_tasks.py's id parsing, property mapping, and push logic.

Every Notion call is stubbed, so this needs no token, no database, and no
network. From the project root:

    .venv/bin/python test_notion_tasks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import notion_tasks as nt  # noqa: E402

# A to-do database shaped the way a real one tends to be: a title, a couple of
# date properties, a status, and some selects.
SCHEMA = {
    "Task": {"type": "title"},
    "Due": {"type": "date"},
    "Created": {"type": "date"},
    "Status": {"type": "status"},
    "Course": {"type": "select"},
    "Type": {"type": "multi_select"},
    "Source": {"type": "url"},
    "Notes": {"type": "rich_text"},
}


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"ok    {label}")
    return True


def reset_overrides():
    # A stand-in database id so resolve_data_source gets past id parsing;
    # every request it would make is stubbed below.
    config.NOTION_DATABASE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    config.NOTION_TOKEN = "test-token"
    config.NOTION_TARGET = "database"
    config.NOTION_PROP_DUE = ""
    config.NOTION_PROP_COURSE = ""
    config.NOTION_PROP_KIND = ""
    config.NOTION_PROP_SOURCE = ""


results = []

# --- id extraction --------------------------------------------------------

def t1():
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    url = f"https://www.notion.so/myspace/{bare}?v=99887766554433221100aabbccddeeff"
    assert nt.extract_id(url) == bare, nt.extract_id(url)
    assert nt.extract_id(bare) == bare
    dashed = "a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6"
    assert nt.extract_id(dashed) == bare, nt.extract_id(dashed)
results.append(run("pulls the database id out of a URL, bare id, or dashed id", t1))


def t2():
    # The view id after ?v= must not win over the database id.
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    view = "99887766554433221100aabbccddeeff"
    assert nt.extract_id(f"https://notion.so/{bare}?v={view}") == bare
results.append(run("the view id in a URL is not mistaken for the database", t2))


def t2b():
    # Notion puts the page title in the URL ahead of the id, and a title made
    # of hex-ish words ("Deface Added Beef Cafe") used to be matched as part
    # of the id, yielding a database that doesn't exist.
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    for title in ("Deface-Added-Beef-Cafe", "Faced-Badace-Decade-Beaded-Cabbage",
                  "Accede-Facade-Deface"):
        url = f"https://www.notion.so/myspace/{title}-{bare}?v=99887766554433221100aabbccddeeff"
        assert nt.extract_id(url) == bare, f"{title}: {nt.extract_id(url)}"
results.append(run("a hex-looking page title is not mistaken for the id", t2b))


def t2c():
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    dashed = "a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6"
    view = "99887766554433221100aabbccddeeff"
    for label, url in (
        ("fragment", f"https://www.notion.so/myspace/Tasks-{bare}#block"),
        ("no scheme", f"notion.so/myspace/Tasks-{bare}?v={view}"),
        ("dashed in a URL", f"https://www.notion.so/myspace/{dashed}?v={view}"),
        ("nested path", f"https://www.notion.so/team/{view}/{bare}?v={view}"),
    ):
        assert nt.extract_id(url) == bare, f"{label}: {nt.extract_id(url)}"
results.append(run("handles fragments, nested paths, and dashed ids in URLs", t2c))


def t3():
    for bad in ("", "https://notion.so/my-page", "not an id"):
        try:
            nt.extract_id(bad)
        except nt.NotionError:
            continue
        raise AssertionError(f"{bad!r} should have raised")
results.append(run("a URL with no id raises a readable error", t3))


# --- property mapping -----------------------------------------------------

def t4():
    reset_overrides()
    m = nt.map_properties(SCHEMA)
    assert m["title"] == "Task", m
    assert m["due"] == "Due", m          # "Due" beats "Created"
    assert m["course"] == "Course", m
    assert m["kind"] == "Type", m
    assert m["source"] == "Source", m
results.append(run("maps a typical to-do database by property name", t4))


def t5():
    reset_overrides()
    config.NOTION_PROP_DUE = "Created"
    m = nt.map_properties(SCHEMA)
    assert m["due"] == "Created", m
    reset_overrides()
results.append(run("an explicit override wins over the name hints", t5))


def t6():
    reset_overrides()
    config.NOTION_PROP_DUE = "Nonexistent"
    try:
        nt.map_properties(SCHEMA)
    except nt.NotionError as exc:
        assert "not in the database" in str(exc), exc
    else:
        raise AssertionError("a bogus override should raise")
    reset_overrides()
results.append(run("an override naming a missing property raises", t6))


def t7():
    reset_overrides()
    # No name hint matches, but there is exactly one date property.
    sparse = {"Thing": {"type": "title"}, "Whenever": {"type": "date"}}
    m = nt.map_properties(sparse)
    assert m["due"] == "Whenever", m
    assert m["course"] is None and m["kind"] is None, m
results.append(run("a lone date property is used even with no matching name", t7))


def t8():
    reset_overrides()
    # Nothing but a title: everything else is skipped, not invented.
    m = nt.map_properties({"Name": {"type": "title"}})
    assert m["title"] == "Name"
    assert all(m[r] is None for r in ("due", "course", "kind", "source")), m
results.append(run("a title-only database maps cleanly with no extras", t8))


def t8b():
    reset_overrides()
    # One select property must not be claimed by both course and kind: both
    # are written on create, so the course code would be overwritten by the
    # item type. This is what happened against the real database.
    one_select = {
        "Name": {"type": "title"},
        "Due": {"type": "date"},
        "Course": {"type": "select"},
        "Source": {"type": "url"},
    }
    m = nt.map_properties(one_select)
    assert m["course"] == "Course", m
    assert m["kind"] is None, f"kind stole the course property: {m}"
    assigned = [v for k, v in m.items() if v]
    assert len(assigned) == len(set(assigned)), f"a property serves two roles: {m}"
results.append(run("one select property is not claimed by two roles", t8b))


def t8c():
    reset_overrides()
    # With a dedicated Type property, kind gets it and course keeps Course.
    both = {
        "Name": {"type": "title"},
        "Course": {"type": "select"},
        "Type": {"type": "select"},
    }
    m = nt.map_properties(both)
    assert m["course"] == "Course" and m["kind"] == "Type", m
results.append(run("a dedicated Type property is still matched to kind", t8c))


def t9():
    reset_overrides()
    try:
        nt.map_properties({"Due": {"type": "date"}})
    except nt.NotionError as exc:
        assert "title" in str(exc), exc
    else:
        raise AssertionError("a schema with no title should raise")
results.append(run("a database with no title property raises", t9))


def t10():
    assert nt._value_for("select", "ACCT-4321") == {"select": {"name": "ACCT-4321"}}
    assert nt._value_for("multi_select", "reading") == {
        "multi_select": [{"name": "reading"}]}
    assert nt._value_for("status", "Todo") == {"status": {"name": "Todo"}}
    assert nt._value_for("url", "https://x") == {"url": "https://x"}
    rich = nt._value_for("rich_text", "hello")
    assert rich["rich_text"][0]["text"]["content"] == "hello", rich
results.append(run("each property type gets the right value shape", t10))


# --- push -----------------------------------------------------------------

class FakeNotion:
    """Stands in for _request, recording what would have been sent."""

    def __init__(self, existing_titles=()):
        self.existing = list(existing_titles)
        self.created = []
        self.queries = []

    def __call__(self, method, path, payload=None):
        if method == "GET" and path.startswith("/databases/"):
            return {"title": [{"plain_text": "My Tasks"}],
                    "data_sources": [{"id": "ds-1", "name": "My Tasks"}]}
        if method == "GET" and path.startswith("/data_sources/"):
            return {"properties": SCHEMA}
        if method == "POST" and path.endswith("/query"):
            # The duplicate check reads the rows and compares here, so the
            # stub hands back whole rows rather than answering a filter.
            self.queries.append(payload)
            return {"results": [
                {"id": f"existing-{i}",
                 "properties": {"Task": {"type": "title",
                                         "title": [{"plain_text": title}]}}}
                for i, title in enumerate(self.existing)
            ], "has_more": False}
        if method == "POST" and path == "/pages":
            self.created.append(payload)
            return {"url": f"https://notion.so/page-{len(self.created)}"}
        raise AssertionError(f"unexpected call: {method} {path}")


ITEMS = [
    {"task": "Read chapter 7", "due_date": "2026-09-15",
     "kind": "reading", "date_source": "stated"},
    {"task": "Problem set 3", "due_date": "2026-09-10",
     "kind": "assignment", "date_source": "assumed"},
]


def t11():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321", source_url="https://drive/doc")
    assert out["added"] == 2 and out["skipped"] == 0, out
    assert len(fake.created) == 2, fake.created

    first = fake.created[0]
    assert first["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}, first
    props = first["properties"]
    assert props["Task"]["title"][0]["text"]["content"] == "Read chapter 7"
    assert props["Due"]["date"]["start"] == "2026-09-15", props
    assert props["Course"]["select"]["name"] == "ACCT-4321", props
    assert props["Type"]["multi_select"][0]["name"] == "reading", props
    assert props["Source"]["url"] == "https://drive/doc", props
    # Status is left alone so the database's own default applies.
    assert "Status" not in props, props
results.append(run("push creates one page per item with mapped properties", t11))


def t12():
    reset_overrides()
    fake = FakeNotion(existing_titles={"Read chapter 7"})
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321")
    assert out["added"] == 1 and out["skipped"] == 1, out
    assert len(fake.created) == 1, fake.created
    assert fake.created[0]["properties"]["Task"]["title"][0]["text"]["content"] \
        == "Problem set 3"
results.append(run("an item already in the database is skipped, not duplicated", t12))


def t12b():
    reset_overrides()
    # UNKNOWN is not a real course, it is every lecture nothing could be
    # matched to. Scoping the duplicate check to it, the way a real course
    # is scoped, would compare a task against every other unrelated
    # lecture's rows ever filed there with no due-date filter to narrow it:
    # two different classes that both landed in UNKNOWN and happened to word
    # a task similarly would silently merge. The check must fall through to
    # a due-date filter instead, so it only ever compares same-day tasks.
    fake = FakeNotion()
    nt._request = fake
    nt.push([{"task": "Submit the reflection", "due_date": "2026-09-29",
             "kind": "assignment"}], config.UNKNOWN_COURSE)
    first_filter = fake.queries[-1].get("filter")
    assert first_filter == {"property": "Due", "date": {"equals": "2026-09-29"}}, \
        f"the duplicate check was scoped to the shared UNKNOWN bucket, not the due date: {first_filter}"

    nt.push([{"task": "Submit the reflection", "due_date": "2026-11-14",
             "kind": "assignment"}], config.UNKNOWN_COURSE)
    second_filter = fake.queries[-1].get("filter")
    assert second_filter == {"property": "Due", "date": {"equals": "2026-11-14"}}, \
        f"the duplicate check was scoped to the shared UNKNOWN bucket, not the due date: {second_filter}"
results.append(run("two different lectures both filed under UNKNOWN are never compared to each other", t12b))


def t13():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    out = nt.push([], "ACCT-4321")
    assert out == {"added": 0, "skipped": 0, "failed": 0, "urls": [],
                   "notes": [], "reasons": []}, out
    assert fake.created == [], "a lecture with no action items must not call Notion"
results.append(run("no action items means no Notion calls at all", t13))


def t14():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321", dry_run=True)
    assert out["added"] == 2, out
    assert fake.created == [], "dry run must not create anything"
results.append(run("dry run reports without writing", t14))


def t15():
    reset_overrides()
    # An item that fails must not stop the ones after it.
    class Flaky(FakeNotion):
        def __call__(self, method, path, payload=None):
            if method == "POST" and path == "/pages" and not self.created:
                self.created.append(None)
                raise nt.NotionError("simulated failure")
            return super().__call__(method, path, payload)

    fake = Flaky()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321")
    assert out["failed"] == 1 and out["added"] == 1, out
results.append(run("one failing item does not lose the rest", t15))


def t16():
    reset_overrides()
    # A database with no date property still takes the tasks, just undated.
    sparse = {"Name": {"type": "title"}}

    class NoDates(FakeNotion):
        def __call__(self, method, path, payload=None):
            if method == "GET" and path.startswith("/data_sources/"):
                return {"properties": sparse}
            return super().__call__(method, path, payload)

    fake = NoDates()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321")
    assert out["added"] == 2, out
    assert list(fake.created[0]["properties"]) == ["Name"], fake.created[0]
results.append(run("a database without a date property still gets the tasks", t16))


# --- weekly page --------------------------------------------------------

from datetime import date  # noqa: E402


def t17():
    cases = [
        ("Sep 9 - Sep 13",           date(2026, 9, 10), (date(2026,9,9), date(2026,9,13))),
        ("Sep 9 - Sep 13",           date(2026, 9, 14), None),
        ("Sep 9 \u2013 13",           date(2026, 9, 10), (date(2026,9,9), date(2026,9,13))),
        ("September 28 - October 4", date(2026, 10, 1), (date(2026,9,28), date(2026,10,4))),
        # A week that straddles New Year has to land in the right two years.
        ("Dec 28 - Jan 3",           date(2027, 1, 1),  (date(2026,12,28), date(2027,1,3))),
        ("Dec 28 - Jan 3",           date(2026, 12, 29),(date(2026,12,28), date(2027,1,3))),
        ("Weekly To-do List",        date(2026, 9, 10), None),
        ("",                         date(2026, 9, 10), None),
    ]
    for text, near, want in cases:
        got = nt.parse_week_range(text, near)
        assert got == want, f"{text!r} near {near}: {got} != {want}"
results.append(run("week headings parse, including across New Year", t17))


def _as_read(todo: dict) -> dict:
    """Echo a to_do the way Notion does: writes send text.content, reads come
    back carrying plain_text too. Without this the fake reads back blank and
    nothing is ever recognised as already present."""
    out = dict(todo)
    out["rich_text"] = [
        dict(part, plain_text=part.get("text", {}).get("content", ""))
        for part in todo.get("rich_text", [])
    ]
    return out


class FakeWeekly:
    """A weekly page: one or more week headings, each with seven day columns.

    `headings` takes several weeks to mirror the real page, which stacks the
    new week above the old one instead of starting a fresh page. Column ids
    are `col-<week>-<weekday>` so a test can name the exact week it expects.
    """

    DAYS = (" Mon", " Tues", " Wed", " Thur", " Fri", " Sat", " Sun")

    def __init__(self, heading="Sep 9 - Sep 13", blanks=3, headings=None):
        self.blocks = {}
        self.appended = []
        self.updated = []
        self.page = "page-1"
        self.headings = list(headings) if headings else [heading]
        # Notion pages open with an intro paragraph, ahead of any heading.
        page_blocks = [{"id": "intro", "type": "paragraph",
                        "paragraph": {"rich_text": [{"plain_text": "Add your to-dos"}]}}]
        for w, text in enumerate(self.headings):
            page_blocks.append({"id": f"h1-{w}", "type": "heading_1",
                                "heading_1": {"rich_text": [{"plain_text": text}]}})
            page_blocks.append({"id": f"div-{w}", "type": "divider", "divider": {}})
            cols = []
            for i, label in enumerate(self.DAYS):
                kids = [{"id": f"h-{w}-{i}", "type": "heading_3",
                         "heading_3": {"rich_text": [{"plain_text": label}]}}]
                for b in range(blanks):
                    kids.append({"id": f"todo-{w}-{i}-{b}", "type": "to_do",
                                 "to_do": {"rich_text": [], "checked": False}})
                self.blocks[f"col-{w}-{i}"] = kids
                cols.append({"id": f"col-{w}-{i}", "type": "column"})
            self.blocks[f"cl-{w}"] = cols
            page_blocks.append({"id": f"cl-{w}", "type": "column_list"})
        self.blocks[self.page] = page_blocks

    def __call__(self, method, path, payload=None):
        if method == "GET" and path.startswith("/databases/"):
            return {"title": [{"plain_text": "To-Do list"}],
                    "data_sources": [{"id": "ds-1", "name": "To-Do list"}]}
        if method == "POST" and path.endswith("/query"):
            return {"results": [{"id": self.page, "properties": {
                "Name": {"type": "title",
                         "title": [{"plain_text": "Weekly To-do List"}]}}}]}
        if method == "GET" and path.startswith("/blocks/"):
            block_id = path.split("/blocks/")[1].split("/children")[0]
            return {"results": self.blocks.get(block_id, [])}
        if method == "PATCH" and path.endswith("/children"):
            col = path.split("/blocks/")[1].split("/children")[0]
            new = {"id": f"new-{len(self.appended)}", "type": "to_do",
                   "to_do": _as_read(payload["children"][0]["to_do"])}
            self.blocks[col].append(new)
            self.appended.append(payload)
            return {"results": [new]}
        if method == "PATCH" and path.startswith("/blocks/"):
            bid = path.split("/blocks/")[1]
            for kids in self.blocks.values():
                for b in kids:
                    if b["id"] == bid:
                        b["to_do"] = _as_read(payload["to_do"])
            self.updated.append(bid)
            return {"id": bid}
        raise AssertionError(f"unexpected {method} {path}")

    def texts(self, day_index, week=0):
        return [
            "".join(p.get("plain_text") or p.get("text", {}).get("content", "")
                    for p in b["to_do"]["rich_text"])
            for b in self.blocks[f"col-{week}-{day_index}"]
            if b["type"] == "to_do"
        ]


ITEM = [{"task": "Read chapter 7", "due_date": "2026-09-10",
         "kind": "reading", "date_source": "stated"}]


def t18():
    reset_overrides()
    fake = FakeWeekly(); nt._request = fake
    out = nt.push_to_weekly(ITEM, "ACCT-4321", "https://drive/doc")
    assert out["added"] == 1 and out["failed"] == 0, out
    # 2026-09-10 is a Thursday: index 3, not any other column.
    assert any("Read chapter 7" in t for t in fake.texts(3)), fake.texts(3)
    for other in (0, 1, 2, 4, 5, 6):
        assert not any(t.strip() for t in fake.texts(other)), f"col {other} touched"
results.append(run("a task lands in its own weekday column", t18))


def t19():
    reset_overrides()
    fake = FakeWeekly(); nt._request = fake
    nt.push_to_weekly(ITEM, "ACCT-4321")
    # The template's blank boxes get used before any are added.
    assert fake.updated, "did not reuse a blank checkbox"
    assert not fake.appended, "appended instead of filling a blank"
    assert len([t for t in fake.texts(3)]) == 3, fake.texts(3)
results.append(run("a blank checkbox is filled before appending a new one", t19))


def t20():
    reset_overrides()
    fake = FakeWeekly(blanks=0); nt._request = fake
    nt.push_to_weekly(ITEM, "ACCT-4321")
    assert fake.appended, "should append when no blank is free"
    assert any("Read chapter 7" in t for t in fake.texts(3)), fake.texts(3)
results.append(run("with no blanks left, a checkbox is appended", t20))


def t21():
    reset_overrides()
    fake = FakeWeekly(); nt._request = fake
    nt.push_to_weekly(ITEM, "ACCT-4321")
    out = nt.push_to_weekly(ITEM, "ACCT-4321")
    assert out["added"] == 0 and out["skipped"] == 1, out
results.append(run("re-processing a lecture does not duplicate a checkbox", t21))


def t22():
    reset_overrides()
    # The page covers a different week: filing it anyway would hide the task
    # in a week you have already finished.
    fake = FakeWeekly(heading="Oct 5 - Oct 11"); nt._request = fake
    out = nt.push_to_weekly(ITEM, "ACCT-4321")
    assert out["added"] == 0 and out["failed"] == 1, out
    assert "no week heading covers" in out["notes"][0], out["notes"]
    assert not fake.updated and not fake.appended, "wrote into the wrong week"
results.append(run("no matching week means skip and say so, not guess", t22))


def t23():
    reset_overrides()
    fake = FakeWeekly(); nt._request = fake
    out = nt.push_to_weekly(
        [{"task": "Vague thing", "due_date": "", "kind": "other"}], "ACCT-4321")
    assert out["failed"] == 1 and out["added"] == 0, out
    assert "no due date" in out["notes"][0], out["notes"]
results.append(run("an undated item has no day to go in and is reported", t23))


def t24():
    reset_overrides()
    fake = FakeWeekly(); nt._request = fake
    nt.push_to_weekly(ITEM, "ACCT-4321", "https://drive/doc")
    body = fake.updated and [b for kids in fake.blocks.values() for b in kids
                             if b["id"] == fake.updated[0]][0]
    parts = body["to_do"]["rich_text"]
    joined = "".join(p["text"]["content"] for p in parts)
    assert joined.startswith("ACCT-4321 "), joined
    assert any((p["text"].get("link") or {}).get("url") == "https://drive/doc"
               for p in parts), parts
results.append(run("the checkbox carries the course and links to the notes", t24))


def t22b():
    """The bug that dropped two of a lecture's three action items.

    The page stacked "Sep 14 - 20" above "Sep 9 - Sep 13", and only the first
    heading was ever read, so anything due in the lower week was reported as
    having no page at all.
    """
    reset_overrides()
    fake = FakeWeekly(headings=["Sep 14 - 20", "Sep 9 - Sep 13"])
    nt._request = fake
    out = nt.push_to_weekly(ITEM, "ACCT-4321")   # due 2026-09-10, a Thursday
    assert out["added"] == 1 and out["failed"] == 0, out
    # Week 1 is "Sep 9 - Sep 13"; index 3 is Thursday.
    assert any("Read chapter 7" in t for t in fake.texts(3, week=1)), \
        fake.texts(3, week=1)
results.append(run("a week lower down the page is found, not just the first", t22b))


def t22c():
    """A day column is looked up inside the matched week, not page-wide.

    Every week has a Thursday column, so a search that scans the page in order
    lands in whichever week sits highest -- silently filing the task a week
    early or late.
    """
    reset_overrides()
    fake = FakeWeekly(headings=["Sep 14 - 20", "Sep 9 - Sep 13"])
    nt._request = fake
    nt.push_to_weekly(ITEM, "ACCT-4321")
    assert not any(t.strip() for t in fake.texts(3, week=0)), \
        f"filed into the wrong week: {fake.texts(3, week=0)}"


results.append(run("the day column comes from the matched week", t22c))


def t22d():
    """Sections are split on headings, and the intro paragraph belongs to none."""
    reset_overrides()
    fake = FakeWeekly(headings=["Sep 14 - 20", "Sep 9 - Sep 13"])
    nt._request = fake
    sections = nt.week_sections(fake.page)
    assert [s["heading"] for s in sections] == ["Sep 14 - 20", "Sep 9 - Sep 13"], \
        sections
    assert [s["column_lists"] for s in sections] == [["cl-0"], ["cl-1"]], sections
results.append(run("each heading owns the columns that follow it", t22d))


def t22e():
    """A dropped to-do produces a line the panel can show."""
    reset_overrides()
    fake = FakeWeekly(heading="Oct 5 - Oct 11"); nt._request = fake
    out = nt.push_to_weekly(ITEM, "ACCT-4321")
    line = nt.outcome_warning(out, len(ITEM))
    assert line.startswith("1 of 1 to-dos did not reach Notion"), line
    assert "no week heading covers 2026-09-10" in line, line
    # It goes into a tab-separated log, so it has to stay one line.
    assert "\t" not in line and "\n" not in line, repr(line)
results.append(run("a dropped to-do is reported as one showable line", t22e))


def t22f():
    """Nothing failed means nothing to warn about."""
    reset_overrides()
    fake = FakeWeekly(); nt._request = fake
    out = nt.push_to_weekly(ITEM, "ACCT-4321")
    assert out["failed"] == 0, out
    assert nt.outcome_warning(out, len(ITEM)) == "", out
    assert nt.outcome_warning(None, 3) == ""
results.append(run("a clean run warns about nothing", t22f))


def t22g():
    """One cause behind many dropped items is reported once, not per item."""
    reset_overrides()
    fake = FakeWeekly(heading="Oct 5 - Oct 11"); nt._request = fake
    items = [dict(ITEM[0], task=f"Task {i}") for i in range(4)]
    out = nt.push_to_weekly(items, "ACCT-4321")
    assert out["failed"] == 4, out
    assert len(out["reasons"]) == 1, out["reasons"]
    line = nt.outcome_warning(out, len(items))
    assert line.startswith("4 of 4 to-dos did not reach Notion"), line
    assert line.count("no week heading covers") == 1, line
results.append(run("one cause behind four dropped items is stated once", t22g))


def t25():
    import importlib
    fresh = importlib.reload(config)
    assert fresh.NOTION_TARGET == "weekly", \
        f"default target is {fresh.NOTION_TARGET!r}; rows are invisible from the weekly page"
    reset_overrides()
results.append(run("the shipped default writes to the weekly page", t25))


def t26():
    reset_overrides()
    config.NOTION_TARGET = "weekly"
    fake = FakeWeekly(); nt._request = fake
    out = nt.push(ITEM, "ACCT-4321", "https://drive/doc")
    assert out["added"] == 1, out
    assert fake.updated, "push() did not reach the weekly page"
    reset_overrides()
results.append(run("push() routes to the weekly page when told to", t26))


def t27():
    reset_overrides()
    config.NOTION_TARGET = "weekly"
    fake = FakeWeekly(); nt._request = fake
    try:
        nt.setup_properties()
    except nt.NotionError as exc:
        assert "weekly" in str(exc).lower(), exc
    else:
        raise AssertionError("--setup should refuse while the target is weekly")
    reset_overrides()
results.append(run("--setup refuses to add row properties the weekly target ignores", t27))


def t28():
    # The case that started this: the instructor brings one reading up again
    # a week later, and the model words it differently the second time.
    reset_overrides()
    fake = FakeNotion(existing_titles=["Read chapter 7"])
    nt._request = fake
    out = nt.push([{"task": "Please read Chapter Seven", "due_date": "2026-09-22",
                    "kind": "reading", "date_source": "stated"}], "ACCT-4321")
    assert out["skipped"] == 1 and out["added"] == 0, out
    assert not fake.created, fake.created
results.append(run("a reworded restatement is not filed a second time", t28))


def t29():
    reset_overrides()
    fake = FakeNotion(existing_titles=["Read chapter 7"])
    nt._request = fake
    out = nt.push([{"task": "Read chapter 8", "due_date": "2026-09-22",
                    "kind": "reading", "date_source": "stated"}], "ACCT-4321")
    assert out["added"] == 1 and out["skipped"] == 0, out
results.append(run("the next chapter is a new task, not a near-miss", t29))


def t30():
    # A moved deadline used to slip past the filter and land as a second row.
    reset_overrides()
    fake = FakeNotion(existing_titles=["Read chapter 7"])
    nt._request = fake
    out = nt.push([{"task": "Read chapter 7", "due_date": "2026-10-01",
                    "kind": "reading", "date_source": "stated"}], "ACCT-4321")
    assert out["skipped"] == 1, out
    # Scoped by course, so a pushed-back due date cannot hide the original.
    assert fake.queries and "Course" in str(fake.queries[0]), fake.queries
results.append(run("a task whose due date moved is still the same task", t30))


def t31():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    nt.push([{"task": "**Read** chapter 7 before class",
              "detail": "Bring the printed case to class.",
              "due_date": "2026-09-15", "kind": "reading",
              "date_source": "stated"}], "ACCT-4321")
    page = fake.created[0]
    title = page["properties"]["Task"]["title"][0]["text"]["content"]
    assert title == "Read chapter 7", title
    body = page["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert body == "Bring the printed case to class.", body
results.append(run("the row title is the errand and the detail is its body", t31))


def t32():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    nt.push(ITEMS, "ACCT-4321")
    assert "children" not in fake.created[0], fake.created[0]
results.append(run("an item with no detail gets no empty paragraph", t32))


def t33():
    reset_overrides()
    config.NOTION_TARGET = "weekly"
    fake = FakeWeekly(); nt._request = fake
    nt.push_to_weekly([{"task": "Read chapter 7", "due_date": "2026-09-10",
                        "kind": "reading", "date_source": "stated"}],
                      "ACCT-4321", "https://drive/doc")
    # Same errand, different words, on a later run of the same course.
    fake.updated.clear()
    out = nt.push_to_weekly([{"task": "Please read Chapter Seven",
                              "due_date": "2026-09-10", "kind": "reading",
                              "date_source": "stated"}],
                            "ACCT-4321", "https://drive/doc")
    assert out["skipped"] == 1 and out["added"] == 0, out
    assert not fake.updated, fake.updated
    reset_overrides()
results.append(run("a reworded restatement is not added to the weekly page", t33))


def t34():
    reset_overrides()
    config.NOTION_TARGET = "weekly"
    fake = FakeWeekly(); nt._request = fake
    nt.push_to_weekly([{"task": "- **Read** chapter 7 before class",
                        "due_date": "2026-09-10", "kind": "reading",
                        "date_source": "stated"}], "ACCT-4321", "https://drive/doc")
    body = [b for kids in fake.blocks.values() for b in kids
            if b["id"] == fake.updated[0]][0]
    parts = body["to_do"]["rich_text"]
    joined = "".join(p["text"]["content"] for p in parts)
    assert "*" not in joined and "\n" not in joined, joined
    assert joined == "ACCT-4321 Read chapter 7 \u2197", repr(joined)
    # The course is its own span, so it can be set back without dimming the task.
    assert parts[0]["annotations"]["color"] == "gray", parts[0]
    assert "annotations" not in parts[1], parts[1]
    reset_overrides()
results.append(run("the checkbox is one clean line with the course set back", t34))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
