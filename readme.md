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
| `CALIBRATE_PASSWORD` | รหัสผ่านของหน้า `/calibrate` และทุก endpoint ที่เขียนค่า **ไม่ตั้ง = ปิดหน้านั้นทั้งหมด (ตอบ 503)** ไม่ใช่เปิดทิ้งไว้ |
| `CALIBRATE_USER` | ชื่อผู้ใช้คู่กับรหัสผ่านข้างบน (ค่าเริ่มต้น `admin`) |
| `HOMOGRAPHY_ENABLED` | `false` เพื่อปิดการจัดแนว homography ใช้ภาพตรง ๆ เหมาะกับกล้องที่ติดตายตัว ไม่ต้อง warp ทุกเฟรมและไม่อัป baseline (ค่าเริ่มต้น `true`) |

สถานีกำหนดใน `stations.json` รับกล้องสองแบบ: `playlist_url` สำหรับ HLS และ
`snapshot_url` สำหรับกล้องที่ให้ภาพนิ่ง JPEG (เช่น Axis `/jpg/image.jpg`) ซึ่งรองรับ
`auth: digest` หรือ `basic` โดย**รหัสผ่านอยู่ใน environment ไม่ใช่ในไฟล์นี้** เพราะไฟล์นี้อยู่ใน git
กล้องภาพนิ่งจะถูกดึงหลายครั้งติดกันตอนวัด เพราะการแยกน้ำออกจากเงาสะท้อนต้องใช้หลายเฟรม
เฟรมที่ซ้ำกันจะถูกตัดทิ้ง และถ้ากล้องไม่รีเฟรชจริงก็จะหยุดขอ (ของรังสิตรีเฟรชทุก 62 วินาที
ขอ 6 ครั้งใน 2.4 วินาทีจึงได้ JPEG เดิมทั้งหมด) การแก้เงาสะท้อนด้วยการเคลื่อนไหวต้องมีอย่างน้อยสองเฟรมที่ต่างกัน
สถานีที่ยังไม่ได้ calibrate จะไม่บันทึกค่า และ API จะตอบ 409 แทนการเดาระดับน้ำ

**หน้า `/calibrate` และการบันทึกค่าต้องใส่รหัสผ่าน** (HTTP Basic ตั้งใน `CALIBRATE_PASSWORD`)
ป้องกันแค่หน้าเว็บอย่างเดียวไม่พอ เพราะ endpoint ที่บันทึกจริงอ่านได้จาก JavaScript ในหน้า
สิ่งที่ถูกล็อกคือ หน้า `/calibrate`, `POST` ของ `/api/<station>/calibration`, `/markers`, `/roi`,
`/api/<station>/calibration/frame` (ตัวนี้เปลี่ยน baseline ของ homography ที่ค่าที่วัดได้ทุกค่าอิงอยู่)
และ `/debug` กับ `/debug-water` (เขียนไฟล์ภาพลง bucket ทุกครั้งที่เรียก)
ส่วน `GET` ของ calibration และ markers ยังเปิดอยู่ เพราะหน้า dashboard ใช้วาดเส้นระดับเตือน
**ถ้าไม่ตั้ง `CALIBRATE_PASSWORD` ทุกเส้นทางข้างบนจะตอบ 503 ไม่ใช่เปิดให้เข้าได้**
รหัสผ่านส่งแบบ base64 ไม่ได้เข้ารหัส จึงควรให้บริการผ่าน HTTPS

**พื้นที่ตรวจจับ (ROI)** ของแต่ละสถานีเป็นกล่องสี่ขอบ — ซ้าย ขวา บน ล่าง — นับพิกเซลจากมุมซ้ายบนของเฟรม
ตัวตรวจจับจะมองเฉพาะinsideกล่องนี้ ขอบที่ไม่ระบุ (ค่าเริ่มต้นจาก `stations.json`) หมายถึง "一直到ขอบภาพ"
แก้จากหน้า `/calibrate` ได้ทั้งการลากขอบบนภาพหรือพิมพ์พิกเซล แล้วกด Save ค่าจะลง MongoDB
(collection `station_config`) แทนค่าในไฟล์ ทำให้ปรับกล้องได้ทันทีโดยไม่ต้อง commit และ replica อื่นจะอ่านค่าใหม่
ในการตรวจรอบถัดไป (ทุก 5 นาที) ปุ่ม `Reset to config` คืนค่ากลับเป็นค่าใน `stations.json`

**สีของมาตรวัด (`staff_paint`)** บอกไว้ในแต่ละสถานีว่าตัวตรวจจับพึ่งอะไรได้ที่จุดนั้น
`"yellow"` = มาตรวัดทาสีเหลืองที่มาสก์สีจับได้ ถ้ารอบไหนหาสีเหลืองไม่เจอจะ**ไม่บันทึก**
ไม่ตกไปใช้เท็กซ์เจอร์เพื่อวัดอย่างอื่นในกล่องแทน `"none"` = มาตรวัดไม่มีสีที่แยกจากฉากหลังได้
(มาตรวัดขาว-แดงใต้สะพานทั้งสองตัวแดงพอ ๆ กับน้ำขุ่น และซีดพอ ๆ กับคอนกรีต) จึงอ่านจากขีดแบนอย่างเดียว
ถ้าไม่ระบุ ตัวตรวจจับจะตัดสินเองทีละเฟรมเหมือนเดิม

**น้ำนิ่งที่สะท้อนเป็นกระจก (`still_water`)** เปิดเฉพาะสถานีที่น้ำนิ่งจนสะท้อนมาตรวัด (ตอนนี้คือรังสิต)
ที่นั่นเท็กซ์เจอร์หาผิวน้ำไม่ได้เลย จึงอ่านจากจุดที่สีจบแทน **ที่อื่นต้องปิดไว้**
เพราะมาสก์สีใช้เกณฑ์ความสว่างแบบตายตัว ซึ่งจริง ๆ แล้วเป็นการสมมติเรื่องแสง
เช้าวันที่ 25 ก.ย. ที่ปทุมธานี ฟ้าหม่นจนมาตรวัดเองเหลือ V p50 = 165 เทียบกับเกณฑ์ 200
มาสก์จึงขาดกลางมาตรวัดที่ยังมองเห็นชัด และอ่านสูงกว่าผิวน้ำจริง 246 px = 1.5 เมตร
รายงานออกมาเป็นน้ำขึ้นทั้งที่ระดับไม่ขยับ (จะผ่อนเกณฑ์ก็ไม่ได้ เพราะเงาสะท้อนของรังสิตจะผ่านทะลุไป 250 px)
จุดที่สีจบมีค่าเฉพาะที่ที่เท็กซ์เจอร์ใช้ไม่ได้ และไม่มีค่าเลยที่ที่เท็กซ์เจอร์ใช้ได้

**ตัวตรวจจับ** อ่านระดับน้ำจากเท็กซ์เจอร์ของมาตรวัด (ขีดแบนดำ) ที่ยุบตัวลงตรงผิวน้ำ
แล้วตรวจสอบด้วยสีของมาตรวัด — ค่าจะผ่านก็ต่อเมื่อสีของมาตรวัดจบตรงจุดนั้นและไม่มีต่อด้านล่าง
เงาสะท้อนไม่ผ่านมาสก์ (ความอิ่มตัว p50 = 84 เทียบกับตัวมาตรวัด 226) แม้ตอนฝนตกที่สีมาตรวัด
ซีดจนหลุดเกณฑ์ความอิ่มตัว (p50 = 79) แต่ก็ยังสว่างกว่าเงา (V p50 254 เทียบกับ 144)
จึงใช้ความสว่างประกอบด้วย

**น้ำนิ่งที่สะท้อนเป็นกระจก** เท็กซ์เจอร์ช่วยไม่ได้เลย เพราะเงาสะท้อนพาขีดแบนลงไปด้วย
(วัดคร่อมผิวน้ำที่รังสิตได้ 127 บนมาตรวัด เทียบกับ 102-128 บนเงาใต้ผิวน้ำ) แต่**สีไม่สะท้อน**
มาสก์สีเหลืองครอบคลุม 70% ของความกว้างคอลัมน์บนมาตรวัด เทียบกับ 4% บนน้ำใต้มาตรวัด
จุดที่สีจบจึงคือผิวน้ำ การสแกนถูกจำกัดไม่ให้เลยจุดนั้นไปไกล (เผื่อไว้เท่ากับ 15% ของความยาวมาตรวัด
เพราะสีใต้น้ำมีตะกอนเกาะจนจบสูงกว่าจริงได้ 10-15 ซม.) และถ้าสแกนไม่เจอจุดยุบตัวเลย จะใช้จุดที่สีจบเป็นคำตอบ

**มาตรวัดที่ไม่มีสี** ไม่มีอะไรให้ตรวจทานคำตอบ เหลือแค่คำถามว่าขีดแบนอ่านออกจริงไหม
กล้องใต้สะพานตัวเดียวกันวัดได้ 30-34 ระดับความต่างต่อแถวตอนกลางวัน แต่เหลือ 5-12 หลังมืด
และค่าที่อ่านได้ก็พังตามไปด้วย (เกิน 15 ขึ้นไป 12 ค่าอยู่ในช่วง 25 px = 14 ซม.
ส่วนที่ต่ำกว่า 10 ค่ากระจาย 125 px = 0.71 ม.) ต่ำกว่าเกณฑ์จึงไม่บันทึก

เมื่อตรวจสอบไม่ผ่าน หรือในกล่องไม่มีมาตรวัดเลย
ระบบจะ**ไม่บันทึกค่า**แทนการเดา และ `/status` กับหน้า `/calibrate` บอกโหมดที่ใช้ผ่าน `detection_mode`:
`gauge` = สี + เท็กซ์เจอร์, `blind` = เท็กซ์เจอร์อย่างเดียว (มาตรวัดขาว-แดง หรือภาพกลางคืน),
`none` = รอบนั้นไม่บันทึก

หน้าเว็บเปิดมาที่ 24 ชั่วโมงล่าสุด เลือกเป็น 7 วัน / 30 วัน / 3 เดือน หรือกำหนดวันเองก็ได้
ช่วงที่ยาวกว่าสองสามวันจะถูกเฉลี่ยเป็นช่วง ๆ ก่อนส่งให้เบราว์เซอร์ (3 ปีเต็มคือ ~315,000 แถว)
โดยแต่ละจุดพ่วงค่าต่ำสุด-สูงสุดจริงในช่วงนั้นไปด้วย ตัวเลข "สูงสุด/ต่ำสุด" จึงไม่ถูกการเฉลี่ยกลบ

**แก้ค่าย้อนหลังจากภาพที่เก็บไว้** ทุกค่าที่บันทึกจะเก็บ URL ของภาพที่ใช้อ่านไว้ด้วย
เมื่อแก้ตัวตรวจจับแล้วจึงย้อนกลับไปอ่านใหม่ได้ `recompute.py` ดึงภาพเดิมมารันใหม่แล้วเขียนทับค่าใน MongoDB
ภาพที่เก็บคือภาพหลังจัดแนว homography แล้ว จึงอ่านซ้ำได้ตรง ๆ ไม่ต้องจัดแนวอีกรอบ
แต่ทุกอย่างที่สถานีใช้ตอนนี้ (calibration, ROI, `staff_paint`, `still_water`) จะถูกใช้ใหม่หมด
ถ้าย้าย ROI หลังจากถ่ายภาพนั้นไปแล้ว ผลที่ได้จะต่างออกไปด้วยเหตุผลที่ไม่เกี่ยวกับโค้ด

```bash
python recompute.py pathumthani --from 2026-09-25T06:00            # ดูก่อนว่าจะเปลี่ยนอะไร
python recompute.py pathumthani --from 2026-09-25T06:00 --apply    # เขียนจริง
python recompute.py --all --from 2026-09-25 --apply --on-decline delete
```

**ไม่ใส่ `--apply` จะไม่เขียนอะไรเลย** ส่วนภาพที่ตัวตรวจจับรอบใหม่ไม่ยอมอ่าน (`declines`)
ค่าเดิมจะถูกทิ้งไว้เฉย ๆ เว้นแต่สั่ง `--on-decline delete` ให้ลบ
เก็บไว้ = ตัวเลขที่ไม่มีอะไรรองรับยังอยู่บนกราฟ ลบ = เหลือเป็นช่องว่าง ซึ่งคือสิ่งที่ tracker จะทิ้งไว้เองอยู่แล้ว
ไม่มีอันไหนถูกเสมอ จึงไม่ตั้งค่าเริ่มต้นแบบเงียบ ๆ ค่าที่เขียนใหม่จะมี `recomputed_at` ติดไว้

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