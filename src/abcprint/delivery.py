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


def _strict_component(name: str, what: str) -> str:
    """One path component, REFUSING anything that is not already one.

    _safe_component takes the basename, so `a/b` silently became `b` and
    `../bob` silently became `bob`. Neither escapes — containment was never the
    problem — but a caller who asks for `a/b` and gets `b` has no way to tell,
    and two callers asking for different nested folders would collide in one.
    Everywhere else this service refuses input it cannot honour rather than
    quietly reinterpreting it; this now does too.
    """
    raw = (name or "").strip()
    if not raw or raw in (".", "..") or "/" in raw or "\\" in raw or "\x00" in raw:
        raise DeliveryError(
            f"{what} must be a single path component, not {name!r}")
    return _safe_component(raw)


def owner_prefix(owner: str) -> str:
    """The one path component a caller may write under. Also the minio key root.

    Delivery had no per-caller namespace. Two tokens delivering `mythesis.pdf`
    wrote the same path, and the second silently replaced the first — measured
    2026-10-19 with two different documents and two different checksums, leaving
    one file and no error. Job isolation was fixed without this, which left
    delivery as the way out of it.
    """
    return _safe_component(owner or "anonymous")


def to_downloads(src: str, filename: str, subdir: str | None = None,
                 owner: str = "anonymous") -> dict:
    """Copy into the mounted downloads directory, under the caller's own folder."""
    if not os.path.isdir(DOWNLOADS_DIR):
        raise DeliveryError(
            f"downloads directory {DOWNLOADS_DIR} is not mounted. Run the service with "
            f"-v \"$HOME/Downloads\":{DOWNLOADS_DIR} to enable this destination.")
    # Every caller gets a folder of their own. A client-supplied subdir nests
    # INSIDE it, so no value of subdir can reach another caller's files.
    target_dir = os.path.join(DOWNLOADS_DIR, owner_prefix(owner))
    if subdir:
        target_dir = os.path.join(target_dir, _strict_component(subdir, "subdir"))
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, _strict_component(filename, "filename"))
    if os.path.commonpath([os.path.abspath(DOWNLOADS_DIR), os.path.abspath(dest)]) \
            != os.path.abspath(DOWNLOADS_DIR):
        raise DeliveryError("destination escapes the downloads directory")
    shutil.copy2(src, dest)
    return {"destination": "downloads", "path": dest, "bytes": os.path.getsize(dest)}


# When set, the ONLY bucket a caller may name. Unset means any bucket the
# service's credentials can reach, which is appropriate for a local run and not
# for a shared deployment — so a shared deployment should set it.
ALLOWED_BUCKET = os.environ.get("ABCPRINT_S3_BUCKET", "")


def minio_key_for(owner: str, key: str) -> str:
    """Validate a client-chosen key against the caller's own prefix.

    `bucket` and `key` arrived wholly from the client, so a token holding the
    `deliver` scope could write anywhere the service's credentials reached —
    including another user's prefix. The module docstring claimed this wrote "to
    the user's home prefix and nowhere else"; nothing enforced it.

    The key is REFUSED rather than silently relocated. Quietly rewriting a
    caller's key would mean the uri in the response is not the one they asked
    for, and they would have no way to tell that from success.
    """
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise DeliveryError(f"invalid key {key!r}")
    required = f"users/{owner_prefix(owner)}/"
    if not key.startswith(required):
        raise DeliveryError(
            f"key must begin with {required!r} — a token may only write under its "
            f"own prefix. Asked for {key!r}.")
    return key


def to_minio(src: str, bucket: str, key: str, *, owner: str = "anonymous",
             endpoint: str | None = None,
             access_key: str | None = None, secret_key: str | None = None,
             secure: bool | None = None) -> dict:
    """Put the artefact at s3://<bucket>/<key>, inside the caller's own prefix.

    Credentials come from the environment by default so they are not carried in
    a request body; a caller may override per-request when the service is
    brokering for a user whose identity differs from the service's own.
    """
    # The REQUEST is validated before the DEPLOYMENT is. Checking credentials
    # first meant a caller who asked to write outside their own prefix was told
    # "object-store delivery is not configured" — true, unrelated, and it would
    # have sent them to an operator instead of to their own key. It also hid the
    # prefix guard from any end-to-end test on an unconfigured deployment.
    if not bucket:
        raise DeliveryError("a bucket is required")
    if ALLOWED_BUCKET and bucket != ALLOWED_BUCKET:
        raise DeliveryError(
            f"this deployment only delivers to bucket {ALLOWED_BUCKET!r}; "
            f"asked for {bucket!r}")
    key = minio_key_for(owner, key)

    endpoint = endpoint or os.environ.get("ABCPRINT_S3_ENDPOINT", "")
    access_key = access_key or os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not (endpoint and access_key and secret_key):
        raise DeliveryError(
            "object-store delivery is not configured: set ABCPRINT_S3_ENDPOINT, "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or pass them in the request.")

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
