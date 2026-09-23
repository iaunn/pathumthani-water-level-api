"""One-off import of the pre-cloud local state into MongoDB.

Only calibration is worth moving: it is hand-tuned and hard to reproduce, whereas
history.json regenerates within a day and its captures live on a disk the app no
longer reads. Run once against the target database, then delete the local files.

    python migrate.py
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import database

CALIBRATION_FILE = "calibration.json"


def main():
    if not os.path.exists(CALIBRATION_FILE):
        print(f"No {CALIBRATION_FILE} to import.")
        return 0

    with open(CALIBRATION_FILE) as f:
        points = json.load(f).get("points", [])

    if not points:
        print(f"{CALIBRATION_FILE} has no points.")
        return 0

    database.init()

    existing = database.load_calibration_points()
    if existing:
        print(f"Calibration already in MongoDB ({len(existing)} points). Refusing to overwrite.")
        return 1

    database.save_calibration_points(points)
    print(f"Imported {len(points)} calibration points into MongoDB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
