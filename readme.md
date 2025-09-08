# Water Level Measurement API จังหวัด ปทุมธานี

โปรเจกต์นี้เป็น API ที่สร้างด้วย Flask เพื่อวัดระดับน้ำในแม่น้ำ โดยการจับภาพจากฟีด CCTV API จะประมวลผลภาพและระบุระดับน้ำจากสีเหลืองในภาพที่แสดงระดับน้ำ

## 📅 Last Updated

**Last Updated**: January 8, 2025 at 5:25 PM (UTC+7)

**Recent Changes**:
- Updated water level pixel mapping for current video resolution
- Simplified approach without image scaling
- Enhanced HLS playlist processing with fallback mechanisms
- Improved error handling and video properties logging
- Changed default port from 5000 to 4050

## Screenshot
  ![example image](/images/water_level_image.jpg)

## คุณสมบัติ

- อ่าน HLS playlist และจับภาพจากวิดีโอ segment ล่าสุด
- ตรวจจับระดับน้ำจากสีเหลืองในภาพโดยใช้ Computer Vision
- ประมวลผลภาพและทำเครื่องหมายระดับน้ำที่กำหนดไว้
- คืนค่าระดับน้ำและ URL ของภาพที่ประมวลผล
- รองรับการทำงานแบบ fallback เมื่อไม่สามารถตรวจจับสีเหลืองได้
- แสดงข้อมูลคุณสมบัติของวิดีโอ (ขนาด, อัตราส่วน, framerate)

## แหล่งที่มาของวิดีโอ
ฟีดวิดีโอสำหรับการวัดระดับน้ำมาจาก:

- [http://101.109.253.60:8999](http://101.109.253.60:8999) (Video Display)
- [http://101.109.253.60:8999/playlist.m3u8](http://101.109.253.60:8999/playlist.m3u8) (HLS Playlist)

## วิธีการใช้งาน API

### `GET /status`
Endpoint นี้จะอ่าน HLS playlist และจับภาพจากวิดีโอ segment ล่าสุด และคืนค่าระดับน้ำปัจจุบันและ URL ของภาพที่ประมวลผล

**ตัวอย่างการตอบกลับ (เมื่อตรวจจับสีเหลืองได้):**

```json
{
    "water_level": 1.90,
    "original_image_url": "http://localhost:4050/images/water_level_image_20231006_154500_original.jpg",
    "processed_image_url": "http://localhost:4050/images/water_level_image_20231006_154500_processed.jpg",
    "water_level_line_image_url": "http://localhost:4050/images/water_level_image_20231006_154500_level_lines.jpg",
    "timestamp": 1696601100
}
```

**ตัวอย่างการตอบกลับ (เมื่อไม่สามารถตรวจจับสีเหลืองได้):**

```json
{
    "water_level": 1.90,
    "original_image_url": "http://localhost:4050/images/water_level_image_20231006_154500_original.jpg",
    "processed_image_url": null,
    "water_level_line_image_url": null,
    "timestamp": 1696601100,
    "note": "Yellow region not detected, using previous water level"
}
```

## การปรับเทียบระดับน้ำ

API ใช้การปรับเทียบพิกเซลเพื่อแปลงตำแหน่งในภาพเป็นระดับน้ำจริง:

- **ช่วงการตรวจจับ**: X: 225-390 pixels
- **ช่วงระดับน้ำ**: 0.40m - 3.20m
- **การตรวจจับ**: ใช้สีเหลือง (HSV: 20-90, 100-255, 100-255)
- **การคำนวณ**: ใช้ Linear Interpolation สำหรับความแม่นยำสูง

## ข้อมูลเทคนิค

- **Port**: 4050 (เปลี่ยนจาก 5000)
- **Caching**: 300 วินาที (ปรับได้ผ่าน CACHE_TTL)
- **Video Format**: HLS (.m3u8) segments
- **Image Processing**: OpenCV with fallback mechanisms
- **Error Handling**: Robust fallback to previous water level

## สำหรับนักพัฒนา
### ข้อกำหนด

- Python 3.7 ขึ้นไป
- Docker

### Deploy from pre-built Docker images
```bash
docker pull ghcr.io/iaunn/pathumthani-water-level-api
docker run -d -it -p 4050:4050 -e CACHE_TTL=300 --name pathumthani-water-level-api ghcr.io/iaunn/pathumthani-water-level-api
```

### การติดตั้ง

1. Clone โค้ดจาก GitHub:
```bash
git clone https://github.com/iaunn/pathumthani-water-level-api.git
cd pathumthani-water-level-api
```
2.  สร้าง Docker image:
```bash
docker build -t pathumthani-water-level-api .
```
3. รัน Docker container:
```bash
docker run -d -it -p 4050:4050 -e CACHE_TTL=300 --name pathumthani-water-level-api pathumthani-water-level-api
```

### หมายเหตุ

-   API นี้ถูกออกแบบมาเพื่อใช้งานร่วมกับฟีดวิดีโอ CCTV ที่ระบุไว้ในส่วน "แหล่งที่มาของวิดีโอ" เท่านั้น
-   URL ของภาพที่ประมวลผลจะถูกสร้างขึ้นแบบไดนามิกและอาจมีการเปลี่ยนแปลงได้
-   การปรับเทียบระดับน้ำถูกปรับให้เหมาะกับความละเอียดของวิดีโอปัจจุบัน
-   API รองรับการทำงานแบบ fallback เมื่อไม่สามารถตรวจจับสีเหลืองได้
-   ข้อมูลวิดีโอ (ขนาด, framerate, codec) จะถูกแสดงใน console log