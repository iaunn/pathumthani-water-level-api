"""S3-compatible object storage for camera frames (MinIO, Cloudflare R2, AWS S3)."""

import os
import cv2
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_bucket = None
_public_base = None
_client = None
_jpeg_quality = 85


def _require(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Object storage is required: configure S3_BUCKET, "
            "S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY and S3_PUBLIC_BASE_URL "
            "(plus S3_ENDPOINT_URL for MinIO or R2)."
        )
    return value


def init():
    """Connect to the bucket. Raises if it is unreachable, so a bad deploy fails at boot."""
    global _bucket, _public_base, _client, _jpeg_quality

    _jpeg_quality = int(os.getenv("JPEG_QUALITY", 85))
    _bucket = _require("S3_BUCKET")
    _public_base = _require("S3_PUBLIC_BASE_URL").rstrip("/")

    _client = boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        aws_access_key_id=_require("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=_require("S3_SECRET_ACCESS_KEY"),
        region_name=os.getenv("S3_REGION", "auto"),
        # R2 and MinIO reject the newer streaming checksums boto3 sends by default.
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "path")},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )

    _client.head_bucket(Bucket=_bucket)
    print(f"Object storage ready: bucket={_bucket} public_base={_public_base}")


def public_url(key):
    return f"{_public_base}/{key}"


def upload_frame(frame, key):
    """Encode a BGR frame as JPEG and store it. Returns the public URL."""
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), _jpeg_quality])
    if not ok:
        raise RuntimeError(f"Failed to encode frame for {key}")

    _client.put_object(
        Bucket=_bucket,
        Key=key,
        Body=buf.tobytes(),
        ContentType="image/jpeg",
        CacheControl="public, max-age=31536000, immutable",
    )
    return public_url(key)


def download_frame(key):
    """Read an object back as a decoded BGR frame, or None if it is not there."""
    import numpy as np

    try:
        body = _client.get_object(Bucket=_bucket, Key=key)["Body"].read()
    except ClientError:
        return None

    frame = cv2.imdecode(np.frombuffer(body, dtype="uint8"), cv2.IMREAD_COLOR)
    return frame


def prune_captures(prefix, keep):
    """Delete all but the newest `keep` captures under one station's prefix."""
    paginator = _client.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=_bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))

    if len(objects) <= keep:
        return 0

    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    stale = [{"Key": o["Key"]} for o in objects[keep:]]

    deleted = 0
    for i in range(0, len(stale), 1000):  # delete_objects caps at 1000 keys per call
        batch = stale[i:i + 1000]
        _client.delete_objects(Bucket=_bucket, Delete={"Objects": batch})
        deleted += len(batch)

    print(f"Pruned {deleted} old captures under {prefix}, keeping {keep}")
    return deleted
