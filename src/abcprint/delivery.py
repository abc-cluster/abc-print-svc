"""Where a built artefact goes.

Two destinations, both chosen by the caller:

  minio      the user's own prefix in object storage. The ABC bucket layout puts
             a user's durable output under users/<pseudonym>/, and pipelines'
             execution areas are runner-only, so this writes to the user's home
             prefix and nowhere else.
  downloads  a host directory mounted into the container. The service cannot
             reach a laptop's ~/Downloads on its own; the operator mounts one and
             the service writes there.

Password-protecting the PDF is explicitly deferred. It is noted here because the
delivery boundary is where it will land, and because "encrypted at rest" and
"password on the file" are different claims that should not get conflated later.
"""
from __future__ import annotations

import os
import shutil

DOWNLOADS_DIR = os.environ.get("ABCPRINT_DOWNLOADS", "/srv/downloads")


class DeliveryError(RuntimeError):
    pass


def _safe_component(name: str) -> str:
    base = os.path.basename(name or "")
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        raise DeliveryError(f"unsafe name: {name!r}")
    return base


def to_downloads(src: str, filename: str, subdir: str | None = None) -> dict:
    """Copy into the mounted downloads directory."""
    if not os.path.isdir(DOWNLOADS_DIR):
        raise DeliveryError(
            f"downloads directory {DOWNLOADS_DIR} is not mounted. Run the service with "
            f"-v \"$HOME/Downloads\":{DOWNLOADS_DIR} to enable this destination.")
    target_dir = DOWNLOADS_DIR
    if subdir:
        target_dir = os.path.join(DOWNLOADS_DIR, _safe_component(subdir))
        os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, _safe_component(filename))
    if os.path.commonpath([os.path.abspath(DOWNLOADS_DIR), os.path.abspath(dest)]) \
            != os.path.abspath(DOWNLOADS_DIR):
        raise DeliveryError("destination escapes the downloads directory")
    shutil.copy2(src, dest)
    return {"destination": "downloads", "path": dest, "bytes": os.path.getsize(dest)}


def to_minio(src: str, bucket: str, key: str, *, endpoint: str | None = None,
             access_key: str | None = None, secret_key: str | None = None,
             secure: bool | None = None) -> dict:
    """Put the artefact at s3://<bucket>/<key>.

    Credentials come from the environment by default so they are not carried in
    a request body; a caller may override per-request when the service is
    brokering for a user whose identity differs from the service's own.
    """
    endpoint = endpoint or os.environ.get("ABCPRINT_S3_ENDPOINT", "")
    access_key = access_key or os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not (endpoint and access_key and secret_key):
        raise DeliveryError(
            "object-store delivery is not configured: set ABCPRINT_S3_ENDPOINT, "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or pass them in the request.")
    if not bucket or key.startswith("/") or ".." in key.split("/"):
        raise DeliveryError(f"invalid bucket/key: {bucket!r}/{key!r}")

    try:
        import boto3  # imported lazily: object storage is optional at runtime
        from botocore.config import Config
    except ImportError as exc:
        raise DeliveryError(f"boto3 is not installed in this image: {exc}")

    if secure is None:
        secure = endpoint.startswith("https://")
    client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        # MinIO needs path-style addressing; virtual-host style assumes DNS per bucket.
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"))
    try:
        client.upload_file(src, bucket, key)
    except Exception as exc:
        raise DeliveryError(f"upload to s3://{bucket}/{key} failed: {exc}")
    return {"destination": "minio", "uri": f"s3://{bucket}/{key}",
            "endpoint": endpoint, "bytes": os.path.getsize(src)}


def downloads_available() -> bool:
    return os.path.isdir(DOWNLOADS_DIR)


def minio_configured() -> bool:
    return bool(os.environ.get("ABCPRINT_S3_ENDPOINT")
                and os.environ.get("AWS_ACCESS_KEY_ID")
                and os.environ.get("AWS_SECRET_ACCESS_KEY"))
