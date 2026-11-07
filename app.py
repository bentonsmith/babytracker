"""babytracker — a small self-hosted newborn care log.

Tracks bottle feedings (amount in oz/ml), diaper changes (wet + solid +
stool color), sleep timers, tummy-time timers, growth measurements
(weight/length/head circumference/temperature), medication doses, and
free-text notes. Data lives in SQLite;
everything is stored as UTC ISO timestamps and rendered in the browser's
local time.

API for programmatic logging (see DEPLOYMENT.md):
  POST /api/events        {"type": "diaper", "details": {"wet": true}}
  POST /api/events/{id}/stop
  PATCH /api/events/{id}
  DELETE /api/events/{id}
  GET  /api/events?days=7
  GET  /api/summary?hours=24
  GET  /api/report?days=30            (or ?start=YYYY-MM-DD&end=YYYY-MM-DD)
  GET  /api/export.csv?days=30        (same params; CSV download)
  GET  /report                        (full-history page)
"""

import base64
import csv
import io
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, time as d_time, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "baby.db")
WRITE_LOCK = threading.Lock()

TYPES = {"feed", "diaper", "sleep", "note", "tummy", "measurement", "medication"}
UNITS = {"oz", "ml"}
COLORS = {"black", "dark_green", "green", "yellow", "brown", "pale", "other"}
OPENABLE_TYPES = {"sleep", "tummy"}
MEASUREMENT_KINDS = {"weight", "length", "head_circumference", "temperature"}
MEASUREMENT_UNITS = {
    "weight": {"lb", "kg", "oz"},
    "length": {"in", "cm"},
    "head_circumference": {"in", "cm"},
    "temperature": {"F", "C"},
}
MEASUREMENT_KIND_LABELS = {
    "weight": "Weight", "length": "Length",
    "head_circumference": "Head", "temperature": "Temp",
}
ALLOWED_META = {"baby_name", "birth_date"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad timestamp: {s!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params=()):
    conn = get_conn()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with tx() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                ts_start TEXT NOT NULL,
                ts_end TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # client_id: idempotency key from the phone's offline outbox, so a
        # retried POST after a dropped connection can never duplicate an entry.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "client_id" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN client_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS events_client_id "
            "ON events(client_id) WHERE client_id IS NOT NULL"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS push_subs (
                endpoint TEXT PRIMARY KEY,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        # One row per medication event we have already reminded about, so a
        # 30-second tick can never double-notify.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS med_reminders (
                event_id INTEGER PRIMARY KEY,
                sent_at TEXT NOT NULL,
                delivered INTEGER,
                total INTEGER
            )"""
        )


init_db()

app = FastAPI(title="babytracker", docs_url="/api/docs")


def event_to_dict(row) -> dict:
    end = row["ts_end"]
    dur = None
    if end:
        dur = int((parse_ts(end) - parse_ts(row["ts_start"])).total_seconds())
        if dur < 0:
            dur = 0
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "type": row["type"],
        "ts_start": row["ts_start"],
        "ts_end": end,
        "details": json.loads(row["details"]),
        "duration_seconds": dur,
    }


def validate_details(etype: str, details: dict) -> dict:
    d = dict(details or {})
    note = d.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(status_code=400, detail="note must be a string")
    if etype == "feed":
        method = d.get("method") or "bottle"
        if method != "bottle":
            raise HTTPException(status_code=400, detail="feed details.method must be 'bottle'")
        d["method"] = "bottle"
        amount = d.get("amount")
        if amount not in (None, ""):
            try:
                d["amount"] = float(amount)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="amount must be a number")
            if d["amount"] < 0:
                raise HTTPException(status_code=400, detail="amount must be >= 0")
        else:
            d["amount"] = None
        unit = d.get("unit") or "oz"
        if unit not in UNITS:
            raise HTTPException(status_code=400, detail="unit must be 'oz' or 'ml'")
        d["unit"] = unit
    elif etype == "diaper":
        wet = bool(d.get("wet"))
        solid = bool(d.get("solid"))
        if not (wet or solid):
            raise HTTPException(status_code=400, detail="diaper needs wet and/or solid")
        d["wet"], d["solid"] = wet, solid
        color = d.get("color")
        if color in ("", None):
            d["color"] = None
        elif color not in COLORS:
            raise HTTPException(status_code=400, detail=f"color must be one of {sorted(COLORS)}")
    elif etype == "sleep":
        pass
    elif etype == "tummy":
        pass
    elif etype == "measurement":
        kind = d.get("kind")
        if kind not in MEASUREMENT_KINDS:
            raise HTTPException(
                status_code=400, detail=f"measurement kind must be one of {sorted(MEASUREMENT_KINDS)}")
        # Pounds + ounces: {"kind":"weight","lb":8,"oz":6} — value stays the
        # canonical decimal pounds so charts/reports/CSV need no change.
        if kind == "weight" and d.get("lb") not in (None, ""):
            try:
                lbv = float(d.get("lb"))
                oz_raw = d.get("oz")
                ozv = 0.0 if oz_raw in (None, "") else float(oz_raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="lb and oz must be numbers")
            if lbv < 0 or ozv < 0 or ozv >= 16:
                raise HTTPException(status_code=400, detail="oz must be between 0 and 15.9")
            if lbv + ozv <= 0:
                raise HTTPException(status_code=400, detail="weight must be > 0")
            d["lb"] = lbv
            d["oz"] = round(ozv, 2)
            d["value"] = round(lbv + ozv / 16.0, 4)
            if not (d.get("unit") or "").strip():
                d["unit"] = "lb"
        try:
            d["value"] = float(d.get("value"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="measurement value must be a number")
        if d["value"] <= 0:
            raise HTTPException(status_code=400, detail="measurement value must be > 0")
        unit = (d.get("unit") or "").strip()
        if unit not in MEASUREMENT_UNITS[kind]:
            raise HTTPException(
                status_code=400, detail=f"unit for {kind} must be one of {sorted(MEASUREMENT_UNITS[kind])}"
            )
        d["unit"] = unit
    elif etype == "medication":
        name = (d.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="medication details.name is required")
        d["name"] = name
        dose = d.get("dose")
        d["dose"] = str(dose).strip() if dose not in (None, "") else None
        iv = d.get("interval_min")
        if iv in (None, ""):
            d["interval_min"] = None
        else:
            try:
                ivf = float(iv)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="interval_min must be a number of minutes")
            if ivf <= 0 or ivf > 2880:
                raise HTTPException(status_code=400, detail="interval_min must be between 1 and 2880")
            d["interval_min"] = int(round(ivf))
    elif etype == "note":
        if not (d.get("text") or "").strip():
            raise HTTPException(status_code=400, detail="note details.text is required")
    return d


class EventIn(BaseModel):
    type: str
    ts_start: Optional[str] = None
    ts_end: Optional[str] = None
    details: dict = {}
    client_id: Optional[str] = None


class EventPatch(BaseModel):
    ts_start: Optional[str] = None
    ts_end: Optional[str] = None
    details: Optional[dict] = None


class MetaIn(BaseModel):
    key: str
    value: str


@app.middleware("http")
async def no_store_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/events")
def create_event(body: EventIn):
    if body.type not in TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {sorted(TYPES)}")
    details = validate_details(body.type, body.details)
    start = parse_ts(body.ts_start) if body.ts_start else utcnow()
    end = parse_ts(body.ts_end) if body.ts_end else None
    if end and end < start:
        raise HTTPException(status_code=400, detail="ts_end is before ts_start")
    openable = body.type in OPENABLE_TYPES and end is None
    with WRITE_LOCK, tx() as conn:
        if body.client_id:
            prev = conn.execute(
                "SELECT * FROM events WHERE client_id=?", (body.client_id,)
            ).fetchone()
            if prev:
                return event_to_dict(prev)   # retry of an already-saved tap
        if openable:
            # Only one open timer per type: auto-close any previous one.
            for r in conn.execute(
                "SELECT id, ts_start FROM events WHERE type=? AND ts_end IS NULL", (body.type,)
            ).fetchall():
                prev = parse_ts(r["ts_start"])
                new_end = start if start > prev else utcnow()
                conn.execute(
                    "UPDATE events SET ts_end=? WHERE id=?", (to_iso(new_end), r["id"])
                )
        cur = conn.execute(
            "INSERT INTO events (type, ts_start, ts_end, details, created_at, client_id)"
            " VALUES (?,?,?,?,?,?)",
            (
                body.type,
                to_iso(start),
                to_iso(end) if end else None,
                json.dumps(details),
                to_iso(utcnow()),
                body.client_id,
            ),
        )
        row = conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
    return event_to_dict(row)


@app.get("/api/events")
def list_events(
    days: float = 7,
    limit: int = 2000,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """Windowed event list. `start`/`end` are ISO timestamps, `end` EXCLUSIVE;
    without them the last `days` days are returned.

    Only running timers (sleep/tummy) escape the window, so a nap that began
    before the window edge still shows up. The old version matched every
    instant event ever logged (their ts_end is NULL), which is why "last 7
    days" returned all of history.
    """
    hi = to_iso(parse_ts(end)) if end else to_iso(utcnow() + timedelta(days=2))
    lo = (
        to_iso(parse_ts(start))
        if start
        else to_iso(utcnow() - timedelta(days=days))
    )
    rows = query(
        "SELECT * FROM events WHERE (ts_start >= ? AND ts_start < ?) "
        "OR (ts_end IS NULL AND type IN ('sleep','tummy')) "
        "ORDER BY ts_start DESC LIMIT ?",
        (lo, hi, limit),
    )
    older = query("SELECT 1 FROM events WHERE ts_start < ? LIMIT 1", (lo,))
    return {"events": [event_to_dict(r) for r in rows], "has_more": bool(older)}


@app.get("/api/events/{event_id}")
def get_event(event_id: int):
    rows = query("SELECT * FROM events WHERE id=?", (event_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="not found")
    return event_to_dict(rows[0])


@app.post("/api/events/{event_id}/stop")
def stop_event(event_id: int, body: Optional[EventPatch] = None):
    rows = query("SELECT * FROM events WHERE id=?", (event_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="not found")
    if rows[0]["ts_end"]:
        raise HTTPException(status_code=400, detail="event already closed")
    start = parse_ts(rows[0]["ts_start"])
    end = parse_ts(body.ts_end) if (body and body.ts_end) else utcnow()
    if end < start:
        end = start
    with WRITE_LOCK, tx() as conn:
        conn.execute("UPDATE events SET ts_end=? WHERE id=?", (to_iso(end), event_id))
    return event_to_dict(query("SELECT * FROM events WHERE id=?", (event_id,))[0])


@app.patch("/api/events/{event_id}")
def patch_event(event_id: int, body: EventPatch):
    rows = query("SELECT * FROM events WHERE id=?", (event_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="not found")
    old = rows[0]
    start = parse_ts(body.ts_start) if body.ts_start else parse_ts(old["ts_start"])
    if body.ts_end is not None:
        end = parse_ts(body.ts_end)
    else:
        end = parse_ts(old["ts_end"]) if old["ts_end"] else None
    if end and end < start:
        end = start
    details = validate_details(
        old["type"], body.details if body.details is not None else json.loads(old["details"])
    )
    with WRITE_LOCK, tx() as conn:
        conn.execute(
            "UPDATE events SET ts_start=?, ts_end=?, details=? WHERE id=?",
            (to_iso(start), to_iso(end) if end else None, json.dumps(details), event_id),
        )
    return event_to_dict(query("SELECT * FROM events WHERE id=?", (event_id,))[0])


@app.delete("/api/events/{event_id}")
def delete_event(event_id: int):
    with WRITE_LOCK, tx() as conn:
        cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "deleted": event_id}


@app.get("/api/summary")
def summary(hours: float = 24):
    cutoff = to_iso(utcnow() - timedelta(hours=hours))
    rows = query("SELECT * FROM events WHERE ts_start >= ? ORDER BY ts_start DESC", (cutoff,))
    out = {
        "window_hours": hours,
        "feeds": {
            "bottle_count": 0,
            "bottle_oz": 0.0,
            "bottle_ml": 0.0,
        },
        "diapers": {"total": 0, "wet": 0, "solid": 0},
        "sleep_seconds": 0,
        "tummy_seconds": 0,
        "medications": 0,
        "notes": 0,
        "open_events": [],
        "events": [],
    }
    for r in rows:
        d = json.loads(r["details"])
        e = event_to_dict(r)
        out["events"].append(e)
        if r["type"] == "feed":
            out["feeds"]["bottle_count"] += 1
            if d.get("amount"):
                if d.get("unit") == "ml":
                    out["feeds"]["bottle_ml"] += d["amount"]
                else:
                    out["feeds"]["bottle_oz"] += d["amount"]
        elif r["type"] == "diaper":
            out["diapers"]["total"] += 1
            if d.get("wet"):
                out["diapers"]["wet"] += 1
            if d.get("solid"):
                out["diapers"]["solid"] += 1
        elif r["type"] == "sleep":
            if e["duration_seconds"]:
                out["sleep_seconds"] += e["duration_seconds"]
        elif r["type"] == "tummy":
            if e["duration_seconds"]:
                out["tummy_seconds"] += e["duration_seconds"]
        elif r["type"] == "medication":
            out["medications"] += 1
        elif r["type"] == "note":
            out["notes"] += 1
        if not r["ts_end"]:
            out["open_events"].append(e)
    out["sleep_seconds"] = int(out["sleep_seconds"])
    out["feeds"]["bottle_oz"] = round(out["feeds"]["bottle_oz"], 2)
    out["feeds"]["bottle_ml"] = round(out["feeds"]["bottle_ml"], 1)
    return out


@app.get("/api/meta")
def get_meta():
    rows = query("SELECT key, value FROM meta")
    return {r["key"]: r["value"] for r in rows}


@app.post("/api/meta")
def set_meta(body: MetaIn):
    if body.key not in ALLOWED_META:
        raise HTTPException(status_code=400, detail=f"key must be one of {sorted(ALLOWED_META)}")
    with WRITE_LOCK, tx() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.key, body.value),
        )
    return get_meta()


# ---------- historical report + CSV export ----------
#
# Day boundaries are the BROWSER's local days: the client passes tz = its
# Date.getTimezoneOffset() in minutes (local = utc - tz), and we bucket
# events by (utc_ts - tz).date(). Default tz=0 (UTC) keeps curl/API usage
# sane. Ranges are capped at 730 days.


def clamp_tz(tz: int) -> int:
    return max(-840, min(840, int(tz)))


def resolve_range(
    days: int, start: Optional[str], end: Optional[str], tz: int
) -> tuple[datetime, datetime, date, date, int]:
    """Turn query params into a UTC window + the local start/end dates."""
    tz = clamp_tz(tz)
    if start or end:
        if not (start and end):
            raise HTTPException(status_code=400, detail="provide both start and end (YYYY-MM-DD)")
        try:
            sd = datetime.fromisoformat(start).date()
            ed = datetime.fromisoformat(end).date()
        except ValueError:
            raise HTTPException(status_code=400, detail="start/end must be YYYY-MM-DD")
        if ed < sd:
            raise HTTPException(status_code=400, detail="end is before start")
        if (ed - sd).days > 730:
            raise HTTPException(status_code=400, detail="range too long (max 730 days)")
        # Local midnight expressed in UTC: local = utc - tz  =>  utc = local + tz
        s = datetime.combine(sd, d_time(0, 0), tzinfo=timezone.utc) + timedelta(minutes=tz)
        e = datetime.combine(ed + timedelta(days=1), d_time(0, 0), tzinfo=timezone.utc) + timedelta(minutes=tz)
    else:
        n = max(1, min(int(days), 730))
        local_now = utcnow() - timedelta(minutes=tz)
        sd = (local_now - timedelta(days=n - 1)).date()
        ed = local_now.date()
        s = datetime.combine(sd, d_time(0, 0), tzinfo=timezone.utc) + timedelta(minutes=tz)
        e = utcnow()
    return s, e, sd, ed, tz


def window_rows(s: datetime, e: datetime):
    return query(
        "SELECT * FROM events WHERE ts_start < ? AND (ts_end IS NULL OR ts_end > ?) "
        "ORDER BY ts_start ASC",
        (to_iso(e), to_iso(s)),
    )


def describe_event_text(etype: str, details: dict, dur: Optional[int]) -> str:
    if etype == "feed":
        amt = details.get("amount")
        unit = details.get("unit") or "oz"
        return "Bottle" + (f" {amt:g} {unit}" if amt else "")
    if etype == "diaper":
        parts = []
        if details.get("wet"):
            parts.append("Wet")
        if details.get("solid"):
            parts.append("Solid")
        text = " + ".join(parts) + " diaper"
        if details.get("color"):
            text += f" ({details['color']})"
        return text
    if etype == "sleep":
        if dur is None:
            return "Nap (still open)"
        m = round(dur / 60)
        return f"Nap {m // 60}h {m % 60}m" if m >= 60 else f"Nap {m}m"
    if etype == "tummy":
        if dur is None:
            return "Tummy time (still open)"
        m = round(dur / 60)
        return f"Tummy time {m // 60}h {m % 60}m" if m >= 60 else f"Tummy time {m}m"
    if etype == "measurement":
        label = MEASUREMENT_KIND_LABELS.get(details.get("kind"), "Measurement")
        unit = details.get("unit") or ""
        val = details.get("value")
        val_txt = f"{val:g}" if isinstance(val, (int, float)) else str(val or "")
        if details.get("kind") == "temperature":
            return f"{label}: {val_txt} °{unit}"
        return f"{label}: {val_txt} {unit}".strip()
    if etype == "medication":
        name = details.get("name") or "Medication"
        dose = details.get("dose")
        return f"Medication: {name}" + (f" ({dose})" if dose else "")
    return (details.get("text") or "").strip() or "Note"


def csv_safe(v) -> object:
    # Defang spreadsheet formula injection from free-text fields.
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def report_payload(s, e, sd, ed, tz: int) -> dict:
    rows = window_rows(s, e)
    daymap: dict = {}
    feed_starts: list = []

    # Seed the gap chain with the last feed before the window, otherwise the
    # first feed in range has no predecessor and its stretch is lost — which
    # made a single-day range disagree with the same day inside a longer one.
    prev = query(
        "SELECT ts_start FROM events WHERE type = 'feed' AND ts_start < ? "
        "ORDER BY ts_start DESC LIMIT 1",
        (to_iso(s),),
    )
    if prev:
        feed_starts.append((parse_ts(prev[0]["ts_start"]), None))

    def blank_day(dk: str) -> dict:
        return {
            "date": dk,
            "feeds": {"count": 0, "oz": 0.0, "ml": 0.0},
            "diapers": {"total": 0, "wet": 0, "solid": 0, "colors": {}},
            "sleep": {"seconds": 0, "count": 0, "longest_nap_seconds": 0},
            "tummy": {"seconds": 0, "count": 0, "longest_seconds": 0},
            "medications": 0,
            "notes": 0,
            "max_feed_gap_seconds": None,
            "entries": [],
        }

    for r in rows:
        ev = event_to_dict(r)
        dk = (parse_ts(r["ts_start"]) - timedelta(minutes=tz)).date().isoformat()
        if dk < sd.isoformat() or dk > ed.isoformat():
            continue  # e.g. an open timer started before the window
        day = daymap.setdefault(dk, blank_day(dk))
        d = ev["details"]
        day["entries"].append(ev)
        if ev["type"] == "feed":
            day["feeds"]["count"] += 1
            amt = d.get("amount") or 0
            if d.get("unit") == "ml":
                day["feeds"]["ml"] += amt
            else:
                day["feeds"]["oz"] += amt
            feed_starts.append((parse_ts(r["ts_start"]), dk))
        elif ev["type"] == "diaper":
            day["diapers"]["total"] += 1
            if d.get("wet"):
                day["diapers"]["wet"] += 1
            if d.get("solid"):
                day["diapers"]["solid"] += 1
            c = d.get("color")
            if c:
                day["diapers"]["colors"][c] = day["diapers"]["colors"].get(c, 0) + 1
        elif ev["type"] == "sleep":
            dur = ev["duration_seconds"] or 0
            day["sleep"]["seconds"] += dur
            if ev["ts_end"]:
                day["sleep"]["count"] += 1
                day["sleep"]["longest_nap_seconds"] = max(
                    day["sleep"]["longest_nap_seconds"], dur
                )
        elif ev["type"] == "tummy":
            dur = ev["duration_seconds"] or 0
            day["tummy"]["seconds"] += dur
            if ev["ts_end"]:
                day["tummy"]["count"] += 1
                day["tummy"]["longest_seconds"] = max(day["tummy"]["longest_seconds"], dur)
        elif ev["type"] == "medication":
            day["medications"] += 1
        elif ev["type"] == "note":
            day["notes"] += 1

    # Longest stretch between feeds; overnight gaps count toward the day of
    # the later feed (what the pediatrician actually asks about).
    for (t0, _), (t1, dk1) in zip(feed_starts, feed_starts[1:]):
        gap = int((t1 - t0).total_seconds())
        day = daymap.get(dk1)
        if day is not None and (
            day["max_feed_gap_seconds"] is None or gap > day["max_feed_gap_seconds"]
        ):
            day["max_feed_gap_seconds"] = gap

    ordered = [daymap[k] for k in sorted(daymap, reverse=True)]
    for day in ordered:
        day["feeds"]["oz"] = round(day["feeds"]["oz"], 2)
        day["feeds"]["ml"] = round(day["feeds"]["ml"], 1)

    totals = {
        "feeds": {
            "count": sum(d["feeds"]["count"] for d in ordered),
            "oz": round(sum(d["feeds"]["oz"] for d in ordered), 2),
            "ml": round(sum(d["feeds"]["ml"] for d in ordered), 1),
        },
        "diapers": {
            "total": sum(d["diapers"]["total"] for d in ordered),
            "wet": sum(d["diapers"]["wet"] for d in ordered),
            "solid": sum(d["diapers"]["solid"] for d in ordered),
            "colors": {},
        },
        "sleep": {
            "seconds": sum(d["sleep"]["seconds"] for d in ordered),
            "count": sum(d["sleep"]["count"] for d in ordered),
            "longest_nap_seconds": max(
                (d["sleep"]["longest_nap_seconds"] for d in ordered), default=0
            ),
        },
        "tummy": {
            "seconds": sum(d["tummy"]["seconds"] for d in ordered),
            "count": sum(d["tummy"]["count"] for d in ordered),
            "longest_seconds": max(
                (d["tummy"]["longest_seconds"] for d in ordered), default=0
            ),
        },
        "medications": sum(d["medications"] for d in ordered),
        "notes": sum(d["notes"] for d in ordered),
        "max_feed_gap_seconds": max(
            (d["max_feed_gap_seconds"] for d in ordered if d["max_feed_gap_seconds"] is not None),
            default=None,
        ),
    }
    for d in ordered:
        for c, n in d["diapers"]["colors"].items():
            totals["diapers"]["colors"][c] = totals["diapers"]["colors"].get(c, 0) + n

    # Growth & health data (days are newest-first, entries within a day ASC,
    # so these lists read newest-first which is what the report page wants).
    growth, med_log = [], []
    for day in ordered:
        for ev in day["entries"]:
            if ev["type"] == "measurement":
                dd = ev["details"]
                growth.append({
                    "date": day["date"], "kind": dd.get("kind"),
                    "value": dd.get("value"), "unit": dd.get("unit"),
                    "lb": dd.get("lb"), "oz": dd.get("oz"),
                })
            elif ev["type"] == "medication":
                med_log.append({
                    "date": day["date"], "name": ev["details"].get("name"),
                    "dose": ev["details"].get("dose"),
                })

    # --- Intake curve: today (by clock time) vs the typical day in range ---
    # Cumulative ounces at each 10-minute mark of the local day. Today's curve
    # comes straight from the DB so it stays correct even when the filter
    # excludes today. The average skips today (a partial day would drag it
    # down) and any day with no feeds logged.
    BUCKET = 10
    NBUCKET = 24 * 60 // BUCKET

    def _oz(details: dict) -> float:
        try:
            amt = float(details.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0
        return amt / 29.5735 if details.get("unit") == "ml" else amt

    def _minutes(dt_utc: datetime) -> int:
        local = dt_utc - timedelta(minutes=tz)
        return local.hour * 60 + local.minute

    def _cum_curve(points: list) -> list:
        inc = [0.0] * (NBUCKET + 1)
        for m, oz in points:
            inc[min(NBUCKET, m // BUCKET)] += oz
        out, run = [], 0.0
        for v in inc:
            run += v
            out.append(round(run, 2))
        return out

    today_local = (utcnow() - timedelta(minutes=tz)).date()
    utc_start = (
        datetime.combine(today_local, d_time.min, tzinfo=timezone.utc)
        + timedelta(minutes=tz)
    )
    today_pts = []
    for r in query(
        "SELECT * FROM events WHERE type='feed' AND ts_start >= ? AND ts_start < ? "
        "ORDER BY ts_start",
        (to_iso(utc_start), to_iso(utc_start + timedelta(days=1))),
    ):
        ev = event_to_dict(r)
        today_pts.append((_minutes(parse_ts(r["ts_start"])), _oz(ev["details"])))

    per_day = []
    for day in ordered:
        if day["date"] == today_local.isoformat() or not day["feeds"]["count"]:
            continue
        per_day.append([
            (_minutes(parse_ts(ev["ts_start"])), _oz(ev["details"]))
            for ev in day["entries"] if ev["type"] == "feed"
        ])

    today_curve = _cum_curve(today_pts)
    now_minute = _minutes(utcnow())
    avg_block = None
    if per_day:
        curves = [_cum_curve(pts) for pts in per_day]
        avg_curve = [
            round(sum(c[i] for c in curves) / len(curves), 2)
            for i in range(NBUCKET + 1)
        ]
        avg_block = {
            "points": avg_curve,
            "days": len(per_day),
            "at_now": avg_curve[min(NBUCKET, now_minute // BUCKET)],
        }

    return {
        "range": {"start_local": sd.isoformat(), "end_local": ed.isoformat(), "tz_minutes": tz},
        "intake_curve": {
            "bucket_minutes": BUCKET,
            "now_minute": now_minute,
            "unit": "oz",
            "today": {
                "date": today_local.isoformat(),
                "points": today_curve,
                "total": round(sum(oz for _, oz in today_pts), 2),
                "count": len(today_pts),
            },
            "avg": avg_block,
        },
        "totals": totals,
        "growth": growth,
        "medication_log": med_log,
        "days": ordered,
    }


@app.get("/api/report")
def report(days: int = 14, start: Optional[str] = None, end: Optional[str] = None, tz: int = 0):
    s, e, sd, ed, tz = resolve_range(days, start, end, tz)
    return report_payload(s, e, sd, ed, tz)


@app.get("/api/export.csv")
def export_csv(days: int = 14, start: Optional[str] = None, end: Optional[str] = None, tz: int = 0):
    s, e, sd, ed, tz = resolve_range(days, start, end, tz)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Time", "Type", "Description", "Amount", "Unit", "Duration (min)", "Note"])
    for r in window_rows(s, e):
        ev = event_to_dict(r)
        local = parse_ts(r["ts_start"]) - timedelta(minutes=tz)
        d = ev["details"]
        note = (d.get("note") or "").strip()
        if ev["type"] == "feed":
            amt, unit = d.get("amount"), (d.get("unit") or "")
            amt = amt if amt is not None else ""
        else:
            amt, unit = "", ""
        dur_min = round(ev["duration_seconds"] / 60) if ev["duration_seconds"] else ""
        w.writerow(
            [
                local.date().isoformat(),
                local.strftime("%H:%M"),
                ev["type"],
                csv_safe(describe_event_text(ev["type"], d, ev["duration_seconds"])),
                amt,
                unit,
                dur_min,
                csv_safe(note),
            ]
        )
    name = f"babytracker-{sd.isoformat()}-to-{ed.isoformat()}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


PWA_HEAD = r"""<!--PWA_HEAD_START-->
<script>
/* This app can be served under a path prefix (e.g. /baby by a reverse proxy).
   A relative URL resolves against "/" when the address has no trailing slash,
   and iOS then picks up the ROOT site's manifest and icon on "Add to Home
   Screen". So: normalise the URL, and emit the PWA links with absolute paths. */
(function () {
  var path = location.pathname;
  var base = path.replace(/\/report\/?$/, '').replace(/\/+$/, '');  // "" at root, "/baby" when mounted
  var root = base + '/';
  if (path === base) {
    history.replaceState(null, '', path + '/' + location.search + location.hash);
  }
  function tag(rel, href, attrs) {
    var l = document.createElement('link');
    l.rel = rel;
    l.href = href;
    if (attrs) { for (var k in attrs) { l.setAttribute(k, attrs[k]); } }
    document.head.appendChild(l);
  }
  tag('manifest', root + 'manifest.webmanifest');
  tag('apple-touch-icon', root + 'icons/apple-touch-icon.png');
  tag('icon', root + 'icons/icon-32.png', { sizes: '32x32', type: 'image/png' });
  tag('icon', root + 'icons/favicon.ico', { sizes: 'any' });
  window.APP_ROOT = root;
})();
</script>
<!--PWA_HEAD_END-->"""

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Baby Log">
<meta name="theme-color" content="#faf9f7">
<meta name="description" content="Feeding, sleep and diaper log.">
<!--PWA_HEAD-->
<title>Baby Log</title>
<style>
  :root {
    --cream:#faf9f7; --oat:#dad4c8; --oat-light:#eee9df;
    --ink:#1a1917; --silver:#9f9b93; --charcoal:#55534e;
    --slushie-tint:#e7f9fe; --slushie-border:#b5ecfb;
    --lemon-tint:#fdf3d9; --lemon-border:#f5e2ae; --lemon-deep:#d08a11;
    --pome-tint:#fdeff0; --pome-border:#f7cdd2;
    --ube-tint:#f1ecfd; --ube-border:#d9ccf7;
    --matcha-tint:#e9f8ef; --matcha-border:#bfeccb;
    --clay-shadow:0 1px 1px rgba(0,0,0,.10), inset 0 -1px 1px rgba(0,0,0,.04), 0 -0.5px 1px rgba(0,0,0,.05);
  }
  * { box-sizing:border-box; }
  button, .mini, .chip { touch-action:manipulation; }
  body { margin:0; font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif; background:var(--cream); color:var(--ink); font-size:16px; -webkit-tap-highlight-color:transparent; }
  .wrap { max-width:640px; margin:0 auto; padding:calc(16px + env(safe-area-inset-top,0px)) 14px calc(130px + env(safe-area-inset-bottom,0px)); }
  header { display:flex; justify-content:space-between; align-items:flex-start; padding:6px 2px 14px; }
  h1 { margin:0; font-size:27px; font-weight:600; letter-spacing:-0.6px; }
  .age { color:var(--silver); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:1.08px; margin-top:3px; }
  .card { background:#fff; border:1px solid var(--oat); border-radius:24px; padding:18px; margin-bottom:14px; box-shadow:var(--clay-shadow); }
  h2 { margin:0 0 12px; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:1.08px; color:var(--silver); }
  h3 { margin:16px 0 6px; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:1.08px; color:var(--charcoal); border-bottom:1px dashed var(--oat); padding-bottom:6px; }
  .status-card { background:#fff; border:1px solid var(--oat); border-radius:18px; padding:14px 16px; margin-bottom:10px; font-size:15.5px; display:flex; justify-content:space-between; align-items:center; gap:8px; box-shadow:var(--clay-shadow); }
  .status-card.open { background:var(--lemon-tint); border-color:var(--lemon-border); }
  .warn { background:#fdf0f1; border:1px solid #f6c3c9; color:#8a2b33; border-radius:16px; padding:12px 14px; margin-bottom:10px; font-size:14px; }
  button { font-family:inherit; font-size:16px; font-weight:500; padding:10px 14px; border-radius:14px; border:1px solid var(--oat); background:#fff; color:var(--ink); cursor:pointer; min-height:52px; box-shadow:var(--clay-shadow); transition:transform .06s ease; touch-action:manipulation; }
  button:active { transform:translateY(2px) scale(.98); box-shadow:none; }
  button:focus-visible { outline:2px solid rgb(20,110,245); outline-offset:2px; }
  .mini { min-height:0; padding:5px 10px; font-size:13px; border-radius:10px; }
  .danger { border-color:#f2b8bd; color:#b3392f; }
  .row { display:flex; align-items:center; gap:8px; padding:9px 0; border-bottom:1px solid var(--oat-light); font-size:15px; }
  .row:last-child { border-bottom:none; }
  .row.pending { opacity:.78; }
  .psync { font-size:12px; margin-left:5px; }
  .ri { width:30px; text-align:center; }
  .rt { color:var(--silver); width:62px; flex-shrink:0; font-size:13.5px; }
  .rd { flex:1; }
  .empty { color:var(--silver); padding:8px 0; }
  .today-line { font-size:15px; line-height:1.75; }
  .live { font-variant-numeric:tabular-nums; font-weight:600; color:var(--lemon-deep); }
  #foot { color:var(--silver); font-size:12px; text-align:center; padding:6px 0 0; }

  /* single bottom log button */
  #fab { position:fixed; bottom:calc(18px + env(safe-area-inset-bottom, 0px)); left:50%; transform:translateX(-50%); z-index:30; height:60px; padding:0 36px; border-radius:999px; border:none; background:var(--ink); color:#fff; font-size:19px; font-weight:600; box-shadow:0 6px 20px rgba(0,0,0,.28); display:flex; align-items:center; gap:10px; }
  #fab:active { transform:translateX(-50%) scale(.96); }

  /* bottom sheet */
  #scrim { position:fixed; inset:0; background:rgba(31,29,26,.45); z-index:50; display:none; }
  #sheet { position:fixed; left:0; right:0; bottom:0; z-index:60; max-width:640px; margin:0 auto; background:#fff; border-radius:26px 26px 0 0; padding:10px 16px calc(18px + env(safe-area-inset-bottom, 0px)); box-shadow:0 -8px 30px rgba(0,0,0,.18); transform:translateY(105%); transition:transform .22s ease; }
  #sheet.open { transform:translateY(0); }
  .grab { width:44px; height:5px; border-radius:3px; background:var(--oat); margin:4px auto 12px; }
  .sh-title { font-size:20px; font-weight:600; letter-spacing:-0.3px; margin:0 0 14px; }
  .hint { color:var(--silver); font-size:14px; margin:-8px 0 12px; }
  .chooser { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .tile { display:flex; flex-direction:column; gap:3px; align-items:center; justify-content:center; padding:14px 6px; min-height:80px; border-radius:18px; }
  .tile .em { font-size:24px; }
  .tile .lab { font-size:14px; font-weight:500; color:var(--charcoal); }
  .t-wet { background:var(--slushie-tint); border-color:var(--slushie-border); }
  .t-solid { background:var(--lemon-tint); border-color:var(--lemon-border); }
  .t-both { background:var(--pome-tint); border-color:var(--pome-border); }
  .t-nap { background:var(--ube-tint); border-color:var(--ube-border); }
  .t-tummy { background:var(--slushie-tint); border-color:var(--slushie-border); }
  .t-more { background:var(--oat-light); border-color:var(--oat); grid-column:1 / -1; }
  .t-bottle { background:var(--matcha-tint); border-color:var(--matcha-border); }
  .chips { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:12px; }
  .chip { min-height:50px; padding:8px 16px; border-radius:14px; font-size:16px; font-weight:600; }
  .chip.wide { width:100%; margin-bottom:6px; }
  .chip .mdose { color:var(--silver); font-weight:500; font-size:13px; margin-left:4px; }
  .sw-pale { border-color:#e8b27d; background:#fdf3e7; }
  .u-toggle { display:flex; gap:8px; margin-bottom:12px; }
  .u-btn { flex:0 0 auto; padding:8px 22px; border-radius:999px; font-weight:600; }
  .u-btn.on { background:var(--ink); color:#fff; border-color:var(--ink); }
  .custom-row { display:flex; gap:8px; margin-bottom:10px; }
  .custom-row input { flex:1; font-family:inherit; font-size:17px; padding:10px 12px; border-radius:14px; border:1px solid #cfc8ba; background:#fff; color:var(--ink); min-width:0; }
  .custom-row button { flex:0 0 auto; font-weight:600; }
  .wt-row { display:flex; gap:8px; margin-bottom:10px; }
  .wt { flex:1; display:flex; align-items:center; gap:6px; padding:0 12px; border-radius:14px; border:1px solid #cfc8ba; background:#fff; min-width:0; }
  .wt input { flex:1; min-width:0; border:none; outline:none; background:transparent; font-family:inherit; font-size:17px; padding:10px 0; color:var(--ink); }
  .wt span { font-size:13px; font-weight:600; color:var(--charcoal); }
  .wt-row button { flex:0 0 auto; font-weight:600; }
  .back { background:transparent; border:none; box-shadow:none; color:var(--silver); min-height:0; padding:8px 4px; font-size:15px; width:100%; }
  .back:active { transform:none; box-shadow:none; }

  #modal { position:fixed; inset:0; background:rgba(31,29,26,.45); display:none; align-items:center; justify-content:center; z-index:70; padding:14px; }
  #modal .card { width:100%; max-width:480px; max-height:88vh; overflow:auto; margin:0; }
  #modal label { display:flex; flex-direction:column; gap:4px; font-size:13px; font-weight:500; color:var(--charcoal); margin-bottom:12px; }
  #modal input, #modal select { font-family:inherit; font-size:16px; padding:10px; border-radius:12px; border:1px solid #cfc8ba; background:#fff; color:var(--ink); }
  #modal input[type=checkbox] { transform:scale(1.3); }
  #modal label.check { flex-direction:row; align-items:center; gap:8px; font-size:16px; color:var(--ink); }
  .modal-btns { display:flex; gap:8px; margin-top:6px; }
  .modal-btns button { flex:1; }
  #toast { display:none; position:fixed; top:14px; left:50%; transform:translateX(-50%); background:#26221c; color:#fff; padding:12px 20px; border-radius:999px; font-size:15px; z-index:80; max-width:90vw; box-shadow:-4px 4px 0 rgba(0,0,0,.12); }
  .hnav { display:flex; align-items:center; gap:8px; margin:0 0 10px; }
  .hnav #hist-title { flex:1; text-align:center; font-size:13px; color:var(--charcoal); letter-spacing:.2px; }
  .hnav button[disabled] { opacity:.28; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div id="head-left"></div>
    <div style="display:flex; gap:6px;">
      <button class="mini" onclick="location.href=BASE+'/report'">📊</button>
      <button class="mini" onclick="openSettings()">⚙️</button>
    </div>
  </header>
  <div id="warn" class="warn" style="display:none"></div>
  <div id="loaderr" class="warn" style="display:none"></div>
  <div id="status"></div>

  <section class="card"><h2>Today</h2><div id="today" class="today-line"></div></section>
  <section class="card"><h2>History</h2>
    <div class="hnav">
      <button class="mini" id="hist-prev" onclick="histPage(-1)" aria-label="Newer">‹</button>
      <div id="hist-title">—</div>
      <button class="mini" id="hist-next" onclick="histPage(1)" aria-label="Older">›</button>
    </div>
    <div id="history"></div></section>
  <div id="foot"></div>
</div>

<button id="fab" onclick="openSheet()">＋ Log</button>

<div id="scrim" onclick="closeSheet()"></div>
<div id="sheet"><div class="grab"></div><div id="sheet-body"></div></div>

<div id="modal" onclick="if(event.target===this)closeModal()"><div id="modal-body" class="card"></div></div>
<div id="toast"></div>

<script>
const S = { events: [], meta: {} };
// Works both at root (/) and when mounted under a path prefix (e.g. /baby):
// api() resolves every fetch relative to the current path.
const BASE = location.pathname.replace(new RegExp('/+$'), '');

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => '&#' + c.charCodeAt(0) + ';'); }

async function api(path, opts = {}) {
  // Cache-bust every GET: some browsers (Safari notably) will otherwise
  // serve a stale cached /api/events response forever, showing 0 entries
  // while the server has data.
  let url = BASE + path;
  if (!opts.method || opts.method === 'GET') url += (url.includes('?') ? '&' : '?') + '_t=' + Date.now();
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) {
    let t = await r.text();
    try { t = JSON.parse(t).detail ?? t; } catch {}
    const err = new Error(t);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

/* ---------- offline-first writes ----------
   Every create is applied to the UI immediately and queued in localStorage.
   The POST carries a client_id the server dedupes on, so retrying after a
   dropped connection (a sleeping phone, a Tailscale path change) is safe.
   A stalled tunnel therefore never blocks a tap. */
const OUTBOX_KEY = 'baby_outbox_v1';
let outbox = [];
try { outbox = JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]') || []; } catch (e) { outbox = []; }
let flushing = false, retryTimer = null, syncNote = 0;

function saveOutbox() { try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(outbox)); } catch (e) {} }
function newCid() {
  return crypto.randomUUID ? crypto.randomUUID()
    : 'c' + Date.now().toString(36) + Math.random().toString(16).slice(2);
}

function sortEvents(list) {
  list.sort((a, b) => new Date(b.ts_start) - new Date(a.ts_start));
  return list;
}

function markSynced(cid, real) {
  S.events = S.events.filter(e => !(e._pending && e.client_id === cid));
  if (real) S.events.push(real);
  sortEvents(S.events);
  render();
}

function queueCreate(body, label) {
  const cid = newCid();
  body.client_id = cid;
  const ev = {
    id: 'tmp-' + cid, client_id: cid, type: body.type,
    ts_start: body.ts_start || new Date().toISOString(),
    ts_end: body.ts_end || null,
    details: body.details || {}, duration_seconds: null, _pending: true,
  };
  S.events.unshift(ev);
  sortEvents(S.events);
  render();
  outbox.push({ cid: cid, body: body, label: label || 'Entry', tries: 0, next: 0 });
  saveOutbox();
  flushOutbox();
}

function scheduleRetry() {
  if (retryTimer || !outbox.length) return;
  const soonest = outbox.reduce((m, o) => Math.min(m, o.next || 0), Date.now());
  retryTimer = setTimeout(() => { retryTimer = null; flushOutbox(); },
    Math.max(1500, soonest - Date.now()));
}

async function flushOutbox() {
  if (flushing || !outbox.length) return;
  flushing = true;
  let deferred = false;
  try {
    for (const item of outbox.slice()) {
      if (item.next && item.next > Date.now()) { deferred = true; continue; }
      try {
        const real = await api('/api/events', { method: 'POST', body: JSON.stringify(item.body) });
        outbox = outbox.filter(o => o.cid !== item.cid);
        saveOutbox();
        markSynced(item.cid, real);
      } catch (err) {
        if (err.status >= 400 && err.status < 500) {
          // The server rejected it — retrying can never succeed.
          outbox = outbox.filter(o => o.cid !== item.cid);
          saveOutbox();
          S.events = S.events.filter(e => !(e._pending && e.client_id === item.cid));
          sortEvents(S.events); render();
          toast('⚠️ ' + item.label + ' not saved: ' + err.message, 6000);
        } else {
          item.tries = (item.tries || 0) + 1;
          item.next = Date.now() + Math.min(60000, 2000 * Math.pow(2, item.tries - 1));
          saveOutbox();
          deferred = true;
          if (Date.now() - syncNote > 60000) {
            syncNote = Date.now();
            toast('⏳ Saved on this phone — syncing when the connection is back', 4000);
          }
        }
      }
    }
  } finally {
    flushing = false;
    if (deferred) scheduleRetry();
  }
}

window.addEventListener('online', () => flushOutbox());
document.addEventListener('visibilitychange', () => { if (!document.hidden) flushOutbox(); });
setInterval(() => { if (outbox.length) flushOutbox(); }, 30000);

function toast(msg, ms = 4000) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', ms);
}

function durSec(e) {
  const end = e.ts_end ? new Date(e.ts_end) : Date.now();
  return Math.max(0, (end - new Date(e.ts_start)) / 1000);
}
const openEv = pred => S.events.find(e => !e.ts_end && pred(e));

function fmtClock(sec) {
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? h + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0') : m + ':' + String(s).padStart(2,'0');
}
function fmtDur(sec) {
  sec = Math.floor(sec);
  if (sec < 60) return sec + 's';
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? h + 'h ' + m + 'm' : m + 'm';
}
function fmtAgo(iso) {
  const sec = (Date.now() - new Date(iso)) / 1000;
  if (sec < 0) return 'in the future?';
  if (sec < 60) return 'just now';
  const m = Math.floor(sec / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 48) return h + 'h ' + (m % 60) + 'm ago';
  return Math.floor(h / 24) + 'd ago';
}
function fmtTime(iso) { return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }

/* Weight is stored as decimal pounds (value) plus the exact lb/oz the parent
   typed, so display never drifts: 8 lb 6 oz round-trips as "8 lb 6 oz". */
function fmtLbOz(d) {
  let lb, oz;
  if (d.lb != null) { lb = Math.floor(d.lb); oz = d.oz || 0; }
  else {
    lb = Math.floor(d.value);
    oz = Math.round(((d.value - lb) * 16) * 10) / 10;
    if (oz >= 16) { lb += 1; oz = 0; }
  }
  const extra = lb + Math.floor(oz / 16);
  lb = extra; oz = Math.round((oz % 16) * 10) / 10;
  return lb + ' lb ' + (Number.isInteger(oz) ? oz : oz.toFixed(1)) + ' oz';
}
function toLocalInput(iso) {
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function describe(e) {
  const d = e.details || {};
  if (e.type === 'feed') {
    const amt = d.amount ? ' · ' + d.amount + ' ' + (d.unit || 'oz') : '';
    return { icon: '🍼', text: 'Bottle' + amt };
  }
  if (e.type === 'diaper') {
    const parts = [];
    if (d.wet) parts.push('Wet');
    if (d.solid) parts.push('Solid');
    if (d.color) parts.push('(' + String(d.color).replace('_',' ') + ')');
    const icon = d.wet && d.solid ? '💧💩' : (d.solid ? '💩' : '💧');
    return { icon, text: parts.join(' + ') };
  }
  if (e.type === 'sleep') {
    return { icon: '😴', text: e.ts_end ? 'Sleep · ' + fmtDur(durSec(e)) : 'Sleeping…' };
  }
  if (e.type === 'tummy') {
    return { icon: '🤸', text: e.ts_end ? 'Tummy time · ' + fmtDur(durSec(e)) : 'Tummy time…' };
  }
  if (e.type === 'measurement') {
    const labels = { weight: 'Weight', length: 'Length', head_circumference: 'Head', temperature: 'Temp' };
    if (d.kind === 'weight' && (d.unit || 'lb') === 'lb')
      return { icon: '⚖️', text: 'Weight: ' + fmtLbOz(d) };
    const unit = d.kind === 'temperature' ? ' °' + d.unit : ' ' + d.unit;
    return { icon: d.kind === 'temperature' ? '🌡️' : '⚖️', text: (labels[d.kind] || 'Measurement') + ': ' + d.value + unit };
  }
  if (e.type === 'medication') {
    return { icon: '💊', text: 'Medication · ' + (d.name || '?') + (d.dose ? ' · ' + d.dose : '') +
      (d.interval_min ? ' · every ' + fmtEvery(d.interval_min) : '') };
  }
  return { icon: '📝', text: d.text || 'Note' };
}

/* History pages are 7 local calendar days each, oldest boundary at midnight:
   page 0 = today + the 6 days before it, page 1 = the 7 before that, etc. */
const HIST = { page: 0, hasMore: false, start: 0, end: 0, recentStart: 0 };

function histWindow(p) {
  const base = new Date(); base.setHours(0, 0, 0, 0);
  const start = new Date(base); start.setDate(start.getDate() - (6 + 7 * p));
  const end = new Date(base);   end.setDate(end.getDate() - 7 * p + 1);   // exclusive
  return { start: start.getTime(), end: end.getTime() };
}

function histQuery(p) {
  const w = histWindow(p);
  return 'start=' + new Date(w.start).toISOString() + '&end=' + new Date(w.end).toISOString();
}

function histPage(d) {
  const p = HIST.page + d;
  if (p < 0 || (d > 0 && !HIST.hasMore)) return;
  loadAll(p);
}

function inHistPage(e) {
  const t = new Date(e.ts_start).getTime();
  return t >= HIST.start && t < HIST.end;
}

/* Status/alert cards only ever look at the current 7-day window, so paging
   back into older history can't make "last feed/sleep" point at last week. */
function recent(e) {
  const t = new Date(e.ts_start).getTime();
  return t >= HIST.recentStart;
}

async function loadAll(page = 0) {
  try {
    const [ev, meta] = await Promise.all([api('/api/events?' + histQuery(page)), api('/api/meta')]);
    if (page === 0) {
      // Refresh wins, but never drop taps that are still waiting to sync.
      const local = S.events.filter(e => e._pending);
      S.events = sortEvents(ev.events.concat(
        local.filter(l => !ev.events.some(x => x.client_id === l.client_id))));
    } else {
      const seen = new Set(S.events.map(x => x.id));
      S.events = S.events.concat(ev.events.filter(x => !seen.has(x.id)));
    }
    const w = histWindow(page);
    HIST.page = page; HIST.hasMore = !!ev.has_more; HIST.start = w.start; HIST.end = w.end;
    HIST.recentStart = histWindow(0).start;
    S.meta = meta;
    document.getElementById('loaderr').style.display = 'none';
  } catch (err) {
    const b = document.getElementById('loaderr');
    b.style.display = 'block';
    b.textContent = "⚠️ Can't load data — " + err.message + " · pull down to refresh, or close & reopen the page";
    return;
  }
  try {
    render();
    document.getElementById('foot').textContent =
      S.events.filter(inHistPage).length + ' entries · refreshed ' + new Date().toLocaleTimeString() + ' · build 14';
    flushOutbox();
  } catch (err) {
    const b = document.getElementById('loaderr');
    b.style.display = 'block';
    b.textContent = '⚠️ Display error — ' + err.message;
  }
}

function render() {
  renderHeader(); renderWarn(); renderStatus(); renderToday(); renderHistory();
}

function renderHeader() {
  const name = S.meta.baby_name || 'Baby';
  let age = '';
  if (S.meta.birth_date) {
    const b = new Date(S.meta.birth_date + 'T00:00:00');
    const days = Math.floor((Date.now() - b) / 864e5);
    if (days < 1) age = 'born today 🎉';
    else if (days < 15) age = days + ' day' + (days === 1 ? '' : 's') + ' old';
    else age = Math.floor(days / 7) + 'w ' + (days % 7) + 'd old';
  }
  document.title = name + ' · Log';
  document.getElementById('head-left').innerHTML =
    '<h1>🍼 ' + esc(name) + '</h1>' + (age ? '<div class="age">' + age + '</div>' : '');
}

function renderWarn() {
  const el = document.getElementById('warn');
  const pale = S.events.filter(recent).find(e => e.type === 'diaper' && e.details && e.details.color === 'pale');
  if (pale) {
    el.style.display = 'block';
    el.innerHTML = '⚠️ Pale/clay-colored stool logged ' + fmtAgo(pale.ts_start) +
      '. Pale stools can signal a bile/liver problem — mention it to your pediatrician.';
  } else el.style.display = 'none';
}

function renderStatus() {
  const el = document.getElementById('status');
  const recentEv = S.events.filter(recent);
  const lastFeed = recentEv.find(e => e.type === 'feed');
  const sleepOpen = openEv(e => e.type === 'sleep');
  const lastSleep = recentEv.find(e => e.type === 'sleep' && e.ts_end);
  const lastDiaper = recentEv.find(e => e.type === 'diaper');
  let f, sl, di;
  if (lastFeed) {
    f = '<span>🍼 ' + esc(describe(lastFeed).text) + ' · <span data-ago="' + lastFeed.ts_start + '">' + fmtAgo(lastFeed.ts_start) + '</span></span>';
  } else f = '<span>🍼 No feeds logged yet</span>';
  if (sleepOpen) {
    sl = '<span>😴 Sleeping · <span class="live" data-live="' + sleepOpen.ts_start + '">' + fmtClock(durSec(sleepOpen)) + '</span></span>' +
         '<button class="mini" onclick="stopOpen(\'sleep\')">Stop</button>';
  } else if (lastSleep) {
    sl = '<span>😴 ' + esc(describe(lastSleep).text) + ' · ended <span data-ago="' + lastSleep.ts_end + '">' + fmtAgo(lastSleep.ts_end) + '</span></span>';
  } else sl = '<span>😴 No naps logged yet</span>';
  if (lastDiaper) {
    di = '<span>' + describe(lastDiaper).icon + ' ' + esc(describe(lastDiaper).text) + ' · <span data-ago="' + lastDiaper.ts_start + '">' + fmtAgo(lastDiaper.ts_start) + '</span></span>';
  } else di = '<span>💧 No diapers logged yet</span>';
  el.innerHTML =
    '<div class="status-card' + (sleepOpen ? ' open' : '') + '">' + f + '</div>' +
    '<div class="status-card">' + di + '</div>' +
    '<div class="status-card' + (sleepOpen ? ' open' : '') + '">' + sl + '</div>';
}

function renderToday() {
  const todayKey = new Date().toDateString();
  const evs = S.events.filter(e => new Date(e.ts_start).toDateString() === todayKey);
  let bottleC = 0, diaperC = 0, oz = 0, ml = 0, wet = 0, solid = 0, sleepSec = 0, tummySec = 0;
  for (const e of evs) {
    if (e.type === 'feed') {
      bottleC++; const a = e.details.amount || 0; if (e.details.unit === 'ml') ml += a; else oz += a;
    } else if (e.type === 'diaper') {
      diaperC++;
      if (e.details.wet) wet++; if (e.details.solid) solid++;
    } else if (e.type === 'sleep') sleepSec += durSec(e);
    else if (e.type === 'tummy') tummySec += durSec(e);
  }
  const feeds = bottleC;
  const vol = [oz ? oz.toFixed(1) + ' oz' : '', ml ? Math.round(ml) + ' ml' : ''].filter(Boolean).join(' + ');
  let out = 'Feedings: ' + feeds + (feeds && vol ? ' — bottle ' + vol : '');
  out += '<br>Diapers: ' + diaperC + ' — 💧 ' + wet + ' · 💩 ' + solid +
         '<br>Sleep: ' + fmtDur(sleepSec);
  if (tummySec > 0) out += '<br>Tummy time: ' + fmtDur(tummySec);
  const nd = nextDose();
  if (nd) out += '<br>💊 ' + esc(nd.name) + (nd.dose ? ' ' + esc(nd.dose) : '') + ' — next ' +
    new Date(nd.due).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  document.getElementById('today').innerHTML = out;
}

function renderHistory() {
  const el = document.getElementById('history');
  const evs = S.events.filter(inHistPage);
  const fmtDay = t => new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' });
  const lastDay = new Date(HIST.end); lastDay.setDate(lastDay.getDate() - 1);
  document.getElementById('hist-title').textContent = fmtDay(HIST.start) + ' – ' + fmtDay(lastDay.getTime());
  const prev = document.getElementById('hist-prev'), next = document.getElementById('hist-next');
  if (prev) prev.disabled = HIST.page === 0;
  if (next) next.disabled = !HIST.hasMore;
  if (!evs.length) { el.innerHTML = '<p class="empty">No entries in this range.</p>'; return; }
  const groups = new Map();
  for (const e of evs) {
    const key = new Date(e.ts_start).toDateString();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const todayKey = new Date().toDateString();
  const yestKey = new Date(Date.now() - 864e5).toDateString();
  let html = '';
  for (const [key, evs] of groups) {
    const label = key === todayKey ? 'Today'
      : key === yestKey ? 'Yesterday'
      : new Date(key).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    html += '<h3>' + label + '</h3>';
    for (const e of evs) {
      const desc = describe(e);
      let live = '';
      if (!e.ts_end && e.type === 'sleep')
        live = ' <span class="live" data-live="' + e.ts_start + '">' + fmtClock(durSec(e)) + '</span>';
      const pending = !!e._pending;
      html += '<div class="row' + (pending ? ' pending' : '') + '"><span class="ri">' + desc.icon + '</span>' +
        '<span class="rt">' + fmtTime(e.ts_start) + '</span>' +
        '<span class="rd">' + esc(desc.text) + live +
          (pending ? '<span class="psync" title="waiting to sync">⏳</span>' : '') + '</span>' +
        (pending ? '' :
          '<button class="mini" onclick="editEvent(' + e.id + ')">✏️</button>' +
          '<button class="mini" onclick="delEvent(' + e.id + ')">🗑️</button>') + '</div>';
    }
  }
  el.innerHTML = html;
}

function tickLive() {
  document.querySelectorAll('[data-live]').forEach(el => {
    el.textContent = fmtClock((Date.now() - new Date(el.dataset.live)) / 1000);
  });
  document.querySelectorAll('[data-ago]').forEach(el => {
    el.textContent = fmtAgo(el.dataset.ago);
  });
}

/* ---------- bottom-sheet logging ---------- */

const sheetEl = document.getElementById('sheet');
const sheetBody = document.getElementById('sheet-body');
const scrimEl = document.getElementById('scrim');
let bUnit = 'oz';

/* ---------- optional entry time ("when") ----------
   Defaults to now and self-updates while the sheet is open. Untouched =
   log at submission time (server default). Editing the field backdates
   the entry; "Now" resets it. */
let whenDirty = false, whenTicker = null;

function whenRowHtml() {
  return '<div class="custom-row"><span style="flex:0 0 auto;font-size:13px;color:var(--silver)">When</span>' +
    '<input id="when" type="datetime-local" value="' + toLocalInput(new Date().toISOString()) + '" oninput="whenDirty=true">' +
    '<button class="mini" onclick="resetWhen()">Now</button></div>';
}
function resetWhen() {
  const i = document.getElementById('when');
  if (i) { i.value = toLocalInput(new Date().toISOString()); whenDirty = false; }
}
function whenTs() {
  if (!whenDirty) return null;
  const i = document.getElementById('when');
  if (!i || !i.value) return null;
  const d = new Date(i.value);
  return isNaN(d.getTime()) ? null : d.toISOString();
}

function openSheet() {
  whenDirty = false;
  renderChooser();
  scrimEl.style.display = 'block';
  sheetEl.classList.add('open');
  document.body.style.overflow = 'hidden';
  clearInterval(whenTicker);
  whenTicker = setInterval(() => { if (!whenDirty) resetWhen(); }, 15000);
}
function closeSheet() {
  sheetEl.classList.remove('open');
  scrimEl.style.display = 'none';
  document.body.style.overflow = '';
  clearInterval(whenTicker);
  whenTicker = null;
}

const TILES = [
  { kind: 'bottle', em: '🍼', lab: 'Bottle', cls: 't-bottle' },
  { kind: 'wet', em: '💧', lab: 'Wet diaper', cls: 't-wet' },
  { kind: 'solid', em: '💩', lab: 'Solid diaper', cls: 't-solid' },
  { kind: 'both', em: '💧💩', lab: 'Wet + solid', cls: 't-both' },
  { kind: 'nap', em: '😴', lab: 'Nap', cls: 't-nap' },
  { kind: 'tummy', em: '🤸', lab: 'Tummy time', cls: 't-tummy' },
  { kind: 'med', em: '💊', lab: 'Medication', cls: 't-med' },
  { kind: 'note', em: '📝', lab: 'Note', cls: 't-note' },
  { kind: 'more', em: '➕', lab: 'More', cls: 't-more' },
];

function renderChooser() {
  const open = openEv(e => e.type === 'sleep');
  const openT = openEv(e => e.type === 'tummy');
  let html = '<div class="sh-title">What happened?</div><div class="chooser">';
  for (const t of TILES) {
    let lab = t.lab;
    if (t.kind === 'nap') lab = open ? 'End nap · ' + fmtClock(durSec(open)) : 'Start nap';
    if (t.kind === 'tummy') lab = openT ? 'End tummy · ' + fmtClock(durSec(openT)) : 'Tummy time';
    html += '<button class="tile ' + t.cls + '" onclick="pick(\'' + t.kind + '\')">' +
      '<span class="em">' + t.em + '</span><span class="lab">' + esc(lab) + '</span></button>';
  }
  html += '</div><button class="back" onclick="closeSheet()">Cancel</button>';
  sheetBody.innerHTML = html;
}

function pick(kind) {
  if (kind === 'wet') return logDiaper(1, 0, null);
  if (kind === 'solid') return renderColor(0, 1);
  if (kind === 'both') return renderColor(1, 1);
  if (kind === 'bottle') return renderBottle();
  if (kind === 'note') return renderNote();
  if (kind === 'med') return renderMed();
  if (kind === 'more') return renderMore();
  if (kind === 'nap' || kind === 'tummy') { closeSheet(); toggleTimer(kind === 'nap' ? 'sleep' : 'tummy'); }
}

/* bottle: one-tap amount chips + custom entry */
const OZ_CHIPS = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8];
const ML_CHIPS = [30, 45, 60, 75, 90, 120, 150, 180, 210, 240];

function renderBottle() {
  const chips = (bUnit === 'oz' ? OZ_CHIPS : ML_CHIPS)
    .map(a => '<button class="chip" onclick="logBottle(' + a + ')">' + a + (bUnit === 'oz' ? ' oz' : ' ml') + '</button>')
    .join('');
  const step = bUnit === 'oz' ? '0.25' : '5';
  const ph = bUnit === 'oz' ? '4.5' : '130';
  sheetBody.innerHTML =
    '<div class="sh-title">🍼 Bottle — how much?</div>' +
    whenRowHtml() +
    '<div class="u-toggle">' +
    '<button class="u-btn' + (bUnit === 'oz' ? ' on' : '') + '" onclick="bUnit=\'oz\'; renderBottle()">oz</button>' +
    '<button class="u-btn' + (bUnit === 'ml' ? ' on' : '') + '" onclick="bUnit=\'ml\'; renderBottle()">ml</button></div>' +
    '<div class="chips">' + chips + '</div>' +
    '<div class="custom-row"><input id="b-amount" type="number" step="' + step + '" min="0" inputmode="decimal" placeholder="' + ph + '">' +
    '<button onclick="logBottle()">Log ' + bUnit + '</button></div>' +
    '<button class="back" onclick="renderChooser()">← Back</button>';
}

async function logBottle(amt) {
  const raw = amt ?? document.getElementById('b-amount').value;
  if (raw === '' || raw == null) { toast('Enter an amount first'); return; }
  const n = parseFloat(raw);
  if (!isFinite(n) || n < 0) { toast('⚠️ Amount must be a number'); return; }
  const body = { type: 'feed', details: { method: 'bottle', amount: n, unit: bUnit } };
  const ts = whenTs();
  if (ts) body.ts_start = ts;
  queueCreate(body, 'Bottle');
  closeSheet();
  toast('✓ Bottle · ' + n + ' ' + bUnit + ' logged', 2500);
}

/* solid diaper: optional color, each tap logs immediately */
const COLOR_LABELS = { black: 'black (meconium)', dark_green: 'dark green', green: 'green', yellow: 'yellow (seedy)', brown: 'brown', pale: 'pale / clay', other: 'other' };

function renderColor(wet, solid) {
  const what = wet && solid ? 'Wet + solid' : 'Solid';
  const chips = Object.keys(COLOR_LABELS).map(k =>
    '<button class="chip sw-' + k + '" onclick="logDiaper(' + wet + ',' + solid + ',\'' + k + '\')">' + COLOR_LABELS[k] + '</button>').join('');
  sheetBody.innerHTML =
    '<div class="sh-title">💩 ' + what + ' — color?</div>' +
    whenRowHtml() +
    '<div class="hint">Optional — tap a color to log it, or skip.</div>' +
    '<div class="chips">' + chips + '</div>' +
    '<button class="chip wide" onclick="logDiaper(' + wet + ',' + solid + ',null)">Log without color</button>' +
    '<button class="back" onclick="renderChooser()">← Back</button>';
}

async function logDiaper(wet, solid, color) {
  const body = { type: 'diaper', details: { wet: !!wet, solid: !!solid, color: color || null } };
  const ts = whenTs();
  if (ts) body.ts_start = ts;
  queueCreate(body, 'Diaper');
  closeSheet();
  toast('✓ Diaper logged', 2000);
}

function renderNote() {
  sheetBody.innerHTML =
    '<div class="sh-title">📝 Note</div>' +
    whenRowHtml() +
    '<div class="custom-row"><input id="n-text" type="text" placeholder="symptom, med, milestone…" onkeydown="if(event.key===\'Enter\')logNote()">' +
    '<button onclick="logNote()">Log</button></div>' +
    '<button class="back" onclick="renderChooser()">← Back</button>';
  setTimeout(() => { const i = document.getElementById('n-text'); if (i) i.focus(); }, 80);
}

async function logNote() {
  const text = document.getElementById('n-text').value.trim();
  if (!text) { toast('Type a note first'); return; }
  const body = { type: 'note', details: { text: text } };
  const ts = whenTs();
  if (ts) body.ts_start = ts;
  queueCreate(body, 'Note');
  closeSheet();
  toast('✓ Note logged', 2000);
}

/* ---------- Medication: its own log screen ---------- */

function renderMed() {
  sheetBody.innerHTML =
    '<div class="sh-title">💊 Medication</div>' +
    whenRowHtml() +
    '<div id="med-area"></div>' +
    '<button class="back" onclick="renderChooser()">← Back</button>';
  renderMedArea();
}

/* ---------- More: measurements ---------- */

const M_KINDS = [
  { k: 'weight', em: '⚖️', lab: 'Weight' },
  { k: 'length', em: '📏', lab: 'Length' },
  { k: 'head_circumference', em: '🎯', lab: 'Head' },
  { k: 'temperature', em: '🌡️', lab: 'Temp' },
];
const M_UNITS = { weight: ['lb', 'kg', 'oz'], length: ['in', 'cm'], head_circumference: ['in', 'cm'], temperature: ['F', 'C'] };
let mKind = null, mUnit = null;

function renderMore() {
  mKind = null;
  const kindChips = M_KINDS.map(k =>
    '<button class="chip" onclick="setMKind(\'' + k.k + '\')">' + k.em + ' ' + k.lab + '</button>').join('');
  sheetBody.innerHTML =
    '<div class="sh-title">➕ More</div>' +
    whenRowHtml() +
    '<div class="hint">Measurement — pick a kind, enter the value.</div>' +
    '<div class="chips">' + kindChips + '</div>' +
    '<div id="m-area"></div>' +
    '<button class="back" onclick="renderChooser()">← Back</button>';
}

function setMKind(k) {
  mKind = k;
  mUnit = M_UNITS[k][0];
  const unitChips = M_UNITS[k].map(u =>
    '<button class="u-btn' + (u === mUnit ? ' on' : '') + '" onclick="mUnit=\'' + u + '\'; setMKind(\'' + k + '\')">' +
    (k === 'temperature' ? '°' + u : u) + '</button>').join('');
  const ph = k === 'temperature' ? '98.6' : k === 'weight' ? '7.5' : k === 'head_circumference' ? '14.5' : '21';
  const step = k === 'temperature' ? '0.1' : k === 'weight' ? '0.05' : '0.25';
  if (k === 'weight' && mUnit === 'lb') {
    // Babies are weighed in pounds and ounces: 8 lb 6 oz.
    document.getElementById('m-area').innerHTML =
      '<div class="u-toggle">' + unitChips + '</div>' +
      '<div class="wt-row">' +
      '<div class="wt"><input id="m-lb" type="number" step="1" min="0" inputmode="numeric" placeholder="8"' +
      ' onkeydown="if(event.key===\'Enter\'){event.preventDefault();document.getElementById(\'m-oz\').focus();}"><span>lb</span></div>' +
      '<div class="wt"><input id="m-oz" type="number" step="0.1" min="0" max="15.9" inputmode="decimal" placeholder="6"' +
      ' onkeydown="if(event.key===\'Enter\'){event.preventDefault();logMeasurement();}"><span>oz</span></div>' +
      '<button onclick="logMeasurement()">Log</button></div>' +
      '<div class="hint">Pounds and ounces — leave ounces empty for a whole pound.</div>';
    const a = document.getElementById('m-lb');
    if (a) a.focus();
    return;
  }
  document.getElementById('m-area').innerHTML =
    '<div class="u-toggle">' + unitChips + '</div>' +
    '<div class="custom-row"><input id="m-value" type="number" step="' + step + '" min="0" inputmode="decimal" placeholder="' + ph + '">' +
    '<button onclick="logMeasurement()">Log</button></div>';
  const i = document.getElementById('m-value');
  if (i) i.focus();
}

function logMeasurement() {
  if (!mKind) { toast('Pick a measurement type first'); return; }
  let details;
  if (mKind === 'weight' && mUnit === 'lb') {
    const lbRaw = document.getElementById('m-lb').value.trim();
    const ozRaw = document.getElementById('m-oz').value.trim();
    const lb = lbRaw === '' ? 0 : parseFloat(lbRaw);
    const oz = ozRaw === '' ? 0 : parseFloat(ozRaw);
    if (!isFinite(lb) || !isFinite(oz) || (lb <= 0 && oz <= 0)) { toast('⚠️ Enter pounds and ounces'); return; }
    if (oz < 0 || oz >= 16) { toast('⚠️ Ounces must be 0–15.9'); return; }
    details = { kind: 'weight', unit: 'lb', lb: lb, oz: oz };
  } else {
    const n = parseFloat(document.getElementById('m-value').value);
    if (!isFinite(n) || n <= 0) { toast('⚠️ Enter a valid value'); return; }
    details = { kind: mKind, value: n, unit: mUnit };
  }
  const body = { type: 'measurement', details: details };
  const ts = whenTs();
  if (ts) body.ts_start = ts;
  queueCreate(body, 'Measurement');
  closeSheet();
  toast('✓ Measurement logged', 2000);
}

/* ---------- medication: repeat doses, interval, dose alerts ---------- */
const MED_INTERVALS = [240, 360, 480, 720];   // 4h, 6h, 8h, 12h
let MED_RECENT = [];

function fmtEvery(min) {
  const h = min / 60;
  return (Number.isInteger(h) ? h : h.toFixed(1)) + 'h';
}

/* Every distinct medication+dose given in the last 24h, newest first. */
function medRecent() {
  const cut = Date.now() - 24 * 3600 * 1000;
  const seen = {}, out = [];
  for (const e of S.events) {
    if (e.type !== 'medication') continue;
    if (new Date(e.ts_start).getTime() < cut) continue;
    const d = e.details || {};
    const key = (d.name || '').toLowerCase() + '|' + (d.dose || '');
    if (!d.name || seen[key]) continue;
    seen[key] = 1;
    out.push({ name: d.name, dose: d.dose || '', interval_min: d.interval_min || null });
  }
  return out;
}

function renderMedArea() {
  const el = document.getElementById('med-area');
  if (!el) return;
  MED_RECENT = medRecent();
  const chips = MED_RECENT.map((m, i) =>
    '<button class="chip med" onclick="pickMed(' + i + ')">' + esc(m.name) +
      (m.dose ? ' <span class="mdose">' + esc(m.dose) + '</span>' : '') + '</button>').join('');
  const ivChips = MED_INTERVALS.map(v =>
    '<button class="u-btn" data-min="' + v + '" onclick="setMedIv(' + v + ')">' + (v / 60) + 'h</button>').join('');
  el.innerHTML =
    (MED_RECENT.length ? '<div class="hint">Given in the last 24h — tap to repeat</div><div class="chips">' + chips + '</div>' : '') +
    '<div class="custom-row"><input id="med-name" type="text" placeholder="medication, e.g. Tylenol">' +
    '<input id="med-dose" type="text" placeholder="dose, e.g. 2.5 ml" style="flex:0 0 120px; min-width:96px"></div>' +
    '<div class="u-toggle">' + ivChips + '</div>' +
    '<div class="custom-row"><span style="flex:0 0 auto;font-size:13px;color:var(--silver)">Repeat</span>' +
    '<input id="med-interval" type="number" step="1" min="1" max="48" inputmode="numeric" placeholder="hours — optional">' +
    '<button onclick="logMed()">Log dose</button></div>' +
    '<div class="hint" style="margin-top:-4px">Repeat sets the next-dose reminder (30 min before).</div>' +
    medAlertNote();
}

function pickMed(i) {
  const m = MED_RECENT[i];
  if (!m) return;
  document.getElementById('med-name').value = m.name;
  document.getElementById('med-dose').value = m.dose || '';
  setMedIv(m.interval_min || null);
  const d = document.getElementById('med-dose');
  if (!d.value) d.focus();
}

function setMedIv(min) {
  const inp = document.getElementById('med-interval');
  if (inp) inp.value = min ? min / 60 : '';
  document.querySelectorAll('#med-area .u-toggle .u-btn').forEach(b =>
    b.classList.toggle('on', !!min && b.dataset.min === String(min)));
}

function logMed() {
  const name = document.getElementById('med-name').value.trim();
  const dose = document.getElementById('med-dose').value.trim();
  if (!name) { toast('Enter the medication name'); document.getElementById('med-name').focus(); return; }
  const ivh = parseFloat(document.getElementById('med-interval').value);
  const details = { name: name, dose: dose || null };
  if (isFinite(ivh) && ivh > 0) {
    if (ivh > 48) { toast('Repeat must be 48 hours or less'); return; }
    details.interval_min = Math.round(ivh * 60);
  }
  const body = { type: 'medication', details: details };
  const ts = whenTs();
  if (ts) body.ts_start = ts;
  queueCreate(body, 'Medication');
  closeSheet();
  let msg = '✓ ' + name + (dose ? ' ' + dose : '');
  if (details.interval_min) {
    const base = ts ? new Date(ts).getTime() : Date.now();
    const next = new Date(base + details.interval_min * 60000)
      .toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    msg += ' — next dose ' + next;
    if (('Notification' in window) && Notification.permission !== 'granted')
      msg += ' · turn on reminders';
  }
  toast(msg, 3200);
}

/* Soonest upcoming dose across all medications (latest dose per name wins). */
function nextDose() {
  const latest = {};
  for (const e of S.events) {
    if (e.type !== 'medication') continue;
    const d = e.details || {};
    const k = (d.name || '').toLowerCase();
    if (!k) continue;
    const t = new Date(e.ts_start).getTime();
    if (!latest[k] || t > latest[k].t) latest[k] = { name: d.name, dose: d.dose, iv: d.interval_min, t: t };
  }
  let best = null;
  for (const m of Object.values(latest)) {
    if (!m.iv) continue;
    const due = m.t + m.iv * 60000;
    if (due < Date.now() - 20 * 60000) continue;
    if (!best || due < best.due) best = { name: m.name, dose: m.dose, due: due };
  }
  return best;
}

function alertsOn() {
  return ('Notification' in window) && Notification.permission === 'granted' &&
         localStorage.getItem('push_ok') === '1';
}

function medAlertNote() {
  if (alertsOn()) return '';
  return '<div class="hint">🔔 Reminders are off — <a href="#" onclick="closeSheet(); openSettings(); return false">turn them on in Settings</a>.</div>';
}

function refreshAlerts() {
  const a = document.getElementById('alert-area');
  if (a) a.innerHTML = medAlertsHtml();
  const m = document.getElementById('med-area');
  if (m) renderMedArea();
}

function medAlertsHtml() {
  const supported = ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
  if (!supported) return '<div class="hint">Dose reminders need the app added to the Home Screen (iOS 16.4+).</div>';
  if (Notification.permission === 'denied')
    return '<div class="hint">🔕 Notifications are blocked — allow them in Settings → Notifications → Baby log.</div>';
  if (alertsOn())
    return '<div class="hint">🔔 Dose reminders on · <a href="#" onclick="sendTestPush();return false">send a test</a></div>';
  return '<div class="custom-row" style="margin-top:2px"><button onclick="enableAlerts()">🔔 Turn on dose reminders</button></div>';
}

function urlB64ToUint8Array(base64) {
  const pad = '='.repeat((4 - base64.length % 4) % 4);
  const raw = atob((base64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function ensurePush(force) {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return false;
  if (!('Notification' in window) || Notification.permission !== 'granted') return false;
  if (!force && window.__pushDone) return true;
  if (window.__pushBusy) return false;
  window.__pushBusy = true;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!force && sub && localStorage.getItem('push_ok') === '1') {
      window.__pushDone = true;
      return true;
    }
    if (!sub) {
      const k = await api('/api/push/key');
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(k.key)
      });
    }
    await api('/api/push/subscribe', { method: 'POST', body: JSON.stringify(sub) });
    localStorage.setItem('push_ok', '1');
    window.__pushDone = true;
    return true;
  } catch (err) {
    console.warn('push setup failed', err);
    return false;
  } finally {
    window.__pushBusy = false;
  }
}

async function enableAlerts() {
  try {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      toast('Add the app to your Home Screen first, then try again', 3500); return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { toast('Notifications were not allowed'); return; }
    const ok = await ensurePush(true);
    toast(ok ? '🔔 Dose reminders on' : '⚠️ Could not register this device', 3000);
    refreshAlerts();
  } catch (err) { toast('⚠️ ' + err.message); }
}

async function sendTestPush() {
  try {
    const r = await api('/api/push/test', { method: 'POST' });
    if (r.sent) toast('✓ Test sent to ' + r.sent + ' device' + (r.sent > 1 ? 's' : ''), 3000);
    else if (!r.subs) toast('No device registered yet — tap “Turn on dose reminders” first', 4000);
    else toast('⚠️ Delivery failed: ' + (r.error || 'unknown error'), 6000);
  } catch (err) { toast('⚠️ ' + err.message); }
}

async function toggleTimer(kind) {
  const open = openEv(e => e.type === kind);
  if (open && open._pending) { toast('⏳ Still saving — try again in a minute', 2500); return; }
  try {
    if (open) {
      const d = durSec(open);
      await api('/api/events/' + open.id + '/stop', { method: 'POST', body: '{}' });
      await loadAll();
      toast('✓ ' + (kind === 'sleep' ? 'Nap' : 'Tummy time') + ' saved · ' + fmtDur(d), 2500);
    } else {
      queueCreate({ type: kind, details: {} }, kind === 'sleep' ? 'Nap' : 'Tummy time');
      toast('✓ ' + (kind === 'sleep' ? 'Nap' : 'Tummy time') + ' started', 2500);
    }
  } catch (err) { toast('⚠️ ' + err.message); }
}

async function stopOpen(kind) {
  const open = openEv(e => e.type === kind);
  if (!open) return;
  if (open._pending) { toast('⏳ Still saving — try again in a minute', 2500); return; }
  try { await api('/api/events/' + open.id + '/stop', { method: 'POST', body: '{}' }); await loadAll(); toast('✓ Saved', 2000); }
  catch (err) { toast('⚠️ ' + err.message); }
}

async function delEvent(id) {
  if (!confirm('Delete this entry?')) return;
  try { await api('/api/events/' + id, { method: 'DELETE' }); await loadAll(); toast('✓ Deleted', 2000); }
  catch (err) { toast('⚠️ ' + err.message); }
}

/* ---------- edit + settings ---------- */

function closeModal() { document.getElementById('modal').style.display = 'none'; }

let editId = null;

function editEvent(id) {
  editId = id;
  const e = S.events.find(x => x.id === id);
  if (!e) return;
  const d = e.details || {};
  let extra = '';
  if (e.type === 'feed') {
    extra = '<label>Amount<input id="e-amount" type="number" step="0.25" min="0" value="' + (d.amount ?? '') + '"></label>' +
      '<label>Unit<select id="e-unit"><option value="oz"' + (d.unit === 'oz' ? ' selected' : '') + '>oz</option>' +
      '<option value="ml"' + (d.unit === 'ml' ? ' selected' : '') + '>ml</option></select></label>';
  } else if (e.type === 'diaper') {
    extra = '<label class="check"><input id="e-wet" type="checkbox"' + (d.wet ? ' checked' : '') + '> Wet</label>' +
      '<label class="check"><input id="e-solid" type="checkbox"' + (d.solid ? ' checked' : '') + '> Solid</label>' +
      '<label>Color<select id="e-color"><option value="">—</option>' + colorOptions(d.color) + '</select></label>';
  } else if (e.type === 'note') {
    extra = '<label>Text<input id="e-text" type="text" value="' + esc(d.text || '') + '"></label>';
  } else if (e.type === 'measurement') {
    const wtLb = d.kind === 'weight' && (d.unit || 'lb') === 'lb';
    const lbVal = d.lb != null ? d.lb : Math.floor(d.value || 0);
    const ozVal = d.oz != null ? d.oz : Math.round((((d.value || 0) - Math.floor(d.value || 0)) * 16) * 10) / 10;
    extra = '<label>Kind<select id="e-mkind">' + M_KINDS.map(k =>
      '<option value="' + k.k + '"' + (d.kind === k.k ? ' selected' : '') + '>' + k.lab + '</option>').join('') + '</select></label>' +
      (wtLb
        ? '<label>Pounds<input id="e-mlb" type="number" step="1" min="0" value="' + lbVal + '"></label>' +
          '<label>Ounces<input id="e-moz" type="number" step="0.1" min="0" max="15.9" value="' + ozVal + '"></label>'
        : '<label>Value<input id="e-mvalue" type="number" step="any" min="0" value="' + (d.value ?? '') + '"></label>') +
      '<label>Unit<input id="e-munit" type="text" value="' + esc(d.unit || '') + '" placeholder="lb / in / F"></label>';
  } else if (e.type === 'medication') {
    extra = '<label>Name<input id="e-medname" type="text" value="' + esc(d.name || '') + '"></label>' +
      '<label>Dose<input id="e-meddose" type="text" value="' + esc(d.dose || '') + '"></label>' +
      '<label>Repeat every (hours)<input id="e-mediv" type="number" step="1" min="1" max="48" value="' +
      (d.interval_min ? d.interval_min / 60 : '') + '"></label>';
  }
  const canEnd = e.type === 'sleep' || e.type === 'tummy';
  const endField = canEnd
    ? '<label>End (empty = still open)<input id="e-end" type="datetime-local" value="' + (e.ts_end ? toLocalInput(e.ts_end) : '') + '"></label>'
    : '';
  document.getElementById('modal-body').innerHTML =
    '<h2>Edit ' + e.type + '</h2>' +
    '<label>Start<input id="e-start" type="datetime-local" value="' + toLocalInput(e.ts_start) + '"></label>' +
    endField + extra +
    '<label>Note<input id="e-note" type="text" value="' + esc(d.note || '') + '"></label>' +
    '<div class="modal-btns"><button onclick="saveEdit()">Save</button>' +
    '<button onclick="closeModal()">Cancel</button>' +
    '<button class="danger" onclick="delEvent(' + e.id + '); closeModal()">Delete</button></div>';
  document.getElementById('modal').style.display = 'flex';
}

function colorOptions(sel) {
  return Object.keys(COLOR_LABELS).map(k =>
    '<option value="' + k + '"' + (k === sel ? ' selected' : '') + '>' + COLOR_LABELS[k] + '</option>').join('');
}

async function saveEdit() {
  const e = S.events.find(x => x.id === editId);
  if (!e) return;
  const start = document.getElementById('e-start').value;
  if (!start) { toast('Start time is required'); return; }
  const body = { ts_start: new Date(start).toISOString() };
  const endEl = document.getElementById('e-end');
  if (endEl) {
    if (endEl.value) body.ts_end = new Date(endEl.value).toISOString();
    else if (e.ts_end) body.ts_end = body.ts_start; // was closed, now zero-length
  }
  const d = Object.assign({}, e.details || {});
  if (e.type === 'feed') {
    const amt = document.getElementById('e-amount').value;
    d.amount = amt === '' ? null : parseFloat(amt);
    d.unit = document.getElementById('e-unit').value;
  } else if (e.type === 'diaper') {
    d.wet = document.getElementById('e-wet').checked;
    d.solid = document.getElementById('e-solid').checked;
    if (!d.wet && !d.solid) { toast('Check wet and/or solid'); return; }
    d.color = document.getElementById('e-color').value || null;
  } else if (e.type === 'note') {
    d.text = document.getElementById('e-text').value.trim();
    if (!d.text) { toast('Text is required'); return; }
  } else if (e.type === 'measurement') {
    d.kind = document.getElementById('e-mkind').value;
    const lbEl = document.getElementById('e-mlb');
    if (lbEl && d.kind === 'weight') {
      const lb = parseFloat(lbEl.value || '0');
      const oz = parseFloat((document.getElementById('e-moz').value || '0'));
      if (!isFinite(lb) || !isFinite(oz) || lb + oz <= 0) { toast('Enter pounds and ounces'); return; }
      if (oz < 0 || oz >= 16) { toast('Ounces must be 0–15.9'); return; }
      d.lb = lb; d.oz = oz; d.value = Math.round((lb + oz / 16) * 10000) / 10000; d.unit = 'lb';
    } else {
      const v = parseFloat(document.getElementById('e-mvalue').value);
      if (!isFinite(v) || v <= 0) { toast('Value must be a positive number'); return; }
      d.value = v;
      delete d.lb; delete d.oz;
      d.unit = document.getElementById('e-munit').value.trim() || null;
    }
  } else if (e.type === 'medication') {
    d.name = document.getElementById('e-medname').value.trim();
    if (!d.name) { toast('Name is required'); return; }
    d.dose = document.getElementById('e-meddose').value.trim() || null;
    const ivEl = document.getElementById('e-mediv');
    const ivh = ivEl ? parseFloat(ivEl.value) : NaN;
    d.interval_min = isFinite(ivh) && ivh > 0 ? Math.round(Math.min(ivh, 48) * 60) : null;
  }
  const noteEl = document.getElementById('e-note');
  d.note = noteEl && noteEl.value.trim() ? noteEl.value.trim() : null;
  body.details = d;
  try {
    await api('/api/events/' + e.id, { method: 'PATCH', body: JSON.stringify(body) });
    closeModal(); await loadAll(); toast('✓ Updated', 2000);
  } catch (err) { toast('⚠️ ' + err.message); }
}

function openSettings() {
  document.getElementById('modal-body').innerHTML =
    '<h2>Settings</h2>' +
    '<label>Baby name<input id="s-name" type="text" value="' + esc(S.meta.baby_name || '') + '"></label>' +
    '<label>Birth date<input id="s-birth" type="date" value="' + esc(S.meta.birth_date || '') + '"></label>' +
    '<div class="hint" style="margin-top:16px">Dose reminders</div>' +
    '<div id="alert-area">' + medAlertsHtml() + '</div>' +
    '<div class="modal-btns"><button onclick="saveSettings()">Save</button><button onclick="closeModal()">Cancel</button></div>';
  document.getElementById('modal').style.display = 'flex';
}

async function saveSettings() {
  S.meta.baby_name = document.getElementById('s-name').value.trim();
  S.meta.birth_date = document.getElementById('s-birth').value;
  try {
    for (const k of ['baby_name', 'birth_date'])
      await api('/api/meta', { method: 'POST', body: JSON.stringify({ key: k, value: S.meta[k] || '' }) });
    closeModal(); render();
  } catch (err) { toast('⚠️ ' + err.message); }
}

loadAll();
ensurePush();
setInterval(tickLive, 1000);
setInterval(() => loadAll(HIST.page).catch(() => {}), 60000);
if ('serviceWorker' in navigator) {
  var swRoot = window.APP_ROOT || './';
  navigator.serviceWorker.register(swRoot + 'sw.js', { scope: swRoot }).catch(() => {});
}
</script>
</body>
</html>
"""


REPORT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Baby Log">
<meta name="theme-color" content="#faf9f7">
<!--PWA_HEAD-->
<title>Baby Log — History</title>
<style>
  :root {
    --cream:#faf9f7; --oat:#dad4c8; --oat-light:#eee9df;
    --ink:#1a1917; --silver:#9f9b93; --charcoal:#55534e;
    --lemon-tint:#fdf3d9; --lemon-border:#f5e2ae; --lemon-deep:#d08a11;
    --ube-tint:#f1ecfd; --ube-border:#d9ccf7;
    --matcha-tint:#e9f8ef; --matcha-border:#bfeccb;
    --slushie-tint:#e7f9fe; --slushie-border:#b5ecfb;
    --pome-tint:#fdeff0; --pome-border:#f7cdd2;
    --clay-shadow:0 1px 1px rgba(0,0,0,.10), inset 0 -1px 1px rgba(0,0,0,.04), 0 -0.5px 1px rgba(0,0,0,.05);
  }
  * { box-sizing:border-box; }
  button, .mini, .chip { touch-action:manipulation; }
  body { margin:0; font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif; background:var(--cream); color:var(--ink); font-size:16px; -webkit-tap-highlight-color:transparent; }
  .wrap { max-width:640px; margin:0 auto; padding:calc(16px + env(safe-area-inset-top,0px)) 14px calc(60px + env(safe-area-inset-bottom,0px)); }
  header { display:flex; justify-content:space-between; align-items:flex-start; padding:2px 2px 14px; }
  .backlink { color:var(--silver); text-decoration:none; font-size:14px; font-weight:500; }
  h1 { margin:2px 0 0; font-size:24px; font-weight:600; letter-spacing:-0.5px; }
  .age { color:var(--silver); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:1.08px; margin-top:3px; }
  .card { background:#fff; border:1px solid var(--oat); border-radius:24px; padding:18px; margin-bottom:14px; box-shadow:var(--clay-shadow); }
  h2 { margin:0 0 12px; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:1.08px; color:var(--silver); }
  h3 { margin:14px 0 6px; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:1.08px; color:var(--charcoal); border-bottom:1px dashed var(--oat); padding-bottom:6px; }
  .warn { background:#fdf0f1; border:1px solid #f6c3c9; color:#8a2b33; border-radius:16px; padding:12px 14px; margin-bottom:10px; font-size:14px; }
  button { font-family:inherit; font-size:15px; font-weight:500; padding:10px 14px; border-radius:14px; border:1px solid var(--oat); background:#fff; color:var(--ink); cursor:pointer; min-height:48px; box-shadow:var(--clay-shadow); transition:transform .06s ease; touch-action:manipulation; }
  button:active { transform:translateY(2px) scale(.98); box-shadow:none; }
  button:focus-visible { outline:2px solid rgb(20,110,245); outline-offset:2px; }
  .mini { min-height:0; padding:6px 10px; font-size:13px; border-radius:10px; }
  .danger { border-color:#f2b8bd; color:#b3392f; }
  .presets { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
  .preset { min-height:0; padding:8px 14px; border-radius:999px; font-weight:600; }
  .preset.on { background:var(--ink); color:#fff; border-color:var(--ink); }
  .row { display:flex; align-items:center; gap:8px; padding:8px 0; border-bottom:1px solid var(--oat-light); font-size:14.5px; }
  .row:last-child { border-bottom:none; }
  .row.pending { opacity:.78; }
  .psync { font-size:12px; margin-left:5px; }
  .ri { width:28px; text-align:center; }
  .rt { color:var(--silver); width:58px; flex-shrink:0; font-size:13px; }
  .rd { flex:1; }
  .empty { color:var(--silver); padding:8px 0; }
  .today-line { font-size:15px; line-height:1.75; }
  .dayline { font-size:14.5px; line-height:1.65; color:var(--charcoal); }
  .gap { color:var(--lemon-deep); font-weight:600; }
  details.daycard { background:#fff; border:1px solid var(--oat); border-radius:18px; padding:12px 16px; margin-bottom:10px; box-shadow:var(--clay-shadow); }
  details.daycard[open] { padding-bottom:6px; }
  details.daycard summary { list-style:none; cursor:pointer; display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
  details.daycard summary::-webkit-details-marker { display:none; }
  .dlabel { font-size:16px; font-weight:600; letter-spacing:-0.2px; white-space:nowrap; }
  .dsum { font-size:13.5px; color:var(--charcoal); text-align:right; }
  .caret { color:var(--silver); font-size:12px; }
  .daycard.dimmed { opacity:.45; }
  .barwrap { margin-top:10px; }
  .barrow { display:flex; align-items:flex-end; gap:3px; height:54px; touch-action:pan-y; -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; }
  .barrow .b { flex:1; border-radius:4px 4px 0 0; min-height:2px; }
  .barlabel { font-size:10px; color:var(--silver); letter-spacing:.6px; text-transform:uppercase; margin:6px 0 2px; display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
  .barlabel span:last-child { text-transform:none; letter-spacing:0; font-size:10.5px; }
  .b-vol { background:var(--matcha-border); }
  .b-slp { background:var(--ube-border); }
  .b-gap { background:var(--pome-border); }
  .barrow .b { opacity:.9; transition:box-shadow .08s ease, opacity .08s ease; }
  .barrow .b.zero { opacity:.3; }
  .barrow .b.on { opacity:1; box-shadow:0 0 0 2px var(--ink); }
  .chart-read { margin-top:11px; border-top:1px dashed var(--oat); padding-top:9px; font-variant-numeric:tabular-nums; }
  .cr-date { font-size:11.5px; font-weight:600; text-transform:uppercase; letter-spacing:.7px; color:var(--silver); }
  .cr-lines { font-size:13.5px; color:var(--charcoal); line-height:1.65; }
  .cr-lines b { color:var(--ink); font-weight:600; }
  .cr-hint { font-size:11.5px; color:var(--silver); margin-top:5px; }
  .rpt-off { display:none !important; }
  .cal-nav { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
  #cal-title { font-size:16px; font-weight:600; letter-spacing:-0.2px; }
  .cal-week { display:grid; grid-template-columns:repeat(7, 1fr); gap:3px; font-size:10px; color:var(--silver); text-transform:uppercase; letter-spacing:.5px; text-align:center; margin-bottom:3px; }
  #cal-grid { display:grid; grid-template-columns:repeat(7, 1fr); gap:3px; }
  .cal-cell { min-height:52px; border-radius:9px; padding:4px 5px; background:#fff; border:1px solid var(--oat-light); font-size:11px; line-height:1.35; cursor:pointer; display:flex; flex-direction:column; overflow:hidden; }
  .cal-cell.blank { border:none; background:transparent; cursor:default; }
  .cal-cell .cd { font-weight:600; font-size:12px; }
  .cal-cell .cs { color:var(--charcoal); white-space:nowrap; }
  .cal-cell .cn { color:var(--silver); }
  .cal-cell.future { opacity:.4; }
  .cal-cell.today { border-color:var(--ink); }
  .cal-cell.insel { border-color:var(--ube-border); box-shadow:inset 0 0 0 1px var(--ube-border); }
  .cal-cell.sel { border-color:var(--ink); box-shadow:inset 0 0 0 2px var(--ink); }
  .cal-legend { font-size:11.5px; color:var(--silver); margin-top:8px; display:flex; align-items:center; gap:5px; }
  .cal-swatch { width:11px; height:11px; border-radius:3px; display:inline-block; }
  .tl-nav { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
  #tl-title { font-size:15px; font-weight:600; letter-spacing:-0.2px; }
  .tl-head { display:grid; grid-template-columns:34px repeat(7, 1fr); gap:0; font-size:10.5px; color:var(--silver); text-align:center; margin-bottom:2px; }
  .tl-grid { display:grid; grid-template-columns:34px repeat(7, 1fr); gap:0; position:relative; }
  .tl-gutter, .tl-col { height:480px; }
  .tl-gutter { position:relative; font-size:9.5px; color:var(--silver); }
  .tl-gutter span { position:absolute; right:5px; transform:translateY(-50%); font-variant-numeric:tabular-nums; }
  .tl-col { position:relative; border-left:1px solid var(--oat-light); background-image:repeating-linear-gradient(to bottom, transparent 0, transparent calc(100%/24 - 1px), var(--oat-light) calc(100%/24 - 1px), var(--oat-light) calc(100%/24)); }
  .tl-col .dlabel { position:absolute; top:2px; left:3px; font-size:9.5px; color:var(--silver); font-weight:600; }
  .tl-col.todaycol { background-color:#fdf3d9; }
  .tl-blk { position:absolute; left:2px; right:2px; border-radius:5px; overflow:hidden; cursor:pointer; display:flex; align-items:flex-start; justify-content:center; font-size:9px; }
  .tl-blk.sleep { background:var(--ube-tint); border:1px solid var(--ube-border); color:var(--charcoal); }
  .tl-blk.tummy { background:var(--slushie-tint); border:1px solid var(--slushie-border); }
  .tl-now { position:absolute; left:0; right:0; height:2px; background:#d66; z-index:5; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:10px; }
  .chip { min-height:0; padding:7px 13px; border-radius:999px; font-size:14px; font-weight:600; }
  .chip.on { background:var(--ink); color:#fff; border-color:var(--ink); }
  #modal { position:fixed; inset:0; background:rgba(31,29,26,.45); display:none; align-items:center; justify-content:center; z-index:70; padding:14px; }
  #modal .card { width:100%; max-width:480px; max-height:88vh; overflow:auto; margin:0; }
  #modal label { display:flex; flex-direction:column; gap:4px; font-size:13px; font-weight:500; color:var(--charcoal); margin-bottom:12px; }
  #modal input, #modal select { font-family:inherit; font-size:16px; padding:10px; border-radius:12px; border:1px solid #cfc8ba; background:#fff; color:var(--ink); }
  #modal input[type=checkbox] { transform:scale(1.3); }
  #modal label.check { flex-direction:row; align-items:center; gap:8px; font-size:16px; color:var(--ink); }
  .modal-btns { display:flex; gap:8px; margin-top:6px; }
  .modal-btns button { flex:1; }
  #toast { display:none; position:fixed; top:14px; left:50%; transform:translateX(-50%); background:#26221c; color:#fff; padding:12px 20px; border-radius:999px; font-size:15px; z-index:80; max-width:90vw; box-shadow:-4px 4px 0 rgba(0,0,0,.12); }
  #foot { color:var(--silver); font-size:12px; text-align:center; padding:6px 0 0; }
  @media print {
    body { background:#fff; }
    .card, details.daycard { box-shadow:none; break-inside:avoid; }
    .presets, .chips, .cal-nav, #modal, #toast, #foot, header button, .cr-hint { display:none !important; }
    details.daycard { border-color:#bbb; }
  }
  .intake-svg { width:100%; height:auto; display:block; margin:2px 0 4px; }
  .intake-svg .ax { font-size:9px; fill:var(--silver); }
  .intake-legend { font-size:11.5px; color:var(--silver); display:flex; gap:14px; margin:0 0 8px; }
  .intake-legend i { display:inline-block; width:14px; height:0; border-top:2.2px solid var(--ink); vertical-align:middle; margin-right:5px; }
  .intake-legend i.dash { border-top:1.6px dashed #b9b3a7; }
  .intake-sum { font-size:13px; color:var(--charcoal); line-height:1.5; }
  .intake-sum .k { color:var(--silver); }
  .intake-sum .delta { margin-top:2px; font-variant-numeric:tabular-nums; }
  .intake-sum .delta.up { color:#2f6b45; }
  .intake-sum .delta.down { color:#8a4a1f; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <a class="backlink" id="backlink" href="/">← Tracker</a>
      <h1>📊 History</h1>
      <div class="age" id="age"></div>
    </div>
    <div style="display:flex; gap:6px;">
      <button class="mini" onclick="openReportPrefs()" title="Choose which reports to show">⚙️</button>
      <button class="mini" onclick="window.print()">🖨️</button>
    </div>
  </header>
  <div id="loaderr" class="warn" style="display:none"></div>

  <section class="card" id="calendar-card">
    <h2 style="display:flex; align-items:center; justify-content:space-between;">Dates
      <button class="mini" onclick="exportCsv()">⬇️ Export CSV</button></h2>
    <div class="cal-nav">
      <button class="mini" onclick="calShift(-1)">‹</button>
      <div id="cal-title"></div>
      <button class="mini" onclick="calShift(1)">›</button>
    </div>
    <div class="cal-week"><span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span></div>
    <div id="cal-grid"></div>
    <div class="presets" id="presets" style="margin:10px 0 0"></div>
    <div class="cal-legend">
      <span id="cal-sleep-key"><span class="cal-swatch" style="background:var(--ube-border)"></span> more sleep · </span>tap a day to view it · tap a second day for a range · tap it again to clear</div>
  </section>

  <section class="card" id="totals-card"><h2>Totals</h2><div id="totals" class="today-line"></div><div id="chart"></div></section>

  <section class="card" id="intake-card" style="display:none">
    <h2>Eaten so far: today vs typical</h2>
    <div id="intake"></div>
  </section>

  <section class="card" id="growth-card" style="display:none"><h2>Growth &amp; Health</h2><div id="growth"></div></section>

  <section class="card" id="timeline-card">
    <h2>Sleep timeline</h2>
    <div class="tl-nav">
      <button class="mini" onclick="tlShift(-1)">‹</button>
      <div id="tl-title"></div>
      <button class="mini" onclick="tlShift(1)">›</button>
    </div>
    <div class="tl-head" id="tl-head"></div>
    <div class="tl-grid" id="tl-grid"></div>
    <div class="cal-legend">
      <span class="cal-swatch" style="background:var(--ube-tint); border:1px solid var(--ube-border)"></span> sleep
      <span class="cal-swatch" style="background:var(--slushie-tint); border:1px solid var(--slushie-border)"></span> tummy time
      · tap a block to open that day
    </div>
  </section>

  <section class="card" id="daily-card">
    <h2>Daily</h2>
    <div class="chips" id="filters"></div>
    <div id="days"></div>
  </section>
  <div id="foot"></div>
</div>

<div id="modal" onclick="if(event.target===this)closeModal()"><div id="modal-body" class="card"></div></div>
<div id="toast"></div>

<script>
// Same mount-path logic as the main page (works under a path prefix mount),
// minus this page's own /report suffix.
const BASE = location.pathname.replace(new RegExp('/+$'), '').replace(/\/report$/, '');
document.getElementById('backlink').href = BASE + '/';

const S = { meta: {}, filter: 'all', range: { days: 14 }, prefs: null };
let CHART = { days: [], sel: null, flags: {} };
let DATA = null;

function esc(s) { return String(s ?? '').replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';'); }

async function api(path, opts = {}) {
  let url = BASE + path;
  if (!opts.method || opts.method === 'GET') url += (url.includes('?') ? '&' : '?') + '_t=' + Date.now();
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail ?? t; } catch {} throw new Error(t); }
  return r.json();
}

function toast(msg, ms = 4000) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', ms);
}

function fmtDur(sec) {
  sec = Math.floor(sec);
  if (sec < 60) return sec + 's';
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? h + 'h ' + m + 'm' : m + 'm';
}
function fmtTime(iso) { return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }

/* Weight is stored as decimal pounds (value) plus the exact lb/oz the parent
   typed, so display never drifts: 8 lb 6 oz round-trips as "8 lb 6 oz". */
function fmtLbOz(d) {
  let lb, oz;
  if (d.lb != null) { lb = Math.floor(d.lb); oz = d.oz || 0; }
  else {
    lb = Math.floor(d.value);
    oz = Math.round(((d.value - lb) * 16) * 10) / 10;
    if (oz >= 16) { lb += 1; oz = 0; }
  }
  const extra = lb + Math.floor(oz / 16);
  lb = extra; oz = Math.round((oz % 16) * 10) / 10;
  return lb + ' lb ' + (Number.isInteger(oz) ? oz : oz.toFixed(1)) + ' oz';
}
function toLocalInput(iso) {
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function durSec(e) {
  const end = e.ts_end ? new Date(e.ts_end) : Date.now();
  return Math.max(0, (end - new Date(e.ts_start)) / 1000);
}

const COLOR_LABELS = { black: 'black (meconium)', dark_green: 'dark green', green: 'green', yellow: 'yellow (seedy)', brown: 'brown', pale: 'pale / clay', other: 'other' };

function describe(e) {
  const d = e.details || {};
  if (e.type === 'feed') {
    const amt = d.amount ? ' · ' + d.amount + ' ' + (d.unit || 'oz') : '';
    return { icon: '🍼', text: 'Bottle' + amt };
  }
  if (e.type === 'diaper') {
    const parts = [];
    if (d.wet) parts.push('Wet');
    if (d.solid) parts.push('Solid');
    if (d.color) parts.push('(' + String(d.color).replace('_',' ') + ')');
    const icon = d.wet && d.solid ? '💧💩' : (d.solid ? '💩' : '💧');
    return { icon, text: parts.join(' + ') };
  }
  if (e.type === 'sleep') return { icon: '😴', text: 'Sleep · ' + fmtDur(durSec(e)) };
  if (e.type === 'tummy') return { icon: '🤸', text: 'Tummy time · ' + fmtDur(durSec(e)) };
  if (e.type === 'measurement') {
    const labels = { weight: 'Weight', length: 'Length', head_circumference: 'Head', temperature: 'Temp' };
    if (d.kind === 'weight' && (d.unit || 'lb') === 'lb')
      return { icon: '⚖️', text: 'Weight: ' + fmtLbOz(d) };
    const unit = d.kind === 'temperature' ? ' °' + d.unit : ' ' + d.unit;
    return { icon: d.kind === 'temperature' ? '🌡️' : '⚖️', text: (labels[d.kind] || 'Measurement') + ': ' + d.value + unit };
  }
  if (e.type === 'medication') {
    return { icon: '💊', text: 'Medication · ' + (d.name || '?') + (d.dose ? ' · ' + d.dose : '') +
      (d.interval_min ? ' · every ' + fmtEvery(d.interval_min) : '') };
  }
  return { icon: '📝', text: d.text || 'Note' };
}

/* ---------- range selection (the calendar is the date filter) ---------- */

const DEFAULT_DAYS = 14;
const PRESETS = [7, 14, 30, 90, 730];

function renderPresets() {
  const el = document.getElementById('presets');
  el.innerHTML = PRESETS.map(n =>
    '<button class="preset' + (!S.range.start && S.range.days === n ? ' on' : '') + '" onclick="setPreset(' + n + ')">' +
    (n === 730 ? 'All' : n + 'd') + '</button>').join('');
}

function setPreset(n) {
  S.range = { days: n };
  CAL.anchor = null;
  renderPresets();
  fetchReport();
}

// ISO start/end covered by the current range (presets end at today)
function rangeBounds() {
  if (S.range.start) return { s: S.range.start, e: S.range.end };
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - (S.range.days - 1));
  return { s: isoOf(start), e: isoOf(end) };
}

function rangeQuery() {
  const tz = new Date().getTimezoneOffset();
  return S.range.start
    ? 'start=' + S.range.start + '&end=' + S.range.end + '&tz=' + tz
    : 'days=' + S.range.days + '&tz=' + tz;
}

function exportCsv() { location.href = BASE + '/api/export.csv?' + rangeQuery(); }

/* ---------- fetch + render ---------- */

async function fetchReport() {
  const b = document.getElementById('loaderr');
  try {
    const [rep, meta] = await Promise.all([api('/api/report?' + rangeQuery()), api('/api/meta')]);
    DATA = rep; S.meta = meta;
    b.style.display = 'none';
  } catch (err) {
    b.style.display = 'block';
    b.textContent = "⚠️ Can't load report — " + err.message;
    return;
  }
  try { render(); } catch (err) {
    b.style.display = 'block';
    b.textContent = '⚠️ Display error — ' + err.message;
  }
}

function render() {
  if (!S.prefs) S.prefs = loadPrefs();
  renderPresets();
  renderMeta();
  renderTotals();
  renderIntake();
  renderGrowth();
  renderFilters();
  renderDays();
  renderCalendar();
  renderTimeline();
  applyReportPrefs();
  document.getElementById('foot').textContent =
    DATA.range.start_local + ' → ' + DATA.range.end_local +
    ' · refreshed ' + new Date().toLocaleTimeString();
}

function renderGrowth() {
  const card = document.getElementById('growth-card');
  const g = DATA.growth || [], m = DATA.medication_log || [];
  if (!g.length && !m.length) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  let out = '';
  if (g.length) {
    // latest value per kind (lists are newest-first)
    const latest = {};
    for (const x of g) if (!(x.kind in latest)) latest[x.kind] = x;
    const labels = { weight: '⚖️ Weight', length: '📏 Length', head_circumference: '🎯 Head', temperature: '🌡️ Temp' };
    out += '<div class="today-line">' + Object.entries(latest).map(([k, x]) =>
      (labels[k] || k) + ': ' +
      (k === 'weight' && (x.unit || 'lb') === 'lb'
        ? fmtLbOz(x) : x.value + (k === 'temperature' ? ' °' : ' ') + x.unit) +
      ' <span style="color:var(--silver)">(' + x.date + ')</span>').join('<br>') + '</div>';
  }
  if (m.length) {
    out += '<h3>Medication log</h3>';
    for (const x of m)
      out += '<div class="row"><span class="ri">💊</span><span class="rt">' + x.date + '</span>' +
        '<span class="rd">' + esc(x.name || '') + (x.dose ? ' · ' + esc(x.dose) : '') + '</span></div>';
  }
  document.getElementById('growth').innerHTML = out;
}

/* ---------- sleep/nap trend calendar ---------- */

let CAL = { y: new Date().getFullYear(), m: new Date().getMonth(), days: null, max: 1 };
let calSeq = 0; // tokens: only the newest request is allowed to draw
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];

function calShift(d) {
  CAL.y += d < 0 && CAL.m === 0 ? -1 : d > 0 && CAL.m === 11 ? 1 : 0;
  CAL.m = (CAL.m + d + 12) % 12;
  renderCalendar();
}

async function renderCalendar() {
  const tok = ++calSeq;
  const y = CAL.y, m = CAL.m;
  const first = new Date(y, m, 1), last = new Date(y, m + 1, 0);
  const sd = y + '-' + String(m + 1).padStart(2, '0') + '-01';
  const ed = y + '-' + String(m + 1).padStart(2, '0') + '-' + String(last.getDate()).padStart(2, '0');
  document.getElementById('cal-title').textContent =
    MONTHS[m] + ' ' + y + (y === new Date().getFullYear() && m === new Date().getMonth() ? ' · this month' : '');
  const grid = document.getElementById('cal-grid');
  grid.innerHTML = '<div style="grid-column:1/-1; color:var(--silver); padding:8px 0">Loading…</div>';
  try {
    const rep = await api('/api/report?start=' + sd + '&end=' + ed + '&tz=' + new Date().getTimezoneOffset());
    if (tok !== calSeq || CAL.y !== y || CAL.m !== m) return; // superseded
    CAL.days = {};
    for (const day of rep.days)
      CAL.days[day.date] = { s: day.sleep.seconds, c: day.sleep.count, t: day.tummy.seconds };
    CAL.max = Math.max(1, ...rep.days.map(d => d.sleep.seconds));
    CAL.hasSleep = rep.days.some(d => d.sleep.seconds > 0);
    drawCalendar(y, m);
  } catch (err) {
    if (tok === calSeq && CAL.y === y && CAL.m === m)
      grid.innerHTML = '<div style="color:#8a2b33; padding:8px 0">⚠️ ' + esc(err.message) + '</div>';
  }
}

function drawCalendar(y, m) {
  const first = new Date(y, m, 1);
  const lead = first.getDay();
  const daysIn = new Date(y, m + 1, 0).getDate();
  const todayKey = new Date().toDateString();
  const b = rangeBounds(); // active filter range → highlighted on the grid
  let html = '';
  for (let i = 0; i < lead; i++) html += '<div class="cal-cell blank"></div>';
  for (let d = 1; d <= daysIn; d++) {
    const iso = y + '-' + String(m + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
    const rec = CAL.days[iso];
    const inR = iso >= b.s && iso <= b.e;
    const cls = 'cal-cell' + (new Date(y, m, d).toDateString() === todayKey ? ' today' : '') +
      (new Date(y, m, d) > new Date() ? ' future' : '') +
      (inR ? (b.s === b.e ? ' sel' : ' insel') : '');
    let inner = '<div class="cd">' + d + '</div>';
    if (rec && rec.s > 0) {
      // color intensity: more sleep = stronger tint
      const a = Math.min(0.85, 0.12 + 0.75 * rec.s / CAL.max);
      const rgb = 'rgba(179,204,247,' + a.toFixed(2) + ')';
      const h = Math.floor(rec.s / 3600), mn = Math.round((rec.s % 3600) / 60);
      inner += '<div class="cs">' + (h ? h + 'h ' + mn + 'm' : mn + 'm') + '</div>';
      if (rec.c) inner += '<div class="cn">' + rec.c + ' nap' + (rec.c === 1 ? '' : 's') + '</div>';
      else inner += '<div class="cn">night</div>';
      html += '<div class="' + cls + '" style="background:' + rgb + '" onclick="calPick(\'' + iso + '\')">' + inner + '</div>';
    } else {
      html += '<div class="' + cls + '" onclick="calPick(\'' + iso + '\')">' + inner + '</div>';
    }
  }
  document.getElementById('cal-grid').innerHTML = html;
  const key = document.getElementById('cal-sleep-key');
  if (key) key.style.display = CAL.hasSleep === false ? 'none' : '';
}

function calPick(iso) {
  // tapping the selected day again clears back to the default range
  if (S.range.start === S.range.end && S.range.start === iso) {
    CAL.anchor = null;
    S.range = { days: DEFAULT_DAYS };
    renderPresets();
    fetchReport();
    return;
  }
  // second tap on a different day turns the selection into a range
  if (CAL.anchor && CAL.anchor !== iso) {
    const s = CAL.anchor < iso ? CAL.anchor : iso;
    const e = CAL.anchor < iso ? iso : CAL.anchor;
    S.range = { start: s, end: e };
    CAL.anchor = null;
    renderPresets();
    fetchReport();
    return;
  }
  openDay(iso);
}

function openDay(iso) {
  CAL.anchor = iso;
  S.range = { start: iso, end: iso };
  renderPresets();
  fetchReport();
}

/* ---------- sleep/nap timeline (Google-Calendar-style week view) ---------- */

let TL = { start: weekStart(new Date()) };
let tlSeq = 0; // tokens: only the newest request is allowed to draw
const DAYNAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

function weekStart(d) { const s = new Date(d); s.setHours(0, 0, 0, 0); s.setDate(s.getDate() - s.getDay()); return s; }

function tlShift(days) {
  const s = new Date(TL.start); s.setDate(s.getDate() + days);
  TL.start = s;
  renderTimeline();
}

function isoOf(d) {
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

async function renderTimeline() {
  const tok = ++tlSeq;
  const start = TL.start;
  const end = new Date(start); end.setDate(end.getDate() + 6);
  const sd = isoOf(start), ed = isoOf(end);
  const grid = document.getElementById('tl-grid');
  const m = d => MONTHS[d.getMonth()].slice(0, 3) + ' ' + d.getDate();
  document.getElementById('tl-title').textContent = m(start) + ' – ' + m(end) +
    (start <= new Date() && end >= new Date() ? ' · includes today' : '');
  // header: day names + dates
  let head = '<span></span>';
  for (let i = 0; i < 7; i++) {
    const d = new Date(start); d.setDate(d.getDate() + i);
    head += '<span>' + DAYNAMES[d.getDay()] + '<br>' + d.getDate() + '</span>';
  }
  document.getElementById('tl-head').innerHTML = head;
  grid.innerHTML = '<div style="grid-column:1/-1; color:var(--silver); padding:8px 0">Loading…</div>';
  try {
    const rep = await api('/api/report?start=' + sd + '&end=' + ed + '&tz=' + new Date().getTimezoneOffset());
    if (tok !== tlSeq || isoOf(TL.start) !== sd) return; // superseded by a newer request
    drawTimeline(rep, start);
  } catch (err) {
    if (tok === tlSeq)
      grid.innerHTML = '<div style="color:#8a2b33; padding:8px 0">⚠️ ' + esc(err.message) + '</div>';
  }
}

function drawTimeline(rep, start) {
  const now = new Date();
  const todayIso = isoOf(now);
  // gutter: hour labels every 2h
  let gutter = '<div class="tl-gutter">';
  for (let h = 0; h <= 24; h += 2) {
    const lab = h === 24 ? '12a' : h === 12 ? '12p' : h < 12 ? h + 'a' : (h - 12) + 'p';
    gutter += '<span style="top:' + (h / 24 * 100) + '%">' + lab + '</span>';
  }
  gutter += '</div>';
  const byIso = {};
  for (const day of rep.days) byIso[day.date] = day;
  let cols = '';
  for (let i = 0; i < 7; i++) {
    const dayStart = new Date(start); dayStart.setDate(start.getDate() + i); dayStart.setHours(0, 0, 0, 0);
    const dayEnd = new Date(dayStart); dayEnd.setDate(dayEnd.getDate() + 1);
    const iso = isoOf(dayStart);
    const day = byIso[iso];
    let blocks = '';
    if (day) {
      for (const e of day.entries) {
        if (e.type !== 'sleep' && e.type !== 'tummy') continue;
        const s = new Date(e.ts_start);
        const en = e.ts_end ? new Date(e.ts_end) : now; // open timer grows to now
        const segStart = Math.max(s, dayStart), segEnd = Math.min(en, dayEnd);
        if (segEnd <= segStart) continue;
        const top = (segStart - dayStart) / 864e5 * 100;
        const h = (segEnd - segStart) / 864e5 * 100;
        blocks += '<div class="tl-blk ' + e.type + '" style="top:' + top + '%; height:' + Math.max(0.4, h) + '%;"' +
          ' title="' + esc(fmtTime(e.ts_start) + '–' + (e.ts_end ? fmtTime(e.ts_end) : 'now') +
            ' · ' + fmtDur((en - s) / 1000)) + '"' +
          ' onclick="openDay(\'' + iso + '\')">' + (h > 7 ? (e.type === 'sleep' ? '😴' : '🤸') : '') + '</div>';
      }
    }
    // now-line clipped to today's column only
    if (iso === todayIso) {
      const nowTop = (now.getHours() * 60 + now.getMinutes()) / 1440 * 100;
      blocks += '<div class="tl-now" style="top:' + nowTop + '%"></div>';
    }
    cols += '<div class="tl-col' + (iso === todayIso ? ' todaycol' : '') + '">' +
      '<span class="dlabel">' + (day && day.sleep.seconds ? fmtDur(day.sleep.seconds) : '') + '</span>' + blocks + '</div>';
  }
  document.getElementById('tl-grid').innerHTML = gutter + cols;
}

function renderMeta() {
  const name = S.meta.baby_name || 'Baby';
  let age = '';
  if (S.meta.birth_date) {
    const bd = new Date(S.meta.birth_date + 'T00:00:00');
    const days = Math.floor((Date.now() - bd) / 864e5);
    if (days >= 0) age = days < 15 ? days + ' days old' : Math.floor(days / 7) + 'w ' + (days % 7) + 'd old';
  }
  document.title = name + ' — History';
  document.getElementById('age').textContent = esc(name) + (age ? ' · ' + age : '');
}

function dayCount() { return Math.max(1, DATA.days.length); }

function renderTotals() {
  const t = DATA.totals, n = dayCount();
  const f = t.feeds, d = t.diapers, s = t.sleep;
  const vol = [f.oz ? f.oz.toFixed(1) + ' oz' : '', f.ml ? Math.round(f.ml) + ' ml' : ''].filter(Boolean).join(' + ');
  const colors = Object.entries(d.colors).map(([c, x]) => COLOR_LABELS[c] + ' ' + x).join(', ');
  let out = '';
  out += 'Feeds: ' + f.count + (vol ? ' — ' + vol : '') + ' · avg ' + (f.oz / n).toFixed(1) + ' oz/day<br>';
  out += 'Diapers: ' + (d.total ?? (d.wet + d.solid)) + ' — 💧 ' + d.wet + ' · 💩 ' + d.solid + (colors ? '<br>Stool colors: ' + esc(colors) : '') + '<br>';
  if (s.seconds || s.count)
    out += 'Sleep: ' + fmtDur(s.seconds) + ' across ' + s.count + ' naps · avg ' + fmtDur(s.seconds / n) + '/day<br>';
  if (t.tummy.seconds) out += 'Tummy time: ' + fmtDur(t.tummy.seconds) + ' across ' + t.tummy.count + ' sessions<br>';
  if (t.medications) out += 'Medications: ' + t.medications + (t.medications === 1 ? ' dose' : ' doses') + '<br>';
  if (t.max_feed_gap_seconds != null) out += 'Longest stretch between feeds: <span class="gap">' + fmtDur(t.max_feed_gap_seconds) + '</span>';
  document.getElementById('totals').innerHTML = out;

  renderCharts();
}

/* ---------- daily bar charts: tap or slide to read a day ---------- */

// One bar slot per calendar day in the selected range, oldest → today, so a
// bar's position always matches its date even on days with no entries.
function chartSeries() {
  const b = rangeBounds(), by = {};
  for (const d of DATA.days) by[d.date] = d;
  // Start at the first day that actually has entries: on a long "All" range a
  // short history would otherwise compress into a few pixels at the right edge.
  const dated = DATA.days.filter(d => d.entries && d.entries.length).map(d => d.date).sort();
  const firstIso = dated.length && dated[0] > b.s && dated[0] <= b.e ? dated[0] : b.s;
  const out = [];
  const cur = new Date(firstIso + 'T00:00:00');
  const end = new Date(b.e + 'T00:00:00');
  while (cur <= end && out.length < 800) {
    const iso = isoOf(cur);
    out.push(by[iso] || {
      date: iso,
      feeds: { count: 0, oz: 0, ml: 0 },
      diapers: { total: 0, wet: 0, solid: 0, colors: {} },
      sleep: { seconds: 0, count: 0, longest_nap_seconds: 0 },
      tummy: { seconds: 0, count: 0 },
      medications: 0, notes: 0,
      max_feed_gap_seconds: null, entries: [],
    });
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

function chartRow(series, cls, vals, label, maxLab) {
  const real = vals.filter(v => v != null);
  const max = real.length ? Math.max(1, ...real) : 1;
  const gap = series.length > 200 ? 0 : series.length > 60 ? 1 : 3;
  let bars = '';
  series.forEach((d, i) => {
    const v = vals[i];
    const zero = v == null || v === 0;
    bars += '<div class="b ' + cls + (zero ? ' zero' : '') + '" data-date="' + d.date +
      '" style="height:' + (zero ? 2 : Math.max(3, 100 * v / max)) + '%"></div>';
  });
  return '<div class="barlabel"><span>' + label + '</span><span>' + (maxLab || '') + '</span></div>' +
    '<div class="barrow" style="gap:' + gap + 'px">' + bars + '</div>';
}

function renderCharts() {
  const el = document.getElementById('chart');
  if (!el) return;
  const series = chartSeries();
  CHART.days = series;
  if (!series.some(d => d.date === CHART.sel))
    CHART.sel = series.length ? series[series.length - 1].date : null; // default: today

  // ml-only loggers would otherwise see an empty chart
  const useMl = !DATA.totals.feeds.oz && DATA.totals.feeds.ml > 0;
  const feedVals = series.map(d => useMl ? d.feeds.ml : d.feeds.oz);
  const gapVals = series.map(d => d.max_feed_gap_seconds);
  const slpVals = series.map(d => d.sleep.seconds);
  const peak = arr => {
    const real = arr.filter(v => v != null && v > 0);
    return real.length ? Math.max(...real) : 0;
  };
  const feedPeak = peak(feedVals), gapPeak = peak(gapVals), slpPeak = peak(slpVals);
  // A metric with nothing logged in this range gets no row at all — an empty
  // flat bar row reads as "zero", which is wrong and just noise.
  CHART.flags = { gap: gapPeak > 0, sleep: slpPeak > 0 };

  const parts = [];
  if (S.prefs['chart-feed'] && feedPeak)
    parts.push(chartRow(series, 'b-vol', feedVals, useMl ? 'Feed volume (ml)' : 'Feed volume (oz)',
      'peak ' + (useMl ? Math.round(feedPeak) + ' ml' : feedPeak.toFixed(1) + ' oz')));
  if (S.prefs['chart-gap'] && gapPeak)
    parts.push(chartRow(series, 'b-gap', gapVals, 'Longest stretch between feeds',
      'peak ' + fmtDur(gapPeak)));
  if (S.prefs['chart-sleep'] && slpPeak)
    parts.push(chartRow(series, 'b-slp', slpVals, 'Sleep per day',
      'peak ' + fmtDur(slpPeak)));

  if (!parts.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="barwrap">' + parts.join('') + '</div>' +
    '<div class="chart-read" id="chart-read"></div>' +
    '<div class="cr-hint">Tap a bar — or press and slide across the bars — to read that day\'s numbers.</div>';
  paintChartSel();
}

function chartReadout(d) {
  const dt = new Date(d.date + 'T00:00:00');
  const head = (dt.toDateString() === new Date().toDateString() ? 'Today · ' : '') +
    dt.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  const vol = d.feeds.oz ? d.feeds.oz.toFixed(1) + ' oz'
    : d.feeds.ml ? Math.round(d.feeds.ml) + ' ml' : 'no volume';
  const dia = d.diapers.total ?? 0;
  const f = CHART.flags || {};
  const bits = [];
  if (f.gap) {
    const gap = d.max_feed_gap_seconds != null
      ? 'longest stretch <b>' + fmtDur(d.max_feed_gap_seconds) + '</b>'
      : 'longest stretch <span style="color:var(--silver)">—</span>';
    bits.push(gap);
  }
  if (f.sleep)
    bits.push('sleep <b>' + fmtDur(d.sleep.seconds) + '</b> in ' +
      d.sleep.count + ' nap' + (d.sleep.count === 1 ? '' : 's'));
  return '<div class="cr-date">' + head + '</div>' +
    '<div class="cr-lines">' +
      '<div><b>' + d.feeds.count + '</b> feed' + (d.feeds.count === 1 ? '' : 's') + ' · ' + vol +
        ' · <b>' + dia + '</b> diaper' + (dia === 1 ? '' : 's') + '</div>' +
      (bits.length ? '<div>' + bits.join(' · ') + '</div>' : '') +
    '</div>';
}

function paintChartSel() {
  const el = document.getElementById('chart');
  if (!el) return;
  el.querySelectorAll('.barrow .b').forEach(b =>
    b.classList.toggle('on', b.dataset.date === CHART.sel));
  const r = document.getElementById('chart-read');
  if (!r) return;
  const d = CHART.days.find(x => x.date === CHART.sel);
  r.innerHTML = d ? chartReadout(d) : '';
}

/* ---------- which reports to show (saved per device) ---------- */

const REPORT_ITEMS = [
  { k: 'calendar', lab: 'Dates calendar', el: 'calendar-card' },
  { k: 'totals', lab: 'Summary totals', el: 'totals-card' },
  { k: 'chart-feed', lab: 'Feed volume chart' },
  { k: 'chart-gap', lab: 'Longest stretch between feeds chart' },
  { k: 'chart-sleep', lab: 'Sleep per day chart' },
  { k: 'intake', lab: 'Eaten so far vs typical', el: 'intake-card' },
  { k: 'growth', lab: 'Growth & health', el: 'growth-card' },
  { k: 'timeline', lab: 'Sleep timeline', el: 'timeline-card' },
  { k: 'daily', lab: 'Daily log', el: 'daily-card' },
];
const PREFS_KEY = 'baby_report_cards_v1';

function loadPrefs() {
  let raw = {};
  try { raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch (e) { raw = {}; }
  const out = {};
  REPORT_ITEMS.forEach(it => { out[it.k] = raw[it.k] !== false; }); // only OFF is stored
  return out;
}

function savePrefs() {
  const off = {};
  REPORT_ITEMS.forEach(it => { if (!S.prefs[it.k]) off[it.k] = false; });
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(off)); } catch (e) {}
}

function applyReportPrefs() {
  REPORT_ITEMS.forEach(it => {
    if (!it.el) return;
    const el = document.getElementById(it.el);
    if (el) el.classList.toggle('rpt-off', !S.prefs[it.k]);
  });
}

function setPref(k, on) {
  S.prefs[k] = !!on;
  savePrefs();
  applyReportPrefs();
  renderCharts();
}

function openReportPrefs() {
  const rows = REPORT_ITEMS.map(it =>
    '<label class="check"><input type="checkbox"' + (S.prefs[it.k] ? ' checked' : '') +
    ' onchange="setPref(\'' + it.k + '\', this.checked)"> ' + esc(it.lab) + '</label>').join('');
  document.getElementById('modal-body').innerHTML =
    '<h2>Reports to show</h2>' + rows +
    '<p style="font-size:12.5px;color:var(--silver);margin:10px 0 2px">Saved on this device.</p>' +
    '<div class="modal-btns"><button onclick="closeModal()">Done</button>' +
    '<button class="mini" onclick="showAllReports()">Show all</button></div>';
  document.getElementById('modal').style.display = 'flex';
}

function showAllReports() {
  REPORT_ITEMS.forEach(it => { S.prefs[it.k] = true; });
  savePrefs();
  applyReportPrefs();
  renderCharts();
  openReportPrefs();
}


/* ---------- intake: today by clock-time vs the typical day in range ---------- */

function renderIntake() {
  const card = document.getElementById('intake-card');
  const ic = DATA && DATA.intake_curve;
  if (!ic || !ic.today) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  const B = ic.bucket_minutes, nowM = ic.now_minute, unit = ic.unit || 'oz';
  const t = ic.today, a = ic.avg;
  const W = 340, H = 158, padL = 28, padR = 10, padT = 10, padB = 20;
  const iw = W - padL - padR, ih = H - padT - padB;
  const nowIdx = Math.max(0, Math.min(t.points.length - 1, Math.floor(nowM / B)));

  const todayPts = [];
  for (let i = 0; i <= nowIdx; i++) todayPts.push([i * B, t.points[i]]);
  if (nowM % B) todayPts.push([nowM, t.points[nowIdx]]);

  const peak = Math.max(1, t.points[nowIdx] || 0, a ? (a.points[a.points.length - 1] || 0) : 0);
  const yMax = Math.max(4, Math.ceil(peak / 4) * 4);

  const X = m => padL + (m / 1440) * iw;
  const Y = v => padT + ih - (v / yMax) * ih;
  const f1 = v => (Math.round(v * 10) / 10).toFixed(1);
  const hhmm = m => {
    const h = Math.floor(m / 60), mm = m % 60;
    const ap = h < 12 ? 'am' : 'pm';
    const h12 = h % 12 === 0 ? 12 : h % 12;
    return h12 + (mm ? ':' + String(mm).padStart(2, '0') : '') + ' ' + ap;
  };

  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="intake-svg" role="img" ' +
            'aria-label="Cumulative intake today compared with the typical day">';

  for (let k = 0; k <= 2; k++) {
    const v = (yMax / 2) * k, y = Y(v);
    svg += '<line x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y +
           '" stroke="#e8e3da" stroke-width="1"/>';
    const lab = Number.isInteger(v) ? String(v) : f1(v);
    svg += '<text x="' + (padL - 5) + '" y="' + (y + 3.5) + '" text-anchor="end" class="ax">' + lab + '</text>';
  }
  [[0, '12a'], [360, '6a'], [720, '12p'], [1080, '6p'], [1440, '12a']].forEach(pair => {
    const m = pair[0], lab = pair[1];
    const anch = m === 0 ? 'start' : m === 1440 ? 'end' : 'middle';
    svg += '<text x="' + X(m) + '" y="' + (H - 6) + '" text-anchor="' + anch + '" class="ax">' + lab + '</text>';
  });

  if (a) {
    const ap = a.points.map((v, i) => [i * B, v]);
    svg += '<polyline fill="none" stroke="#b9b3a7" stroke-width="1.6" stroke-dasharray="4 3" ' +
           'stroke-linejoin="round" points="' +
           ap.map(pt => X(pt[0]) + ',' + Y(pt[1])).join(' ') + '"/>';
  }

  if (todayPts.length > 1) {
    const line = todayPts.map(pt => X(pt[0]) + ',' + Y(pt[1])).join(' ');
    svg += '<polygon fill="rgba(26,25,23,.055)" points="' + X(0) + ',' + Y(0) + ' ' + line +
           ' ' + X(nowM) + ',' + Y(0) + '"/>';
    svg += '<polyline fill="none" stroke="#1a1917" stroke-width="2.2" stroke-linecap="round" ' +
           'stroke-linejoin="round" points="' + line + '"/>';
    svg += '<circle cx="' + X(nowM) + '" cy="' + Y(todayPts[todayPts.length - 1][1]) + '" r="3.2" fill="#1a1917"/>';
  }

  svg += '<line x1="' + X(nowM) + '" y1="' + padT + '" x2="' + X(nowM) + '" y2="' + (padT + ih) +
         '" stroke="#d8d2c6" stroke-width="1" stroke-dasharray="2 3"/>';
  svg += '</svg>';

  const todayNow = t.points[nowIdx] || 0;
  let out = '<div class="intake-legend"><span><i></i>today</span>' +
            (a ? '<span><i class="dash"></i>' + a.days + '-day average</span>' : '') + '</div>';
  out += svg;
  out += '<div class="intake-sum">' +
         '<div><span class="k">Today</span> <strong>' + f1(todayNow) + ' ' + unit + '</strong> by ' + hhmm(nowM) +
         ' · ' + t.count + ' feed' + (t.count === 1 ? '' : 's') + '</div>';
  if (a) {
    const diff = todayNow - a.at_now;
    let cls = '', mark = '≈ ';
    if (diff >= 0.05) { cls = 'up'; mark = '▲ +'; }
    else if (diff <= -0.05) { cls = 'down'; mark = '▼ '; }
    out += '<div><span class="k">Typical</span> ' + f1(a.at_now) + ' ' + unit + ' by ' + hhmm(nowM) + '</div>';
    out += '<div class="delta ' + cls + '">' + mark + f1(Math.abs(diff)) + ' ' + unit + ' vs typical</div>';
  } else {
    out += '<div class="k">Not enough other days in this range to build an average.</div>';
  }
  out += '</div>';
  document.getElementById('intake').innerHTML = out;
}

const FILTERS = [
  { k: 'all', lab: 'All' }, { k: 'feed', lab: '🍼 Feeds' }, { k: 'diaper', lab: '💧 Diapers' },
  { k: 'sleep', lab: '😴 Sleep' }, { k: 'tummy', lab: '🤸 Tummy' }, { k: 'measurement', lab: '📏 Growth' },
  { k: 'medication', lab: '💊 Meds' }, { k: 'note', lab: '📝 Notes' },
];

function renderFilters() {
  document.getElementById('filters').innerHTML = FILTERS.map(f =>
    '<button class="chip' + (S.filter === f.k ? ' on' : '') + '" onclick="setFilter(\'' + f.k + '\')">' + f.lab + '</button>').join('');
}

function setFilter(k) { S.filter = k; renderFilters(); renderDays(); }

function visible(e) { return S.filter === 'all' || e.type === S.filter; }

function renderDays() {
  const el = document.getElementById('days');
  if (!DATA.days.length) {
    el.innerHTML = '<p class="empty">No entries in this range yet.</p>';
    return;
  }
  const todayKey = new Date().toDateString();
  const yestKey = new Date(Date.now() - 864e5).toDateString();
  let html = '';
  for (const day of DATA.days) {
    const d = new Date(day.date + 'T00:00:00');
    const label = d.toDateString() === todayKey ? 'Today'
      : d.toDateString() === yestKey ? 'Yesterday'
      : d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    const f = day.feeds, di = day.diapers, s = day.sleep;
    const vol = [f.oz ? f.oz.toFixed(1) + ' oz' : '', f.ml ? Math.round(f.ml) + ' ml' : ''].filter(Boolean).join(' + ');
    const vis = day.entries.filter(visible).length;
    let sum = [];
    if (f.count) sum.push('🍼 ' + f.count + (vol ? ' · ' + vol : ''));
    if (di.wet || di.solid) sum.push('💧 ' + di.wet + ' · 💩 ' + di.solid);
    if (s.seconds) sum.push('😴 ' + fmtDur(s.seconds));
    if (day.tummy.seconds) sum.push('🤸 ' + fmtDur(day.tummy.seconds));
    if (day.medications) sum.push('💊 ' + day.medications);
    if (day.notes) sum.push('📝 ' + day.notes);
    let gapLine = '';
    if (day.max_feed_gap_seconds != null)
      gapLine = '<div class="dayline">Longest stretch between feeds: <span class="gap">' + fmtDur(day.max_feed_gap_seconds) + '</span></div>';
    let entriesHtml = '';
    for (const e of day.entries) {
      if (!visible(e)) continue;
      const desc = describe(e);
      entriesHtml += '<div class="row"><span class="ri">' + desc.icon + '</span>' +
        '<span class="rt">' + fmtTime(e.ts_start) + '</span>' +
        '<span class="rd">' + esc(desc.text) + '</span>' +
        '<button class="mini" onclick="editEvent(' + e.id + ')">✏️</button>' +
        '<button class="mini" onclick="delEvent(' + e.id + ')">🗑️</button></div>';
    }
    if (vis && !entriesHtml) entriesHtml = '<p class="empty">No matching entries.</p>';
    html += '<details class="daycard' + (vis ? '' : ' dimmed') + '">' +
      '<summary><span class="dlabel">' + label + '</span>' +
      '<span class="dsum">' + (sum.join(' · ') || 'no entries') + '</span><span class="caret">▾</span></summary>' +
      gapLine +
      (entriesHtml ? '<div style="margin-top:8px">' + entriesHtml + '</div>' : '') +
      '</details>';
  }
  el.innerHTML = html;
}

/* ---------- edit + delete (same semantics as the main page) ---------- */

function closeModal() { document.getElementById('modal').style.display = 'none'; }

function findEvent(id) {
  for (const day of DATA.days) {
    const e = day.entries.find(x => x.id === id);
    if (e) return e;
  }
  return null;
}

let editId = null;

function editEvent(id) {
  editId = id;
  const e = findEvent(id);
  if (!e) return;
  const d = e.details || {};
  let extra = '';
  if (e.type === 'feed') {
    extra = '<label>Amount<input id="e-amount" type="number" step="0.25" min="0" value="' + (d.amount ?? '') + '"></label>' +
      '<label>Unit<select id="e-unit"><option value="oz"' + (d.unit === 'oz' ? ' selected' : '') + '>oz</option>' +
      '<option value="ml"' + (d.unit === 'ml' ? ' selected' : '') + '>ml</option></select></label>';
  } else if (e.type === 'diaper') {
    extra = '<label class="check"><input id="e-wet" type="checkbox"' + (d.wet ? ' checked' : '') + '> Wet</label>' +
      '<label class="check"><input id="e-solid" type="checkbox"' + (d.solid ? ' checked' : '') + '> Solid</label>' +
      '<label>Color<select id="e-color"><option value="">—</option>' + colorOptions(d.color) + '</select></label>';
  } else if (e.type === 'note') {
    extra = '<label>Text<input id="e-text" type="text" value="' + esc(d.text || '') + '"></label>';
  } else if (e.type === 'measurement') {
    const wtLb = d.kind === 'weight' && (d.unit || 'lb') === 'lb';
    const lbVal = d.lb != null ? d.lb : Math.floor(d.value || 0);
    const ozVal = d.oz != null ? d.oz : Math.round((((d.value || 0) - Math.floor(d.value || 0)) * 16) * 10) / 10;
    extra = '<label>Kind<select id="e-mkind">' + M_KINDS.map(k =>
      '<option value="' + k.k + '"' + (d.kind === k.k ? ' selected' : '') + '>' + k.lab + '</option>').join('') + '</select></label>' +
      (wtLb
        ? '<label>Pounds<input id="e-mlb" type="number" step="1" min="0" value="' + lbVal + '"></label>' +
          '<label>Ounces<input id="e-moz" type="number" step="0.1" min="0" max="15.9" value="' + ozVal + '"></label>'
        : '<label>Value<input id="e-mvalue" type="number" step="any" min="0" value="' + (d.value ?? '') + '"></label>') +
      '<label>Unit<input id="e-munit" type="text" value="' + esc(d.unit || '') + '" placeholder="lb / in / F"></label>';
  } else if (e.type === 'medication') {
    extra = '<label>Name<input id="e-medname" type="text" value="' + esc(d.name || '') + '"></label>' +
      '<label>Dose<input id="e-meddose" type="text" value="' + esc(d.dose || '') + '"></label>' +
      '<label>Repeat every (hours)<input id="e-mediv" type="number" step="1" min="1" max="48" value="' +
      (d.interval_min ? d.interval_min / 60 : '') + '"></label>';
  }
  const canEnd = e.type === 'sleep' || e.type === 'tummy';
  const endField = canEnd
    ? '<label>End (empty = still open)<input id="e-end" type="datetime-local" value="' + (e.ts_end ? toLocalInput(e.ts_end) : '') + '"></label>'
    : '';
  document.getElementById('modal-body').innerHTML =
    '<h2>Edit ' + e.type + '</h2>' +
    '<label>Start<input id="e-start" type="datetime-local" value="' + toLocalInput(e.ts_start) + '"></label>' +
    endField + extra +
    '<label>Note<input id="e-note" type="text" value="' + esc(d.note || '') + '"></label>' +
    '<div class="modal-btns"><button onclick="saveEdit()">Save</button>' +
    '<button onclick="closeModal()">Cancel</button>' +
    '<button class="danger" onclick="delEvent(' + e.id + '); closeModal()">Delete</button></div>';
  document.getElementById('modal').style.display = 'flex';
}

function colorOptions(sel) {
  return Object.keys(COLOR_LABELS).map(k =>
    '<option value="' + k + '"' + (k === sel ? ' selected' : '') + '>' + COLOR_LABELS[k] + '</option>').join('');
}

async function saveEdit() {
  const e = findEvent(editId);
  if (!e) return;
  const start = document.getElementById('e-start').value;
  if (!start) { toast('Start time is required'); return; }
  const body = { ts_start: new Date(start).toISOString() };
  const endEl = document.getElementById('e-end');
  if (endEl) {
    if (endEl.value) body.ts_end = new Date(endEl.value).toISOString();
    else if (e.ts_end) body.ts_end = body.ts_start; // was closed, now zero-length
  }
  const d = Object.assign({}, e.details || {});
  if (e.type === 'feed') {
    const amt = document.getElementById('e-amount').value;
    d.amount = amt === '' ? null : parseFloat(amt);
    d.unit = document.getElementById('e-unit').value;
  } else if (e.type === 'diaper') {
    d.wet = document.getElementById('e-wet').checked;
    d.solid = document.getElementById('e-solid').checked;
    if (!d.wet && !d.solid) { toast('Check wet and/or solid'); return; }
    d.color = document.getElementById('e-color').value || null;
  } else if (e.type === 'note') {
    d.text = document.getElementById('e-text').value.trim();
    if (!d.text) { toast('Text is required'); return; }
  } else if (e.type === 'measurement') {
    d.kind = document.getElementById('e-mkind').value;
    const lbEl = document.getElementById('e-mlb');
    if (lbEl && d.kind === 'weight') {
      const lb = parseFloat(lbEl.value || '0');
      const oz = parseFloat((document.getElementById('e-moz').value || '0'));
      if (!isFinite(lb) || !isFinite(oz) || lb + oz <= 0) { toast('Enter pounds and ounces'); return; }
      if (oz < 0 || oz >= 16) { toast('Ounces must be 0–15.9'); return; }
      d.lb = lb; d.oz = oz; d.value = Math.round((lb + oz / 16) * 10000) / 10000; d.unit = 'lb';
    } else {
      const v = parseFloat(document.getElementById('e-mvalue').value);
      if (!isFinite(v) || v <= 0) { toast('Value must be a positive number'); return; }
      d.value = v;
      delete d.lb; delete d.oz;
      d.unit = document.getElementById('e-munit').value.trim() || null;
    }
  } else if (e.type === 'medication') {
    d.name = document.getElementById('e-medname').value.trim();
    if (!d.name) { toast('Name is required'); return; }
    d.dose = document.getElementById('e-meddose').value.trim() || null;
    const ivEl = document.getElementById('e-mediv');
    const ivh = ivEl ? parseFloat(ivEl.value) : NaN;
    d.interval_min = isFinite(ivh) && ivh > 0 ? Math.round(Math.min(ivh, 48) * 60) : null;
  }
  const noteEl = document.getElementById('e-note');
  d.note = noteEl && noteEl.value.trim() ? noteEl.value.trim() : null;
  body.details = d;
  try {
    await api('/api/events/' + e.id, { method: 'PATCH', body: JSON.stringify(body) });
    closeModal(); await fetchReport(); toast('✓ Updated', 2000);
  } catch (err) { toast('⚠️ ' + err.message); }
}

async function delEvent(id) {
  if (!confirm('Delete this entry?')) return;
  try { await api('/api/events/' + id, { method: 'DELETE' }); await fetchReport(); toast('✓ Deleted', 2000); }
  catch (err) { toast('⚠️ ' + err.message); }
}

window.onbeforeprint = () => document.querySelectorAll('details').forEach(d => d.open = true);

/* Tap or press-and-slide across any bar row to read that day. Pointer capture
   keeps the readout following the finger; touch-action:pan-y on .barrow leaves
   vertical page scrolling alone. */
let scrubRow = null;

function chartIndexAt(row, clientX) {
  const r = row.getBoundingClientRect(), n = row.children.length;
  if (!n || !r.width) return null;
  const i = Math.floor((clientX - r.left) / (r.width / n));
  return Math.max(0, Math.min(n - 1, i));
}

function chartPickAt(row, clientX) {
  const i = chartIndexAt(row, clientX);
  if (i == null) return;
  const b = row.children[i];
  const date = b && b.dataset.date;
  if (!date || date === CHART.sel) return;
  CHART.sel = date;
  paintChartSel();
}

(function bindChartScrub() {
  const el = document.getElementById('chart');
  if (!el) return;
  el.addEventListener('pointerdown', ev => {
    const row = ev.target.closest ? ev.target.closest('.barrow') : null;
    if (!row) return;
    scrubRow = row;
    try { row.setPointerCapture(ev.pointerId); } catch (e) {}
    chartPickAt(row, ev.clientX);
  });
  el.addEventListener('pointermove', ev => { if (scrubRow) chartPickAt(scrubRow, ev.clientX); });
  const stop = () => { scrubRow = null; };
  el.addEventListener('pointerup', stop);
  el.addEventListener('pointercancel', stop);
})();

fetchReport();
// Re-fetch every 60s so entries logged from chat or the main page (another
// tab) appear on the report page without a manual reload. The calendar and
// timeline re-render with the fresh data via render().
setInterval(() => { if (!document.hidden) fetchReport().catch(() => {}); }, 60000);
if ('serviceWorker' in navigator) {
  var swRoot = window.APP_ROOT || './';
  navigator.serviceWorker.register(swRoot + 'sw.js', { scope: swRoot }).catch(() => {});
}
</script>
</body>
</html>
"""


ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICON_FILES = {
    "icon-192.png", "icon-512.png", "apple-touch-icon.png",
    "maskable-512.png", "icon-32.png", "favicon.ico",
}


HTML = HTML.replace("<!--PWA_HEAD-->", PWA_HEAD)
REPORT_HTML = REPORT_HTML.replace("<!--PWA_HEAD-->", PWA_HEAD)


@app.get("/manifest.webmanifest")
def manifest():
    """Web app manifest. Deliberately generic — no baby name in the shipped
    shell, so the repo can be shared. Paths are relative so the same file
    works at / and under a sub-path mount (fetched from that scope)."""
    return JSONResponse(
        {
            "name": "Baby Log",
            "short_name": "Baby Log",
            "description": "Feeding, sleep and diaper log.",
            "id": "./",
            "start_url": "./",
            "scope": "./",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#faf9f7",
            "theme_color": "#faf9f7",
            "categories": ["health", "lifestyle", "productivity"],
            "icons": [
                {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/icons/{name}")
def icon(name: str):
    if name not in ICON_FILES:
        raise HTTPException(status_code=404, detail="not found")
    media = "image/x-icon" if name.endswith(".ico") else "image/png"
    return FileResponse(os.path.join(ICON_DIR, name), media_type=media,
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(ICON_DIR, "favicon.ico"), media_type="image/x-icon")


@app.get("/sw.js")
def service_worker():
    """Exists so the app is installable. It caches ONLY icons + the manifest:
    pages and /api/* stay network-only, so a stale cache can never hide a
    newly logged entry."""
    js = r"""const CACHE = 'baby-log-v1';
const ASSETS = ['icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png', 'manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('push', e => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(self.registration.showNotification(p.title || 'Baby log', {
    body: p.body || '',
    tag: p.tag || 'babylog',
    data: { url: p.url || './' },
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png'
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || './';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    const abs = new URL(url, self.registration.scope).href;
    for (const c of cs) if (c.url === abs) return c.focus();
    return clients.openWindow(abs);
  }));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname.includes('/api/')) return;
  if (/\.(png|ico|webmanifest)$/.test(url.pathname) || url.pathname.endsWith('/sw.js')) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
"""
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


# ---------------- dose reminders (web push) ----------------

REMIND_LEAD_MIN = 30      # notify this long before the next dose
REMIND_GRACE_MIN = 20     # ...and still nudge this long after it (laptop asleep)
# APNs rejects a VAPID `sub` that is not a real reachable URI ("BadJwtToken"),
# so never use a .local address here. Set VAPID_SUB to a URL or mailto: you
# control; the placeholder default is only there so push still works out of
# the box on Android/desktop.
VAPID_SUB = os.environ.get("VAPID_SUB", "https://example.com")
LAST_PUSH_ERROR = None


def _vapid_keys():
    """P-256 VAPID keys, generated once and stored in `meta` so rebuilding the
    container keeps the same identity — changing them silently kills every
    existing subscription."""
    rows = query("SELECT key, value FROM meta WHERE key IN ('vapid_private','vapid_public')")
    have = {r["key"]: r["value"] for r in rows}
    if "vapid_private" in have and "vapid_public" in have:
        return have["vapid_private"], have["vapid_public"]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with WRITE_LOCK, tx() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('vapid_private',?)", (pem,))
        conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('vapid_public',?)", (pub,))
    return pem, pub


_VAPID_FILE = None


def _vapid_key_file():
    """py_vapid only parses PEM from a path (not from a string), so keep one
    copy alongside the DB — it is the same key every time."""
    global _VAPID_FILE
    if _VAPID_FILE and os.path.exists(_VAPID_FILE):
        return _VAPID_FILE
    pem, _ = _vapid_keys()
    d = os.path.dirname(os.path.abspath(DB_PATH)) or "/data"
    path = os.path.join(d, ".vapid-private.pem")
    with open(path, "w") as f:
        f.write(pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _VAPID_FILE = path
    return path


def push_subs():
    return query("SELECT * FROM push_subs")


def send_push(row, payload):
    """Returns (ok, error). Dead endpoints (404/410) are pruned."""
    global LAST_PUSH_ERROR
    from pywebpush import WebPushException, webpush
    key_file = _vapid_key_file()
    try:
        webpush(
            subscription_info={
                "endpoint": row["endpoint"],
                "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=key_file,
            vapid_claims={"sub": VAPID_SUB},
            timeout=10,
        )
        return True, None
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            with WRITE_LOCK, tx() as conn:
                conn.execute("DELETE FROM push_subs WHERE endpoint=?", (row["endpoint"],))
        msg = (f"{code} " if code else "") + str(e)[:200]
        LAST_PUSH_ERROR = msg
        print("push failed:", row["endpoint"][:48], msg, flush=True)
        return False, msg
    except Exception as e:  # network errors, bad keys
        LAST_PUSH_ERROR = str(e)[:200]
        print("push failed:", row["endpoint"][:48], LAST_PUSH_ERROR, flush=True)
        return False, LAST_PUSH_ERROR


def notify_all(title, body, tag="dose"):
    subs = push_subs()
    ok = 0
    for row in subs:
        if send_push(row, {"title": title, "body": body, "tag": tag, "url": "./"})[0]:
            ok += 1
    return ok, len(subs)


def latest_doses():
    """Newest dose per medication name (case-insensitive). A newer dose of the
    same medication therefore supersedes the previous schedule by design."""
    rows = query("SELECT id, ts_start, details FROM events WHERE type='medication' "
                 "ORDER BY ts_start DESC, id DESC")
    latest = {}
    for r in rows:
        d = json.loads(r["details"] or "{}")
        name = (d.get("name") or "").strip()
        key = name.lower()
        if not key or key in latest:
            continue
        latest[key] = {
            "id": r["id"], "name": name, "dose": d.get("dose"),
            "interval_min": d.get("interval_min"), "start": parse_ts(r["ts_start"]),
        }
    return latest


def check_dose_reminders(now=None):
    """Send the 30-minutes-before nudge for each medication whose latest dose
    carries an interval. Returns the number of reminders sent."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for med in latest_doses().values():
        iv = med["interval_min"]
        if not iv:
            continue
        due = med["start"] + timedelta(minutes=int(iv))
        if now < due - timedelta(minutes=REMIND_LEAD_MIN):
            continue
        if now > due + timedelta(minutes=REMIND_GRACE_MIN):
            continue
        if query("SELECT 1 FROM med_reminders WHERE event_id=?", (med["id"],)):
            continue
        mins = int(round((due - now).total_seconds() / 60))
        if mins > 1:
            when = f"due in {mins} minutes"
        elif mins >= -1:
            when = "due now"
        else:
            when = f"was due {abs(mins)} minutes ago"
        body = med["name"] + (f" {med['dose']}" if med["dose"] else "") + f" — {when}."
        ok, total = notify_all("Dose reminder", body, tag=f"med-{med['id']}")
        if ok:
            with WRITE_LOCK, tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO med_reminders (event_id, sent_at, delivered, total) "
                    "VALUES (?,?,?,?)", (med["id"], to_iso(now), ok, total))
            sent += 1
    return sent


def scheduler_loop():
    while True:
        try:
            check_dose_reminders()
        except Exception:
            pass
        time.sleep(30)


threading.Thread(target=scheduler_loop, name="dose-reminders", daemon=True).start()


class PushSubIn(BaseModel):
    endpoint: str
    keys: dict = {}


@app.get("/api/push/key")
def push_key():
    _, pub = _vapid_keys()
    return {"key": pub, "subs": len(push_subs())}


@app.post("/api/push/subscribe")
def push_subscribe(body: PushSubIn):
    k = body.keys or {}
    p256dh, auth = k.get("p256dh"), k.get("auth")
    if not (body.endpoint and p256dh and auth):
        raise HTTPException(status_code=400, detail="endpoint and keys are required")
    with WRITE_LOCK, tx() as conn:
        conn.execute(
            "INSERT INTO push_subs (endpoint, p256dh, auth, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth",
            (body.endpoint, p256dh, auth, to_iso(datetime.now(timezone.utc))),
        )
    return {"ok": True, "subs": len(push_subs())}


@app.post("/api/push/test")
def push_test():
    global LAST_PUSH_ERROR
    LAST_PUSH_ERROR = None
    ok, total = notify_all("Baby log", "Test notification — dose reminders are working.", tag="test")
    return {"sent": ok, "subs": total, "error": None if ok else LAST_PUSH_ERROR}


@app.get("/report", response_class=HTMLResponse)
def report_page():
    return REPORT_HTML


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML
