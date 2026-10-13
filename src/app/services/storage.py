import mimetypes
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings


@lru_cache
def get_client() -> BaseClient:
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url or None,
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def make_key(job_id: str, filename: str):
    return f"media/{job_id}/{filename}"


def upload_file(path: Path, key: str):
    """Upload a local file and return its size in bytes."""
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    get_client().upload_file(
        str(path),
        get_settings().s3_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )

    return path.stat().st_size


def generate_signed_url(key: str, filename: str | None = None) -> str:
    """Presigned GET URL. Signing is local, so this makes no network call."""
    params = {"Bucket": get_settings().s3_bucket, "Key": key}
    if filename:
        # Makes the browser save the file under a proper name.
        params["ResponseContentDisposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return get_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=get_settings().signed_url_ttl_seconds,
    )


def delete_object(key: str) -> None:
    """Deleting a missing object is a harmless no-op on S3-compatible stores."""
    get_client().delete_object(Bucket=get_settings().s3_bucket, Key=key)


def list_objects(prefix: str = "media/") -> list[tuple[str, datetime]]:
    """Return (key, last_modified) for every object under the prefix."""
    paginator = get_client().get_paginator("list_objects_v2")
    found: list[tuple[str, datetime]] = []
    for page in paginator.paginate(Bucket=get_settings().s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            found.append((obj["Key"], obj["LastModified"]))
    return found


def check_connection() -> bool:
    """Used by /health. True if the bucket is reachable with our credentials."""
    try:
        get_client().head_bucket(Bucket=get_settings().s3_bucket)
        return True
    except (BotoCoreError, ClientError):
        return False
