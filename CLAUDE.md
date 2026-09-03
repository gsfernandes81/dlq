# dlq — working notes for Claude

The pre-midnight expiring-quota download queue. Split out of the `or3`
monorepo's `termux/expire/` on 2026-08-28, together with its siblings `ytq`
(the YouTube front end) and `zwana-quota` (the portal client and quota widget
the runner reads its guard figures through). **Every byte the runner spends
crosses the phone's metered mobile radio** — the whole design is about
spending allowance that would expire anyway, and never more.

Modules find the sibling checkouts as checkouts, never as installed packages:
`$EXPIRE_HOME` / `$YTQ_HOME` / `$ZWANA_HOME` first, then a clone beside this
repo, then `~/<name>`. `expire_sched._zwana_root` predicts the runner's
resolution so `dlq status` can name a missing checkout instead of letting an
import traceback.

Decisions that travel with this code — each was arrived at the hard way:

- **`dlq` typed on its own opens the screen** — `default_action`, whose other
  half is the half to keep pinned: with no terminal (a pipe, a script, ssh
  with no tty) it is `status`, which is what a bare `dlq` always printed.
- **`dlq <url>` queues a direct file download**, uniform with `ytq <url>`
  (2026-08-28; it was a separate `dlq` command, then briefly a verb). A URL
  is routed before the verbs — no verb contains `://` — and `dlq.py` keeps
  its own module and flags; the dispatch in `expire_sched.main`
  is only the door.
- **The listing is the whole queue.** The old second-level queue screen is
  gone (2026-09-02) — `dlq`'s listing is the only screen there is, and its
  legend keys (`n` run now, `s` settings, `l` runner log) and the settings
  page's (`d` destinations, `j` arm/cancel) reach everything that screen used
  to. Every verb that lives at both ends is still **one function called by
  both** — `do_arm`, `do_cancel`, `set_dest`, `queue_run_argv` — because a
  screen and a command that disagree about whether arming worked leave
  nobody able to tell. It is also **ytq's screen**: `pick_place` is this
  listing holding a phantom — a video not written yet, drawn and *planned*
  through the same `preview`/`tonight_plan`/`cut_index`/`compose_rows` the
  main screen uses (`holding` is the one held-item loop, `m` and the picker
  both), so the cut line answers with the new item counted in the place it is
  being dragged to; it writes nothing, and `place` takes the place afterwards
  through `do_reorder`.
- **`run-now` carries `--force`**, and that is what makes now mean now: it
  overrides the clock gate and nothing else — the floor, the per-item caps and
  the portal reading still decide everything they decided.
- **Stopping a run from the screen is SIGINT, never SIGTERM**
  (`Firing.SIGNAL`): the item downloads in a session of its own, so what stops
  the download is the runner *unwinding* — `run_item` kills the item's tree on
  the way past — and only an interrupt does that. SIGTERM leaves yt-dlp
  spending data with nothing watching it.
- **`dlq run-now --blind`** is the bargain when `ic.zwana.io` cannot be
  reached at all: every guard the runner carries is a portal figure, so a
  blind run has none and is bounded by what the items declared
  (`blind_budget`, called by the front end rather than re-derived). **Nothing
  interrupts a blind download for the time** — a run cut short would only buy
  the same bytes again; `NO_DEADLINE` is the one spelling of "no stop time".
  A blind run that finds the portal up is an ordinary run with the floor
  intact.
- **`dlq status` leads with the verdict**, and the verdict is
  `expire_runner.gate()` — the same function the firing gates on, so a screen
  saying "waiting for the window" cannot be a night the runner stopped for
  some other reason. A test must pin the *order* the gate answers in.
- **`dlq ui` is the only thing in the queue that changes anything**, and an
  item is *four* things — the file in `queue/`, the partial in `work/<item>/`,
  anything finished in `out/<item>/`, and its record in `state.json`.
  `expire_ui.belongings` is the only place that decides that; every move and
  removal is spelled from it.
- **The order is a position, never a number** — always **two digits**, because
  the runner sorts *file names*: `100` sorts before `20`. `ytq.next_number`
  caps at `MAX_PRIORITY` for the same reason.
- **Removing keeps what was paid for.** The confirm's keys never vary with the
  item (`removal_answers` guarantees it). A done download whose file was
  deleted removes itself, gated on `_readable` — an `os.listdir`, because
  Android's Downloads *is there* and raises before `termux-setup-storage`.
  Unreadable is `away` and left alone; only `gone` is acted on.
- **What `n` says depends on where the phone is** —
  `expire_runner.portal_reachable`, a 1.5 s TCP connect to `ic.zwana.io`;
  the portal is on the vessel's network, so it answering *is* "on vessel
  wifi".
- **Nothing is queued twice** — items record `# SOURCE:` and `ytq.write_item`
  is the one door that refuses a duplicate; `dlq` goes through it too.
- **Destinations**: `dlq dest` holds them; the item always downloads into
  `out/<item>/` and the *runner* moves it at delivery (`shutil.move`, never a
  rename — shared storage is a different filesystem), so an unreachable
  destination leaves the file safe.
- **Both commands anchor to the checkout, not `__file__`** — `ytq._root()`
  resolves the queue root (this repo) and `dlq` takes the same answer, so an
  installed copy still manages the real queue; the alternative is a nightly
  job firing faithfully onto an empty queue, saying nothing. The Installing
  section of `docs/download-queue.md` explains it.

- **A flick scrolls; only a keypress spends** (2026-08-28). The listing
  takes wheel events — which is what Termux turns a touch drag into — through
  `ytq.enable_touch_scroll`/`ytq.read_wheel` (one copy, in ytq): the cursor
  in the list, the held item's position while moving. Wheels only, on
  purpose: a tap must never press a key on a screen whose keys fire
  downloads.
- **One reading feeds the word, the figures and the bar** (2026-08-28). The
  item screen showed a download's size twice — the head from `row["have"]`,
  counted off the disk when the listing was read, and the foot from the
  download's own `.status.json`, read fresh at draw time — so a second apart
  they disagreed and nothing said which was stale. `item_screen` now asks
  once per draw and hands the same reading to `_with_live` (head) and
  `progress_bar` (foot), and the foot is a bar rather than a second copy of
  the numbers. A live line naming a *different* item stays: that is the only
  place that says so. Two rules travel with it: **`where` is the directory,
  not the activity** — an item being downloaded is still in `queue/`, which
  is why the head said "queued" over a download in flight, so the word is
  now chosen by `Queue.downloading` while `where` goes on deciding
  everything else; and **an unstated total is never invented**, because
  `_of` prints the declared cap with a `≤` until a server states a size.
  The bar is `=` against `·` like `quota_widget`'s, never block glyphs —
  they are East-Asian ambiguous width and a phone may draw them double.
- **`dlq dump [NAME]` is the bug report** (2026-08-28): environment, sibling
  resolution, gate verdicts, state rows, failing items' heads and the newest
  logs, every section guarded so it finishes on the broken trees it exists
  for. And **the queue root carries a `ytdl_item.py` shim**: pre-split items
  import ytdl_item off the queue root, the real module moved to the ytq
  checkout, and the shim replaces itself in `sys.modules` with the real one —
  it can go when the last pre-split item is gone.
- **`dlq settings` / `s` on the main screen** (2026-09-02): the window, the
  reserve, `reserve-when-paid`, `paid-min`, `auto` and `notify-blocked` live
  in `config.json` beside the destinations, and are read at the moment they
  are used, never cached — `expire_runner.SETTINGS` is the one spec, and both
  front ends read it through the runner rather than keeping a second copy of
  the names, the defaults or the rules. **`off` is the first thing `gate()`
  asks**, ahead of the empty queue, because it is the answer to "why did
  nothing happen tonight" on every night it is off; `run-now`'s `--force`
  steps over it exactly as it steps over the clock, because the switch is a
  *when*, not a guard over money — the reserve, the per-item caps and the
  portal reading answer to nobody's `--force`. A value found in `config.json`
  that fails its rule reads as the default rather than raising: this is read
  at the top of a firing nobody is watching, and a stray character typed into
  the file must not be able to stop a night's downloads or take out the
  reserve on its way past — `dlq settings` and `dlq dump` name what they
  declined instead. **A change stays on the page and is said there**: the
  screen used to return on the first one, which put somebody back on the
  listing they had not asked for, one keypress into a page of six settings,
  with the change's own sentence — 67 columns of it — clipped onto the row the
  legend keys sit on. Both pages stay now, redraw, and say what happened in a
  **said area** under the rows (`said_lines`, wrapped, in the flash tone); the
  foot goes on carrying the hints and nothing but each screen's own one-line
  refusals, so no sentence is ever both clipped and whole. What comes back is
  the **receipts** — a list, `dest_screen`'s handed up to `settings_screen`
  and `settings_screen`'s extending the listing's — and the tonight reading is
  re-asked once, on the way out, rather than at every change. **Reading a
  broken `config.json` is forgiving; writing over one is refused** —
  `load_config` answers a file that will not parse with an empty dict, so a
  save on top of it would be a fresh file holding only the new key, with the
  destinations and the other settings gone under a line saying it worked.
  `config_problem` is the one line that says so:
  `set_setting` and `set_dest` refuse with it and write nothing, and both
  screens and the dump print it. Six settings do not fit a 20-row phone, so
  `settings_body` — the one rule, checked rather than trusted — gives up the
  blank lines between the blocks first, then the grey line saying what each
  setting means, and last the said area, never a setting's name, its value or
  a red line naming a stored value being ignored. The said area goes last of
  the three and not first: dropping it first would leave the 20-row phone that
  most needs the sentence the one screen that never shows it. And **whether a
  stored value is in force is one function's answer** — `setting_state`, keyed
  on the key being *present*, because the three places that show the settings
  each used to decide it and a file holding `null` read as refused on the
  screen and as nothing at all from the command. The reserve is waived on
  `paid.left_bytes` alone, never on
  `free.left_bytes`, because `paid.left_bytes` can only understate what is
  actually paid for while `free.left_bytes` can only overstate what is
  actually free — "there is paid data" is the one direction that reading
  cannot be wrong about, so it is the only reading the waiver asks.
  **`paid-min` defaults to 0, which is that same question**
  (`reserve_waived`'s threshold floors at one byte, so nought means "any paid
  data at all" and the waiver behaves exactly as it did before there was a
  figure); a figure above it is the person's own judgement that their last few
  MB of paid data are not worth the reserve they would stand down, and because
  the reading understates, wanting "at least this much" can only ever keep the
  reserve for longer. It qualifies `reserve-when-paid` and does nothing on its
  own, which the setting's own sentence says rather than a refusal saying it.
  **`notify-blocked` covers the blocked-firing notification and no other** —
  that one repeats every ~15 minutes on a phone off the vessel's wifi, which
  is how a person learns to ignore notifications, while a malformed item and
  an item that has run out of nights each happen once and still need somebody;
  off leaves the log line and `dlq status` exactly as they were, and the
  switch is asked in `say_blocked`, not inside `notify`. And the window is a
  multiple of 15 minutes because that is JobScheduler's own floor for a
  periodic job — a window that is not a multiple of one would buy nothing
  beyond the nearest firing below it.
- **The cut line in the queued list is computed, never an item** (2026-09-02).
  `expire_runner.admit()` is the one admission rule — what a single item may
  take in a firing, and why not — and `fire()` calls it instead of carrying
  its own copy of the arithmetic; `plan()` is the one projection, walking a
  night of firings against `admit()` the same way `fire()` would. The sum
  `plan()` gives back is never more than the budget it was given, for *any*
  ordering of the items — a test must prove it by permutation rather than
  trusting one order to stand for all of them. Reordering the queue moves the
  line, never the budget: the example to pin is the user's own one-keypress
  one, queue `one, two | three` becoming `one, three | two` after moving
  `three` up once — the line still falls after exactly as much as the budget
  allows, just between a different pair of names. The projection assumes
  every firing lands, which is the same bias the README already names for
  the pre-midnight window itself — Doze can still defer a firing the plan
  counted on. Two things the line cannot say on its own, and both are said
  beside it: **the item the line falls inside of** carries `· 46 MiB tonight`
  on its own row (`_tonight_share`, off the same projection the line is drawn
  from — one plan per draw, handed to both — and on a line of its own where a
  phone cannot hold it beside the figures), because a resumable download
  above the line is not the same thing as one that finishes tonight; and
  **why there is nothing** is answered from as far up as it goes — a verdict
  that stops the night, then `_nothing_to_spend` where there is no budget at
  all, and only then the first item's own refusal, since with nothing to
  spend every item is refused and the first refusal blames the item for a
  portal that never answered.

## Checks

`make test` (pytest) = `make check` (`.githooks/checks.sh`, the one copy; the
pre-push hook runs it). Offline, and they need the sibling checkouts. About
half a minute; `make mutants` is the slow half and is deliberately not in it.

**The suite has one seam and it is the queue root.** Every module here anchors
to a checkout rather than to `__file__` — that is what keeps an installed `dlq`
managing the real queue — so the only honest way to test one is to build a
checkout somewhere else and run the modules out of *it*. `tests/conftest.py`
copies this checkout's five modules into a temporary directory, points
`EXPIRE_HOME` and `HOME` at it and imports them there, so `expire_runner.ROOT`,
`ytq.HERE` and every path spelled from them land under `tmp_path`. `HOME` is
what keeps the suite offline: `zwana_quota` reads its credentials from
`$HOME/zwana-quota/.env` and its cookie from `$HOME/.cache`, so with neither
there the portal call fails at the credentials, before a socket is opened —
every test that needs a reading builds one by handing raw portal figures to
`quota_widget.derive`, the function that builds the real one. The import is
done once for the session and the tree is *emptied* between tests rather than
rebuilt, because the import is the expensive part and every path the modules
spelled is a constant; an autouse fixture asserts the real `config.json`,
`state.json` and `queue/` are untouched afterwards, which is the check that a
stray `ROOT` would fail.

**A screen is checked on a screen.** The front ends *are* their screens, and
the failures worth catching there — a page that leaves when it should stay, a
sentence clipped over the keys, a move that says it happened and renames
nothing — do not show in a return value. `tests/test_screens.py` and
`tests/test_settings_screen.py` open the real `dlq ui` on a pty and read it
back through `pyte`, and assert on the *shape*: which screen is up by its title
bar, that a row for a download exists, that the legend keys are on the key row,
that after `m ↓ ⏎` the file on disk has a smaller number than its neighbour.
`pyte` answers neither `CSI S` nor `CSI T`, which is how ncurses scrolls a
region to reuse lines, so `tests/_pty.py` teaches it both: without them the
emulator keeps text a terminal has already scrolled away and the assertions
read a page nobody was ever shown. They are marked `tui` and are most of the
suite's wall clock.

**What is pinned is behaviour, never wording.** A reworded refusal, a
rearranged screen and a renamed helper are all things this code does; a test
that fails on one of them is a test that has to be rewritten to say the same
thing again. So the assertions are on properties and round-trips: the sum a
projection promises against the budget it was given, for every permutation and
over random nights (`hypothesis`); a figure spelled and parsed back; every line
of every screen fitting the terminal at any width from 32 up; every download
appearing on the listing exactly once. Where a sentence *is* the behaviour —
`admit`'s refusal, quoted word for word on the cut line — what is checked is
that there is one and that it is the same one, not what it says.

One file per guarantee, and each says which:

| file | what it pins |
|---|---|
| `test_tonight_budget.py` | the sum `plan()` promises is never more than the budget, for any ordering; every refusal `admit()` gives has a sentence on it |
| `test_gate.py` | the order `gate()` answers in — two things wrong at once, and which is reported |
| `test_firing.py` | `fire()` admits through `admit()`; SIGINT and not SIGTERM; an item leads its own session |
| `test_settings.py` | a bad stored value reads as the default; a broken `config.json` is never written over; what waives the reserve |
| `test_clock.py` | the clock pinned and the timezone swept either side of the date line |
| `test_downloader.py` | `expire_dl` against a loopback server: a file smaller than one chunk is *fetched* |
| `test_listing.py` | the reader: `gone` vs `away`, and every download listed once |
| `test_ui_moves.py` | the moves and removals on a real queue: bytes never lose their item |
| `test_cut_line.py` | the cut line is computed, drawn as a heading, and moves without moving the budget |
| `test_layout.py` | every line fits, at 32, 40, 80 and swept between |
| `test_cli.py` | what a bare `dlq` does, and where each word goes |
| `test_item_contract.py` | every way an item can fail to declare itself is a reason and never an exception |
| `test_dump.py` | `dlq dump` finishes on the broken trees it exists for |
| `test_screens.py`, `test_settings_screen.py` | the two screens, on a pty |

**`make mutants` is the suite's own check.** poodle changes one operator,
literal or comparison at a time and runs the tests against it; a mutant that
survives is a line nothing was asserting anything about, which is the question
coverage cannot answer. `poodle_config.py` carries the whole arrangement and
the reasons — the flat layout, the sibling checkouts a worker's copy cannot
find for itself, and the copy filters that keep this device's `state.json` and
`config.json` out of a run that exists to be independent of them. The curses
event loops in `expire_ui.py` are fenced `# nomut: start` / `# nomut: end`: a
mutant inside one does not fail a test, it hangs a terminal until the timeout
kills it, and what those loops do is checked under a pty instead. It is a
**ratchet and never a gate** — some mutants are equivalent, and a good few are
a message reworded, which this suite declines to pin on purpose — so
`--fail_under` sits under the score already reached and goes up when the score
does. It is out of `make test` and out of the pre-push hook because it takes
hours, and a push here is a deploy.

The whole run is the ratchet; the way to get an *answer* about one module is a
scoped one, because the command is run once per mutant and most of the suite
does not touch most of the code. `POODLE_TESTS` narrows it, and pairs with
poodle's own `--only`:

```
POODLE_TESTS="tests/test_gate.py tests/test_tonight_budget.py" \
    uv run --group mutants poodle --only expire_runner.py
```

Same mutants, same verdicts, a fraction of the wall clock — and a survivor
list short enough to read.

Two things that fail for reasons that are not regressions: a **long checkout
path**, because the front ends check that every line fits 32 columns and the
root is on the status screen; and a **missing sibling checkout**, because the
modules import across them the same way a real run does.
