# Water Level Measurement API จังหวัด ปทุมธานี

โปรเจกต์นี้เป็น API ที่สร้างด้วย Flask เพื่อวัดระดับน้ำในแม่น้ำ โดยการจับภาพจากฟีด CCTV API จะประมวลผลภาพและระบุระดับน้ำจากสีเหลืองในภาพที่แสดงระดับน้ำ

## Screenshot
  ![example image](/images/water_level_image.jpg)

## คุณสมบัติ

- ดึงฟีดวิดีโอและประมวลผลเฟรมเพื่อตรวจจับระดับน้ำ
- ทำเครื่องหมายระดับน้ำที่กำหนดไว้ในภาพที่ได้
- คืนค่าระดับน้ำและ URL ของภาพที่ประมวลผล

## แหล่งที่มาของวิดีโอ
ฟีดวิดีโอสำหรับการวัดระดับน้ำมาจาก:

- [http://101.109.253.60:8999](http://101.109.253.60:8999)

## วิธีการใช้งาน API

### `GET /status`
Endpoint นี้จะจับภาพจากฟีดวิดีโอ CCTV และคืนค่าระดับน้ำปัจจุบันและ URL ของภาพที่ประมวลผล

**ตัวอย่างการตอบกลับ:**

```json
{
    "water_level": 1.90,
    "original_image_url": "http://localhost:5000/images/water_level_image_20231006_154500_original.jpg",
    "processed_image_url": "http://localhost:5000/images/water_level_image_20231006_154500_processed.jpg"
}
```

## สำหรับนักพัฒนา
### ข้อกำหนด

- Python 3.7 ขึ้นไป
- Docker

### Deploy from prebuild Docker
```bash
docker pull ghcr.io/iaunn/pathumthani-water-level-api
docker run -d -it -p 5000:5000 -e CACHE_TTL=300 --name pathumthani-water-level-api ghcr.io/iaunn/pathumthani-water-level-api
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
docker run -d -it -p 5000:5000 -e CACHE_TTL=300 --name pathumthani-water-level-api pathumthani-water-level-api
```

### หมายเหตุ

-   API นี้ถูกออกแบบมาเพื่อใช้งานร่วมกับฟีดวิดีโอ CCTV ที่ระบุไว้ในส่วน "แหล่งที่มาของวิดีโอ" เท่านั้น
-   URL ของภาพที่ประมวลผลจะถูกสร้างขึ้นแบบไดนามิกและอาจมีการเปลี่ยนแปลงได้