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
previous_water_level = 0

# Define the water level pixel mappings
original_water_level_mapping = {
    400: 0.40,
    311: 1.30,
    297: 1.40,
    287: 1.50,
    276: 1.60,
    263: 1.70,
    250: 1.80,
    236: 1.90,
    230: 2.00,
    205: 2.10,
    191: 2.20,
    176: 2.30,
    159: 2.40,
    144: 2.50,
    126: 2.60,
    108: 2.70,
    90: 2.80,
    71: 2.90,
    52: 3.00,
    33: 3.10,
    12: 3.20
}

# Define level offset
level_offset = 0

# Create new mapping by adjusting the key with the level_offset
water_level_mapping = {key + level_offset: value for key, value in original_water_level_mapping.items()}

# Output the new adjusted water level mapping
print(water_level_mapping)

# Directory to save images
save_directory = "images"

# Ensure the directory exists
if not os.path.exists(save_directory):
    os.makedirs(save_directory)

def cache_key():
    """Return a unique cache key based on the request URL path."""
    return request.path  # Use only the URL path as the cache key

def parse_hls_playlist(playlist_content):
    """Parse HLS playlist content and return list of video segments."""
    segments = []
    lines = playlist_content.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        # Skip comments and empty lines
        if line.startswith('#') or not line:
            continue
        # Check if it's a video segment (usually ends with .ts, .mp4, etc.)
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
                # Get the last segment
                last_segment = segments[-1]
                # If the segment URL is relative, make it absolute
                if last_segment.startswith('http'):
                    return last_segment
                else:
                    return base_url + last_segment
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
                # Return the last few segments as fallback options
                recent_segments = segments[-3:] if len(segments) >= 3 else segments
                # Convert relative URLs to absolute
                absolute_segments = []
                for segment in recent_segments:
                    if segment.startswith('http'):
                        absolute_segments.append(segment)
                    else:
                        absolute_segments.append(base_url + segment)
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

def get_water_level_from_y(y, water_level_mapping):
    """Map the y-coordinate to the corresponding water level."""
    for y_coord, level in water_level_mapping.items():
        if y >= y_coord:
            return level
    return None

def draw_level_lines(image, water_level_mapping, y_lowest_yellow):
    """Draw horizontal lines for each water level."""
    for y_coord, level in water_level_mapping.items():
        line_color = (0, 255, 0) if y_coord <= y_lowest_yellow else (0, 0, 255)  # Green for above, red for below
        cv2.line(image, (0, y_coord), (image.shape[1], y_coord), line_color, 2)
        cv2.putText(image, f"{level}m", (950, y_coord - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, line_color, 2)

def capture_last_frame_from_video(video_url):
    """Capture the last frame from the video segment with robust error handling."""
    # Set up video capture with specific backend and parameters to handle H.264 issues
    cap = cv2.VideoCapture(video_url)
    
    # Configure video capture to handle H.264 issues better
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Reduce buffer size
    # cap.set(cv2.CAP_PROP_FPS, 30)  # Set expected FPS
    
    if not cap.isOpened():
        print(f"Failed to open video: {video_url}")
        return None
    
    # Get and print video properties
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0
    
    aspect_ratio = width / height if height > 0 else 0
    
    # Get additional video properties
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])
    
    print(f"Video Properties:")
    print(f"  Size: {width}x{height} pixels")
    print(f"  Aspect Ratio: {aspect_ratio:.2f} ({width}:{height})")
    print(f"  Framerate: {fps:.2f} FPS")
    print(f"  Total Frames: {total_frames}")
    print(f"  Duration: {duration:.2f} seconds")
    print(f"  Codec: {codec} (FOURCC: {fourcc})")
    print(f"  URL: {video_url}")
    print("-" * 50)
    
    # Try multiple approaches to get a valid frame
    frame = None
    
    # Method 1: Try to get the last frame if frame count is available
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames > 0:
        # Try the last frame first
        cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
        ret, frame = cap.read()
        if ret and frame is not None:
            print(f"✅ Successfully captured frame: {frame.shape[1]}x{frame.shape[0]} pixels")
            cap.release()
            return frame
        
        # If last frame fails, try a few frames before the end
        for offset in [2, 5, 10]:
            if total_frames > offset:
                cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - offset)
                ret, frame = cap.read()
                if ret and frame is not None:
                    print(f"✅ Successfully captured frame (offset {offset}): {frame.shape[1]}x{frame.shape[0]} pixels")
                    cap.release()
                    return frame
    
    # Method 2: Try reading from the beginning and skip frames
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    for i in range(10):  # Try up to 10 frames
        ret, frame = cap.read()
        if ret and frame is not None:
            # Skip a few frames to get a more stable one
            for _ in range(3):
                cap.read()
            ret, frame = cap.read()
            if ret and frame is not None:
                print(f"✅ Successfully captured frame (method 2, attempt {i+1}): {frame.shape[1]}x{frame.shape[0]} pixels")
                cap.release()
                return frame
    
    # Method 3: Try reading any available frame
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ret, frame = cap.read()
    if ret and frame is not None:
        print(f"✅ Successfully captured frame (method 3): {frame.shape[1]}x{frame.shape[0]} pixels")
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
    return image_filename  # Return only the filename for easier URL construction

def generate_water_level_line_image(original_image, y_lowest_yellow, water_level):
    """Generate an image that shows only the water level line matching the detected level."""
    # Create a blank image with the same dimensions as the original
    water_level_image = original_image.copy()

    if water_level is not None:  # Ensure water_level is valid
        # Determine the exact y-coordinate for the interpolated water level
        y_coords = sorted(water_level_mapping.keys())  # Ensure we have the y-coordinates sorted
        y_coord_for_water_level = None

        for i in range(len(y_coords) - 1):
            if water_level_mapping[y_coords[i]] >= water_level > water_level_mapping[y_coords[i + 1]]:
                # Linear interpolation for exact position
                lower_y_coord = y_coords[i]
                upper_y_coord = y_coords[i + 1]
                lower_level = water_level_mapping[lower_y_coord]
                upper_level = water_level_mapping[upper_y_coord]

                # Calculate exact y-coordinate for the water level
                y_coord_for_water_level = lower_y_coord + (upper_y_coord - lower_y_coord) * (water_level - lower_level) / (upper_level - lower_level)
                break

        # Draw the line at the calculated y-coordinate
        if y_coord_for_water_level is not None:
            line_color = (0, 255, 0)  # Green for the matching level
            cv2.line(water_level_image, (0, int(y_coord_for_water_level)), (water_level_image.shape[1], int(y_coord_for_water_level)), line_color, 2)
            cv2.putText(water_level_image, f"{water_level:.2f}m", (950, int(y_coord_for_water_level) - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, line_color, 2)

    return water_level_image

def get_interpolated_water_level(y, water_level_mapping):
    """Map the y-coordinate to the corresponding water level using interpolation."""
    # Sort the water level mapping by y-coordinates
    y_coords = sorted(water_level_mapping.keys())
    # Check if the y-coordinate is below the lowest or above the highest
    if y >= y_coords[0]:
        for i in range(len(y_coords) - 1):
            if y_coords[i + 1] >= y > y_coords[i]:  # Found the interval
                # Interpolate between the two levels
                level_low = water_level_mapping[y_coords[i]]
                level_high = water_level_mapping[y_coords[i + 1]]

                # Calculate exact level using linear interpolation
                interpolated_level = level_low + (level_high - level_low) * (y - y_coords[i]) / (y_coords[i + 1] - y_coords[i])
                return interpolated_level
    return None

@app.route('/status', methods=['GET'])
@cache.cached(timeout=CACHE_TTL, key_prefix=cache_key)
def get_status():
    """Endpoint to get the water level and return image URLs."""
    global previous_water_level  # Declare as global to modify it
    
    # Try to get video segments with fallback options
    video_segments = get_video_segments()
    
    if not video_segments:
        return jsonify({"error": "Failed to retrieve video segments from HLS playlist."}), 500
    
    original_frame = None
    used_video_url = None
    
    # Try each video segment until we get a valid frame
    for video_url in reversed(video_segments):  # Try most recent first
        print(f"Trying video URL: {video_url}")
        original_frame = capture_last_frame_from_video(video_url)
        if original_frame is not None:
            used_video_url = video_url
            print(f"Successfully captured frame from: {video_url}")
            break
        else:
            print(f"Failed to capture frame from: {video_url}")
    
    if original_frame is not None:
        y_lowest_yellow = detect_yellow_region(original_frame)

        # If the yellow region is not detected, use the previous water level
        if y_lowest_yellow is None:
            water_level = previous_water_level  # Fallback to previous level
            print("Yellow region not detected, using previous water level:", water_level)
            
            # Still save the original image even if no yellow region detected
            original_image_filename = save_image(original_frame, "water_level_image", "_original")
            
            base_url = request.host_url
            unix_timestamp = int(datetime.now().timestamp())

            return jsonify({
                "water_level": water_level,
                "original_image_url": f"{base_url}images/{original_image_filename}",
                "processed_image_url": None,
                "water_level_line_image_url": None,
                "timestamp": unix_timestamp,
                "note": "Yellow region not detected, using previous water level"
            })
        else:
            # Detect water level from the image
            water_level = get_interpolated_water_level(y_lowest_yellow, water_level_mapping)
            previous_water_level = water_level  # Update the previous water level with the new one

            processed_frame = original_frame.copy()
            draw_level_lines(processed_frame, water_level_mapping, y_lowest_yellow)

            processed_image_filename = save_image(processed_frame, "water_level_image", "_processed")
            original_image_filename = save_image(original_frame, "water_level_image", "_original")

            # Generate the water level line image with the detected water level
            water_level_line_image = generate_water_level_line_image(original_frame, y_lowest_yellow, water_level)
            water_level_line_image_filename = save_image(water_level_line_image, "water_level_image", "_level_lines")

            base_url = request.host_url
            unix_timestamp = int(datetime.now().timestamp())

            return jsonify({
                "water_level": water_level,
                "original_image_url": f"{base_url}images/{original_image_filename}",
                "processed_image_url": f"{base_url}images/{processed_image_filename}",
                "water_level_line_image_url": f"{base_url}images/{water_level_line_image_filename}",
                "timestamp": unix_timestamp
            })
    else:
        return jsonify({"error": "Failed to capture frame from any video segment."}), 500

@app.route('/images/<filename>', methods=['GET'])
def serve_image(filename):
    """Serve the saved images from the folder."""
    return send_from_directory(save_directory, filename)

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4050)
