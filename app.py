import cv2
import numpy as np
import requests
import os
import warnings
from flask import Flask, jsonify, send_from_directory, request
from datetime import datetime
from flask_caching import Cache

# Suppress OpenCV warnings and H.264 decoder warnings
warnings.filterwarnings("ignore", category=UserWarning)
os.environ['OPENCV_FFMPEG_LOGLEVEL'] = '-8'  # Suppress FFmpeg logs

# Initialize Flask app
app = Flask(__name__)

# Configure caching
cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})

# Get TTL from environment variable or set a default
CACHE_TTL = int(os.getenv('CACHE_TTL', 300))

# Base URL for fetching HLS playlist
base_url = "http://101.109.253.60:8999/"
playlist_url = base_url + "playlist.m3u8"

# Initialize previous water level
previous_water_level = 0.0

# =========================
# Calibration (NEW MODEL)
# =========================
# Pixel is measured from top of the image (y increases downward)
# Ensure points are sorted by pixel (descending level with increasing pixel)
CAL_POINTS = [
    (20,  4.00),
    (34,  3.90),
    (235, 3.00),
    (317, 2.50),
]
# sort by pixel ascending just in case
CAL_POINTS = sorted(CAL_POINTS, key=lambda x: x[0])  # [(20,4.0), (34,3.9), (235,3.0), (317,2.5)]

# Useful bounds
PIX_MIN, LVL_MAX = CAL_POINTS[0]
PIX_MAX, LVL_MIN = CAL_POINTS[-1]

def pixel_to_level(y: float) -> float:
    """
    Piecewise-linear interpolation from pixel (y, from top) to water level (m).
    Extrapolates using the nearest segment if y is outside calibration range.
    """
    # exact match
    for py, lv in CAL_POINTS:
        if y == py:
            return lv

    # choose segment
    if y < CAL_POINTS[0][0]:
        # above top point -> extrapolate using first segment
        (x1, y1), (x2, y2) = CAL_POINTS[0], CAL_POINTS[1]
    elif y > CAL_POINTS[-1][0]:
        # below bottom point -> extrapolate using last segment
        (x1, y1), (x2, y2) = CAL_POINTS[-2], CAL_POINTS[-1]
    else:
        # find adjacent calibration points
        for i in range(len(CAL_POINTS) - 1):
            x1, y1 = CAL_POINTS[i]
            x2, y2 = CAL_POINTS[i + 1]
            if x1 <= y <= x2:
                break

    # linear interpolation y = m*x + b  (but here variables are renamed)
    # we want level = y1 + (y2 - y1) * ( (y - x1) / (x2 - x1) )
    if x2 == x1:
        return y1  # degenerate safety
    t = (y - x1) / (x2 - x1)
    return y1 + (y2 - y1) * t

def level_to_pixel(level_m: float) -> float:
    """
    Inverse mapping: given a level (m), return pixel y (from top).
    Piecewise-linear using the same calibration points.
    """
    # exact match
    for py, lv in CAL_POINTS:
        if abs(level_m - lv) < 1e-9:
            return py

    # choose segment (levels decrease with pixel)
    levels = [lv for _, lv in CAL_POINTS]
    if level_m > levels[0]:
        (x1, y1), (x2, y2) = CAL_POINTS[0], CAL_POINTS[1]
    elif level_m < levels[-1]:
        (x1, y1), (x2, y2) = CAL_POINTS[-2], CAL_POINTS[-1]
    else:
        for i in range(len(CAL_POINTS) - 1):
            x1, y1 = CAL_POINTS[i]
            x2, y2 = CAL_POINTS[i + 1]
            # y1 >= level >= y2 in normal case
            if (y1 >= level_m >= y2) or (y1 <= level_m <= y2):
                break

    if y1 == y2:
        return x1
    t = (level_m - y1) / (y2 - y1)
    return x1 + (x2 - x1) * t

# Directory to save images
save_directory = "images"
if not os.path.exists(save_directory):
    os.makedirs(save_directory)

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

def get_video_url():
    """Fetch the HLS playlist and get the last video segment URL."""
    try:
        response = requests.get(playlist_url, timeout=10)
        if response.status_code == 200:
            segments = parse_hls_playlist(response.text)
            if segments:
                last_segment = segments[-1]
                return last_segment if last_segment.startswith('http') else base_url + last_segment
    except requests.RequestException as e:
        print(f"Error fetching playlist: {e}")
    return None

def get_video_segments():
    """Fetch the HLS playlist and return multiple recent video segments."""
    try:
        response = requests.get(playlist_url, timeout=10)
        if response.status_code == 200:
            segments = parse_hls_playlist(response.text)
            if segments:
                recent_segments = segments[-3:] if len(segments) >= 3 else segments
                absolute_segments = []
                for segment in recent_segments:
                    absolute_segments.append(segment if segment.startswith('http') else base_url + segment)
                return absolute_segments
    except requests.RequestException as e:
        print(f"Error fetching playlist: {e}")
    return []

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

def draw_reference_ticks(image, y_detected):
    """
    Draw reference level ticks every 0.1 m using the calibrated inverse mapping.
    Green for ticks above (<= y_detected), red for below.
    """
    # Choose a reasonable display range based on calibration
    lvl_top = max(4.2, LVL_MAX)   # a bit above
    lvl_bot = min(2.3, LVL_MIN)   # a bit below
    lvl = lvl_top
    while lvl >= lvl_bot:
        y = int(level_to_pixel(lvl))
        color = (0, 255, 0) if y <= (y_detected or 10**9) else (0, 0, 255)
        cv2.line(image, (0, y), (image.shape[1], y), color, 1)
        cv2.putText(image, f"{lvl:.1f}m", (image.shape[1]-150, max(15, y-5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        lvl -= 0.1

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

def save_image(image, prefix="", postfix=""):
    """Save the image to the specified directory with a timestamped filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_filename = f"{prefix}_{timestamp}{postfix}.jpg"
    save_path = os.path.join(save_directory, image_filename)
    cv2.imwrite(save_path, image)
    return image_filename

def generate_water_level_line_image(original_image, y_lowest_yellow, water_level):
    """Generate an image that shows only the water level line matching the detected level."""
    water_level_image = original_image.copy()
    if water_level is not None:
        y = int(level_to_pixel(water_level))
        line_color = (0, 255, 0)
        cv2.line(water_level_image, (0, y), (water_level_image.shape[1], y), line_color, 2)
        cv2.putText(water_level_image, f"{water_level:.2f}m",
                    (water_level_image.shape[1]-200, max(15, y-5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, line_color, 2)
    return water_level_image

@app.route('/status', methods=['GET'])
@cache.cached(timeout=CACHE_TTL, key_prefix=cache_key)
def get_status():
    """Endpoint to get the water level and return image URLs."""
    global previous_water_level

    video_segments = get_video_segments()
    if not video_segments:
        return jsonify({"error": "Failed to retrieve video segments from HLS playlist."}), 500

    original_frame = None
    used_video_url = None

    for video_url in reversed(video_segments):  # Try most recent first
        print(f"Trying video URL: {video_url}")
        original_frame = capture_last_frame_from_video(video_url)
        if original_frame is not None:
            used_video_url = video_url
            print(f"Successfully captured frame from: {video_url}")
            break
        else:
            print(f"Failed to capture frame from: {video_url}")

    if original_frame is None:
        return jsonify({"error": "Failed to capture frame from any video segment."}), 500

    y_lowest_yellow = detect_yellow_region(original_frame)

    if y_lowest_yellow is None:
        water_level = previous_water_level
        print("Yellow region not detected, using previous water level:", water_level)

        original_image_filename = save_image(original_frame, "water_level_image", "_original")
        base_ = request.host_url
        unix_timestamp = int(datetime.now().timestamp())

        return jsonify({
            "water_level": water_level,
            "original_image_url": f"{base_}images/{original_image_filename}",
            "processed_image_url": None,
            "water_level_line_image_url": None,
            "timestamp": unix_timestamp,
            "note": "Yellow region not detected, using previous water level"
        })

    # Compute level using calibrated mapping
    water_level = float(pixel_to_level(float(y_lowest_yellow)))
    previous_water_level = water_level

    processed_frame = original_frame.copy()
    # Draw reference ticks for context
    draw_reference_ticks(processed_frame, y_lowest_yellow)

    processed_image_filename = save_image(processed_frame, "water_level_image", "_processed")
    original_image_filename = save_image(original_frame, "water_level_image", "_original")
    water_level_line_image = generate_water_level_line_image(original_frame, y_lowest_yellow, water_level)
    water_level_line_image_filename = save_image(water_level_line_image, "water_level_image", "_level_lines")

    base_ = request.host_url
    unix_timestamp = int(datetime.now().timestamp())

    return jsonify({
        "water_level": water_level,
        "original_image_url": f"{base_}images/{original_image_filename}",
        "processed_image_url": f"{base_}images/{processed_image_filename}",
        "water_level_line_image_url": f"{base_}images/{water_level_line_image_filename}",
        "timestamp": unix_timestamp,
        "calibration_points": CAL_POINTS
    })

@app.route('/images/<filename>', methods=['GET'])
def serve_image(filename):
    """Serve the saved images from the folder."""
    return send_from_directory(save_directory, filename)

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4050)
