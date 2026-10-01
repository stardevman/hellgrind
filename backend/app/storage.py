"""S3 uyumlu depolama (Kademe 1'de MinIO, sonra AWS S3)."""
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings


@lru_cache(maxsize=1)
def client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )


def ensure_bucket() -> None:
    try:
        client().head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        client().create_bucket(Bucket=settings.s3_bucket)


def put_file(local_path: str, key: str, content_type: str = "video/mp4") -> str:
    client().upload_file(local_path, settings.s3_bucket, key, ExtraArgs={"ContentType": content_type})
    return key


def download(key: str, local_path: str) -> str:
    client().download_file(settings.s3_bucket, key, local_path)
    return local_path


def exists(key: str) -> bool:
    try:
        client().head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError:
        return False


def delete(key: str) -> None:
    client().delete_object(Bucket=settings.s3_bucket, Key=key)


def copy(src_key: str, dst_key: str) -> None:
    client().copy_object(Bucket=settings.s3_bucket, Key=dst_key,
                         CopySource={"Bucket": settings.s3_bucket, "Key": src_key})


def purge(key: str) -> str | None:
    """Elenen dosyayı imha eder. quarantine modunda önce karantinaya kopyalar."""
    if settings.purge_mode == "quarantine":
        q = f"quarantine/{key}"
        copy(key, q)
        delete(key)
        return q
    delete(key)
    return None
