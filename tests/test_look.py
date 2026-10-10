"""The house style: what the restyle onto bewitch promises, checked off a canvas.

The screens draw through bewitch where there is a bewitch checkout — Crush's
look, ``bewitch/docs/house-style.md`` — and plainly where there is not. What
that buys is a look; what it must not cost is any of what this queue's screens
already promised, and the restyle adds a few promises of its own. So these
draw the real screens onto a bewitch ``Canvas`` the size of a terminal, with
no terminal under it, and ask the canvas:

* every part lands inside the terminal, at every width from 32 up, on the
  20-row phone and the 24-row window — a part that ran off the edge would be
  clipped by the canvas and nobody would see it go, so the window here refuses
  one instead;
* the first row is the screen's header, and the hint row is the line
  :func:`expire_ui.hint` says it is, with the way out whole on it;
* the cursor row is a filled bar, never reverse video;
* a dialog is composed over the screen it came from rather than clearing it,
  and never loses its answers or its way out, however short the screen;
* nothing on any screen is a glyph bewitch has not had drawn on the phone;
* and with no bewitch at all the same screens still draw, plainly.

Nothing here knows what any line says: the wording is the part that improves.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

MiB = 1024**2

#: The phone, either side of the tight-hint switch, the wide-row switch, and a
#: desktop. The pty tests cover 40 and 80 on a real terminal.
WIDTHS = [32, 33, 39, 40, 41, 56, 71, 72, 80, 120]
HEIGHTS = [20, 24]


@pytest.fixture
def ui(dlq):
    """``expire_ui``, drawing in the house style — or the reason it cannot."""
    if not dlq.ui.LOOK:
        pytest.skip("no bewitch checkout: the house style is not drawn here")
    return dlq.ui


class Screen:
    """A terminal-shaped window over a bewitch canvas.

    It has the slice of a curses window the screens use, and it is strict where
    a canvas is forgiving: a part drawn past the edge is recorded rather than
    clipped, because clipping is exactly how a line too wide goes unseen.
    """

    def __init__(self, ui, cols: int, rows: int, keys=b"q"):
        from bewitch.lipgloss import Canvas

        self.ui, self.cols, self.rows = ui, cols, rows
        self.canvas = Canvas(cols, rows)
        self.keys = list(keys)
        self.overflow: list[str] = []

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        self.canvas.clear()

    def addnstr(self, y, x, text, n, attr=0):
        from bewitch.lipgloss import Block, truncate

        self.canvas.compose(Block.text(truncate(text, n)), x, y)

    def draw(self, y, x, block):
        if x < 0 or y < 0 or x + block.width > self.cols or y + block.height > self.rows:
            self.overflow.append(f"{block!r} at ({y}, {x}) on {self.cols}x{self.rows}")
        self.canvas.compose(block, x, y)

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def keypad(self, flag):
        pass

    def move(self, y, x):
        pass

    def lines(self) -> list[str]:
        return self.canvas.render().plain().split("\n")

    def cells(self, y: int):
        return self.canvas.render().rows()[y]


class Plain(Screen):
    """The same window with no ``draw``: plain curses, as far as a screen can tell."""

    draw = None


def paint(ui) -> dict:
    """The plain fallback's attributes, without asking a terminal for colour."""
    return {tone: attr for tone, (_, _, attr) in ui.TONES.items()}


def stocked(dlq):
    """One of everything the listing draws differently."""
    long = "a-really-quite-long-name-for-a-download-that-somebody-queued"
    dlq.item(f"10-{long}.py", cap=6 * 1024 * MiB, desc=f"{long} {long}")
    dlq.item("20-small.py", cap=MiB, desc="a short one")
    dlq.item("25-middling.py", cap=300 * MiB, desc="partly fetched")
    dlq.item("30-failed.py", where="failed", desc="this one failed")
    dlq.item("40-done.py", where="done/2026-09-01", desc="this one finished")
    (dlq.root / "queue" / "50-broken.py").write_text("# nothing declared here\n")
    work = dlq.root / "work" / "25-middling.py"
    work.mkdir(parents=True, exist_ok=True)
    (work / "part.bin").write_bytes(b"x" * 4096)
    dlq.state({"30-failed.py": {"attempts": 2}, "20-small.py": {"attempts": 1}})
    return dlq.sched.items()


class Queue:
    """The slice of :class:`expire_ui.Queue` the drawing reads, with no threads."""

    def __init__(self, rows, facts):
        self.rows = rows
        self.tonight = SimpleNamespace(facts=facts, note="")
        self.live = ""
        self.running = SimpleNamespace(name="", opened="", alive=False, askable=False)

    def live_line(self, width):
        return ""

    def mine(self):
        return False

    def place(self, name):
        queued = [row["name"] for row in self.rows if row["where"] == "queued"]
        return (queued.index(name) + 1, len(queued)) if name in queued else None


def every_screen(dlq, ui, win, cursor=1):
    """Draw each screen in turn on *win*, handing back ``(name, hint, lines)``.

    *hint* is the hint set the screen's foot carries, or ``None`` for a screen
    whose keys are inside a dialog.
    """
    rows = stocked(dlq)
    facts = dlq.facts(portal=dlq.reading(free=400 * MiB), force=True)
    queue = Queue(rows, facts)
    ink = paint(ui)
    job = [("nightly job", "armed, fires every 15m", "32")]
    said = "auto: off — the nightly job fires and does nothing; run-now still works"
    queued = next(row for row in rows if row["where"] == "queued")
    shots = []

    class Shots(list):
        def append(self, shot):
            # Each screen with what ran off its own edges, and nothing else's.
            super().append((*shot, list(win.overflow)))
            win.overflow.clear()

    shots = Shots()

    ui.draw_list(win, ink, queue, cursor, 0, "")
    shots.append(("listing", "list", win.lines()))
    ui.draw_list(win, ink, queue, 0, 0, "", moving=queued["name"], pos=1)
    shots.append(("moving", "moving", win.lines()))
    row, item = ui.phantom_of("60-new-video.py", 200 * MiB, True)
    ui.draw_list(win, ink, queue, 0, 0, "", moving=row["name"], pos=0,
                 phantom=(row, item))
    shots.append(("pick", "pick", win.lines()))
    for each in rows:
        ui.draw_item(win, ink, queue, each, dict(ui.actions_for(each)), "",
                     "a flash", False, None)
        shots.append((f"item {each['name']}", "item", win.lines()))
    ui.draw_settings(win, ink, job, said, "")
    shots.append(("settings", "settings", win.lines()))
    ui.draw_dest(win, ink, said, "")
    shots.append(("dest", "dest", win.lines()))
    ui.draw_list(win, ink, queue, cursor, 0, "")
    ui.draw_wait(win, ink, " run the queue now ", "asking zwana what tonight has…", 3, 7)
    shots.append(("wait", None, win.lines()))
    ui.draw_list(win, ink, queue, cursor, 0, "")
    ui.confirm(win, ink, " run the queue — NOT COUNTED ", ui.run_note({**facts,
               "blind": True}))
    shots.append(("confirm", None, win.lines()))
    ui.draw_item(win, ink, queue, queued, dict(ui.actions_for(queued)), "", "",
                 False, None)
    ui.confirm(win, ink, " remove ", ui.removal_note(queued), ui.removal_answers(queued))
    shots.append(("remove", None, win.lines()))
    win.keys = [27]  # esc, which leaves the field
    ui.ask(win, ink, "rename", "name — what it is listed and logged as", "small",
           "lowercase, dashes for anything else; ctrl-U clears the field.")
    shots.append(("ask", None, win.lines()))
    log = dlq.root / "logs" / "runner.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(f"line {n} of a long log " * 4 for n in range(60)))
    win.keys = [ord("q")]
    ui.file_screen(win, ink, log)
    shots.append(("log", "log", win.lines()))
    return shots


# --------------------------------------------------------------------------- #
# Every screen, at every width
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("height", HEIGHTS)
@pytest.mark.parametrize("width", WIDTHS)
def test_every_screen_lands_inside_its_terminal(dlq, ui, width, height):
    """No part runs off the edge, every screen opens on its header, and the
    hint row is the line ``hint`` says — the way out on it, whole."""
    win = Screen(ui, width, height)
    for name, hinted, lines, overflow in every_screen(dlq, ui, win):
        assert not overflow, (name, overflow)
        assert len(lines) == height
        assert lines[0].startswith(f" {ui.NAME} "), (name, lines[0])
        if hinted:
            assert lines[-2].strip() == ui.hint(hinted, width), (name, lines[-2])
            key, what = ui.hint_pairs(hinted, width)[-1]
            assert lines[-2].rstrip().endswith(f"{key} {what}".strip()), name


@pytest.mark.parametrize("width", WIDTHS)
def test_the_screens_are_told_apart_by_their_header(dlq, ui, width):
    """No reverse-video title bar any more, so the header is what says which
    screen is up — to a person, and to the pty tests."""
    win = Screen(ui, width, 24)
    heads = {name: lines[0] for name, _, lines, _ in every_screen(dlq, ui, win)}
    assert "queue" in heads["listing"]
    assert "moving" in heads["moving"] and "moving" in heads["pick"]
    assert "settings" in heads["settings"]
    assert "queue" not in heads["item 20-small.py"]
    assert "small" in heads["item 20-small.py"]


@pytest.mark.parametrize("width", [32, 40, 80])
def test_the_cursor_row_is_a_filled_bar_and_never_reverse_video(dlq, ui, width):
    """Both lines of a two-line row, as wide as the listing, in ``primary``."""
    win = Screen(ui, width, 24)
    rows = stocked(dlq)
    facts = dlq.facts(portal=dlq.reading(free=400 * MiB), force=True)
    ui.draw_list(win, paint(ui), Queue(rows, facts), 1, 0, "")
    barred = [
        y for y in range(24)
        if all(pen.bg == ui.THEME.primary for _, pen in win.cells(y)[: width - 1])
    ]
    assert barred, win.lines()
    # The whole row and only the row: one entry's lines, next to each other.
    assert barred == list(range(barred[0], barred[0] + len(barred)))
    drawn = dict(ui.compose_rows(rows, width))
    assert len(barred) == len(drawn[1])
    assert "small" in win.lines()[barred[0]]
    for y in range(24):
        assert not any(pen.reverse for _, pen in win.cells(y)), win.lines()[y]


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", [32, 40, 80])
def test_a_dialog_is_drawn_over_the_screen_and_not_instead_of_it(dlq, ui, width):
    """What was behind a confirm is still behind it: the header it came from,
    and the item's own hint row under the box."""
    win = Screen(ui, width, 24)
    rows = stocked(dlq)
    facts = dlq.facts(portal=dlq.reading(free=400 * MiB), force=True)
    queue = Queue(rows, facts)
    row = next(row for row in rows if row["where"] == "queued")
    ui.draw_item(win, paint(ui), queue, row, dict(ui.actions_for(row)), "", "",
                 False, None)
    before = win.lines()
    assert ui.confirm(win, paint(ui), " remove ", ui.removal_note(row),
                      ui.removal_answers(row)) == ""
    after = win.lines()
    assert after[0] == before[0]
    assert after[-2] == before[-2]
    boxed = [line for line in after if "╭" in line or "╰" in line]
    assert len(boxed) == 2


def answer_lines(body):
    return ["".join(text for text, _, _ in line) for line in body]


words = st.lists(
    st.text(alphabet="abcdefghij", min_size=1, max_size=9), min_size=1, max_size=14
).map(" ".join)


@given(
    lines=st.lists(words, min_size=1, max_size=7),
    two=st.booleans(),
    inner=st.integers(16, 56),
    rows=st.integers(1, 24),
)
def test_a_confirm_never_loses_its_answers_or_its_question(dlq, lines, two, inner, rows):
    """However short the screen, every answer that has words is on the body,
    whole and in order, and so is the question's first line. A confirm whose
    answers are cut off is one on which every key means no."""
    ui = dlq.ui
    answers = [("y", "remove it, keep the 3 MiB"), ("b", "remove it and delete it")]
    answers = answers if two else [("yb", "remove it")]
    body = ui.confirm_body(lines, answers, inner, rows)
    said = answer_lines(body)
    picks = [ui._fit(f"{' or '.join(keys)}  {label}", inner) for keys, label in answers]
    assert said[-len(picks):] == picks
    assert said[0].startswith(ui._wrap(lines[0], inner)[0].split()[0])
    assert body[0][0][2] is True  # the question, in bold
    assert len(body) <= max(rows, 1 + len(picks))
    for text in said:
        assert len(text) <= inner


def test_a_confirm_with_one_plain_answer_lists_none(dlq):
    """``y`` with no words is "y do it" in the keys row, not a line of its own."""
    body = dlq.ui.confirm_body(["stop the nightly job?"], [("y", "")], 30, 10)
    assert answer_lines(body) == ["stop the nightly job?", ""]


@pytest.mark.parametrize("width", range(32, 121))
def test_the_dialogs_keys_fit_inside_the_box(dlq, width):
    """A confirm's keys are drawn inside a box narrower than the screen, and
    the way out is still whole there."""
    ui = dlq.ui
    inner = ui.dialog_width(width) - 4
    for name in ("confirm", "confirm-two"):
        pairs = ui.hint_pairs(name, width)
        drawn = ui.help_text(pairs, inner)
        key, what = pairs[-1]
        assert drawn.endswith(f"{key} {what}".strip()), (name, drawn)
        assert len(drawn) <= inner


# --------------------------------------------------------------------------- #
# The parts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", range(32, 121))
def test_every_hint_keeps_its_way_out_whole_at_every_width(dlq, width):
    """``short_help`` gives up the pair that does not fit, and the last pair is
    the way out — so every set is measured to fit whole, dots or no dots."""
    ui = dlq.ui
    for name in ui.HINTS:
        pairs = ui.hint_pairs(name, width)
        drawn = ui.hint(name, width)
        assert len(drawn) <= width - 2, (name, drawn)
        for key, what in pairs:
            assert f"{key} {what}".strip() in drawn, (name, drawn)


@pytest.mark.parametrize("title", ["queued", "tonight ends here: 1.20 GiB",
                                   "nothing tonight: no portal reading", "x" * 90])
@pytest.mark.parametrize("info", ["", "3", "120"])
def test_a_rule_is_measured_as_the_section_that_draws_it(ui, title, info):
    """The listing is laid out — and its layout tested — as text; the text of a
    rule is ``bubbles.section``'s own, so what was measured is what is drawn."""
    for room in range(1, 100):
        drawn = ui.bubbles.section(title, room, info or None).plain().rstrip()
        assert ui._section_text(title, room, info).rstrip() == drawn, room


@pytest.mark.parametrize("width", [32, 40, 80])
def test_the_headings_are_rules_and_still_headings(dlq, width):
    """``queued ──── 3`` and the cut line are both :class:`Rule` headings —
    index ``None``, so the cursor steps over them — and say which they are."""
    rows = stocked(dlq)
    facts = dlq.facts(portal=dlq.reading(free=400 * MiB), force=True)
    order = [row["name"] for row in rows if row["where"] == "queued"]
    planned = dlq.ui.tonight_plan(order, facts)
    cut = dlq.ui.cut_index(order, facts, width, planned)
    entries = dlq.ui.compose_rows(rows, width, "", cut, planned)
    headings = [lines[0] for index, lines in entries if index is None]
    assert all(isinstance(text, dlq.ui.Rule) for text in headings)
    assert [text.kind for text in headings].count("cut") == 1
    assert {text.title for text in headings if text.kind == "group"} == {
        "queued", "failed", "done"
    }
    for text in headings:
        assert len(text) == width - 1


def test_the_bar_is_bewitchs_and_never_claims_a_finish(ui):
    """``bubbles.progress_bar``, exactly as wide as it was asked, in its own
    two glyphs — tested on the phone, each one column — and short of 100% until
    the last byte is there. No total, or no room, is no bar."""
    allowed = set(ui.glyphs.BAR_FULL + ui.glyphs.BAR_EMPTY + "0123456789% ")
    for width in range(12, 80):
        for have in (0, 1, 50, 99, 995, 999):
            bar = ui.bar_block(have, 1000, width)
            assert bar.width == width
            assert set(bar.plain()) <= allowed
            assert "100%" not in bar.plain()
        assert ui.bar_block(1000, 1000, width).plain().endswith("100%")
    assert ui.bar_block(3, 0, 40) is None
    assert ui.bar_block(3, 4, 11) is None


def test_every_tone_has_a_role_in_the_theme(ui):
    """A tone with no role would raise on the first row worth that colour."""
    for tone, (role, _, _) in ui.TONES.items():
        assert getattr(ui.THEME, role) is not None, tone
    for tone, (mark, mark_tone, text_tone, sub_tone) in ui.ROW_LOOK.items():
        assert tone in ui.TONES
        assert {mark_tone, text_tone, sub_tone} <= set(ui.TONES)
        if mark:
            assert mark in ui.glyphs.GLYPHS


@pytest.mark.parametrize("width", [32, 40, 80])
def test_nothing_drawn_is_a_glyph_the_phone_has_not_shown(dlq, ui, width):
    """Only glyphs bewitch has had drawn on the phone: its glyph table, the
    rounded border and the spinner its recipes draw with — and the text's own
    punctuation, which is the wording's. Never ``⚠`` or ``✗``, never one of
    ``glyphs.EXCLUDED``."""
    from bewitch.lipgloss import rounded_border

    border = rounded_border()
    allowed = {glyph for glyph, _ in ui.glyphs.GLYPHS.values()}
    allowed |= set(border.top + border.bottom + border.left + border.right
                   + border.top_left + border.top_right + border.bottom_left
                   + border.bottom_right)
    allowed |= set("".join(ui._spinner.Dot.frames))
    allowed |= {"—", "≤"}
    win = Screen(ui, width, 24)
    drawn = "".join(
        "".join(lines) for _, _, lines, _ in every_screen(dlq, ui, win)
    )
    strange = {ch for ch in drawn if ord(ch) > 127} - allowed
    assert not strange
    assert not set(drawn) & (set(ui.glyphs.EXCLUDED) | {"⚠", "✗"})


# --------------------------------------------------------------------------- #
# Without bewitch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", [32, 40, 80])
def test_with_no_bewitch_every_screen_still_draws_plainly(dlq, monkeypatch, width):
    """No bewitch checkout: the standard library's curses, and every screen as
    it was — the same rows, the same keys, the hints joined by two spaces."""
    ui = dlq.ui
    monkeypatch.setattr(ui, "LOOK", False)
    win = Plain(ui, width, 24)
    for name, hinted, lines, _ in every_screen(dlq, ui, win):
        if hinted:
            assert lines[-2].strip() == ui._plain_help(ui.hint_pairs(hinted, width))
            assert ui.hint(hinted, width) == lines[-2].strip(), name
    heads = {name: lines[0] for name, _, lines, _ in every_screen(dlq, ui, win)}
    assert "settings" in heads["settings"]
    assert "queue" in heads["listing"] and "moving" in heads["moving"]


@pytest.mark.parametrize("width", [32, 40, 80])
def test_a_window_with_no_draw_gets_the_text_of_every_part(dlq, ui, width):
    """``_put`` is the one place a part meets the window; a window that cannot
    compose a bewitch block is written its plain text instead, and every screen
    still opens on its header and ends on its keys."""
    win = Plain(ui, width, 24)
    for name, hinted, lines, _ in every_screen(dlq, ui, win):
        assert lines[0].startswith(f" {ui.NAME} "), (name, lines[0])
        if hinted:
            assert lines[-2].strip() == ui.hint(hinted, width), name


@pytest.mark.parametrize("width", [32, 40])
def test_the_picker_is_the_listing_holding_a_phantom(dlq, ui, width):
    """ytq's ``p``: dlq's own listing, in its own header, with the video that
    does not exist yet in the air — and ⏎ takes the place it was moved to."""
    queued = sum(row["where"] == "queued" for row in stocked(dlq))
    win = Screen(ui, width, 24, keys=[ui.curses.KEY_UP, 10])
    # It opens last, where the video would go if nobody said, and ↑ is one up.
    assert ui.pick_place(win, "60-new-video.py", 200 * MiB, True) == queued - 1
    lines = win.lines()
    assert lines[0].startswith(f" {ui.NAME} ") and "moving" in lines[0]
    assert lines[-2].strip() == ui.hint("pick", width)
    assert not win.overflow
