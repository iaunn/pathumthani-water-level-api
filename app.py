from dotenv import load_dotenv

# Before any import that reads the environment.
load_dotenv()

import cv2
import numpy as np
import requests
import os
import warnings
import json
import threading
import time
from flask import Flask, jsonify, request, render_template, abort
from datetime import datetime
from flask_caching import Cache

import storage
import database
import stations
from stations import Station

# Suppress OpenCV warnings and H.264 decoder warnings
warnings.filterwarnings("ignore", category=UserWarning)
os.environ['OPENCV_FFMPEG_LOGLEVEL'] = '-8'  # Suppress FFmpeg logs

# Initialize Flask app
app = Flask(__name__)

# Configure caching
cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})

# Get TTL from environment variable or set a default
CACHE_TTL = int(os.getenv('CACHE_TTL', 300))

# Maximum number of captures to keep (older ones are deleted automatically)
MAX_KEEP = int(os.getenv("MAX_KEEP_IMAGES", 200))

storage.init()
database.init()
stations.load()

# Base URL for fetching HLS playlist
# Detection smoothing window, kept per station
MAX_HISTORY = 5

# =========================
# Calibration (NEW MODEL)
# =========================
# Pixel is measured from top of the image (y increases downward)
# Ensure points are sorted by pixel (descending level with increasing pixel)
def load_calibration(station):
    try:
        station.cal_points = sorted(database.load_calibration_points(station.id), key=lambda x: x[0])
    except Exception as e:
        print(f"Error loading calibration for {station.id}: {e}")

def save_calibration_to_file(station, points):
    try:
        database.save_calibration_points(station.id, points)
        return True
    except Exception as e:
        print(f"Error saving calibration for {station.id}: {e}")
        return False

def load_roi(station):
    """Pick up a detection region saved from the calibrate page -- by this replica
    or another. Nothing saved means stations.json still decides, which is how a
    station is given its region in the first place."""
    saved = database.load_roi(station.id)
    if not saved:
        station.roi_source = "config"
        return False

    # Overlay the saved edges on the ones in force, so a save predating the
    # vertical edges keeps its columns and defaults the rows to the whole frame.
    station.set_roi(dict(station.roi, **saved), source="database")
    return True

# =========================
# History Tracking
# =========================
# Readings are small; keeping three years of them costs little and is what the
# dashboard's longest range needs. Captures are pruned separately by MAX_KEEP,
# since only recent frames are ever displayed.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", 1095))

# How far back a single view may span. Readings are kept for three years, but one
# request covers at most three months of them, which keeps the aggregation inside
# a few hundred milliseconds however far back the window is dragged.
MAX_RANGE_DAYS = int(os.getenv("MAX_RANGE_DAYS", 92))

# How many raw captures the dashboard's strip and current reading need.
RECENT_LIMIT = 24

def background_tracker(station):
    """Runs every 5 minutes to capture and record one station's water level."""
    while True:
        try:
            print(f"[{station.id}] running background water level check...")
            # picks up calibration and ROI saved by another replica
            load_calibration(station)
            load_roi(station)

            if not station.cal_points:
                # Without calibration a pixel row converts to a meaningless
                # level, which is worse on a flood dashboard than no reading.
                print(f"[{station.id}] no calibration yet, skipping")
                time.sleep(300)
                continue

            # Several frames, not one: still water mirrors the staff, and only
            # the reflection moves between them.
            segment = capture_station_frames(station)
            frame = segment[-1] if segment else None

            if frame is not None:
                with station.lock:
                    aligned_frame = align_image(station, frame)
                    aligned_segment = [align_image(station, f) for f in segment] or None
                    meta = {}
                    raw_y = detect_water_level_on_gauge(
                        aligned_frame, station.x_start, station.x_end,
                        y_start=station.y_start, y_end=station.y_end,
                        frames=aligned_segment, meta=meta)
                    # Nothing behind it. The gauge detector refusing -- the
                    # colour check failed, or the region holds no staff -- is
                    # the honest answer, and the cascade that used to run here
                    # only ever guessed: it reported 885 px with the surface
                    # at 646, and 742 px where the staff met the water at 985.

                    # Which detector answers is worth keeping with the reading:
                    # a blind number has no gauge colour behind it.
                    station.detection_mode = (meta.get("mode")
                                              if raw_y is not None else "none")
                    # Smooth only an observation. When the detector declines,
                    # smoothing would hand back a value carried from earlier
                    # cycles, and five-minute repeats would draw a flat line
                    # over a gap that ought to be visible as a gap.
                    y = smooth_detection(station, raw_y) if raw_y is not None else None

                if y is not None:
                    level = float(pixel_to_level(station, float(y)))

                    timestamp = int(time.time())
                    image_url = store_frame(station, aligned_frame, "history", f"_{timestamp}")

                    # "y" lets the dashboard draw the detected waterline over
                    # the frame, so a wrong reading is visible rather than implied.
                    database.add_reading(station.id, timestamp, level, image_url, y,
                                          mode=station.detection_mode)
                    database.prune_readings(station.id, RETENTION_DAYS)
                    rotate_images(station)
                    print(f"[{station.id}] check successful. Level: {level:.2f}m")
        except Exception as e:
            # One station's camera going down must not stop the others.
            print(f"[{station.id}] background tracker error: {e}")

        time.sleep(300) # Wait 5 minutes

# Useful bounds
def get_bounds(station):
    if station.cal_points:
        return station.cal_points[0], station.cal_points[-1]
    return (0, 0), (0, 0)

def pixel_to_level(station, y: float) -> float:
    """
    Piecewise-linear interpolation from pixel (y, from top) to water level (m).
    Extrapolates using the nearest segment if y is outside calibration range.

    Raises when the station has not been calibrated: a station is added before
    anyone sets its points, and a made-up level is worse than a refusal.
    """
    if len(station.cal_points) < 2:
        raise RuntimeError(f"Station '{station.id}' has no calibration yet.")

    # exact match
    for py, lv in station.cal_points:
        if y == py:
            return lv

    # choose segment
    if y < station.cal_points[0][0]:
        # above top point -> extrapolate using first segment
        (x1, y1), (x2, y2) = station.cal_points[0], station.cal_points[1]
    elif y > station.cal_points[-1][0]:
        # below bottom point -> extrapolate using last segment
        (x1, y1), (x2, y2) = station.cal_points[-2], station.cal_points[-1]
    else:
        # find adjacent calibration points
        for i in range(len(station.cal_points) - 1):
            x1, y1 = station.cal_points[i]
            x2, y2 = station.cal_points[i + 1]
            if x1 <= y <= x2:
                break

    # linear interpolation y = m*x + b  (but here variables are renamed)
    # we want level = y1 + (y2 - y1) * ( (y - x1) / (x2 - x1) )
    if x2 == x1:
        return y1  # degenerate safety
    t = (y - x1) / (x2 - x1)
    return y1 + (y2 - y1) * t

def level_to_pixel(station, level_m: float) -> float:
    """
    Inverse mapping: given a level (m), return pixel y (from top).
    Piecewise-linear using the same calibration points.
    """
    if len(station.cal_points) < 2:
        raise RuntimeError(f"Station '{station.id}' has no calibration yet.")

    # exact match
    for py, lv in station.cal_points:
        if abs(level_m - lv) < 1e-9:
            return py

    # choose segment (levels decrease with pixel)
    levels = [lv for _, lv in station.cal_points]
    if level_m > levels[0]:
        (x1, y1), (x2, y2) = station.cal_points[0], station.cal_points[1]
    elif level_m < levels[-1]:
        (x1, y1), (x2, y2) = station.cal_points[-2], station.cal_points[-1]
    else:
        for i in range(len(station.cal_points) - 1):
            x1, y1 = station.cal_points[i]
            x2, y2 = station.cal_points[i + 1]
            # y1 >= level >= y2 in normal case
            if (y1 >= level_m >= y2) or (y1 <= level_m <= y2):
                break

    if y1 == y2:
        return x1
    t = (level_m - y1) / (y2 - y1)
    return x1 + (x2 - x1) * t

# =========================
# Homography Alignment
# =========================
# Warping every frame onto a stored baseline costs an ORB pass and a warp per
# capture, plus a baseline upload each time the calibration view opens. A camera
# bolted in place never drifts, so HOMOGRAPHY_ENABLED=false skips all of that and
# detection runs on the frame exactly as it came.
HOMOGRAPHY_ENABLED = os.getenv("HOMOGRAPHY_ENABLED", "true").lower() not in ("false", "0", "no")

def _adopt_reference(station, frame):
    """Hold `frame` in memory as the station's homography baseline."""
    gray_ref = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    kp, des = station.orb.detectAndCompute(gray_ref, None)
    station.reference["image"] = frame.copy()
    station.reference["keypoints"] = kp
    station.reference["descriptors"] = des

def set_reference_frame(station, frame):
    """Make `frame` the new homography baseline and persist it."""
    _adopt_reference(station, frame)
    storage.upload_frame(frame, station.reference_key)
    print(f"Stored new reference frame for {station.id}.")
    return True

def init_reference_frame(station, frame):
    """Load the stored baseline, falling back to `frame` when there is none yet."""
    stored = storage.download_frame(station.reference_key)
    if stored is not None:
        _adopt_reference(station, stored)
        print(f"Loaded reference frame for {station.id}.")
        return True

    return set_reference_frame(station, frame)

def align_image(station, frame):
    """Align the given frame to the station's reference frame using ORB feature matching."""
    if not HOMOGRAPHY_ENABLED:
        # Flag off: the camera does not move, so there is no offset to undo --
        # and no reference frame to fetch or adopt either.
        return frame

    if station.reference["image"] is None:
        init_reference_frame(station, frame)
        return frame # First frame is reference
        
    gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    kp_frame, des_frame = station.orb.detectAndCompute(gray_frame, None)
    
    if des_frame is None or station.reference["descriptors"] is None:
        return frame
        
    # Match features
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(station.reference["descriptors"], des_frame)
    matches = sorted(matches, key=lambda x: x.distance)
    
    # Keep top matches
    GOOD_MATCH_PERCENT = 0.15
    num_good_matches = int(len(matches) * GOOD_MATCH_PERCENT)
    matches = matches[:num_good_matches]
    
    if len(matches) < 10:
        print("Not enough matches for homography, returning original frame.")
        return frame
        
    # Extract location of good matches
    points1 = np.zeros((len(matches), 2), dtype=np.float32)
    points2 = np.zeros((len(matches), 2), dtype=np.float32)
    
    for i, match in enumerate(matches):
        points1[i, :] = station.reference["keypoints"][match.queryIdx].pt
        points2[i, :] = kp_frame[match.trainIdx].pt
        
    # Find homography
    h_matrix, inliers = cv2.findHomography(points2, points1, cv2.RANSAC)
    
    if h_matrix is not None:
        # Warp frame to align with reference
        height, width, channels = station.reference["image"].shape
        aligned_frame = cv2.warpPerspective(frame, h_matrix, (width, height))
        print("Successfully aligned frame to reference.")
        return aligned_frame
    
    print("Homography matrix calculation failed, returning original frame.")
    return frame

def cache_key():
    """Return a unique cache key based on the request URL path."""
    return request.path

def parse_hls_playlist(playlist_content):
    """Parse HLS playlist content and return list of video segments."""
    segments = []
    lines = playlist_content.strip().split('\n')
    for line in lines:
        line = line.strip()
        if line.startswith('#') or not line:
            continue
        if any(line.endswith(ext) for ext in ['.ts', '.mp4', '.m4s', '.webm']):
            segments.append(line)
    return segments

def get_video_url(station):
    """Fetch the HLS playlist and get the last video segment URL."""
    try:
        response = requests.get(station.playlist_url, timeout=10)
        if response.status_code == 200:
            segments = parse_hls_playlist(response.text)
            if segments:
                last_segment = segments[-1]
                return last_segment if last_segment.startswith('http') else station.base_url + last_segment
    except requests.RequestException as e:
        print(f"Error fetching playlist: {e}")
    return None

def get_video_segments(station):
    """Fetch the HLS playlist and return multiple recent video segments."""
    try:
        response = requests.get(station.playlist_url, timeout=10)
        if response.status_code == 200:
            segments = parse_hls_playlist(response.text)
            if segments:
                recent_segments = segments[-3:] if len(segments) >= 3 else segments
                absolute_segments = []
                for segment in recent_segments:
                    absolute_segments.append(segment if segment.startswith('http') else station.base_url + segment)
                return absolute_segments
    except requests.RequestException as e:
        print(f"Error fetching playlist: {e}")
    return []

def _roi_rows(image, y_start=0, y_end=None):
    """The rows the detector may look at, clipped to the frame.

    The bottom edge is commonly the frame's own height (or larger), so this is
    where "to the bottom of the picture" stops being a sentinel and becomes
    actual rows. At least one row always survives, so a caller that would
    otherwise get an empty band still gets an answer rather than an error.
    """
    h = image.shape[0]
    top = 0 if y_start is None else max(0, min(int(y_start), h))
    bottom = h if y_end is None else max(top, min(int(y_end), h))
    if bottom - top < 1:
        bottom = min(h, top + 1)
    return top, bottom


def detect_yellow_region_enhanced(image, x_start=225, x_end=390, y_start=0, y_end=None):
    """Highly accurate yellow region detection with multiple enhancement strategies."""
    h = image.shape[0]
    w = image.shape[1]
    ys, ye = _roi_rows(image, y_start, y_end)
    
    # Multi-scale color detection for robustness
    detected_rows = []
    
    # Try multiple HSV ranges to catch different lighting conditions
    hsv_ranges = [
        ([20, 120, 100], [30, 255, 255]),   # Pure yellow
        ([15, 100, 80],  [35, 255, 255]),  # Extended yellow-orange
        ([18, 80, 80],   [45, 255, 255]),  # Broad yellow range
        ([25, 150, 100], [35, 255, 255]),  # Bright yellow
    ]
    
    for lower_hsv, upper_hsv in hsv_ranges:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        
        # Create mask
        mask = cv2.inRange(hsv, np.array(lower_hsv), np.array(upper_hsv))
        mask[:, :x_start] = 0
        mask[:, x_end:] = 0
        mask[:ys] = 0
        mask[ye:] = 0
        
        # Enhanced morphological operations
        kernel_small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        kernel_large = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        
        # Close small gaps, then open to remove noise
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_small)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_large)
        
        # Find contours
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Enhanced contour filtering
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > 50:  # Lower minimum area for better sensitivity
                x, y, w_rect, h_rect = cv2.boundingRect(cnt)
                aspect_ratio = w_rect / max(h_rect, 1)
                
                # More flexible aspect ratio requirements
                if aspect_ratio > 1.2:  # Allow more vertical features
                    # Calculate centroid and use bottom edge
                    centroid_y = y + h_rect
                    detected_rows.append((centroid_y, area, aspect_ratio))
    
    # 2. SEGMENTATION-BASED DETECTION
    # Use k-means clustering to separate yellow regions
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    
    # Adaptive thresholding for edge detection
    edges = cv2.Canny(blurred, 50, 150)
    
    # Find horizontal lines specifically
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=50, 
                            minLineLength=30, maxLineGap=10)
    
    if lines is not None:
        # OpenCV 5 returns HoughLinesP as (N, 4); OpenCV 4 returned (N, 1, 4),
        # which is what line[0] was written against. Reshaping accepts both.
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            # Focus on relatively horizontal lines
            if x_start <= min(x1, x2) and max(x1, x2) <= x_end:
                angle = np.arctan2(y2-y1, x2-x1) * 180 / np.pi
                if abs(angle) < 15:  # Nearly horizontal (±15 degrees)
                    # Get yellow pixels near this line
                    line_y = int((y1 + y2) / 2)
                    if max(50, ys) <= line_y <= min(h - 50, ye):  # In region, off frame edge
                        # Check for yellow content near this line
                        roi_patch = image[line_y-10:line_y+10, x_start:x_end]
                        hsv_patch = cv2.cvtColor(roi_patch, cv2.COLOR_BGR2HSV)
                        
                        # Count yellow pixels
                        yellow_mask = cv2.inRange(hsv_patch, np.array([20, 100, 100]), 
                                                np.array([35, 255, 255]))
                        yellow_count = np.sum(yellow_mask > 0)
                        
                        if yellow_count > 20:  # Significant yellow presence
                            detected_rows.append((line_y, yellow_count, 10.0))  # High aspect ratio for lines
    
    # 3. TEMPLATE MATCHING for water level markers
    if not detected_rows:
        # Template matching as last resort
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        
        # Look for horizontal edge patterns
        kernel_edge = np.array([[-1, -1, -1], [2, 2, 2], [-1, -1, -1]])
        edge_response = cv2.filter2D(gray, -1, kernel_edge)
        
        # Find peaks in edge response within ROI
        roi_edges = edge_response[ys:ye, x_start:x_end]
        
        # Find horizontal lines with high edge response
        for y in range(max(100, ys), min(h - 100, ye), 5):  # Sample every 5 pixels
            line_response = np.mean(roi_edges[max(0, y - 5 - ys):y + 5 - ys, :])
            if line_response > np.percentile(edge_response, 80):
                # Verify with color
                color_sample = image[y-5:y+5, x_start:x_end]
                hsv_sample = cv2.cvtColor(color_sample, cv2.COLOR_BGR2HSV)
                yellow_ratio = np.sum(cv2.inRange(hsv_sample, np.array([20, 100, 100]), 
                                                np.array([35, 255, 255])) > 0) / color_sample.size
                
                if yellow_ratio > 0.1:  # At least 10% yellow
                    detected_rows.append((y, line_response * 1000, 5.0))
    
    # FINAL SELECTION: Choose the best detection
    if detected_rows:
        # Sort by position (bottom most first), weighted by detection confidence
        detected_rows.sort(key=lambda item: (h - item[0]) + item[1] // 10000, reverse=True)
        
        # Return the position of the most confident bottom detection
        return detected_rows[0][0]
    
    return None

def smooth_detection(station, raw_detection):
    """Apply temporal smoothing to reduce noise in detections."""
    station.detection_history.append(raw_detection)

    if len(station.detection_history) > MAX_HISTORY:
        station.detection_history = station.detection_history[-MAX_HISTORY:]

    valid_detections = [d for d in station.detection_history if d is not None]
    
    if len(valid_detections) < 2:
        return raw_detection
    
    # Apply different smoothing strategies based on detection quality
    if len(valid_detections) >= 3:
        # For stable histories, use median filtering + small mean
        valid_detections.sort()
        median_val = valid_detections[len(valid_detections) // 2]
        
        # Check if recent values are close to median (stable detection)
        recent_vals = valid_detections[-3:]
        median_distances = [abs(val - median_val) for val in recent_vals]
        avg_distance = np.mean(median_distances)
        
        if avg_distance < 20:  # Stable detection
            # Use median for stability
            return int(median_val)
        else:
            # Use weighted average favoring recent values
            weights = [i + 1 for i in range(len(valid_detections))]
            weighted_avg = sum(val * weight for val, weight in zip(valid_detections, weights)) / sum(weights)
            return int(weighted_avg)
    else:
        # For short histories, use simple average
        return int(np.mean(valid_detections))

def adaptive_color_range(image):
    """Adapt HSV ranges based on image properties."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    # Calculate image brightness
    brightness = np.mean(hsv[:, :, 2])  # Value channel average
    
    # Adjust ranges based on brightness
    if brightness < 80:  # Dark image
        return ([15, 60, 60], [40, 255, 255])
    elif brightness > 200:  # Bright image
        return ([20, 150, 120], [35, 255, 255])
    else:  # Normal lighting
        return ([18, 100, 100], [35, 255, 255])

def detect_yellow_region_adaptive(image, x_start=225, x_end=390, y_start=0, y_end=None):
    """Ultra-precisive yellow detection with adaptive parameters."""
    h = image.shape[0]
    w = image.shape[1]
    ys, ye = _roi_rows(image, y_start, y_end)
    
    detected_rows = []
    
    # 1. ADAPTIVE COLOR RANGES
    lower_hsv, upper_hsv = adaptive_color_range(image)
    base_hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    # Try multiple variations around adaptive range
    hue_variations = [-3, -2, -1, 0, 1, 2, 3]
    sat_variations = [-20, -10, 0, 10, 20]
    
    for hue_var in hue_variations:
        for sat_var in sat_variations:
            adaptive_lower = np.array([
                max(0, lower_hsv[0] + hue_var),
                max(0, lower_hsv[1] + sat_var),
                lower_hsv[2]
            ])
            adaptive_upper = np.array([
                min(179, upper_hsv[0] + hue_var),
                min(255, upper_hsv[1] + sat_var),
                upper_hsv[2]
            ])
            
            mask = cv2.inRange(base_hsv, adaptive_lower, adaptive_upper)
            mask[:, :x_start] = 0
            mask[:, x_end:] = 0
            mask[:ys] = 0
            mask[ye:] = 0
            
            # Smart morphological operations
            if np.sum(mask > 0) > 100:  # Only process if enough yellow pixels
                # Variable kernel size based on image resolution
                kernel_size = max(3, min(7, h // 100))
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
                
                mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
                
                # Find contiguous regions
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                for cnt in contours:
                    area = cv2.contourArea(cnt)
                    if area > 30:  # Lower threshold for sensitivity
                        x, y, w_rect, h_rect = cv2.boundingRect(cnt)
                        
                        # More sophisticated feature extraction
                        if w_rect > h_rect:  # Horizontal preference
                            solidity = area / cv2.contourArea(cv2.convexHull(cnt))
                            aspect_ratio = w_rect / max(h_rect, 1)
                            
                            if solidity > 0.7 and aspect_ratio > 0.8:  # Solid, reasonably shaped regions
                                centroid_y = y + h_rect // 2
                                confidence = area * solidity * min(aspect_ratio, 3.0)
                                detected_rows.append((centroid_y, confidence, area))
    
    # 2. EDGE-DENSITY BASED DETECTION
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    
    # Multi-scale edge detection
    for sigma in [0.5, 1.0, 1.5, 2.0]:
        edges = cv2.Canny(cv2.GaussianBlur(gray, (0, 0), sigma), 30, 100)
        
        # Find horizontal edge density
        roi_edges = edges[ys:ye, x_start:x_end]
        
        for y in range(max(20, ys), min(h - 20, ye), 3):  # Fine sampling
            window_size = 11
            edge_window = roi_edges[max(0, y - window_size // 2 - ys):y + window_size // 2 - ys, :]
            edge_density = np.sum(edge_window > 0) / edge_window.size
            
            if edge_density > 0.1:  # Significant edge density
                # Verify with color content
                color_window = image[y-window_size//2:y+window_size//2, x_start:x_end]
                hsv_window = cv2.cvtColor(color_window, cv2.COLOR_BGR2HSV)
                
                yellow_count = np.sum(cv2.inRange(hsv_window, adaptive_lower, adaptive_upper) > 0)
                yellow_ratio = yellow_count / color_window.size
                
                if yellow_ratio > 0.15:  # High yellow content
                    line_confidence = edge_density * 1000 + yellow_ratio * 500
                    detected_rows.append((y, line_confidence, edge_density * 1000))
    
    # 3. CONTOUR CONVERGENCE ANALYSIS
    if detected_rows:
        # Group nearby detections
        detected_rows.sort(key=lambda x: x[0])
        groups = []
        current_group = [detected_rows[0]]
        
        for det in detected_rows[1:]:
            if abs(det[0] - current_group[-1][0]) < 15:  # Within 15 pixels
                current_group.append(det)
            else:
                groups.append(current_group)
                current_group = [det]
        groups.append(current_group)
        
        # Select best from each group
        best_detections = []
        for group in groups:
            if len(group) > 1:  # Multiple detections in group
                # Weight by confidence and count
                avg_y = sum(d[0] for d in group) / len(group)
                total_confidence = sum(d[1] for d in group)
                total_size = sum(d[2] for d in group)
                
                best_detections.append((avg_y, total_confidence * len(group), total_size))
            else:
                best_detections.append(group[0])
        
        if best_detections:
            # Select bottom-most detection with good confidence
            best_detections.sort(key=lambda x: (-x[1], x[0]), reverse=True)  # Confidence first, then position
            return int(best_detections[0][0])
    
    return None

def detect_yellow_regions_fused(image, x_start=225, x_end=390, y_start=0, y_end=None):
    """Ultimate yellow detection cascade with all enhancement strategies."""
    ys, ye = _roi_rows(image, y_start, y_end)

    # 1. Try most advanced adaptive method
    result = detect_yellow_region_adaptive(image, x_start, x_end, ys, ye)
    if result is not None:
        return result
    
    # 2. Try enhanced multi-scale method
    result = detect_yellow_region_enhanced(image, x_start, x_end, ys, ye)
    if result is not None:
        return result
    
    # 3. Try Hough line detection specifically for water lines
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    
    # Detect horizontal lines
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=30, 
                            minLineLength=40, maxLineGap=15)
    
    if lines is not None:
        candidates = []
        # (N, 4) on OpenCV 5, (N, 1, 4) on 4 -- reshape takes either.
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            
            # Check if line is in ROI and roughly horizontal
            mid_x = int((x1 + x2) / 2)
            mid_y = int((y1 + y2) / 2)
            
            if x_start <= mid_x <= x_end and max(50, ys) <= mid_y <= min(image.shape[0] - 50, ye):
                # Check if there's yellow content near this line
                line_y = max(y1, y2)
                roi_patch = image[max(0, line_y-15):min(image.shape[0], line_y+15), 
                                x_start:x_end]
                
                if roi_patch.size > 0:
                    hsv_patch = cv2.cvtColor(roi_patch, cv2.COLOR_BGR2HSV)
                    yellow_mask = cv2.inRange(hsv_patch, np.array([15, 100, 100]), 
                                            np.array([40, 255, 255]))
                    yellow_ratio = np.sum(yellow_mask > 0) / max(1, roi_patch.size)
                    
                    if yellow_ratio > 0.1:
                        candidates.append((line_y, yellow_ratio))
        
        if candidates:
            # Return the lowest line with highest yellow content
            candidates.sort(key=lambda x: (-x[1], -x[0]))  # Yellow ratio desc, position asc
            return candidates[0][0]
    
    return None

def _smooth(values, window=5):
    return np.convolve(values, np.ones(window) / window, mode="same")


def _yellow_column(image, x_start, x_end, y_start=0, y_end=None):
    """Locate the staff by its yellow paint, and say which of its rows are lit."""
    ys, ye = _roi_rows(image, y_start, y_end)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([20, 140, 120]), np.array([30, 255, 255]))
    mask[:, :x_start] = 0
    mask[:, x_end:] = 0
    mask[:ys] = 0
    mask[ye:] = 0

    ys, xs = np.nonzero(mask)
    if len(xs) < 50:
        return None

    left, right = int(np.percentile(xs, 5)), int(np.percentile(xs, 95))
    if right - left < 6:
        left, right = max(0, left - 4), right + 4
    return left, right, int(ys.min()), int(np.percentile(ys, 90))


def _loose_yellow_staff(image, x_start, x_end, y_start=0, y_end=None):
    """
    Is there a yellow staff in the box, even one too pale to measure?

    Heavy rain drops the paint's saturation below the reading mask -- p50 79 on
    one frame, against 226 when dry -- so the strict pass finds nothing and the
    colour-blind fallback would take over and measure the platform behind the
    staff instead. This pass only has to separate painted rows from incidental
    yellow, which shape does: the fraction of rows carrying at least eight
    yellow pixels measured 0.96 for a washed-out staff and 0.42-0.57 for wet
    ones, against 0.00 for the box around a white/red staff and none at all
    for a night frame.
    """
    ys, ye = _roi_rows(image, y_start, y_end)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([20, 40, 120]), np.array([30, 255, 255]))
    mask[:, :x_start] = 0
    mask[:, x_end:] = 0
    mask[:ys] = 0
    mask[ye:] = 0

    rows, yy = (mask > 0).sum(axis=1), np.nonzero(mask)[0]
    if len(yy) == 0:
        return False
    y0, y1 = int(yy.min()), int(yy.max())
    return float((rows[y0:y1 + 1] >= 8).mean()) >= 0.25


def _banding_column(image, x_start, x_end, y_start=0, y_end=None):
    """
    Locate the staff without relying on colour.

    Not every gauge is yellow -- one of these rivers is measured against a red
    and white staff -- but they all carry graduation bands, so the staff is the
    column with the most horizontal edges, and it is the longest unbroken run of
    them: masonry above the staff is textured too, but only in patches.
    """
    ys, ye = _roi_rows(image, y_start, y_end)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(float)
    band = gray[ys:ye, x_start:x_end + 1]
    if band.shape[0] < 2:
        return None

    edges = _smooth(np.percentile(np.abs(np.diff(band, axis=0)), 90, axis=0), 3)
    if edges.max() <= 0:
        return None

    cols = np.nonzero(edges >= edges.max() * 0.5)[0]
    if len(cols) == 0:
        return None

    runs = []
    start = prev = cols[0]
    for c in cols[1:]:
        if c - prev > 2:
            runs.append((start, prev))
            start = c
        prev = c
    runs.append((start, prev))
    a, b = max(runs, key=lambda r: r[1] - r[0])
    left, right = x_start + a, x_start + b

    # Rows of that column, still inside the region, so the lit-run search below
    # cannot walk back out of it.
    strip = gray[ys:ye, left - x_start:right - x_start + 1]
    contrast = _smooth(strip.max(axis=1) - strip.min(axis=1))
    lit = np.nonzero(contrast >= contrast.max() * 0.45)[0] + ys
    if len(lit) < 20:
        return None

    runs = []
    start = prev = lit[0]
    for y in lit[1:]:
        if y - prev > 12:
            runs.append((start, prev))
            start = y
        prev = y
    runs.append((start, prev))
    top, bottom = max(runs, key=lambda r: r[1] - r[0])
    if bottom - top < 40:
        return None
    return left, right, int(top), int(top + (bottom - top) * 0.6)


def _gauge_mask(image, left, right):
    """Rows of the painted staff and nothing else: yellow hue, saturated past
    the murk, and bright.

    Brightness is the part that survives weather. On one Rangsit pair the staff
    read S p50 226 on a clear frame and 79 in heavy rain -- washed clean out of
    any saturation cut that would still exclude the reflection -- while staying
    at V p50 254 against the reflection's 144. Hue and saturation alone hold
    only when the light does.
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([18, 70, 200]), np.array([35, 255, 255]))
    return mask[:, left:right + 1]


def _mask_ends(mask, waterline, top, end, lookback=40, lookahead=40,
               min_above=20, max_below=80):
    """Whether the staff's colour stops at `waterline` instead of carrying on.

    The reflection never passes the mask (S p50 84-85 against the staff's 226)
    and neither does silted submerged paint, so above a true surface the staff
    is yellow and below it there is none. A dip mid-staff has yellow continuing
    underneath it, which is what tells a graduation band from the waterline.
    False means the caller should keep walking rather than settle.
    """
    above = int((mask[max(top, waterline - lookback):waterline + 1] > 0).sum())
    if above < min_above:
        return False          # not standing on the gauge at all
    under = int((mask[waterline + 1:min(end, waterline + 1 + lookahead)] > 0).sum())
    return under <= max_below


def _mask_bottom(mask, top, end, coverage=0.15, sustain=4, window=12):
    """The deepest row where the staff's own paint still covers the column.

    Where the water is still enough to mirror the staff, the reflection carries
    the graduation bands with it, so it is as textured as the staff and the
    contrast scan walks straight into it. The paint does not go with it.
    Measured down the Rangsit staff, the mask holds 70% of the column width on
    the staff against 4% on the reflection immediately beneath it, so a coverage
    floor separates the two without a brightness cut that would move with the
    light. The run requirement keeps a few specular pixels on a wave crest from
    reading as staff: in direct sun the reflection's first rows passed a bare
    three-pixel test and put the bottom 30px low.
    """
    rows = (mask > 0).sum(axis=1)
    need = max(4, int(round(mask.shape[1] * coverage)))
    bottom = None
    for y in range(top, min(end, len(rows))):
        if rows[y] < need:
            continue
        if int((rows[max(top, y - window + 1):y + 1] >= need).sum()) >= sustain:
            bottom = y
    return bottom


def detect_water_level_on_gauge(image, x_start=225, x_end=390, y_start=0, y_end=None,
                                frames=None, texture_drop=0.5, sustain_rows=9,
                                motion_ratio=3.0, meta=None):
    """
    Find the waterline by where the gauge staff stops looking like a gauge staff.

    Colour alone is not enough. The lower staff is wet and silted, so its yellow
    fades below any threshold tight enough to exclude the water -- which shares
    the staff's hue and differs only in saturation -- and the reading comes out
    10-15cm high. Loosening the threshold instead walks the mask down the staff's
    own reflection to the bottom of the frame.

    What separates the two cleanly is texture: the staff carries black graduation
    bands, so each of its rows spans a wide range of brightness, while water is
    smooth. That range collapses at the waterline -- measured at 121 to 47 on one
    frame and 68 to 42 on another, both exactly at the surface -- and is read
    relative to the staff's own rows, so it holds as the light changes.

    Where the water is still enough to mirror the staff the texture does not
    collapse at all: the reflection carries the graduation bands down with it and
    measured 102-128 against the staff's 127 right across the surface, so no
    threshold separates them. Paint does not reflect, though. The bright-yellow
    mask covers 70% of the column on the staff and 4% on the water beneath it, so
    the row where that coverage stops is the surface, and it is read without any
    threshold that moves with the light. The scan is bounded by it and falls back
    to it, which is what `_mask_bottom` is for.

    Motion is the other tell, where the camera will give it: the staff is static,
    so its rows vary by 0 across frames of one segment while rippling water below
    varies by 9. Pass `frames` and `_correct_for_reflection` uses it. It is inert
    on the muddy rivers that never reflect -- and on any camera that serves the
    same still repeatedly, which is why the paint edge and not motion is what
    Rangsit rests on.

    Colour then returns as a check rather than as the answer: the reading only
    stands where the staff's own bright yellow stops. Texture alone has two
    ways to be wrong in the same direction. A wide black graduation band
    collapses it just as water does, and in rain the water keeps its texture so
    the scan runs straight past the real surface -- measured at y501 and y918
    against a true line of y646. Both sit where the staff is no longer yellow,
    so both are rejected and the station reports nothing rather than something
    plausible and wrong. A column found without any yellow (a white/red staff,
    a night frame in infrared) has no colour to check against and is left as
    it was; `meta["mode"]` records which of the two ran.
    """
    ys, ye = _roi_rows(image, y_start, y_end)
    yellow = _yellow_column(image, x_start, x_end, ys, ye)
    if yellow is None and _loose_yellow_staff(image, x_start, x_end, ys, ye):
        # The staff is in the box but washed out of the reading mask -- heavy
        # rain took its saturation to p50 79 on a frame where it stayed bright.
        # Texture alone would then measure whatever else shares the box: it
        # reported 862 px with the surface at 646. Decline instead, and say it
        # was the colour check that declined.
        if meta is not None:
            meta["mode"] = "gauge"
        return None
    column = yellow or _banding_column(image, x_start, x_end, ys, ye)
    if column is None:
        if meta is not None:
            meta["mode"] = "none"
        return None
    left, right, top, ref_end = column
    if meta is not None:
        # "gauge" found the painted staff and can be checked against its colour;
        # "blind" found no yellow and is measuring texture alone.
        meta["mode"] = "gauge" if yellow else "blind"

    # The staff's own bright yellow, for checking where the reading lands.
    # Skipped for a colourless column: there is nothing to check it with.
    mask = _gauge_mask(image, left, right) if yellow else None

    strip = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(float)[:, left:right + 1]
    contrast = _smooth(strip.max(axis=1) - strip.min(axis=1))

    reference = np.median(contrast[top:max(top + 40, ref_end)])
    if reference <= 0:
        return None

    threshold = reference * texture_drop
    waterline = None
    below = 0
    collapsed = False
    # Where the paint gives out. The contrast scan is allowed a little way past
    # it, because submerged paint silts over and the colour can stop high -- the
    # 10-15cm that first motivated the texture scan is 22px at Pathumthani -- but
    # not far enough to reach a reflection: at Rangsit the scan's first candidate
    # row sat 137px below the paint. Taken as a fraction of the staff's own
    # length those are 0.06 and 0.47, so the budget comes from that rather than
    # from a pixel count, which would mean different things at 480 and 1080 lines.
    bottom = _mask_bottom(mask, top, ye) if mask is not None else None
    limit = ye if bottom is None else min(ye, bottom + int((bottom - top) * 0.15) + 1)

    for y in range(top, min(len(contrast), limit)):
        if contrast[y] >= threshold:
            waterline = y
            below = 0
        else:
            below += 1
            # A few dim rows are just a wide black band; a sustained run is water.
            if waterline is not None and below >= sustain_rows:
                if mask is None or _mask_ends(mask, waterline, top, ye):
                    collapsed = True
                    break
                # Gauge-yellow is still there under this dip: the staff carries
                # on, so the dip was a graduation band or textured water, not
                # the surface. Walk past it.
                below = 0

    # The scan finding no collapse is not the same as there being no waterline.
    # On still water there is nothing for it to find: the reflection's contrast
    # measured 102-128 against the staff's 127 right across the surface. The
    # paint stopping is itself the observation -- it is the row where the staff
    # enters the water -- and it held to within 3px across 19 Rangsit frames
    # while the scan was reporting a spread of 276px, 0.8m of river that never
    # moved. Without a painted column there is nothing to fall back on, and a
    # dark frame or a gauge out of shot still reports nothing.
    if not collapsed:
        return bottom

    # One frame, or the same frame several times over, carries no motion to
    # measure; the correction would read stillness everywhere and stand aside.
    if waterline is None or not frames or len(frames) < 2:
        return waterline

    return _correct_for_reflection(frames, left, right, top, waterline, motion_ratio)


def _correct_for_reflection(frames, left, right, top, waterline, motion_ratio):
    """Pull the waterline back up out of a mirror image, using the fact that only
    the water moves between frames."""
    stack = np.stack([
        cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(float)[:, left:right + 1] for f in frames
    ])
    motion = _smooth(stack.std(axis=0).mean(axis=1))

    static = np.median(motion[top:max(top + 40, waterline - 150)])
    low, high = max(0, waterline - 80), min(len(motion), waterline + 80)
    moving = np.median(motion[low:high])

    if moving <= max(static, 0.05) * motion_ratio:
        return waterline   # nothing is rippling; there is no reflection to undo

    cut = (static + moving) / 2
    run = 0
    for y in range(top, waterline + 1):
        if motion[y] > cut:
            run += 1
            if run >= 6:
                return max(top, y - 5)
        else:
            run = 0
    return waterline


def detect_yellow_region_fused(image, x_start=225, x_end=390, y_start=0, y_end=None,
                               meta=None):
    """Enhanced yellow detection with specialized water level detection."""
    ys, ye = _roi_rows(image, y_start, y_end)

    # First try specialized water level detection on gauge
    result = detect_water_level_on_gauge(image, x_start, x_end, y_start=ys, y_end=ye,
                                         meta=meta)
    if result is not None:
        print(f"Water level detected on gauge: {result}")
        return result
    
    # Fallback to cascade method
    result = detect_yellow_regions_fused(image, x_start, x_end, ys, ye)
    if result is not None:
        return result
    
    h = image.shape[0]
    w = image.shape[1]
    
    # FALLBACK: Original fused method
    # 1. COLOR DETECTION (existing method)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    # More refined yellow range for better accuracy
    lower_yellow = np.array([18, 80, 80])
    upper_yellow = np.array([90, 255, 255])
    
    color_mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
    color_mask[:, :x_start] = 0
    color_mask[:, x_end:] = 0
    color_mask[:ys] = 0
    color_mask[ye:] = 0
    
    # Morphological operations to clean up the mask
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    color_mask = cv2.morphologyEx(color_mask, cv2.MORPH_CLOSE, kernel)
    color_mask = cv2.morphologyEx(color_mask, cv2.MORPH_OPEN, kernel)
    
    # 2. EDGE DETECTION for horizontal water lines
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    
    # Apply Gaussian blur to reduce noise
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    
    # Detect horizontal edges (water level indicators)
    sobel_x = cv2.Sobel(blurred, cv2.CV_64F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(blurred, cv2.CV_64F, 0, 1, ksize=3)
    
    # Focus on horizontal edges (stronger sobel_y response)
    edge_strength = np.abs(sobel_y) - 0.5 * np.abs(sobel_x)
    edge_strength = np.maximum(edge_strength, 0)
    
    # Threshold for strong edges
    edge_threshold = np.percentile(edge_strength, 85)  # Top 15% strongest edges
    edge_mask = (edge_strength > edge_threshold).astype(np.uint8) * 255
    
    # Restrict edge detection to ROI
    edge_mask[:, :x_start] = 0
    edge_mask[:, x_end:] = 0
    edge_mask[:ys] = 0
    edge_mask[ye:] = 0
    
    # 3. FUSION: Combine color and edge detection
    # Weighted combination
    fused_mask = cv2.addWeighted(color_mask, 0.7, edge_mask, 0.3, 0)
    
    # Apply final morphological operations
    kernel_final = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fused_mask = cv2.morphologyEx(fused_mask, cv2.MORPH_CLOSE, kernel_final)
    
    # Find contours in fused mask
    contours, _ = cv2.findContours(fused_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if contours:
        # Filter contours by area and aspect ratio (water level indicators)
        valid_contours = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > 100:  # Minimum area threshold
                x, y, w_rect, h_rect = cv2.boundingRect(cnt)
                aspect_ratio = w_rect / max(h_rect, 1)
                if aspect_ratio > 1.5:  # Prefer horizontal features
                    valid_contours.append((cnt, y + h_rect, area))
        
        if valid_contours:
            # Select the lowest (highest y-coordinate) weighted by area
            best_contour = max(valid_contours, key=lambda item: item[1] + item[2] // 1000)
            return best_contour[1]
        else:
            # Fallback: just find the lowest contour
            lowest_contour = max(contours, key=lambda cnt: cv2.boundingRect(cnt)[1])
            _, y, _, _ = cv2.boundingRect(lowest_contour)
            return y
    
    return None

def visualize_detection_debug(image, x_start=225, x_end=390, y_start=0, y_end=None):
    """Create debug visualization showing color mask, edge mask, and fused result."""
    h = image.shape[0]
    w = image.shape[1]
    ys, ye = _roi_rows(image, y_start, y_end)
    
    # Color detection
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_yellow = np.array([18, 80, 80])
    upper_yellow = np.array([90, 255, 255])
    color_mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
    color_mask[:, :x_start] = 0
    color_mask[:, x_end:] = 0
    color_mask[:ys] = 0
    color_mask[ye:] = 0
    
    # Edge detection
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    sobel_x = cv2.Sobel(blurred, cv2.CV_64F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(blurred, cv2.CV_64F, 0, 1, ksize=3)
    edge_strength = np.abs(sobel_y) - 0.5 * np.abs(sobel_x)
    edge_strength = np.maximum(edge_strength, 0)
    edge_threshold = np.percentile(edge_strength, 85)
    edge_mask = (edge_strength > edge_threshold).astype(np.uint8) * 255
    edge_mask[:, :x_start] = 0
    edge_mask[:, x_end:] = 0
    edge_mask[:ys] = 0
    edge_mask[ye:] = 0
    
    # Fusion
    fused_mask = cv2.addWeighted(color_mask, 0.7, edge_mask, 0.3, 0)
    
    # Create debug image with 4 panels
    debug_h = h * 2
    debug_w = w * 2
    debug_image = np.zeros((debug_h, debug_w, 3), dtype=np.uint8)
    
    # Panel 1: Original with ROI
    roi_image = image.copy()
    cv2.rectangle(roi_image, (x_start, ys), (x_end, ye), (0, 255, 0), 2)
    debug_image[0:h, 0:w] = roi_image
    
    # Panel 2: Color mask
    debug_image[0:h, w:debug_w] = cv2.cvtColor(color_mask, cv2.COLOR_GRAY2BGR)
    
    # Panel 3: Edge mask  
    debug_image[h:debug_h, 0:w] = cv2.cvtColor(edge_mask, cv2.COLOR_GRAY2BGR)
    
    # Panel 4: Fused mask
    debug_image[h:debug_h, w:debug_w] = cv2.cvtColor(fused_mask, cv2.COLOR_GRAY2BGR)
    
    return debug_image

def visualize_water_level_debug(image, x_start=225, x_end=390, y_start=0, y_end=None):
    """Create detailed visualization of water level detection process."""
    ys, ye = _roi_rows(image, y_start, y_end)

    # Run water level detection
    detected_y = detect_water_level_on_gauge(image, x_start, x_end, y_start=ys, y_end=ye)
    
    # Create visualization image
    debug_img = image.copy()
    
    # Draw ROI boundary
    cv2.rectangle(debug_img, (x_start, ys), (x_end, ye), (0, 255, 0), 2)
    
    if detected_y is not None:
        # Draw detected water level
        cv2.line(debug_img, (x_start, detected_y), (x_end, detected_y), (0, 0, 255), 3)
        cv2.putText(debug_img, f"Water Level: {detected_y}", (x_start, detected_y - 10), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    
    # Try to show gauge detection
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_gauge = np.array([20, 140, 120])
    upper_gauge = np.array([30, 255, 255])
    gauge_mask = cv2.inRange(hsv, lower_gauge, upper_gauge)
    
    # Find gauge contour
    contours, _ = cv2.findContours(gauge_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        gauge_contour = max(contours, key=cv2.contourArea)
        cv2.drawContours(debug_img, [gauge_contour], -1, (255, 0, 255), 2)
        
        # Draw gauge bounding rectangle
        x_gauge, y_gauge_top, w_gauge, h_gauge = cv2.boundingRect(gauge_contour)
        cv2.rectangle(debug_img, (x_gauge, y_gauge_top), 
                     (x_gauge + w_gauge, y_gauge_top + h_gauge), (255, 255, 0), 1)
    
    return debug_img

def detect_yellow_region(image, x_start=225, x_end=390):
    """Detect the lowest yellow region within the specified x-axis range."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_yellow = np.array([20, 100, 100])
    upper_yellow = np.array([90, 255, 255])
    mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
    mask[:, :x_start] = 0
    mask[:, x_end:] = 0
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if contours:
        lowest_contour = max(contours, key=lambda cnt: cv2.boundingRect(cnt)[1])  # Max y-coordinate
        _, y, _, _ = cv2.boundingRect(lowest_contour)
        return y
    return None

def draw_reference_ticks(station, image, y_detected):
    """
    Draw reference level ticks every 0.1 m using the calibrated inverse mapping.
    Green for ticks above (<= y_detected), red for below.
    """
    # Choose a reasonable display range based on calibration
    (_, lvl_max), (_, lvl_min) = get_bounds(station)
    lvl_top = max(4.2, lvl_max)   # a bit above
    lvl_bot = min(2.3, lvl_min)   # a bit below
    lvl = lvl_top
    while lvl >= lvl_bot:
        y = int(level_to_pixel(station, lvl))
        color = (0, 255, 0) if y <= (y_detected or 10**9) else (0, 0, 255)
        cv2.line(image, (0, y), (image.shape[1], y), color, 1)
        cv2.putText(image, f"{lvl:.1f}m", (image.shape[1]-150, max(15, y-5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        lvl -= 0.1

def fetch_snapshot(station):
    """One JPEG from a still-image camera endpoint."""
    response = requests.get(station.snapshot_url, auth=station.auth, timeout=15)
    response.raise_for_status()
    frame = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        print(f"[{station.id}] snapshot did not decode as an image")
    return frame


def capture_snapshots(station, count=6, spacing=0.4):
    """
    Several stills in quick succession, with repeats dropped.

    A snapshot endpoint hands back one frame per request, and telling moving
    water from a reflection of the staff needs frames that are actually a moment
    apart. Not every endpoint has one to give: Rangsit's refreshes its still once
    every 62 seconds, so six requests 0.4s apart came back as six byte-identical
    JPEGs. Handing those on as a segment is worse than handing on nothing,
    because the reflection test reads no motion anywhere and concludes the water
    is still. Returning only what actually differs keeps that test honest, and
    the detector requires two frames before it runs.
    """
    frames = []
    repeats = 0
    for i in range(count):
        if i:
            time.sleep(spacing)
        try:
            frame = fetch_snapshot(station)
        except requests.RequestException as e:
            print(f"[{station.id}] snapshot request failed: {e}")
            break
        if frame is None:
            break
        if frames and np.array_equal(frames[-1], frame):
            repeats += 1
            # Asking a camera that is plainly not refreshing costs it requests
            # and the tracker seconds: six of these took 14s against Rangsit.
            if repeats >= 2:
                break
            continue
        repeats = 0
        frames.append(frame)
    return frames


def capture_station_frame(station):
    """A single current frame, whichever way this station's camera is reached."""
    if station.is_snapshot:
        return fetch_snapshot(station)

    video_url = get_video_url(station)
    if not video_url:
        return None
    return capture_last_frame_from_video(video_url)


def capture_station_frames(station, count=10):
    """Frames for one station, whichever way its camera is reached."""
    if station.is_snapshot:
        return capture_snapshots(station, count=min(count, 6))

    video_url = get_video_url(station)
    if not video_url:
        return []
    return capture_frames_from_video(video_url, count)


def capture_frames_from_video(video_url, count=10):
    """A handful of frames spread across the segment, for telling water from a
    reflection. Returns [] rather than raising if the segment will not open."""
    cap = cv2.VideoCapture(video_url)
    if not cap.isOpened():
        return []

    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()

    if len(frames) <= count:
        return frames
    step = len(frames) // count
    return frames[::step][:count]


def capture_last_frame_from_video(video_url):
    """Capture the last frame from the video segment with robust error handling."""
    cap = cv2.VideoCapture(video_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print(f"Failed to open video: {video_url}")
        return None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])

    print("Video Properties:")
    print(f"  Size: {width}x{height} pixels")
    print(f"  Framerate: {fps:.2f} FPS")
    print(f"  Total Frames: {total_frames}")
    print(f"  Duration: {duration:.2f} seconds")
    print(f"  Codec: {codec}")
    print(f"  URL: {video_url}")
    print("-" * 50)

    frame = None
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total_frames - 1))
        ret, frame = cap.read()
        if ret and frame is not None:
            cap.release()
            return frame
        for offset in [2, 5, 10]:
            if total_frames > offset:
                cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - offset)
                ret, frame = cap.read()
                if ret and frame is not None:
                    cap.release()
                    return frame

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    for _ in range(10):
        ret, frame = cap.read()
        if ret and frame is not None:
            for __ in range(3):
                cap.read()
            ret, frame = cap.read()
            if ret and frame is not None:
                cap.release()
                return frame

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ret, frame = cap.read()
    if ret and frame is not None:
        cap.release()
        return frame

    print(f"Failed to capture any valid frame from video segment: {video_url}")
    cap.release()
    return None

def store_frame(station, image, prefix="", postfix=""):
    """Upload a frame to the station's area of object storage. Returns its public URL."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    key = f"{station.capture_prefix}{prefix}_{timestamp}{postfix}.jpg"
    return storage.upload_frame(image, key)

def rotate_images(station):
    """Delete old captures for one station, keeping the most recent MAX_KEEP."""
    try:
        storage.prune_captures(station.capture_prefix, MAX_KEEP)
    except Exception as e:
        print(f"Error pruning captures for {station.id}: {e}")

def generate_water_level_line_image(station, original_image, y_lowest_yellow, water_level):
    """Generate an image that shows only the water level line matching the detected level."""
    water_level_image = original_image.copy()
    if water_level is not None:
        y = int(level_to_pixel(station, water_level))
        line_color = (0, 255, 0)
        cv2.line(water_level_image, (0, y), (water_level_image.shape[1], y), line_color, 2)
        cv2.putText(water_level_image, f"{water_level:.2f}m",
                    (water_level_image.shape[1]-200, max(15, y-5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, line_color, 2)
    return water_level_image

def require_calibration(station):
    if len(station.cal_points) < 2:
        abort(409, description=f"Station '{station.id}' has not been calibrated yet.")


def resolve_station(station_id):
    """A station from the URL. Unknown ids are a 404 rather than a silent fallback,
    so a mistyped link cannot quietly show another river's readings."""
    if station_id is None:
        return stations.get(None)
    if not stations.exists(station_id):
        abort(404, description=f"Unknown station '{station_id}'")
    return stations.get(station_id)

@app.route('/api/stations', methods=['GET'])
def get_stations():
    return jsonify({
        "default": stations.default_id(),
        "stations": [st.public() for st in stations.all_stations()],
    })

@app.route('/status', methods=['GET'])
@app.route('/status/<station_id>', methods=['GET'])
@cache.cached(timeout=CACHE_TTL, key_prefix=cache_key)
def get_status(station_id=None):
    """Endpoint to get the water level and return image URLs."""
    station = resolve_station(station_id)
    require_calibration(station)

    if station.is_snapshot:
        original_frame = fetch_snapshot(station)
    else:
        video_segments = get_video_segments(station)
        if not video_segments:
            return jsonify({"error": "Failed to retrieve video segments from HLS playlist."}), 500

        original_frame = None
        for video_url in reversed(video_segments):  # Try most recent first
            print(f"Trying video URL: {video_url}")
            original_frame = capture_last_frame_from_video(video_url)
            if original_frame is not None:
                print(f"Successfully captured frame from: {video_url}")
                break
            print(f"Failed to capture frame from: {video_url}")

    if original_frame is None:
        return jsonify({"error": "Failed to capture a frame"}), 500

    # Align frame to handle camera movement
    with station.lock:
        aligned_frame = align_image(station, original_frame)

        # Perform enhanced detection with smoothing
        meta = {}
        raw_detection = detect_water_level_on_gauge(
            aligned_frame, station.x_start, station.x_end,
            y_start=station.y_start, y_end=station.y_end, meta=meta)
        # The same detector the tracker uses: "gauge" (colour + texture) or
        # "blind" (texture only), and nothing when it declines to answer. The
        # cascade that used to sit behind this endpoint guessed -- 885 px with
        # the surface at 646 -- so it stays in /debug, where guesses are the
        # point.
        station.detection_mode = (meta.get("mode") if raw_detection is not None
                                  else "none")
        y_lowest_yellow = smooth_detection(station, raw_detection)

    if y_lowest_yellow is None:
        water_level = station.previous_level
        print("Yellow region not detected, using previous water level:", water_level)

        original_image_url = store_frame(station, aligned_frame, "water_level_image", "_original")

        # Clean up old captures to maintain MAX_KEEP limit
        rotate_images(station)

        unix_timestamp = int(datetime.now().timestamp())

        return jsonify({
            "water_level": water_level,
            "original_image_url": original_image_url,
            "processed_image_url": None,
            "water_level_line_image_url": None,
            "timestamp": unix_timestamp,
            "detection_mode": station.detection_mode,
            "note": "Yellow region not detected, using previous water level"
        })

    # Compute level using calibrated mapping
    water_level = float(pixel_to_level(station, float(y_lowest_yellow)))
    station.previous_level = water_level

    processed_frame = aligned_frame.copy()
    # Draw reference ticks for context
    draw_reference_ticks(station, processed_frame, y_lowest_yellow)

    processed_image_url = store_frame(station, processed_frame, "water_level_image", "_processed")
    original_image_url = store_frame(station, aligned_frame, "water_level_image", "_original")
    water_level_line_image = generate_water_level_line_image(station, aligned_frame, y_lowest_yellow, water_level)
    water_level_line_image_url = store_frame(station, water_level_line_image, "water_level_image", "_level_lines")

    # Clean up old captures to maintain MAX_KEEP limit
    rotate_images(station)

    unix_timestamp = int(datetime.now().timestamp())

    return jsonify({
        "water_level": water_level,
        "original_image_url": original_image_url,
        "processed_image_url": processed_image_url,
        "water_level_line_image_url": water_level_line_image_url,
        "timestamp": unix_timestamp,
        "detection_mode": station.detection_mode,
        "calibration_points": station.cal_points
    })

@app.route('/debug', methods=['GET'])
@app.route('/debug/<station_id>', methods=['GET'])
def debug_detection(station_id=None):
    """Debug endpoint to visualize color + edge fusion detection."""
    station = resolve_station(station_id)
    try:
        original_frame = capture_station_frame(station)
        if original_frame is None:
            return jsonify({"error": "Failed to capture frame"}), 500
            
        aligned_frame = align_image(station, original_frame)
        
        # Create debug visualization
        debug_image = visualize_detection_debug(
            aligned_frame, station.x_start, station.x_end,
            station.y_start, station.y_end)
        
        # Run detection
        detected_y = detect_yellow_region_fused(
            aligned_frame, station.x_start, station.x_end,
            station.y_start, station.y_end)
        
        # Draw detection result on debug image
        if detected_y is not None:
            h = aligned_frame.shape[0]
            cv2.line(debug_image, (0, detected_y), (debug_image.shape[1], detected_y), (0, 0, 255), 3)
            cv2.putText(debug_image, f"Detected Y: {detected_y}", (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        # Save debug image
        debug_image_url = store_frame(station, debug_image, "detection_debug")

        return jsonify({
            "debug_image_url": debug_image_url,
            "detected_y": detected_y,
            "detection_success": detected_y is not None
        })
        
    except Exception as e:
        return jsonify({"error": f"Debug failed: {str(e)}"}), 500

@app.route('/debug-water', methods=['GET'])
@app.route('/debug-water/<station_id>', methods=['GET'])
def debug_water_level(station_id=None):
    """Specialized debug endpoint for water level detection only."""
    station = resolve_station(station_id)
    require_calibration(station)
    try:
        original_frame = capture_station_frame(station)
        if original_frame is None:
            return jsonify({"error": "Failed to capture frame"}), 500
            
        aligned_frame = align_image(station, original_frame)
        
        # Run water level detection
        meta = {}
        detected_y = detect_water_level_on_gauge(
            aligned_frame, station.x_start, station.x_end,
            y_start=station.y_start, y_end=station.y_end, meta=meta)
        station.detection_mode = (meta.get("mode") if detected_y is not None
                                  else "none")
        
        # Create water level debug visualization
        debug_image = visualize_water_level_debug(
            aligned_frame, station.x_start, station.x_end,
            station.y_start, station.y_end)
        
        # Save debug image
        water_level_debug_url = store_frame(station, debug_image, "water_level_debug")

        water_level = None
        
        if detected_y is not None:
            water_level = float(pixel_to_level(station, float(detected_y)))
        
        return jsonify({
            "water_level_debug_url": water_level_debug_url,
            "detected_y_pixel": detected_y,
            "water_level_meters": water_level,
            "detection_success": detected_y is not None,
            "detection_mode": meta.get("mode"),
            "calibration_points": station.cal_points
        })
        
    except Exception as e:
        return jsonify({"error": f"Water level debug failed: {str(e)}"}), 500

@app.route('/calibrate', methods=['GET'])
@app.route('/calibrate/<station_id>', methods=['GET'])
def calibrate_ui(station_id=None):
    """Serve the calibration UI page."""
    return render_template('calibrate.html', station_id=resolve_station(station_id).id)

@app.route('/api/calibration', methods=['GET', 'POST'])
@app.route('/api/<station_id>/calibration', methods=['GET', 'POST'])
def handle_calibration(station_id=None):
    """Get or update calibration points."""
    station = resolve_station(station_id)

    if request.method == 'GET':
        return jsonify({"points": station.cal_points})
        
    elif request.method == 'POST':
        data = request.json
        if not data or 'points' not in data:
            return jsonify({"error": "Invalid payload"}), 400
            
        points = data['points']
        # Validate format
        if not isinstance(points, list) or not all(isinstance(p, list) and len(p) == 2 for p in points):
            return jsonify({"error": "Points must be a list of [pixel, level] pairs"}), 400
            
        if save_calibration_to_file(station, points):
            load_calibration(station)
            return jsonify({"success": True, "points": station.cal_points})
        else:
            return jsonify({"error": "Failed to save calibration"}), 500

@app.route('/api/markers', methods=['GET', 'POST'])
@app.route('/api/<station_id>/markers', methods=['GET', 'POST'])
def handle_markers(station_id=None):
    """Reference lines drawn across the trend chart: warning levels and past floods."""
    station = resolve_station(station_id)

    if request.method == 'GET':
        return jsonify({"markers": database.load_markers(station.id)})

    data = request.json or {}
    if not isinstance(data.get("markers"), list):
        return jsonify({"error": "markers must be a list"}), 400

    cleaned = []
    for raw in data["markers"]:
        if not isinstance(raw, dict):
            return jsonify({"error": "each marker must be an object"}), 400

        label = str(raw.get("label", "")).strip()
        if not label:
            return jsonify({"error": "every marker needs a label"}), 400

        try:
            level = round(float(raw["level"]), 2)
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": f"marker '{label}' needs a numeric level"}), 400

        color = raw.get("color", "grey")
        if color not in database.MARKER_COLORS:
            return jsonify({
                "error": f"marker '{label}' has an unknown colour; use one of "
                         + ", ".join(database.MARKER_COLORS)
            }), 400

        cleaned.append({"label": label[:40], "level": level, "color": color})

    cleaned.sort(key=lambda m: m["level"], reverse=True)
    database.save_markers(station.id, cleaned)
    return jsonify({"success": True, "markers": cleaned})

@app.route('/api/roi', methods=['GET', 'POST'])
@app.route('/api/<station_id>/roi', methods=['GET', 'POST'])
def handle_roi(station_id=None):
    """The rectangle the detector looks at: read it, move it, or hand it back to
    stations.json. Saved to MongoDB so no commit is needed to aim a camera."""
    station = resolve_station(station_id)

    if request.method == 'GET':
        return jsonify({
            "roi": station.roi,
            "config": station.config_roi,
            "source": station.roi_source,
        })

    data = request.json or {}

    if data.get("reset"):
        # Validate before touching anything: the rectangle is the committed one,
        # but a stations.json edited to nonsense should not clear a working one.
        try:
            candidate = Station.validate_roi(station.config_roi)
        except ValueError as e:
            return jsonify({"error": f"stations.json region is unusable: {e}"}), 409

        try:
            database.clear_roi(station.id)
        except Exception as e:
            print(f"Error clearing ROI for {station.id}: {e}")
            return jsonify({"error": "Failed to clear the saved region"}), 500

        with station.lock:
            station.set_roi(candidate, source="config")
        return jsonify({"roi": station.roi, "source": "config"})

    # Validate first, then persist, then apply: a failed write must not leave this
    # replica seeing a region the others will overwrite at their next check.
    try:
        candidate = Station.validate_roi(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        database.save_roi(station.id, candidate)
    except Exception as e:
        print(f"Error saving ROI for {station.id}: {e}")
        return jsonify({"error": "Failed to save the region"}), 500

    with station.lock:
        station.set_roi(candidate, source="database")
    return jsonify({"roi": station.roi, "source": "database"})

@app.route('/api/calibration/frame', methods=['GET'])
@app.route('/api/<station_id>/calibration/frame', methods=['GET'])
def get_calibration_frame(station_id=None):
    """Fetch the latest video frame, set it as homography reference, and return its URL."""
    station = resolve_station(station_id)
    try:
        original_frame = capture_station_frame(station)
        if original_frame is None:
            return jsonify({"error": "Failed to capture frame"}), 500
            
        # Set this new frame as the reference for homography -- skipped when the
        # flag has it off: there is no baseline to keep, and turning it back on
        # later adopts the next frame on its own.
        if HOMOGRAPHY_ENABLED:
            with station.lock:
                set_reference_frame(station, original_frame)

        # Save frame to return to UI
        image_url = store_frame(station, original_frame, "calibration_frame")

        return jsonify({
            "image_url": image_url
        })
    except Exception as e:
        return jsonify({"error": f"Failed to get calibration frame: {str(e)}"}), 500

@app.route('/')
@app.route('/s/<station_id>')
def dashboard(station_id=None):
    """Serve the main dashboard."""
    return render_template('index.html', station_id=resolve_station(station_id).id)

@app.route('/api/recent', methods=['GET'])
@app.route('/api/<station_id>/recent', methods=['GET'])
def get_recent(station_id=None):
    """Raw recent readings, for the current value and the capture strip."""
    return jsonify(database.latest_readings(resolve_station(station_id).id, RECENT_LIMIT))

@app.route('/api/reading', methods=['GET'])
@app.route('/api/<station_id>/reading', methods=['GET'])
def get_reading(station_id=None):
    """The capture nearest a moment, so a point on the chart can show its frame."""
    station = resolve_station(station_id)
    try:
        at = int(request.args.get("at", ""))
    except ValueError:
        return jsonify({"error": "at must be unix seconds"}), 400

    # Wide enough to land inside the bucket a long-range chart point covers,
    # capped so a click on an empty stretch does not drag back a distant frame.
    window = max(600, min(int(request.args.get("window", 1800)), 6 * 3600))

    reading = database.reading_near(station.id, at, window)
    if not reading:
        return jsonify({"error": "no capture near that time"}), 404
    return jsonify(reading)

@app.route('/api/history', methods=['GET'])
@app.route('/api/<station_id>/history', methods=['GET'])
def get_history(station_id=None):
    """
    Level over a time range, for the chart.

    Takes `from` and `to` as unix seconds; defaults to the last 24 hours. Long
    spans come back averaged into buckets, with each point's true low and high,
    so a three-year view is a few hundred points instead of a few hundred
    thousand.
    """
    station = resolve_station(station_id)
    now = int(time.time())
    try:
        end_ts = int(request.args.get("to", now))
        start_ts = int(request.args.get("from", end_ts - 86400))
    except ValueError:
        return jsonify({"error": "from and to must be unix seconds"}), 400

    if start_ts >= end_ts:
        return jsonify({"error": "from must be earlier than to"}), 400

    # Clamp rather than reject: a range beyond retention has no data anyway, and
    # an unbounded span would scan the whole collection.
    start_ts = max(start_ts, end_ts - MAX_RANGE_DAYS * 86400)

    series = database.history_series(station.id, start_ts, end_ts)
    series["from"] = start_ts
    series["to"] = end_ts
    series["max_range_days"] = MAX_RANGE_DAYS
    return jsonify(series)

# ROI edits live in MongoDB so aiming a camera does not need a commit; a station
# with nothing saved keeps the region from stations.json. Loaded before the
# threads so the first reading of a fresh replica already sees the current one.
for _station in stations.all_stations():
    try:
        load_roi(_station)
    except Exception as e:
        print(f"[{_station.id}] saved detection region could not be applied: {e}")

# Started here, not next to background_tracker: the threads run immediately and
# would race the rest of this module, calling helpers that are not defined yet.
# One thread per station keeps a dead camera from stalling the others.
#
# TRACKER_ENABLED=false lets a script import this module without it starting to
# capture and write, which is what a one-off query or a test wants.
if os.getenv("TRACKER_ENABLED", "true").lower() not in ("false", "0", "no"):
    for _station in stations.all_stations():
        threading.Thread(target=background_tracker, args=(_station,), daemon=True).start()
else:
    print("Background trackers disabled (TRACKER_ENABLED=false)")

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4050)
