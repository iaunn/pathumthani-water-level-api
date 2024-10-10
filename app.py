import cv2
import numpy as np
import requests
import os
from flask import Flask, jsonify, send_from_directory, request
from datetime import datetime
from flask_caching import Cache

# Initialize Flask app
app = Flask(__name__)

# Configure caching
cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})

# Get TTL from environment variable or set a default
CACHE_TTL = int(os.getenv('CACHE_TTL', 300))

# Base URL for fetching video metadata and constructing the video URL
base_url = "http://101.109.253.60:8999/"
metadata_url = base_url + "load.jsp"

# Initialize previous water level
previous_water_level = 0

# Define the water level pixel mappings
water_level_mapping = {
    1080: 1.40,
    1045: 1.50,
    1017: 1.60,
    984: 1.70,
    950: 1.80,
    922: 1.90,
    892: 2.00,
    856: 2.10,
    822: 2.20,
    785: 2.30,
    750: 2.40,
    708: 2.50,
    670: 2.60,
    627: 2.70,
    585: 2.80,
    540: 2.90,
    495: 3.00,
    445: 3.10,
    400: 3.20,
    351: 3.30,
    304: 3.40,
    254: 3.50,
    206: 3.60,
    155: 3.70,
    105: 3.80,
    53: 3.90,
    20: 4.00,
}

# Directory to save images
save_directory = "images"

# Ensure the directory exists
if not os.path.exists(save_directory):
    os.makedirs(save_directory)

def cache_key():
    """Return a unique cache key based on the request URL path."""
    return request.path  # Use only the URL path as the cache key

def get_video_url():
    """Fetch the video metadata and construct the video URL."""
    response = requests.get(metadata_url)
    if response.status_code == 200:
        data = response.json()
        video_name = data.get("videoname")
        if video_name:
            return base_url + video_name
    return None

def detect_yellow_region(image, x_start=680, x_end=800):
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
    """Capture the last frame from the video stream."""
    cap = cv2.VideoCapture(video_url)
    
    if not cap.isOpened():
        print(f"Failed to open video: {video_url}")
        return None
    
    # Get the total number of frames in the video
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    if total_frames > 0:
        # Seek to the last frame (total_frames - 1 because frames are zero-indexed)
        cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
    
        # Capture the frame
        ret, frame = cap.read()
        
        if ret:
            cap.release()
            return frame
        else:
            print("Failed to capture the last frame.")
    
    cap.release()
    return None

def save_image(image, postfix=""):
    """Save the image to the specified directory with a timestamped filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_filename = f"water_level_image_{timestamp}{postfix}.jpg"
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
    video_url = get_video_url()

    if video_url:
        print(f"Video URL: {video_url}")

        original_frame = capture_last_frame_from_video(video_url)

        if original_frame is not None:
            y_lowest_yellow = detect_yellow_region(original_frame)

            # If the yellow region is not detected, use the previous water level
            if y_lowest_yellow is None:
                water_level = previous_water_level  # Fallback to previous level
                print("Yellow region not detected, using previous water level:", water_level)
            else:
                # Detect water level from the image
                water_level = get_interpolated_water_level(y_lowest_yellow, water_level_mapping)
                previous_water_level = water_level  # Update the previous water level with the new one

                processed_frame = original_frame.copy()
                draw_level_lines(processed_frame, water_level_mapping, y_lowest_yellow)

                processed_image_filename = save_image(processed_frame, "_processed")
                original_image_filename = save_image(original_frame, "_original")

                # Generate the water level line image with the detected water level
                water_level_line_image = generate_water_level_line_image(original_frame, y_lowest_yellow, water_level)
                water_level_line_image_filename = save_image(water_level_line_image, "_level_lines")

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
            return jsonify({"error": "Failed to capture frame from video."}), 500
    else:
        return jsonify({"error": "Failed to retrieve video metadata."}), 500

@app.route('/images/<filename>', methods=['GET'])
def serve_image(filename):
    """Serve the saved images from the folder."""
    return send_from_directory(save_directory, filename)

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
