"""MongoDB persistence for readings and calibration."""

import os
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING


# Reference lines are drawn from a fixed palette rather than free colours, so the
# chart keeps a consistent reading of severity across the whole dashboard.
MARKER_COLORS = ("yellow", "orange", "red", "grey")

_readings = None
_calibration = None
_markers = None
_station_config = None


def init():
    """Connect and ensure indexes. Raises if unreachable, so a bad deploy fails at boot."""
    global _readings, _calibration, _markers, _station_config

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. MongoDB is required: configure MONGODB_URI "
            "(and optionally MONGODB_DATABASE)."
        )

    client = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    client.admin.command("ping")

    db = client[os.getenv("MONGODB_DATABASE", "water_level")]
    _readings = db["readings"]
    _calibration = db["calibration"]
    _markers = db["markers"]
    # Per-station settings that used to live only in the committed config file.
    _station_config = db["station_config"]

    # Every query is scoped to one station, so the station leads the index.
    _readings.create_index([("station", ASCENDING), ("timestamp", DESCENDING)])

    print(f"MongoDB ready: database={db.name}")


def add_reading(station_id, timestamp, level, image_url, y, mode=None):
    timestamp = int(timestamp)
    level = round(float(level), 2)

    doc = {
        "station": station_id,
        "timestamp": timestamp,
        "level": level,
        "image_url": image_url,
        "y": int(y),
        "created_at": datetime.now(timezone.utc),
    }
    # Which detector answered. Worth knowing afterwards: a "blind" reading came
    # from texture alone, with no gauge colour in the region to check it against.
    if mode:
        doc["mode"] = mode

    _readings.insert_one(doc)


def latest_readings(station_id, limit):
    """Newest raw readings, oldest-first. Feeds the current value and the capture strip."""
    docs = _readings.find(
        {"station": station_id},
        {"_id": 0, "timestamp": 1, "level": 1, "image_url": 1, "y": 1, "mode": 1}
    ).sort("timestamp", DESCENDING).limit(limit)
    return sorted(docs, key=lambda d: d["timestamp"])


def reading_near(station_id, timestamp, window):
    """
    The capture closest to `timestamp`, within `window` seconds either side.

    A chart point on a long range is an average over a bucket and has no single
    frame behind it, so clicking one asks for the nearest actual capture instead.
    """
    timestamp = int(timestamp)
    candidates = list(_readings.find(
        {"station": station_id,
         "timestamp": {"$gte": timestamp - window, "$lte": timestamp + window}},
        {"_id": 0, "timestamp": 1, "level": 1, "image_url": 1, "y": 1},
    ).sort("timestamp", ASCENDING))

    if not candidates:
        return None
    return min(candidates, key=lambda d: abs(d["timestamp"] - timestamp))


# Smallest bucket first. A three-year span holds ~315k readings, so anything past a
# couple of days has to be aggregated before it can reach a browser or a chart.
_BUCKETS = [300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 259200]


def _bucket_for(span_seconds, target_points):
    for bucket in _BUCKETS:
        if span_seconds / bucket <= target_points:
            return bucket
    return _BUCKETS[-1]


def history_series(station_id, start_ts, end_ts, target_points=800):
    """
    Level over a time range, averaged into buckets when the span is long.

    Each point also carries the true low/high within its bucket, so the summary
    figures stay honest: on a year view a peak would otherwise be flattened by
    the averaging and the dashboard would under-report it.
    """
    bucket = _bucket_for(max(1, end_ts - start_ts), target_points)

    points = list(_readings.aggregate([
        {"$match": {"station": station_id,
                    "timestamp": {"$gte": int(start_ts), "$lte": int(end_ts)}}},
        {"$group": {
            "_id": {"$subtract": ["$timestamp", {"$mod": ["$timestamp", bucket]}]},
            "level": {"$avg": "$level"},
            "low": {"$min": "$level"},
            "high": {"$max": "$level"},
        }},
        {"$sort": {"_id": 1}},
    ]))

    return {
        "bucket_seconds": bucket,
        "points": [{
            "timestamp": p["_id"],
            "level": round(p["level"], 2),
            "low": round(p["low"], 2),
            "high": round(p["high"], 2),
        } for p in points],
    }


def prune_readings(station_id, retention_days):
    """Drop readings past the retention window."""
    cutoff = int(datetime.now(timezone.utc).timestamp()) - retention_days * 86400
    return _readings.delete_many({"station": station_id,
                                  "timestamp": {"$lt": cutoff}}).deleted_count


def load_calibration_points(station_id):
    doc = _calibration.find_one({"_id": station_id})
    if not doc:
        return []
    return [tuple(p) for p in doc.get("points", [])]


def load_roi(station_id):
    """The detection region saved from the calibrate page, or None when nothing
    has been saved and stations.json still speaks for this station. Only the
    edges actually stored are returned: a save made before the vertical edges
    existed carries the columns alone, and the caller fills in the rest."""
    doc = _station_config.find_one({"_id": station_id}, {"_id": 0, "roi": 1})
    if not doc or not isinstance(doc.get("roi"), dict):
        return None
    roi = doc["roi"]
    try:
        edges = {k: int(roi[k])
                 for k in ("x_start", "x_end", "y_start", "y_end") if k in roi}
    except (TypeError, ValueError):
        return None
    return edges or None


def save_roi(station_id, roi):
    _station_config.update_one(
        {"_id": station_id},
        {"$set": {
            "roi": {k: int(roi[k])
                    for k in ("x_start", "x_end", "y_start", "y_end")},
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def clear_roi(station_id):
    """Hand the station back to the region committed in stations.json."""
    _station_config.update_one({"_id": station_id}, {"$unset": {"roi": ""}})


def load_markers(station_id):
    doc = _markers.find_one({"_id": station_id})
    if not doc:
        return []
    return doc.get("markers", [])


def save_markers(station_id, markers):
    _markers.update_one(
        {"_id": station_id},
        {"$set": {"markers": markers, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def save_calibration_points(station_id, points):
    _calibration.update_one(
        {"_id": station_id},
        {"$set": {
            "points": [[int(p[0]), float(p[1])] for p in points],
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
