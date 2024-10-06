import cv2
import numpy as np
import requests
import os
from flask import Flask, jsonify, send_from_directory
from datetime import datetime

# Initialize Flask app
app = Flask(__name__)

# Base URL for fetching video metadata and constructing the video URL
base_url = "http://101.109.253.60:8999/"
metadata_url = base_url + "load.jsp"

# Define the water level pixel mappings
water_level_mapping = {
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
    # Convert image to HSV (Hue, Saturation, Value) color space
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    # Define yellow color range in HSV
    lower_yellow = np.array([20, 100, 100])
    upper_yellow = np.array([30, 255, 255])
    
    # Threshold the image to get only yellow colors
    mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
    
    # Focus only on the specified x-axis range (680 to 800)
    mask[:, :x_start] = 0  # Zero out everything left of x_start
    mask[:, x_end:] = 0    # Zero out everything right of x_end

    # Find contours of the yellow region
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if contours:
        # Get the contour with the lowest y-coordinate (this should correspond to the lowest visible water level)
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
    """Draw horizontal lines for each water level.
    
    Lines above the water level are green, and lines below are red.
    """
    for y_coord, level in water_level_mapping.items():
        # Set line color: green if above the water level, red if below
        if y_coord <= y_lowest_yellow:
            line_color = (0, 255, 0)  # Green for above
        else:
            line_color = (0, 0, 255)  # Red for below

        # Draw the line
        cv2.line(image, (0, y_coord), (image.shape[1], y_coord), line_color, 2)
        
        # Put the level text next to the line
        cv2.putText(image, f"{level}m", (10, y_coord - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, line_color, 2)

def capture_frame_from_video(video_url):
    """Capture a frame from the video stream."""
    cap = cv2.VideoCapture(video_url)
    ret, frame = cap.read()
    if ret:
        return frame
    else:
        print("Failed to capture the video frame.")
        return None
    cap.release()

def save_image(image):
    """Save the image to the specified directory with a timestamped filename."""
    # Generate a timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_filename = f"water_level_image_{timestamp}.jpg"
    save_path = os.path.join(save_directory, image_filename)
    
    cv2.imwrite(save_path, image)
    return image_filename  # Return only the filename for easier URL construction

@app.route('/getStatus', methods=['GET'])
def get_status():
    """Endpoint to get the water level and return image URL."""
    # Step 1: Get the dynamic video URL
    video_url = get_video_url()

    if video_url:
        print(f"Video URL: {video_url}")
        
        # Step 2: Capture a frame from the video
        frame = capture_frame_from_video(video_url)
        
        if frame is not None:
            # Step 3: Detect the lowest yellow region in the River Level Measurement
            y_lowest_yellow = detect_yellow_region(frame)
            
            if y_lowest_yellow is not None:
                # Step 4: Get water level from the y-coordinate
                water_level = get_water_level_from_y(y_lowest_yellow, water_level_mapping)
                
                # Step 5: Draw level lines for each predefined water level
                draw_level_lines(frame, water_level_mapping, y_lowest_yellow)
                
                # Step 6: Save the processed image with a timestamped filename
                image_filename = save_image(frame)

                # Return water level and image URL
                return jsonify({
                    "water_level": water_level,
                    "image_url": f"/images/{image_filename}"
                })
            else:
                return jsonify({"error": "Yellow region not detected."}), 500
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
