"""What a queue item has to say for itself, and where its file ends up.

:func:`expire_runner.parse_item` is the door every download comes through, and
it runs over every file in the queue at the top of every firing — **before
anything else happens**. So the rule it is built to is that every way of
failing comes back as an ``error`` and never as a raised exception: an
exception escaping here is not one bad item, it is the whole night, silently,
for as long as the file sits there. A photo called ``20-holiday.png`` is enough
to do it, which is why one of these is a PNG.

Nothing here executes an item. The declaration is read statically, because an
``--estimate`` mode would mean running untrusted code outside the guarded
window — exactly where a buggy script could spend bytes before any guard
exists. The number an item declares is a **cap enforced against it**, never a
promise believed.
"""

from __future__ import annotations

import sys

import pytest

MiB = 1024**2


def parsed(dlq, text: str, name: str = "10-thing.py", executable: bool = False):
    path = dlq.root / "queue" / name
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    if executable:
        path.chmod(0o755)
    return dlq.runner.parse_item(path)


GOOD = "# EXPIRE: v1\n# EXPECT_BYTES: 1000\n"


# --------------------------------------------------------------------------- #
# What is declared
# --------------------------------------------------------------------------- #


def test_a_well_formed_item_declares_a_cap_and_nothing_else_is_required(dlq):
    item = parsed(dlq, GOOD)
    assert "error" not in item
    assert item["cap"] == 1000
    assert item["partial"] is False
    assert item["slice_min"] == dlq.runner.SLICE_MIN_BYTES
    assert item["dest"] == ""
    # With nothing said about it, an item is described by its own name.
    assert item["desc"] == "10-thing.py"


def test_the_headers_an_item_may_carry(dlq):
    item = parsed(
        dlq,
        "# EXPIRE: v1\n"
        "# EXPECT_BYTES: 4200000000\n"
        "# PARTIAL: yes\n"
        "# SLICE_MIN_BYTES: 8388608\n"
        "# DEST: video\n"
        "# DESC: a film somebody asked for\n",
    )
    assert item["cap"] == 4_200_000_000
    assert item["partial"] is True
    assert item["slice_min"] == 8 * MiB
    assert item["dest"] == "video"
    assert item["desc"] == "a film somebody asked for"


@pytest.mark.parametrize("said", ["yes", "true", "1", "YES", " Yes "])
def test_a_resumable_item_says_so_however_it_is_written(dlq, said):
    assert parsed(dlq, f"{GOOD}# PARTIAL: {said}\n")["partial"] is True


@pytest.mark.parametrize("said", ["no", "false", "0", "", "maybe"])
def test_anything_else_is_not_a_resumable_item(dlq, said):
    """Which matters: a whole item is all-or-nothing, and a partial one is
    given a slice and asked to come back."""
    assert parsed(dlq, f"{GOOD}# PARTIAL: {said}\n")["partial"] is False


def test_headers_stop_at_the_first_run_of_code(dlq):
    """They are a header block, not a search of the whole file: a URL in a
    comment further down is not a declaration."""
    item = parsed(
        dlq,
        "# EXPIRE: v1\n# EXPECT_BYTES: 1000\n"
        "import sys\n"
        "# EXPECT_BYTES: 999999999999\n",
    )
    assert item["cap"] == 1000


def test_a_comment_before_the_headers_does_not_stop_them(dlq):
    """A shebang comes first on every item ytq and dlq write."""
    item = parsed(dlq, f"#!{sys.executable}\n{GOOD}", executable=True)
    assert "error" not in item and item["cap"] == 1000


# --------------------------------------------------------------------------- #
# Every way of failing is an error, never an exception
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing at all
        "print('hello')\n",  # a script that never claimed to be an item
        "# EXPIRE: v2\n# EXPECT_BYTES: 1000\n",  # a contract nobody wrote
        "# EXPIRE: v1\n",  # no cap
        "# EXPIRE: v1\n# EXPECT_BYTES: lots\n",  # a cap that is not a number
        "# EXPIRE: v1\n# EXPECT_BYTES: 0\n",  # a cap of nothing
        "# EXPIRE: v1\n# EXPECT_BYTES: -5\n",
        "# EXPIRE: v1\n# EXPECT_BYTES: 1000\n# SLICE_MIN_BYTES: some\n",
    ],
)
def test_a_declaration_that_will_not_do_comes_back_as_a_reason(dlq, text):
    item = parsed(dlq, text)
    assert item["error"].strip()
    assert item["name"] == "10-thing.py"
    # And nothing is invented for it: an item with an error has no cap to
    # spend against.
    assert "cap" not in item


def test_a_file_that_is_not_text_is_not_an_item(dlq):
    """``20-holiday.png`` matches the item naming and is not UTF-8, and this
    runs over every file in the queue at the top of every firing."""
    item = parsed(dlq, b"\x89PNG\r\n\x1a\n\xff\xfe", "20-holiday.py")
    assert item["error"].strip()
    assert "move it out of the queue" in item["error"]


def test_a_file_that_cannot_be_read_is_a_reason_rather_than_a_traceback(dlq):
    item = dlq.runner.parse_item(dlq.root / "queue" / "10-not-there.py")
    assert item["error"].strip()


def test_an_interpreter_that_is_not_there_is_caught_before_it_is_spawned(dlq):
    """A wrong shebang fails at exec with a bare "No such file or directory"
    naming the *script*, which reads as though the item vanished.

    On Termux ``/bin/bash`` in particular does not exist — ``/bin`` is a
    symlink to ``/system/bin`` — so catching it here turns three wasted nights
    into one line in the log.
    """
    item = parsed(dlq, f"#!/no/such/python3\n{GOOD}", executable=True)
    assert "shebang" in item["error"]
    # Not executable, so the shebang is nobody's business: the runner hands it
    # to bash rather than exec'ing it.
    plain = parsed(dlq, f"#!/no/such/python3\n{GOOD}", "20-not-executable.py")
    assert "error" not in plain


# --------------------------------------------------------------------------- #
# Which files are items at all
# --------------------------------------------------------------------------- #


def test_only_files_named_like_items_are_considered(dlq):
    """Without this the contract README in the same directory parses as a live
    item, because the example header in its code fence is a perfectly valid
    one — documentation would schedule itself as a download."""
    dlq.item("10-real.py")
    (dlq.root / "queue" / "notes.md").write_text(GOOD)
    (dlq.root / "queue" / "scratch.py").write_text(GOOD)
    (dlq.root / "queue" / ".hidden-10-thing.py").write_text(GOOD)
    (dlq.root / "queue" / "10-a-directory.py").mkdir()

    good, bad = dlq.runner.queued_items()
    assert [item["name"] for item in good] == ["10-real.py"]
    assert bad == []


def test_a_malformed_item_is_reported_and_a_conforming_one_still_runs(dlq):
    """One bad file is one bad file, never the whole night."""
    dlq.item("10-good.py")
    (dlq.root / "queue" / "20-bad.py").write_text("nothing declared\n")
    good, bad = dlq.runner.queued_items()
    assert [item["name"] for item in good] == ["10-good.py"]
    assert [item["name"] for item in bad] == ["20-bad.py"]
    assert bad[0]["error"].strip()


def test_the_run_order_is_the_file_name_order(dlq):
    """Which is why every number is two digits: ``100`` sorts before ``20``."""
    for name in ("30-c.py", "10-a.py", "20-b.py"):
        dlq.item(name)
    good, _ = dlq.runner.queued_items()
    assert [item["name"] for item in good] == ["10-a.py", "20-b.py", "30-c.py"]


# --------------------------------------------------------------------------- #
# Where the file goes
# --------------------------------------------------------------------------- #


def test_a_destination_is_resolved_at_delivery_and_not_at_queue_time(dlq):
    """It names a *kind*, so changing where videos go moves the ones already
    waiting in the queue too.

    That is what makes it a default rather than a decision taken once, months
    ago, by a command you have since reconfigured.
    """
    films = dlq.root / "films"
    films.mkdir()
    item = {"dest": "video"}
    assert dlq.runner.dest_of(item) == dlq.runner.dests()["video"]
    dlq.sched.set_dest("video", str(films))
    assert dlq.runner.dest_of(item) == films


def test_an_absolute_path_in_the_header_wins_over_the_setting(dlq):
    somewhere = dlq.root / "somewhere"
    assert dlq.runner.dest_of({"dest": str(somewhere)}) == somewhere


def test_an_item_with_no_destination_stays_where_it_downloaded(dlq):
    """Hand-written items predate this and never agreed to be moved anywhere."""
    assert dlq.runner.dest_of({}) is None
    assert dlq.runner.dest_of({"dest": "  "}) is None


def test_off_the_phone_the_queue_keeps_its_own_out_directory(dlq):
    """Android's Downloads is where a phone user looks; off the phone there is
    no such folder and inventing one would be a guess."""
    assert dlq.runner.on_termux() is False
    assert set(dlq.runner.default_dests()) == set(dlq.runner.DEST_KINDS)
    assert set(dlq.runner.default_dests().values()) == {dlq.runner.OUT}


def test_a_destination_that_cannot_be_delivered_into_says_why(dlq):
    """Checked before it matters as well as when it does, because the usual
    cause is a permission never granted and finding that out at the moment of
    delivery means finding it out after the data is spent."""
    there = dlq.root / "landing"
    there.mkdir()
    assert dlq.runner.dest_problem(there) is None
    # Not there yet is not a problem in itself: a new folder inside one that
    # exists is a reasonable thing to ask for.
    assert dlq.runner.dest_problem(there / "new") is None
    # Two levels down from nothing is a typo.
    assert dlq.runner.dest_problem(there / "new" / "deeper")
    assert dlq.runner.dest_problem(dlq.root / "no" / "such" / "tree")


def test_the_folder_android_has_not_granted_yet_is_named_for_what_to_do(dlq):
    """It is *there* before ``termux-setup-storage`` has been run, and the line
    that says so is the only thing anyone can act on."""
    problem = dlq.runner.dest_problem(dlq.runner.ANDROID_DOWNLOADS / "sub")
    assert problem and "termux-setup-storage" in problem
