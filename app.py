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

# Maximum number of images to keep (delete older ones automatically)
MAX_KEEP = int(os.getenv("MAX_KEEP_IMAGES", 200))

# Base URL for fetching HLS playlist
base_url = "http://101.109.253.60:8999/"
playlist_url = base_url + "playlist.m3u8"

# Initialize previous water level
previous_water_level = 0.0

# Detection smoothing buffers
detection_history = []  # Store last 5 detections
MAX_HISTORY = 5

# =========================
# Calibration (NEW MODEL)
# =========================
# Pixel is measured from top of the image (y increases downward)
# Ensure points are sorted by pixel (descending level with increasing pixel)
CAL_POINTS = [
    (34,  3.90),
    (55,  3.80),
    (143, 3.40),
    (185, 3.20),
    (143, 3.40),
    (224, 3.00),
    (263, 2.80),
    (282, 2.70),
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

def detect_yellow_region_enhanced(image, x_start=225, x_end=390):
    """Highly accurate yellow region detection with multiple enhancement strategies."""
    h = image.shape[0]
    w = image.shape[1]
    
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
        for line in lines:
            x1, y1, x2, y2 = line[0]
            # Focus on relatively horizontal lines
            if x_start <= min(x1, x2) and max(x1, x2) <= x_end:
                angle = np.arctan2(y2-y1, x2-x1) * 180 / np.pi
                if abs(angle) < 15:  # Nearly horizontal (±15 degrees)
                    # Get yellow pixels near this line
                    line_y = int((y1 + y2) / 2)
                    if 50 <= line_y <= h - 50:  # Avoid edges
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
        roi_edges = edge_response[:, x_start:x_end]
        
        # Find horizontal lines with high edge response
        for y in range(100, h-100, 5):  # Sample every 5 pixels
            line_response = np.mean(roi_edges[y-5:y+5, :])
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

def smooth_detection(raw_detection):
    """Apply temporal smoothing to reduce noise in detections."""
    global detection_history
    
    # Add current detection to history
    detection_history.append(raw_detection)
    
    # Keep only recent detections
    if len(detection_history) > MAX_HISTORY:
        detection_history = detection_history[-MAX_HISTORY:]
    
    # Remove invalid detections (None)
    valid_detections = [d for d in detection_history if d is not None]
    
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

def detect_yellow_region_adaptive(image, x_start=225, x_end=390):
    """Ultra-precisive yellow detection with adaptive parameters."""
    h = image.shape[0]
    w = image.shape[1]
    
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
        roi_edges = edges[:, x_start:x_end]
        
        for y in range(20, h-20, 3):  # Fine sampling
            window_size = 11
            edge_window = roi_edges[y-window_size//2:y+window_size//2, :]
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

def detect_yellow_regions_fused(image, x_start=225, x_end=390):
    """Ultimate yellow detection cascade with all enhancement strategies."""
    # 1. Try most advanced adaptive method
    result = detect_yellow_region_adaptive(image, x_start, x_end)
    if result is not None:
        return result
    
    # 2. Try enhanced multi-scale method
    result = detect_yellow_region_enhanced(image, x_start, x_end)
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
        for line in lines:
            x1, y1, x2, y2 = line[0]
            
            # Check if line is in ROI and roughly horizontal
            mid_x = int((x1 + x2) / 2)
            mid_y = int((y1 + y2) / 2)
            
            if x_start <= mid_x <= x_end and 50 <= mid_y <= image.shape[0] - 50:
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

def detect_water_level_on_gauge(image, x_start=225, x_end=390):
    """
    Specialized detection for water level on yellow gauge markers.
    Focuses on finding the interface between water and air at gauge markings.
    """
    h = image.shape[0]
    w = image.shape[1]
    
    # 1. ENHANCED GAUGE MARKER DETECTION
    # Focus specifically on finding yellow background of the gauge
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    # Tight yellow range for gauge background
    lower_gauge = np.array([20, 140, 120])
    upper_gauge = np.array([30, 255, 255])
    gauge_mask = cv2.inRange(hsv, lower_gauge, upper_gauge)
    
    # Restrict to ROI
    gauge_mask[:, :x_start] = 0
    gauge_mask[:, x_end:] = 0
    
    # Clean up the gauge mask
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 7))  # Vertical rectangular kernel
    gauge_mask = cv2.morphologyEx(gauge_mask, cv2.MORPH_CLOSE, kernel)
    gauge_mask = cv2.morphologyEx(gauge_mask, cv2.MORPH_OPEN, kernel)
    
    # Find gauge boundaries
    contours, _ = cv2.findContours(gauge_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return None
    
    # Select largest contour (the gauge)
    gauge_contour = max(contours, key=cv2.contourArea)
    x_gauge, y_gauge_top, w_gauge, h_gauge = cv2.boundingRect(gauge_contour)
    
    # Extend gauge region slightly for water interface detection
    gauge_roi_x1 = max(0, x_gauge - 5)
    gauge_roi_x2 = min(w, x_gauge + w_gauge + 5)
    gauge_roi_y1 = max(0, y_gauge_top - 10)
    gauge_roi_y2 = min(h, y_gauge_top + h_gauge + 10)
    
    gauge_roi = image[gauge_roi_y1:gauge_roi_y2, gauge_roi_x1:gauge_roi_x2]
    
    if gauge_roi.size == 0:
        return None
    
    # 2. WATER INTERFACE DETECTION USING GRADIENT ANALYSIS
    # Convert ROI to grayscale
    gray_roi = cv2.cvtColor(gauge_roi, cv2.COLOR_BGR2GRAY)
    
    # Apply Gaussian blur to reduce noise
    blurred_roi = cv2.GaussianBlur(gray_roi, (3, 3), 0)
    
    # Calculate vertical gradient (focus on horizontal transitions)
    gradient_y = cv2.Sobel(blurred_roi, cv2.CV_64F, 0, 1, ksize=3)
    gradient_y = np.abs(gradient_y)
    
    # Find the strongest horizontal gradient (water surface typically has significant contrast)
    water_candidates = []
    
    # Scan vertically through the gauge ROI
    for y_offset in range(10, gauge_roi.shape[0] - 10):
        # Get horizontal line of gradient values
        line_gradient = gradient_y[y_offset, :]
        
        # Calculate gradient strength for this line
        gradient_strength = np.mean(line_gradient)
        gradient_variance = np.var(line_gradient)
        
        # Check if this line shows water-like characteristics
        roi_line = gauge_roi[y_offset-2:y_offset+3, :]
        
        if roi_line.size > 0:
            # Check color properties of the line area
            hsv_line = cv2.cvtColor(roi_line, cv2.COLOR_BGR2HSV)
            
            # Dark/brown water detection
            lower_dark = np.array([0, 0, 0])
            upper_dark = np.array([180, 255, 100])  # Low brightness for water
            dark_mask = cv2.inRange(hsv_line, lower_dark, upper_dark)
            dark_ratio = np.sum(dark_mask > 0) / roi_line.size
            
            # Yellow gauge detection for comparison
            lower_yellow = np.array([20, 100, 120])
            upper_yellow = np.array([30, 255, 255])
            yellow_mask = cv2.inRange(hsv_line, lower_yellow, upper_yellow)
            yellow_ratio = np.sum(yellow_mask > 0) / roi_line.size
            
            # Score this line as potential water level
            # High gradient strength + reasonable water color + low yellow ratio
            water_score = gradient_strength * (1 + gradient_variance / 1000) * (dark_ratio > 0.3) * (yellow_ratio < 0.4)
            
            if water_score > 50:  # Threshold for water level candidate
                absolute_y = gauge_roi_y1 + y_offset
                water_candidates.append((absolute_y, water_score, dark_ratio, yellow_ratio))
    
    # 3. REFINE WATER LEVEL USING TEMPORAL COHERENCE AND PHYSICS
    if water_candidates:
        # Sort by position (lowest water level first) and score
        water_candidates.sort(key=lambda x: (x[0], -x[1]))
        
        # Select candidates that make physical sense:
        # 1. Should be near the middle-lower portion of gauge
        # 2. Should have significant water color vs gauge color
        # 3. Should have strong gradient signature
        
        valid_candidates = []
        gauge_center_y = y_gauge_top + h_gauge // 2
        
        for candidate in water_candidates:
            y_pos, score, dark_ratio, yellow_ratio = candidate
            
            # Skip if too close to bottom (water level rarely at very bottom of gauge)
            if y_pos > y_gauge_top + h_gauge * 0.9:
                continue
                
            # Skip if too close to top (water level rarely at very top)
            if y_pos < y_gauge_top + h_gauge * 0.1:
                continue
            
            # Prefer candidates with good water-to-gauge ratio
            if dark_ratio > 0.2 and yellow_ratio < 0.6:
                valid_candidates.append((y_pos, score, dark_ratio, yellow_ratio))
        
        if valid_candidates:
            # Select the best candidate based on combined score
            # Weight position (prefer middle-lower), score, and color balance
            final_scores = []
            for candidate in valid_candidates:
                y_pos, score, dark_ratio, yellow_ratio = candidate
                
                # Position score: prefer lower but not bottom
                position_weight = 1.2 if y_gauge_top + h_gauge * 0.3 <= y_pos <= y_gauge_top + h_gauge * 0.8 else 1.0
                
                # Color balance score
                color_balance = dark_ratio / max(yellow_ratio, 0.1)
                
                # Combined score
                final_score = score * position_weight * color_balance
                final_scores.append((y_pos, final_score))
            
            if final_scores:
                # Return the position with highest combined score
                final_scores.sort(key=lambda x: -x[1])
                return int(final_scores[0][0])
    
    # 4. FALLBACK: Use color-based detection in gauge region
    # Look for significant color change from yellow gauge to darker water
    for y_offset in range(gauge_roi.shape[0] // 3, gauge_roi.shape[0]):
        if y_offset >= gauge_roi.shape[0]:
            break
            
        # Sample lines above and below current position
        above_line = gauge_roi[max(0, y_offset-5):y_offset, :]
        below_line = gauge_roi[y_offset:min(gauge_roi.shape[0], y_offset+5), :]
        
        if above_line.size > 0 and below_line.size > 0:
            # Above should be more yellow (gauge), below should be more water-colored
            above_hsv = cv2.cvtColor(above_line, cv2.COLOR_BGR2HSV)
            below_hsv = cv2.cvtColor(below_line, cv2.COLOR_BGR2HSV)
            
            # Check brightness difference (water typically darker)
            above_brightness = np.mean(above_hsv[:, :, 2])
            below_brightness = np.mean(below_hsv[:, :, 2])
            
            # Significant brightness change indicates water interface
            if below_brightness < above_brightness * 0.8:
                absolute_y = gauge_roi_y1 + y_offset
                return int(absolute_y)
    
    return None

def detect_yellow_region_fused(image, x_start=225, x_end=390):
    """Enhanced yellow detection with specialized water level detection."""
    # First try specialized water level detection on gauge
    result = detect_water_level_on_gauge(image, x_start, x_end)
    if result is not None:
        print(f"Water level detected on gauge: {result}")
        return result
    
    # Fallback to cascade method
    result = detect_yellow_regions_fused(image, x_start, x_end)
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

def visualize_detection_debug(image, x_start=225, x_end=390):
    """Create debug visualization showing color mask, edge mask, and fused result."""
    h = image.shape[0]
    w = image.shape[1]
    
    # Color detection
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_yellow = np.array([18, 80, 80])
    upper_yellow = np.array([90, 255, 255])
    color_mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
    color_mask[:, :x_start] = 0
    color_mask[:, x_end:] = 0
    
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
    
    # Fusion
    fused_mask = cv2.addWeighted(color_mask, 0.7, edge_mask, 0.3, 0)
    
    # Create debug image with 4 panels
    debug_h = h * 2
    debug_w = w * 2
    debug_image = np.zeros((debug_h, debug_w, 3), dtype=np.uint8)
    
    # Panel 1: Original with ROI
    roi_image = image.copy()
    cv2.rectangle(roi_image, (x_start, 0), (x_end, h), (0, 255, 0), 2)
    debug_image[0:h, 0:w] = roi_image
    
    # Panel 2: Color mask
    debug_image[0:h, w:debug_w] = cv2.cvtColor(color_mask, cv2.COLOR_GRAY2BGR)
    
    # Panel 3: Edge mask  
    debug_image[h:debug_h, 0:w] = cv2.cvtColor(edge_mask, cv2.COLOR_GRAY2BGR)
    
    # Panel 4: Fused mask
    debug_image[h:debug_h, w:debug_w] = cv2.cvtColor(fused_mask, cv2.COLOR_GRAY2BGR)
    
    return debug_image

def visualize_water_level_debug(image, x_start=225, x_end=390):
    """Create detailed visualization of water level detection process."""
    # Run water level detection
    detected_y = detect_water_level_on_gauge(image, x_start, x_end)
    
    # Create visualization image
    debug_img = image.copy()
    
    # Draw ROI boundary
    cv2.rectangle(debug_img, (x_start, 0), (x_end, image.shape[0]), (0, 255, 0), 2)
    
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

def rotate_images(dir_="images"):
    """
    Delete old images, keeping only the most recent MAX_KEEP images.
    Files are sorted by filename which contains timestamp.
    """
    try:
        files = sorted([f for f in os.listdir(dir_) if f.endswith(".jpg")])
        if len(files) > MAX_KEEP:
            files_to_delete = files[:-MAX_KEEP]
            deleted_count = 0
            for f in files_to_delete:
                try:
                    file_path = os.path.join(dir_, f)
                    os.remove(file_path)
                    deleted_count += 1
                except OSError as e:
                    print(f"Error deleting file {f}: {e}")
            print(f"Image rotation: Deleted {deleted_count} old images, keeping {MAX_KEEP} most recent")
    except OSError as e:
        print(f"Error during image rotatation: {e}")

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

    # Perform enhanced detection with smoothing
    raw_detection = detect_yellow_region_fused(original_frame)
    y_lowest_yellow = smooth_detection(raw_detection)

    if y_lowest_yellow is None:
        water_level = previous_water_level
        print("Yellow region not detected, using previous water level:", water_level)

        original_image_filename = save_image(original_frame, "water_level_image", "_original")
        
        # Clean up old images to maintain MAX_KEEP limit
        rotate_images()
        
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
    
    # Clean up old images to maintain MAX_KEEP limit
    rotate_images()

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

@app.route('/debug', methods=['GET'])
def debug_detection():
    """Debug endpoint to visualize color + edge fusion detection."""
    try:
        video_url = get_video_url()
        if not video_url:
            return jsonify({"error": "Failed to get video URL"}), 500
        
        original_frame = capture_last_frame_from_video(video_url)
        if original_frame is None:
            return jsonify({"error": "Failed to capture frame"}), 500
        
        # Create debug visualization
        debug_image = visualize_detection_debug(original_frame)
        
        # Run detection
        detected_y = detect_yellow_region_fused(original_frame)
        
        # Draw detection result on debug image
        if detected_y is not None:
            h = original_frame.shape[0]
            cv2.line(debug_image, (0, detected_y), (debug_image.shape[1], detected_y), (0, 0, 255), 3)
            cv2.putText(debug_image, f"Detected Y: {detected_y}", (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        # Save debug image
        debug_filename = save_image(debug_image, "detection_debug")
        
        base_ = request.host_url
        return jsonify({
            "debug_image_url": f"{base_}images/{debug_filename}",
            "detected_y": detected_y,
            "detection_success": detected_y is not None
        })
        
    except Exception as e:
        return jsonify({"error": f"Debug failed: {str(e)}"}), 500

@app.route('/debug-water', methods=['GET'])
def debug_water_level():
    """Specialized debug endpoint for water level detection only."""
    try:
        video_url = get_video_url()
        if not video_url:
            return jsonify({"error": "Failed to get video URL"}), 500
        
        original_frame = capture_last_frame_from_video(video_url)
        if original_frame is None:
            return jsonify({"error": "Failed to capture frame"}), 500
        
        # Run water level detection
        detected_y = detect_water_level_on_gauge(original_frame)
        
        # Create water level debug visualization
        debug_image = visualize_water_level_debug(original_frame)
        
        # Save debug image
        debug_filename = save_image(debug_image, "water_level_debug")
        
        base_ = request.host_url
        water_level = None
        
        if detected_y is not None:
            water_level = float(pixel_to_level(float(detected_y)))
        
        return jsonify({
            "water_level_debug_url": f"{base_}images/{debug_filename}",
            "detected_y_pixel": detected_y,
            "water_level_meters": water_level,
            "detection_success": detected_y is not None,
            "calibration_points": CAL_POINTS
        })
        
    except Exception as e:
        return jsonify({"error": f"Water level debug failed: {str(e)}"}), 500

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4050)
