"""One-off migrations for data written by an earlier version.

Run once against the target database; both steps are safe to repeat.

  1. Import calibration.json, from before calibration moved into MongoDB.
  2. Attach existing single-site data to a station, from before the app tracked
     more than one river.

    python migrate.py
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import database
import stations

CALIBRATION_FILE = "calibration.json"


def import_calibration_file(station_id):
    if not os.path.exists(CALIBRATION_FILE):
        return

    with open(CALIBRATION_FILE) as f:
        points = json.load(f).get("points", [])
    if not points:
        return

    if database.load_calibration_points(station_id):
        print(f"{station_id}: calibration already in MongoDB, leaving it alone.")
        return

    database.save_calibration_points(station_id, points)
    print(f"{station_id}: imported {len(points)} calibration points from {CALIBRATION_FILE}.")


def adopt_unassigned(station_id):
    """Claim the pre-multi-station documents for `station_id`."""
    readings = database._readings.update_many(
        {"station": {"$exists": False}}, {"$set": {"station": station_id}}
    ).modified_count
    if readings:
        print(f"{station_id}: tagged {readings} readings.")

    # Calibration and markers used to live under a single "current" document.
    for name, collection in (("calibration", database._calibration), ("markers", database._markers)):
        legacy = collection.find_one({"_id": "current"})
        if not legacy:
            continue
        if collection.find_one({"_id": station_id}):
            print(f"{station_id}: {name} already present, leaving the legacy document in place.")
            continue
        legacy["_id"] = station_id
        collection.insert_one(legacy)
        collection.delete_one({"_id": "current"})
        print(f"{station_id}: moved {name} across.")


def main():
    stations.load()
    database.init()

    target = stations.default_id()
    print(f"Migrating pre-existing data into station '{target}'.")

    adopt_unassigned(target)
    import_calibration_file(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
