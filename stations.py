"""Monitoring sites: feed URL, where the staff sits in frame, and per-site runtime state.

Every spatial figure differs between cameras -- Pathumthani is 640x480 with the
staff at x 225-390, Pakkret is 1280x720 with it at x 715-808 -- so the detector is
shared but nothing positional is. Calibration, markers, readings and the
homography baseline are all scoped to a station too.
"""

import json
import os
import threading
from urllib.parse import urljoin

import cv2
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

CONFIG_FILE = os.getenv("STATIONS_FILE", "stations.json")

_stations = {}
_default_id = None


class Station:
    # A region smaller than this cannot contain a staff, and no camera in use
    # produces an image beyond this. An edge past it simply means "to the end of
    # the frame", which is how an unbounded bottom or right reads.
    MIN_ROI_WIDTH = 8
    MIN_ROI_HEIGHT = 8
    MAX_ROI_EDGE = 8192

    def __init__(self, cfg):
        self.id = cfg["id"]
        self.name = cfg.get("name") or {"en": cfg["id"]}
        # A station is fed either an HLS playlist or a still-image endpoint. The
        # network cameras only offer the latter, behind digest auth.
        # Where this feed comes from, credited in the dashboard footer.
        self.source = cfg.get("source")
        self.playlist_url = cfg.get("playlist_url")
        self.snapshot_url = cfg.get("snapshot_url")
        if not (self.playlist_url or self.snapshot_url):
            raise RuntimeError(f"Station '{self.id}' needs a playlist_url or a snapshot_url.")

        # Segment paths in a playlist are usually relative to the playlist itself.
        self.base_url = urljoin(self.playlist_url, ".") if self.playlist_url else None

        # Credentials never live in this file; it is committed. The station names
        # an environment variable holding "user:password" instead.
        self.auth_scheme = (cfg.get("auth") or "").lower()
        self.credentials_env = cfg.get("credentials_env") or f"STATION_{self.id.upper()}_CREDENTIALS"

        # All four edges, in frame pixels: x runs left to right, y top to bottom.
        # stations.json usually carries only the columns, so the rows default to
        # the whole frame.
        roi = dict(cfg.get("roi") or {})
        roi.setdefault("x_start", 0)
        roi.setdefault("x_end", 0)
        roi.setdefault("y_start", 0)
        roi.setdefault("y_end", self.MAX_ROI_EDGE)

        # What stations.json says, kept so a region saved from the calibrate page
        # can be undone. That file stays the default: it is committed, so it is
        # where a brand-new station's region is first set.
        try:
            self.config_roi = self.validate_roi(roi)
        except ValueError as e:
            raise RuntimeError(f"Station '{self.id}' has an unusable roi in {CONFIG_FILE}: {e}")
        self.roi_source = "config"
        self.set_roi(self.config_roi, source="config")

        self.cal_points = []

        # Mutable detection state. Each station is tracked by its own thread, and
        # ORB is not safe to share across them, so nothing here is global.
        self.reference = {"image": None, "keypoints": None, "descriptors": None}
        self.orb = cv2.ORB_create(nfeatures=2000)
        self.detection_history = []
        self.previous_level = 0.0
        # Which detector produced the last answer: "gauge" (colour + texture),
        # "blind" (texture only, no gauge colour in the region), "fused", or
        # "none". Surfaced so a blind reading is never mistaken for a normal one.
        self.detection_mode = None
        self.lock = threading.Lock()

    @property
    def is_snapshot(self):
        return bool(self.snapshot_url)

    @property
    def roi(self):
        return {"x_start": self.x_start, "x_end": self.x_end,
                "y_start": self.y_start, "y_end": self.y_end}

    @classmethod
    def validate_roi(cls, roi):
        """The four edges as integers, or ValueError saying what is wrong with them.

        `roi` is a dict, so a request body, a stations.json entry and a document
        read back from MongoDB all go through the same checks.
        """
        missing = [k for k in ("x_start", "x_end", "y_start", "y_end")
                   if roi.get(k) is None]
        if missing:
            raise ValueError("Missing ROI edge(s): " + ", ".join(missing) + ".")
        try:
            xs, xe = int(roi["x_start"]), int(roi["x_end"])
            ys, ye = int(roi["y_start"]), int(roi["y_end"])
        except (TypeError, ValueError):
            raise ValueError("ROI edges must be whole pixels.")
        if min(xs, ys) < 0 or max(xe, ye) > cls.MAX_ROI_EDGE:
            raise ValueError(
                f"ROI edges must be between 0 and {cls.MAX_ROI_EDGE} pixels.")
        if xe - xs < cls.MIN_ROI_WIDTH:
            raise ValueError(
                f"Detection region must be at least {cls.MIN_ROI_WIDTH} pixels wide.")
        if ye - ys < cls.MIN_ROI_HEIGHT:
            raise ValueError(
                f"Detection region must be at least {cls.MIN_ROI_HEIGHT} pixels tall.")
        return {"x_start": xs, "x_end": xe, "y_start": ys, "y_end": ye}

    def set_roi(self, roi, source="database"):
        """Move the detection region. Raises ValueError when any edge is unusable."""
        checked = self.validate_roi(roi)
        self.x_start, self.x_end = checked["x_start"], checked["x_end"]
        self.y_start, self.y_end = checked["y_start"], checked["y_end"]
        self.roi_source = source

    @property
    def auth(self):
        """requests auth for this station, or None when it needs no credentials."""
        if not self.auth_scheme:
            return None

        raw = os.getenv(self.credentials_env)
        if not raw or ":" not in raw:
            raise RuntimeError(
                f"Station '{self.id}' uses {self.auth_scheme} auth but "
                f"{self.credentials_env} is not set to 'user:password'."
            )

        user, _, password = raw.partition(":")
        if self.auth_scheme == "digest":
            return HTTPDigestAuth(user, password)
        if self.auth_scheme == "basic":
            return HTTPBasicAuth(user, password)
        raise RuntimeError(
            f"Station '{self.id}' has unknown auth '{self.auth_scheme}'; use digest or basic."
        )

    @property
    def capture_prefix(self):
        return f"captures/{self.id}/"

    @property
    def reference_key(self):
        return f"reference/{self.id}.jpg"

    def public(self):
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "roi": self.roi,
            "detection_mode": self.detection_mode,
        }


def load():
    """Read the station list. Raises if it is missing or empty, so a bad deploy fails at boot."""
    global _stations, _default_id

    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(f"{CONFIG_FILE} not found. It lists the monitoring sites to track.")

    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)

    entries = cfg.get("stations") or []
    if not entries:
        raise RuntimeError(f"{CONFIG_FILE} lists no stations.")

    _stations = {}
    for entry in entries:
        station = Station(entry)
        if station.id in _stations:
            raise RuntimeError(f"Duplicate station id '{station.id}' in {CONFIG_FILE}.")
        _stations[station.id] = station

    _default_id = cfg.get("default") or entries[0]["id"]
    if _default_id not in _stations:
        raise RuntimeError(f"Default station '{_default_id}' is not in {CONFIG_FILE}.")

    for station in _stations.values():
        station.auth   # fail at boot, not five minutes later, if credentials are missing

    def _region(s):
        ye = "bottom" if s.y_end >= Station.MAX_ROI_EDGE else s.y_end
        xe = "right" if s.x_end >= Station.MAX_ROI_EDGE else s.x_end
        return f"{s.id} ({'snapshot' if s.is_snapshot else 'hls'}, " \
               f"x{s.x_start}-{xe}, y{s.y_start}-{ye})"

    print("Stations: " + ", ".join(_region(s) for s in _stations.values()))
    return _stations


def all_stations():
    return list(_stations.values())


def get(station_id):
    """Look up a station, falling back to the default when the id is unknown or absent."""
    if station_id and station_id in _stations:
        return _stations[station_id]
    return _stations[_default_id]


def exists(station_id):
    return station_id in _stations


def default_id():
    return _default_id
