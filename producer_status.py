#!/usr/bin/env python3
"""Per-producer liveness ledger.

The five Monday digest producers (regime, confluence, discovery, crowding,
stocknews) keep no durable state of their own, so the weekly heartbeat can't
otherwise tell when one silently stops posting — exactly what happened when the
Stooq anti-bot wall crashed regime.py and the Capitol Trades IP-block took the
congress feed dark. Each producer records its run here; heartbeat.py reads the
ledger and flags any producer missing or older than its expected cadence.

Stdlib-only. Shared file, written only by the (single-concurrency) heartbeat job.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STATUS_PATH = Path(__file__).parent / "producer_status.json"

# The Monday digests run weekly; allow a day of grace before flagging a miss.
WEEKLY = 8


def load(path=None):
    """Return the ledger dict ({producer: {last_run, ok}}), or {} if absent."""
    path = path or STATUS_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def record(producer, ok=True, path=None):
    """Upsert this producer's {last_run: now, ok} into the ledger. Best-effort —
    a write failure is logged, never raised, so it can't break a digest run."""
    path = path or STATUS_PATH
    data = load(path)
    data[producer] = {
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": bool(ok),
    }
    try:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as e:
        print(f"[WARN] could not write {path.name}: {e}", file=sys.stderr)


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00") if s.endswith("Z") else s)
    except ValueError:
        return None


def stale(status, expected, now, max_age_days=WEEKLY):
    """Return [(producer, note)] for `expected` producers that are missing,
    older than max_age_days, or whose last run reported a failure.

    An empty ledger (the very first run, before anything has recorded) yields no
    warnings — otherwise every producer would be flagged on day one.
    """
    if not status:
        return []
    out = []
    for producer in expected:
        rec = status.get(producer)
        last = _parse(rec.get("last_run")) if rec else None
        if last is None:
            out.append((producer, "no run recorded"))
        elif (now - last).days > max_age_days:
            out.append((producer, f"last ran {last.date().isoformat()}"))
        elif not rec.get("ok", True):
            out.append((producer, "last run reported a failure"))
    return out
