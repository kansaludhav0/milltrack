from datetime import datetime, timezone
from uuid import uuid4

import boto3

from app.core.config import settings

_s3 = boto3.client("s3", region_name=settings.aws_region)


def presign_put(prefix: str, content_type: str = "image/jpeg") -> dict:
    """Return a short-lived S3 PUT URL plus the public photo_url to store on the record."""
    key = f"images/{prefix}/{datetime.now(timezone.utc):%Y/%m/%d}/{uuid4()}.jpg"
    upload_url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=300,
    )
    photo_url = f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"
    return {"upload_url": upload_url, "photo_url": photo_url}


def upload_bytes(prefix: str, filename: str, data: bytes, content_type: str = "application/pdf") -> str:
    """Direct server-side upload, not a presigned client PUT — for when the server
    already holds the bytes in hand (a generated PDF), rather than a client photo."""
    key = f"documents/{prefix}/{datetime.now(timezone.utc):%Y/%m/%d}/{filename}"
    _s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
    return f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"


def upload_fixed(key: str, data: bytes, content_type: str) -> str:
    """Upload to a FIXED key, overwriting whatever was there before — for a singleton
    asset like the latest APK build, unlike upload_bytes's date-partitioned, ever-growing
    keys used for a history of distinct documents."""
    _s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
    return f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"


def presign_get(photo_url: str, expires_in: int = 900) -> str:
    """Turn a stored photo_url back into a short-lived signed GET URL — the bucket is
    private, so the plain photo_url on a record is never viewable on its own. Longer
    default than presign_put's: this signs URLs embedded in list responses that may sit
    on screen a while, not a one-shot upload used the instant it's issued."""
    host_prefix = f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/"
    if not photo_url.startswith(host_prefix):
        raise ValueError("photo_url is not in this bucket")
    key = photo_url[len(host_prefix):]
    return _s3.generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=expires_in
    )
