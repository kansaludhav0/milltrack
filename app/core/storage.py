from datetime import datetime, timezone
from uuid import uuid4

import boto3

from app.core.config import settings

_s3 = boto3.client("s3", region_name=settings.aws_region)


def presign_put(prefix: str, content_type: str = "image/jpeg") -> dict:
    """Return a short-lived S3 PUT URL plus the public photo_url to store on the record."""
    key = f"{prefix}/{datetime.now(timezone.utc):%Y/%m/%d}/{uuid4()}.jpg"
    upload_url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=300,
    )
    photo_url = f"https://{settings.s3_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"
    return {"upload_url": upload_url, "photo_url": photo_url}
