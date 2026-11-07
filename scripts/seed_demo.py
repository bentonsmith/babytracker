#!/usr/bin/env python3
"""Seed a throwaway DB with plausible demo data for README screenshots.

Not part of the app — used once to produce docs/*.png without any real
household data. Run against a scratch container, then delete the DB.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("BASE", "http://127.0.0.1:3014")
TZ = timezone(timedelta(hours=-7))


def post(type_, start, details, end=None):
    body = {"type": type_, "ts_start": start.isoformat(), "details": details}
    if end:
        body["ts_end"] = end.isoformat()
    req = urllib.request.Request(BASE + "/api/events",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def main():
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # baby profile: 5 weeks old, generic name
    # /api/meta takes one key per call
    for k, v in (("baby_name", "Baby"),
                 ("birth_date", (today - timedelta(days=37)).date().isoformat())):
        req = urllib.request.Request(
            BASE + "/api/meta",
            data=json.dumps({"key": k, "value": v}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            print("meta", k, r.status)

    feed_times = [(7, 10), (10, 30), (13, 45), (16, 40), (19, 20), (22, 10), (2, 30)]
    amounts = [3.0, 3.5, 2.5, 3.0, 4.0, 2.5, 3.0]
    diaper_times = [(8, 5), (11, 15), (14, 30), (18, 0), (21, 0), (1, 20)]
    n = 0
    for d in range(7, -1, -1):
        day = today - timedelta(days=d)
        for i, ((h, m), amt) in enumerate(zip(feed_times, amounts)):
            start = day + timedelta(hours=h, minutes=m)
            start += timedelta(minutes=(d * 11) % 31) + timedelta(minutes=(i * 4) % 9)
            if start > now:
                continue
            # vary volume per feed and per day so "today" never equals "typical"
            jitter = (((d * 3 + i * 5) % 7) - 3) * 0.25
            amt = max(1.0, round(amt + jitter, 1))
            post("feed", start, {"method": "bottle", "amount": amt, "unit": "oz"})
            n += 1
        for (h, m) in diaper_times:
            start = day + timedelta(hours=h, minutes=m)
            if start > now:
                continue
            post("diaper", start, {"wet": True, "solid": h in (8, 18),
                                   "color": "yellow" if h == 8 else "brown"})
            n += 1
        # naps
        for (sh, sm, eh, em) in [(9, 30, 10, 10), (14, 0, 15, 30)]:
            s = day + timedelta(hours=sh, minutes=sm)
            e = day + timedelta(hours=eh, minutes=em)
            if e > now:
                continue
            post("sleep", s, {}, e)
            n += 1
        # night sleep crossing midnight
        s = day + timedelta(hours=20, minutes=45)
        e = day + timedelta(hours=30, minutes=30)     # 06:30 next morning
        if e < now:
            post("sleep", s, {}, e)
            n += 1

    # measurements + meds + note
    post("measurement", today - timedelta(days=2, hours=-10),
         {"kind": "weight", "value": 9.2, "unit": "lb"})
    post("measurement", today - timedelta(days=2, hours=-10),
         {"kind": "length", "value": 21.5, "unit": "in"})
    post("medication", today - timedelta(hours=9),
         {"name": "Vitamin D", "dose": "400 IU"})
    post("note", today - timedelta(hours=5),
         {"text": "More alert after feeds today."})
    print("seeded", n)


if __name__ == "__main__":
    sys.exit(main())
