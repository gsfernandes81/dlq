"""``dlq dump`` finishes, especially on the trees it exists for.

It is the bug report: the thing somebody pastes when a download failed on the
phone and the fix is being worked out somewhere else entirely. So the property
that matters is not what any section says but that **every section is reached**
— a dump that tracebacks in the middle is a bug report with the evidence
missing, on the one tree where the evidence is the point.

Each test below therefore breaks something real and asks for the whole dump
back: no state file, a config that will not parse, an item that is not text, a
sibling checkout that is not there, a logs directory that is not there.
"""

from __future__ import annotations

import pytest

MiB = 1024**2


def dumped(dlq, capsys, target=None) -> tuple[str, list[str]]:
    """``(everything it printed, the sections it got through)``."""
    assert dlq.sched.dump(target) == 0
    said = capsys.readouterr().out
    sections = [
        line[3:].strip() for line in said.splitlines() if line.startswith("== ")
    ]
    return said, sections


@pytest.fixture
def whole(dlq, capsys):
    """A dump of a healthy tree, to compare a broken one's sections against."""
    _, sections = dumped(dlq, capsys)
    assert sections
    return sections


def test_a_dump_of_a_working_queue_carries_the_evidence(dlq, capsys, whole):
    """The environment, how each checkout resolved, the gate, and the items."""
    dlq.item("10-thing.py", cap=MiB, desc="a thing", body="import expire_dl\n")
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert str(dlq.root) in said
    assert "expire_runner.py" in said
    assert "quota_widget.py" in said
    # An item's head is where the evidence usually is: its sys.path lines.
    assert "10-thing.py" in said
    assert "import expire_dl" in said


def test_it_finishes_with_no_state_file(dlq, capsys, whole):
    assert not (dlq.root / "state.json").exists()
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert "no state.json" in said


def test_it_finishes_on_a_state_file_that_will_not_parse(dlq, capsys, whole):
    (dlq.root / "state.json").write_text("{ this is not json")
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert "unreadable" in said


def test_it_names_the_config_it_could_not_read(dlq, capsys, whole):
    """Which is why every setting below it reads as its default, and why the
    person filing the report could not change any of them."""
    dlq.config("{ not json either")
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert dlq.runner.config_problem() in said
    for name in dlq.runner.SETTINGS:
        assert name in said


def test_it_names_a_stored_value_it_is_ignoring(dlq, capsys, whole):
    """The value, not just the fact that there is one: a hand-edited file is
    the reason a phone is spending by a figure nobody recognises."""
    dlq.config({"window_minutes": 7})
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert "7" in said
    assert "config.json" in said


def test_it_finishes_on_an_item_that_is_not_a_text_file(dlq, capsys, whole):
    """A photo called ``20-holiday.png`` matches the item naming and is not
    UTF-8, and this runs over every file in the queue."""
    (dlq.root / "queue" / "20-holiday.py").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert "20-holiday.py" in said


def test_it_finishes_with_a_sibling_checkout_missing(dlq, capsys, monkeypatch, whole):
    """The runner imports ``quota_widget`` from the zwana-quota checkout, and a
    dump written because that checkout is missing is the one that must arrive.
    """

    def gone():
        raise ImportError("No module named 'quota_widget'")

    monkeypatch.setattr(dlq.sched, "_runner", gone)
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert "unreadable" in said


def test_it_finishes_with_no_logs_directory(dlq, capsys, whole):
    for path in (dlq.root / "logs").iterdir():
        path.unlink()
    (dlq.root / "logs").rmdir()
    said, sections = dumped(dlq, capsys)
    assert sections == whole
    assert "no logs" in said


def test_it_finishes_on_a_tree_that_is_barely_a_queue_at_all(dlq, capsys, whole):
    """Everything wrong at once, which is what a bug report looks like."""
    (dlq.root / "queue" / "README.md").unlink()
    dlq.config("}{")
    (dlq.root / "state.json").write_text("nope")
    for path in (dlq.root / "logs").iterdir():
        path.unlink()
    (dlq.root / "logs").rmdir()
    _, sections = dumped(dlq, capsys)
    assert sections == whole


def test_a_named_item_is_the_one_it_shows(dlq, capsys):
    """``dlq dump NAME`` is the item somebody is asking about."""
    dlq.item("10-wanted.py", desc="the one in the report")
    dlq.item("20-other.py", desc="not this one")
    said, _ = dumped(dlq, capsys, target="wanted")
    assert "10-wanted.py" in said
    assert "20-other.py" not in said


def test_with_nothing_failing_it_still_shows_how_items_import(dlq, capsys):
    """The queue's own heads say how an item reaches its downloader, which is
    what a pre-split item gets wrong."""
    dlq.item("10-thing.py", body="import sys\nsys.path.insert(0, '/somewhere')\n")
    said, _ = dumped(dlq, capsys)
    assert "sys.path.insert" in said


def test_a_failed_item_is_what_it_shows_by_default(dlq, capsys):
    dlq.item("10-queued.py")
    dlq.item("20-failed.py", where="failed")
    said, _ = dumped(dlq, capsys)
    assert "20-failed.py" in said
