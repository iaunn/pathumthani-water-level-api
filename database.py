"""MongoDB persistence for readings and calibration."""

import os
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING

CALIBRATION_ID = "current"

_readings = None
_calibration = None


def init():
    """Connect and ensure indexes. Raises if unreachable, so a bad deploy fails at boot."""
    global _readings, _calibration

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

    _readings.create_index([("timestamp", DESCENDING)])

    print(f"MongoDB ready: database={db.name}")


def add_reading(timestamp, level, image_url, y):
    _readings.insert_one({
        "timestamp": int(timestamp),
        "level": round(float(level), 2),
        "image_url": image_url,
        "y": int(y),
        "created_at": datetime.now(timezone.utc),
    })


def recent_readings(limit):
    """Readings oldest-first, which is the order the dashboard charts them in."""
    docs = _readings.find(
        {}, {"_id": 0, "timestamp": 1, "level": 1, "image_url": 1, "y": 1}
    ).sort("timestamp", DESCENDING).limit(limit)
    return sorted(docs, key=lambda d: d["timestamp"])


def prune_readings(keep):
    """Drop readings beyond the newest `keep`, mirroring the capture rotation."""
    total = _readings.count_documents({})
    if total <= keep:
        return 0

    cutoff = list(
        _readings.find({}, {"timestamp": 1}).sort("timestamp", DESCENDING).skip(keep).limit(1)
    )
    if not cutoff:
        return 0

    result = _readings.delete_many({"timestamp": {"$lte": cutoff[0]["timestamp"]}})
    return result.deleted_count


def load_calibration_points():
    doc = _calibration.find_one({"_id": CALIBRATION_ID})
    if not doc:
        return []
    return [tuple(p) for p in doc.get("points", [])]


def save_calibration_points(points):
    _calibration.update_one(
        {"_id": CALIBRATION_ID},
        {"$set": {
            "points": [[int(p[0]), float(p[1])] for p in points],
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
