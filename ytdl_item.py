"""Compatibility shim: the real ytdl_item lives in the ytq checkout.

Queue items written before the 2026-08-28 split import ``ytdl_item`` off the
queue root, because the module directory and the queue root used to be the
same directory. The migration's ``sed`` re-points their ``sys.path`` line at
this checkout — and then the import must still resolve, or every pre-split
video fails each night with a ModuleNotFoundError in its log and nothing on
the screen. This file answers that import by loading the real module from
the ytq checkout and replacing itself with it in ``sys.modules``, which the
import machinery honours: the item ends up holding the real module.

New items spell the ytq directory into their own ``sys.path`` lines and
never reach this file. Nothing in this repo imports ytdl_item either — the
shim exists for the items already on disk, and can go when the last
pre-split item has been downloaded or removed.
"""

import importlib.util
import os
import sys
from pathlib import Path

_home = os.environ.get("YTQ_HOME")
_beside = Path(__file__).resolve().parent.parent / "ytq"
_root = (
    Path(_home).expanduser().resolve()
    if _home
    else (_beside.resolve() if _beside.is_dir() else Path.home() / "ytq")
)
_spec = importlib.util.spec_from_file_location("ytdl_item", _root / "ytdl_item.py")
if _spec is None or _spec.loader is None:  # pragma: no cover - a broken clone
    raise ImportError(f"no ytdl_item.py in {_root}; clone ytq or set YTQ_HOME")
_real = importlib.util.module_from_spec(_spec)
sys.modules["ytdl_item"] = _real
_spec.loader.exec_module(_real)
