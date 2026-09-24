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
    "timestamp": 1696601100,
    "detection_mode": "gauge"
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
    "detection_mode": "none",
    "note": "Yellow region not detected, using previous water level"
}
```

## การปรับเทียบระดับน้ำ

API ใช้การปรับเทียบพิกเซลเพื่อแปลงตำแหน่งในภาพเป็นระดับน้ำจริง:

- **ช่วงการตรวจจับ**: กล่องสี่ขอบ (ซ้าย ขวา บน ล่าง) เช่น X: 225-390, Y: ทั้งเฟรม<br>
  กำหนดต่อสถานี ค่าเริ่มต้นอยู่ใน `stations.json` และแก้จากหน้า `/calibrate` ได้
- **ช่วงระดับน้ำ**: 0.40m - 3.20m
- **การตรวจจับ**: ใช้สีเหลือง (HSV: 20-90, 100-255, 100-255)
- **การคำนวณ**: ใช้ Linear Interpolation สำหรับความแม่นยำสูง

## ข้อมูลเทคนิค

- **Port**: 4050
- **Caching**: 300 วินาที (ปรับได้ผ่าน CACHE_TTL)
- **Video Format**: HLS (.m3u8) segments
- **Image Processing**: OpenCV with fallback mechanisms
- **Object storage**: S3-compatible (MinIO, Cloudflare R2, AWS S3)
- **Database**: MongoDB

## การตั้งค่า (Environment variables)

แอปนี้ต้องมี object storage และ MongoDB เสมอ ถ้าตั้งค่าไม่ครบจะหยุดทำงานตั้งแต่ตอนบูต
พร้อมข้อความบอกว่าขาดตัวไหน ค่าเหล่านี้อ่านได้จากไฟล์ `.env` ที่รากโปรเจกต์
(ดูตัวอย่างครบชุดที่ `.env.example`) หรือจาก environment โดยตรง

| ตัวแปร | ความหมาย |
|---|---|
| `S3_BUCKET` | ชื่อ bucket |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | คีย์สำหรับเขียนไฟล์ |
| `S3_ENDPOINT_URL` | endpoint ที่แอปเขียนไฟล์ ใส่สำหรับ MinIO/R2 เว้นว่างได้ถ้าใช้ AWS S3 |
| `S3_PUBLIC_BASE_URL` | URL ที่เบราว์เซอร์ใช้อ่านภาพ (bucket แบบ public หรือโดเมน CDN) |
| `S3_REGION` | `auto` สำหรับ R2/MinIO หรือ region จริงของ AWS S3 |
| `S3_ADDRESSING_STYLE` | `path` สำหรับ MinIO, `virtual` สำหรับ R2/AWS |
| `MONGODB_URI` | connection string ของ MongoDB |
| `MONGODB_DATABASE` | ชื่อฐานข้อมูล (ค่าเริ่มต้น `water_level`) |
| `STATION_<ID>_CREDENTIALS` | `user:password` ของกล้องที่ต้องล็อกอิน ตั้งชื่อตัวแปรได้เองผ่าน `credentials_env` |
| `MAX_KEEP_IMAGES` | จำนวนภาพที่เก็บไว้ ภาพเก่ากว่านั้นถูกลบจาก bucket (ค่าเริ่มต้น 200) |
| `RETENTION_DAYS` | เก็บค่าที่วัดได้ย้อนหลังกี่วัน (ค่าเริ่มต้น 1095 = 3 ปี) |
| `MAX_RANGE_DAYS` | ดูได้ครั้งละกี่วัน (ค่าเริ่มต้น 92 = 3 เดือน) |
| `HOMOGRAPHY_ENABLED` | `false` เพื่อปิดการจัดแนว homography ใช้ภาพตรง ๆ เหมาะกับกล้องที่ติดตายตัว ไม่ต้อง warp ทุกเฟรมและไม่อัป baseline (ค่าเริ่มต้น `true`) |

สถานีกำหนดใน `stations.json` รับกล้องสองแบบ: `playlist_url` สำหรับ HLS และ
`snapshot_url` สำหรับกล้องที่ให้ภาพนิ่ง JPEG (เช่น Axis `/jpg/image.jpg`) ซึ่งรองรับ
`auth: digest` หรือ `basic` โดย**รหัสผ่านอยู่ใน environment ไม่ใช่ในไฟล์นี้** เพราะไฟล์นี้อยู่ใน git
กล้องภาพนิ่งจะถูกดึงหลายครั้งติดกันตอนวัด เพราะการแยกน้ำออกจากเงาสะท้อนต้องใช้หลายเฟรม
สถานีที่ยังไม่ได้ calibrate จะไม่บันทึกค่า และ API จะตอบ 409 แทนการเดาระดับน้ำ

**พื้นที่ตรวจจับ (ROI)** ของแต่ละสถานีเป็นกล่องสี่ขอบ — ซ้าย ขวา บน ล่าง — นับพิกเซลจากมุมซ้ายบนของเฟรม
ตัวตรวจจับจะมองเฉพาะinsideกล่องนี้ ขอบที่ไม่ระบุ (ค่าเริ่มต้นจาก `stations.json`) หมายถึง "一直到ขอบภาพ"
แก้จากหน้า `/calibrate` ได้ทั้งการลากขอบบนภาพหรือพิมพ์พิกเซล แล้วกด Save ค่าจะลง MongoDB
(collection `station_config`) แทนค่าในไฟล์ ทำให้ปรับกล้องได้ทันทีโดยไม่ต้อง commit และ replica อื่นจะอ่านค่าใหม่
ในการตรวจรอบถัดไป (ทุก 5 นาที) ปุ่ม `Reset to config` คืนค่ากลับเป็นค่าใน `stations.json`

**ตัวตรวจจับ** อ่านระดับน้ำจากเท็กซ์เจอร์ของมาตรวัด (ขีดแบนดำ) ที่ยุบตัวลงตรงผิวน้ำ
แล้วตรวจสอบด้วยสีของมาตรวัด — ค่าจะผ่านก็ต่อเมื่อสีของมาตรวัดจบตรงจุดนั้นและไม่มีต่อด้านล่าง
เงาสะท้อนไม่ผ่านมาสก์ (ความอิ่มตัว p50 = 84 เทียบกับตัวมาตรวัด 226) แม้ตอนฝนตกที่สีมาตรวัด
ซีดจนหลุดเกณฑ์ความอิ่มตัว (p50 = 79) แต่ก็ยังสว่างกว่าเงา (V p50 254 เทียบกับ 144)
จึงใช้ความสว่างประกอบด้วย เมื่อตรวจสอบไม่ผ่าน หรือในกล่องไม่มีมาตรวัดเลย
ระบบจะ**ไม่บันทึกค่า**แทนการเดา และ `/status` กับหน้า `/calibrate` บอกโหมดที่ใช้ผ่าน `detection_mode`:
`gauge` = สี + เท็กซ์เจอร์, `blind` = เท็กซ์เจอร์อย่างเดียว (มาตรวัดขาว-แดง หรือภาพกลางคืน),
`none` = รอบนั้นไม่บันทึก

หน้าเว็บเปิดมาที่ 24 ชั่วโมงล่าสุด เลือกเป็น 7 วัน / 30 วัน / 3 เดือน หรือกำหนดวันเองก็ได้
ช่วงที่ยาวกว่าสองสามวันจะถูกเฉลี่ยเป็นช่วง ๆ ก่อนส่งให้เบราว์เซอร์ (3 ปีเต็มคือ ~315,000 แถว)
โดยแต่ละจุดพ่วงค่าต่ำสุด-สูงสุดจริงในช่วงนั้นไปด้วย ตัวเลข "สูงสุด/ต่ำสุด" จึงไม่ถูกการเฉลี่ยกลบ

ภาพถูกเก็บที่ `captures/` และภาพอ้างอิงสำหรับ homography อยู่ที่
`reference/reference_frame.jpg` ส่วนค่า calibration กับประวัติระดับน้ำเก็บใน MongoDB
ทำให้ container ไม่มี state ของตัวเอง รันหลาย replica หรือ redeploy ได้โดยไม่ต้อง mount volume

## Local development

```bash
docker compose up -d                 # MinIO + MongoDB
# ถ้าพอร์ต 27017 ถูกใช้อยู่แล้ว: MONGO_PORT=27018 docker compose up -d
```

สร้าง bucket ครั้งแรกและเปิดให้อ่านแบบ public:

```bash
python -c "
import boto3, json
from botocore.config import Config
c = boto3.client('s3', endpoint_url='http://localhost:9000',
    aws_access_key_id='minioadmin', aws_secret_access_key='minioadmin',
    region_name='auto', config=Config(signature_version='s3v4', s3={'addressing_style':'path'}))
c.create_bucket(Bucket='water-level')
c.put_bucket_policy(Bucket='water-level', Policy=json.dumps({'Version':'2012-10-17','Statement':[
    {'Effect':'Allow','Principal':{'AWS':['*']},'Action':['s3:GetObject'],
     'Resource':['arn:aws:s3:::water-level/*']}]}))
"
```

จากนั้น `cp .env.example .env` แล้วรัน `python app.py` ได้เลย แอปอ่านไฟล์ `.env`
ให้อัตโนมัติ ส่วนตัวแปรที่ตั้งไว้ใน environment จริงจะถูกใช้ก่อนค่าในไฟล์เสมอ
ตอน deploy ด้วย docker ใช้ `--env-file .env` หรือกำหนดตัวแปรผ่าน orchestrator ตามปกติ

## ย้ายข้อมูลเดิม

ถ้าเคยรันเวอร์ชันที่เก็บไฟล์บนดิสก์ ให้นำค่า calibration ที่ปรับไว้เข้า MongoDB ก่อน:

```bash
python migrate.py
```

สคริปต์อ่าน `calibration.json` แล้วเขียนลง MongoDB และจะไม่เขียนทับถ้ามีข้อมูลอยู่แล้ว
ส่วน `history.json` ไม่ได้ย้ายให้ เพราะข้อมูลสร้างใหม่ได้ภายในวันเดียว และภาพที่อ้างถึง
อยู่บนดิสก์ที่แอปไม่อ่านแล้ว

## สำหรับนักพัฒนา
### ข้อกำหนด

- Python 3.7 ขึ้นไป
- Docker

### Deploy from pre-built Docker images
```bash
docker pull ghcr.io/iaunn/pathumthani-water-level-api
docker run -d -it -p 4050:4050 --env-file .env --name pathumthani-water-level-api ghcr.io/iaunn/pathumthani-water-level-api
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
docker run -d -it -p 4050:4050 --env-file .env --name pathumthani-water-level-api pathumthani-water-level-api
```

### หมายเหตุ

-   API นี้ถูกออกแบบมาเพื่อใช้งานร่วมกับฟีดวิดีโอ CCTV ที่ระบุไว้ในส่วน "แหล่งที่มาของวิดีโอ" เท่านั้น
-   URL ของภาพที่ประมวลผลจะถูกสร้างขึ้นแบบไดนามิกและอาจมีการเปลี่ยนแปลงได้
-   การปรับเทียบระดับน้ำถูกปรับให้เหมาะกับความละเอียดของวิดีโอปัจจุบัน
-   API รองรับการทำงานแบบ fallback เมื่อไม่สามารถตรวจจับสีเหลืองได้
-   ข้อมูลวิดีโอ (ขนาด, framerate, codec) จะถูกแสดงใน console log