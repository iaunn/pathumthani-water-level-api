"""Re-read stored captures with the current detector and correct the database.

Every reading keeps the frame it was read from, so a detector fix can be applied
backwards: download the frame, run it again, and write what it says now. That
matters because a wrong reading does not age out -- the 3.00m Pathumthani filed
through a hazy dawn stays on the chart, and in the history export, until
something rewrites it.

The frames are stored exactly as the tracker read them, after homography
alignment, so they are re-read as they are and never aligned a second time.
What does change is everything the station carries now: calibration, detection
region, staff_paint, still_water, and the detector itself. That is the point,
and it is also the risk -- a region moved since a capture was taken will give a
different answer for reasons that have nothing to do with the code.

Nothing is written without --apply. Run it once without to see what would change.

    python recompute.py pathumthani --from 2026-09-25T06:00
    python recompute.py pathumthani --from 2026-09-25T06:00 --apply
    python recompute.py --all --from 2026-09-25 --apply --on-decline delete

A capture the detector still cannot read is put on hold by default: the frame
and the point stay, the level becomes the last one that was actually read, and
calculated=false says so. That is what the tracker does live, so a recompute
leaves the series in the same shape. --on-decline keep leaves the old number
standing as a measurement; --on-decline delete removes the point outright.

The reverse is the reason the frames are kept at all: a capture that was held
because the detector could not read it, and that a fixed detector now can,
stops being a carried value and becomes a reading. --only-held revisits just
those.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# Set before app is imported: importing it starts one capture-and-write thread
# per station, which would be filing new readings while this rewrites old ones.
os.environ["TRACKER_ENABLED"] = "false"

# app, and with it storage, the database and the station list, is imported once
# the arguments are known -- importing it opens both connections, and --help
# should not need MinIO and MongoDB to be up to print itself.
app = database = stations = storage = None


def connect():
    global app, database, stations, storage
    import app as _app
    import database as _database
    import stations as _stations
    import storage as _storage
    app, database, stations, storage = _app, _database, _stations, _storage


def parse_when(text, end_of_day=False):
    """A timestamp from 2026-09-25, 2026-09-25T06:00, or a plain epoch."""
    if text.isdigit():
        return int(text)
    for fmt, whole_day in (("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False),
                           ("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            when = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if whole_day and end_of_day:
            when += timedelta(days=1, seconds=-1)
        return int(when.timestamp())
    raise argparse.ArgumentTypeError(
        f"{text!r} is not a date, a date and time, or an epoch second.")


def local(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Tally:
    def __init__(self):
        self.seen = self.changed = self.same = self.filled = 0
        self.declined = self.held = self.deleted = self.missing = self.unreadable = 0
        self.worst = 0.0
        self.worst_at = None


def recompute_station(station, start_ts, end_ts, limit, on_decline, apply_changes,
                      smooth, verbose, only_held):
    app.load_calibration(station)
    app.load_roi(station)
    if not station.cal_points:
        print(f"[{station.id}] no calibration, so a pixel row means nothing here. Skipped.")
        return None

    readings = database.readings_in_range(station.id, start_ts, end_ts, limit)
    if only_held:
        readings = [d for d in readings if d.get("calculated") is False]
    span = f"up to {local(end_ts)}" if start_ts <= 0 else \
           f"between {local(start_ts)} and {local(end_ts)}"
    print(f"[{station.id}] {len(readings)} readings {span}"
          f"  (region x{station.x_start}-{station.x_end}, "
          f"staff_paint={station.staff_paint}, still_water={station.still_water})")

    # Smoothing is a running state in the tracker, so it is rebuilt here in the
    # same order rather than carried in from whatever ran last.
    station.detection_history = []
    tally = Tally()

    # What a frame that cannot be read holds. Seeded from before the range, so
    # recomputing an old window carries what was true then, not what is true now.
    before = database.last_measured(station.id, before_ts=start_ts)
    carry = before["level"] if before else None

    for doc in readings:
        tally.seen += 1
        key = storage.key_for_url(doc.get("image_url"))
        if key is None:
            tally.unreadable += 1
            if verbose:
                print(f"  {local(doc['timestamp'])}  no usable image_url, left alone")
            continue

        frame = storage.download_frame(key)
        if frame is None:
            tally.missing += 1
            if verbose:
                print(f"  {local(doc['timestamp'])}  frame is gone from storage, left alone")
            continue

        meta = {}
        raw_y = app.detect_water_level_on_gauge(
            frame, station.x_start, station.x_end,
            y_start=station.y_start, y_end=station.y_end,
            # One stored frame per reading, so there is no segment to measure
            # motion across; the reflection correction stands aside either way.
            frames=None, meta=meta,
            staff_paint=station.staff_paint, still_water=station.still_water)

        mode = meta.get("mode") if raw_y is not None else "none"
        y = app.smooth_detection(station, raw_y) if (smooth and raw_y is not None) else raw_y

        was_held = doc.get("calculated") is False

        if y is None:
            tally.declined += 1
            if was_held:
                # Already marked and already holding; nothing to say about it.
                if verbose:
                    print(f"  {local(doc['timestamp'])}  still unread, holding {doc['level']:.2f}m")
                continue

            if on_decline == "delete":
                if apply_changes:
                    database.delete_reading(station.id, doc["timestamp"])
                tally.deleted += 1
                print(f"  {local(doc['timestamp'])}  {doc['level']:.2f}m -> unread  "
                      f"({'deleted' if apply_changes else 'would delete'})")
            elif on_decline == "hold" and carry is not None:
                if apply_changes:
                    database.hold_reading(station.id, doc["timestamp"], carry)
                tally.held += 1
                print(f"  {local(doc['timestamp'])}  {doc['level']:.2f}m -> unread, "
                      f"holding {carry:.2f}m (calculated=false)")
            else:
                print(f"  {local(doc['timestamp'])}  {doc['level']:.2f}m -> unread  (kept as is)")
            continue

        level = round(float(app.pixel_to_level(station, float(y))), 2)
        carry = level
        shift = abs(level - float(doc["level"]))

        if not was_held and int(y) == int(doc.get("y", -1)) \
                and level == round(float(doc["level"]), 2):
            tally.same += 1
            if verbose:
                print(f"  {local(doc['timestamp'])}  {level:.2f}m unchanged")
            continue

        if apply_changes:
            database.update_reading(station.id, doc["timestamp"], level, y, mode)

        if was_held:
            # A frame that was being held now reads, which is the whole point of
            # keeping it: the point stops being a carried value and becomes one.
            tally.filled += 1
            print(f"  {local(doc['timestamp'])}  held {doc['level']:.2f}m -> read "
                  f"{level:.2f}m (y{int(y)}, {mode})   {level - float(doc['level']):+.2f}m")
            continue

        tally.changed += 1
        if shift > tally.worst:
            tally.worst, tally.worst_at = shift, doc["timestamp"]
        print(f"  {local(doc['timestamp'])}  {doc['level']:.2f}m (y{doc.get('y')}) -> "
              f"{level:.2f}m (y{int(y)}, {mode})   {level - float(doc['level']):+.2f}m")

    return tally


def report(station_id, tally, apply_changes):
    verb = "rewrote" if apply_changes else "would rewrite"
    line = (f"[{station_id}] {tally.seen} read, {verb} {tally.changed}, "
            f"{tally.filled} held frames now read, "
            f"{tally.same} already right, {tally.declined} still unread")
    if tally.held:
        line += f" ({tally.held} put on hold)"
    if tally.deleted:
        line += f" ({tally.deleted} deleted)"
    if tally.missing:
        line += f", {tally.missing} frames missing from storage"
    if tally.unreadable:
        line += f", {tally.unreadable} without a usable image_url"
    print(line)
    if tally.worst_at:
        print(f"[{station_id}] largest correction {tally.worst:.2f}m at {local(tally.worst_at)}")


def main():
    ap = argparse.ArgumentParser(
        description="Re-read stored captures with the current detector.")
    ap.add_argument("station", nargs="*", help="station ids; omit with --all")
    ap.add_argument("--all", action="store_true", help="every station in stations.json")
    ap.add_argument("--from", dest="start", default="0",
                    help="date, date and time, or epoch (default: the beginning)")
    ap.add_argument("--to", dest="end", default=None,
                    help="date, date and time, or epoch (default: now)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many readings")
    ap.add_argument("--apply", action="store_true",
                    help="write the results; without it nothing is changed")
    ap.add_argument("--on-decline", choices=("hold", "keep", "delete"), default="hold",
                    help="what to do with a capture the detector no longer reads. "
                         "hold (default) keeps the frame and the point but marks it "
                         "calculated=false, holding the last level that was read; "
                         "keep leaves the old number standing as a measurement; "
                         "delete removes the point")
    ap.add_argument("--only-held", action="store_true",
                    help="only revisit captures already marked calculated=false")
    ap.add_argument("--no-smoothing", action="store_true",
                    help="write each frame's own reading, not the tracker's rolling median")
    ap.add_argument("--verbose", action="store_true", help="print unchanged readings too")
    args = ap.parse_args()

    if not args.station and not args.all:
        ap.error("name a station, or pass --all.")

    try:
        start_ts = parse_when(args.start)
        end_ts = parse_when(args.end, end_of_day=True) if args.end else int(time.time())
    except argparse.ArgumentTypeError as e:
        ap.error(str(e))
    if end_ts < start_ts:
        ap.error("--to is before --from")

    connect()

    if args.all:
        targets = list(stations.all_stations())
    else:
        targets = []
        for station_id in args.station:
            if not stations.exists(station_id):
                ap.error(f"no station '{station_id}' in stations.json")
            targets.append(stations.get(station_id))

    if not args.apply:
        print("Dry run: nothing will be written. Add --apply to save the results.\n")
    elif args.on_decline == "delete":
        print("Applying, and deleting readings the detector still cannot read.\n")
    elif args.on_decline == "keep":
        print("Applying. Readings the detector cannot read keep their old value, "
              "standing as measurements.\n")
    else:
        print("Applying. Readings the detector cannot read keep their frame and "
              "their place, marked calculated=false and holding the last level "
              "that was read.\n")

    for station in targets:
        tally = recompute_station(
            station, start_ts, end_ts, args.limit, args.on_decline,
            args.apply, not args.no_smoothing, args.verbose, args.only_held)
        if tally:
            report(station.id, tally, args.apply)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
