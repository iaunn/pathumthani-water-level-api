"""MongoDB persistence for readings and calibration."""

import os
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING

CALIBRATION_ID = "current"
MARKERS_ID = "current"

# Reference lines are drawn from a fixed palette rather than free colours, so the
# chart keeps a consistent reading of severity across the whole dashboard.
MARKER_COLORS = ("yellow", "orange", "red", "grey")

_readings = None
_calibration = None
_markers = None


def init():
    """Connect and ensure indexes. Raises if unreachable, so a bad deploy fails at boot."""
    global _readings, _calibration, _markers

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

    _readings.create_index([("timestamp", DESCENDING)])

    print(f"MongoDB ready: database={db.name}")


def add_reading(timestamp, level, image_url, y):
    timestamp = int(timestamp)
    level = round(float(level), 2)

    _readings.insert_one({
        "timestamp": timestamp,
        "level": level,
        "image_url": image_url,
        "y": int(y),
        "created_at": datetime.now(timezone.utc),
    })


def latest_readings(limit):
    """Newest raw readings, oldest-first. Feeds the current value and the capture strip."""
    docs = _readings.find(
        {}, {"_id": 0, "timestamp": 1, "level": 1, "image_url": 1, "y": 1}
    ).sort("timestamp", DESCENDING).limit(limit)
    return sorted(docs, key=lambda d: d["timestamp"])


def reading_near(timestamp, window):
    """
    The capture closest to `timestamp`, within `window` seconds either side.

    A chart point on a long range is an average over a bucket and has no single
    frame behind it, so clicking one asks for the nearest actual capture instead.
    """
    timestamp = int(timestamp)
    candidates = list(_readings.find(
        {"timestamp": {"$gte": timestamp - window, "$lte": timestamp + window}},
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


def history_series(start_ts, end_ts, target_points=800):
    """
    Level over a time range, averaged into buckets when the span is long.

    Each point also carries the true low/high within its bucket, so the summary
    figures stay honest: on a year view a peak would otherwise be flattened by
    the averaging and the dashboard would under-report it.
    """
    bucket = _bucket_for(max(1, end_ts - start_ts), target_points)

    points = list(_readings.aggregate([
        {"$match": {"timestamp": {"$gte": int(start_ts), "$lte": int(end_ts)}}},
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


def prune_readings(retention_days):
    """Drop readings past the retention window."""
    cutoff = int(datetime.now(timezone.utc).timestamp()) - retention_days * 86400
    return _readings.delete_many({"timestamp": {"$lt": cutoff}}).deleted_count


def load_calibration_points():
    doc = _calibration.find_one({"_id": CALIBRATION_ID})
    if not doc:
        return []
    return [tuple(p) for p in doc.get("points", [])]


def load_markers():
    doc = _markers.find_one({"_id": MARKERS_ID})
    if not doc:
        return []
    return doc.get("markers", [])


def save_markers(markers):
    _markers.update_one(
        {"_id": MARKERS_ID},
        {"$set": {"markers": markers, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def save_calibration_points(points):
    _calibration.update_one(
        {"_id": CALIBRATION_ID},
        {"$set": {
            "points": [[int(p[0]), float(p[1])] for p in points],
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
