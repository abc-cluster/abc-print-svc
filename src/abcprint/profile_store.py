"""Published profile bundles: added and improved without rebuilding the worker.

A profile bundle is a directory — `profile.yaml`, the template it layers into the
workspace, optional OFL fonts, an example — published once as an immutable,
content-addressed archive. The worker image keeps only what *interprets* a
profile (the engines, the toolchain, the zip safety guards and the bundle-rule
validator); what is interpreted lives here and changes by publishing.

    <source>/index.json
    <source>/bundles/<name>/<version>/<hex>.tar.gz

`<source>` is `file:///…` or `s3://bucket/prefix` (ABCPRINT_PROFILE_SOURCE). The
index names, for each profile, its versions and a `default` — the version an
unpinned request gets, moved deliberately with `promote`, never by publishing
alone. A version, once published, never changes: re-publishing it with other
content is refused.

**Profiles contain code** (an extension's Lua filters, a pipeline's scripts), so
only the operator publishes (`python -m abcprint.publish`); a client only ever
names one. The front door resolves a request to an exact `name@version` at
admission; the worker fetches that version's bundle and refuses to build unless
the extracted content hashes to the digest the index recorded. Every run records
`{name, version, digest, source}`.

The digest is of the CONTENT (paths, the executable bit, file bytes), not of the
archive, so the same directory always has the same digest however it was packed
— and a baked profile in an older image gets a comparable digest too.

**Signatures** (0.18.0). The digest proves a bundle is the one the index names;
it does not prove the operator published it — anyone who can write the store
can write the index too. A version may carry an Ed25519 signature over
`statement(name, version, digest)`, made with the operator's key on the
operator's machine (`openssl pkeyutl -sign -rawin`; the private key never
reaches the cluster). Trusted public keys are configuration
(ABCPRINT_PROFILE_TRUSTED_KEYS), and ABCPRINT_PROFILE_SIGNATURES=require makes
the front door refuse to admit, and the worker refuse to build, any version
whose signature is missing or does not verify.

**Aliases.** A version may answer to other names (`aliases:` in profile.yaml),
so a pin recorded before the profile was published — `su-fmhs@git-d0e8657`, or
`@unversioned` — still resolves, to the version built from that same revision.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

SOURCE_VAR = "ABCPRINT_PROFILE_SOURCE"
CACHE_VAR = "ABCPRINT_PROFILE_CACHE"
TTL_VAR = "ABCPRINT_PROFILE_INDEX_TTL_S"
INDEX = "index.json"
INDEX_SCHEMA = 1
# Not part of a profile's content: vendoring metadata that changes on every vendor run.
DIGEST_EXCLUDE = frozenset({"PROVENANCE.json"})
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_FILES = 20000
KEYS_VAR = "ABCPRINT_PROFILE_TRUSTED_KEYS"
SIG_POLICY_VAR = "ABCPRINT_PROFILE_SIGNATURES"


class StoreError(RuntimeError):
    """The store is unreachable, corrupt, or a bundle failed verification."""


class SignatureError(StoreError):
    """A version's signature is missing or wrong, and the policy requires one."""


# ── content digest ───────────────────────────────────────────────────────────

def dir_digest(root: str) -> str:
    """`sha256:<hex>` over every regular file: relative path, executable bit, content.

    Symlinks and other non-regular entries are refused rather than followed: a
    profile is data the operator ships, and a link would make its content depend
    on the machine it is read on.
    """
    h = hashlib.sha256()
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                raise StoreError(f"symlink in profile: {os.path.join(dirpath, d)}")
        for f in filenames:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if rel in DIGEST_EXCLUDE:
                continue
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise StoreError(f"not a regular file in profile: {rel}")
            entries.append((rel, p, bool(st.st_mode & 0o111)))
    for rel, p, exe in sorted(entries):
        fh = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                fh.update(chunk)
        h.update(f"{rel}\0{'x' if exe else '-'}\0{fh.hexdigest()}\n".encode())
    return "sha256:" + h.hexdigest()


def pack(root: str) -> bytes:
    """A deterministic tar.gz of the profile directory (sorted, no owners, mtime 0)."""
    buf = io.BytesIO()
    import gzip
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
            paths = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for f in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
                    if rel not in DIGEST_EXCLUDE:
                        paths.append(rel)
            for rel in sorted(paths):
                p = os.path.join(root, rel)
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    raise StoreError(f"not a regular file in profile: {rel}")
                ti = tarfile.TarInfo(rel)
                ti.size, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = st.st_size, 0, 0, 0, "", ""
                ti.mode = 0o755 if st.st_mode & 0o111 else 0o644
                with open(p, "rb") as fh:
                    tf.addfile(ti, fh)
    return buf.getvalue()


def _safe_unpack(data: bytes, dest: str) -> None:
    """Regular files only, every path relative and inside dest, bounded size.

    Written out by hand rather than with tarfile's extraction filters, which the
    worker image's Python (3.11.2) predates.
    """
    import gzip
    total = files = 0
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as tf:
        for m in tf:
            name = m.name
            pp = PurePosixPath(name)
            if pp.is_absolute() or ".." in pp.parts or "\\" in name or "\0" in name:
                raise StoreError(f"unsafe path in bundle: {name!r}")
            if m.isdir():
                os.makedirs(os.path.join(dest, *pp.parts), exist_ok=True)
                continue
            if not m.isreg():
                raise StoreError(f"non-regular entry in bundle: {name!r}")
            files += 1
            total += m.size
            if files > MAX_BUNDLE_FILES or total > MAX_BUNDLE_BYTES:
                raise StoreError("bundle exceeds the size limits")
            out = os.path.join(dest, *pp.parts)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            src = tf.extractfile(m)
            with open(out, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.chmod(out, 0o755 if m.mode & 0o111 else 0o644)


# ── signatures ───────────────────────────────────────────────────────────────

def statement(name: str, version: str, digest: str) -> bytes:
    """The exact bytes a signature covers. Changing this breaks every signature."""
    return f"abc-print-profile-v1 {name}@{version} {digest}\n".encode()


def key_id(raw_public_key: bytes) -> str:
    return hashlib.sha256(raw_public_key).hexdigest()[:16]


def trusted_keys(spec: str | None = None) -> dict[str, bytes]:
    """`{key_id: raw 32-byte Ed25519 public key}` from ABCPRINT_PROFILE_TRUSTED_KEYS.

    The variable holds base64 raw public keys separated by commas or whitespace
    (`openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64`).
    """
    import base64
    import binascii
    spec = os.environ.get(KEYS_VAR, "") if spec is None else spec
    out = {}
    for tok in spec.replace(",", " ").split():
        try:
            raw = base64.b64decode(tok, validate=True)
        except (binascii.Error, ValueError):
            raise StoreError(f"{KEYS_VAR}: {tok[:12]}… is not base64") from None
        if len(raw) != 32:
            raise StoreError(f"{KEYS_VAR}: {tok[:12]}… is not a 32-byte Ed25519 public key")
        out[key_id(raw)] = raw
    return out


def signature_policy() -> str:
    """off | warn | require. Unset: require when trusted keys are configured."""
    pol = os.environ.get(SIG_POLICY_VAR, "").strip().lower()
    if not pol:
        return "require" if os.environ.get(KEYS_VAR, "").strip() else "off"
    if pol not in ("off", "warn", "require"):
        raise StoreError(f"{SIG_POLICY_VAR} must be off, warn or require; got {pol!r}")
    return pol


def check_signature(name: str, version: str, rec: dict,
                    keys: dict[str, bytes] | None = None) -> str:
    """'valid', 'unsigned', 'untrusted-key' or 'invalid' — never raises for a bad signature."""
    import base64
    sig = rec.get("signature") or {}
    if not sig:
        return "unsigned"
    keys = trusted_keys() if keys is None else keys
    pub = keys.get(sig.get("key_id", ""))
    if pub is None:
        return "untrusted-key"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:                       # pragma: no cover - the images carry it
        raise StoreError("signature checking needs the `cryptography` package") from None
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(
            base64.b64decode(sig.get("sig", "")), statement(name, version, rec["digest"]))
        return "valid"
    except (InvalidSignature, ValueError):
        return "invalid"


def enforce_signature(name: str, version: str, rec: dict) -> str:
    """Apply the policy to one version. Returns the verdict; raises under `require`."""
    pol = signature_policy()
    if pol == "off":
        return "unchecked"
    verdict = check_signature(name, version, rec)
    if verdict != "valid" and pol == "require":
        raise SignatureError(f"{name}@{version}: signature {verdict}; refusing it "
                         f"({SIG_POLICY_VAR}=require)")
    return verdict


# ── sources ──────────────────────────────────────────────────────────────────

class _FileSource:
    def __init__(self, root: str):
        self.root = root
        self.url = "file://" + root

    def get(self, key: str) -> bytes | None:
        p = os.path.join(self.root, key)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as fh:
            return fh.read()

    def put(self, key: str, data: bytes) -> None:
        p = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".put-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)             # atomic: a reader sees the old or the new index


class _S3Source:
    def __init__(self, bucket: str, prefix: str):
        import boto3                    # imported lazily: optional at runtime
        from botocore.config import Config
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.url = f"s3://{bucket}/{self.prefix}" if self.prefix else f"s3://{bucket}"
        self.s3 = boto3.client(
            "s3", endpoint_url=os.environ.get("ABCPRINT_PROFILE_S3_ENDPOINT") or None,
            aws_access_key_id=_secret("ABCPRINT_PROFILE_S3_ACCESS_KEY"),
            aws_secret_access_key=_secret("ABCPRINT_PROFILE_S3_SECRET_KEY"),
            region_name=os.environ.get("ABCPRINT_PROFILE_S3_REGION", "us-east-1"),
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3},
                          connect_timeout=5, read_timeout=30))

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise StoreError(f"profile store {self.url}: {exc}") from None

    def put(self, key: str, data: bytes) -> None:
        ctype = "application/json" if key.endswith(".json") else "application/gzip"
        self.s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=ctype)


def _secret(var: str) -> str | None:
    path = os.environ.get(f"{var}_FILE")
    if path:
        with open(path) as fh:
            return fh.read().strip() or None
    return os.environ.get(var) or None


def open_source(url: str):
    if url.startswith("file://"):
        return _FileSource(url[len("file://"):])
    if url.startswith("s3://"):
        rest = url[len("s3://"):]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise StoreError(f"no bucket in {url!r}")
        return _S3Source(bucket, prefix)
    raise StoreError(f"{SOURCE_VAR} must be file://… or s3://…; got {url!r}")


# ── the store ────────────────────────────────────────────────────────────────

@dataclass
class Resolved:
    name: str
    version: str
    digest: str
    key: str
    entry: dict           # the index entry for this version (descriptor, bundle rules, …)
    source: str
    signature: str = "unchecked"   # valid | unsigned | untrusted-key | invalid | unchecked


class Store:
    def __init__(self, source_url: str, cache_dir: str | None = None, ttl_s: float = 60.0):
        self.src = open_source(source_url)
        self.cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), "abcprint-profile-cache")
        self.ttl_s = ttl_s
        self._index: dict | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return self.src.url

    def index(self, *, fresh: bool = False) -> dict:
        with self._lock:
            if fresh or self._index is None or time.monotonic() - self._loaded_at > self.ttl_s:
                raw = self.src.get(INDEX)
                idx = json.loads(raw) if raw else {"schema": INDEX_SCHEMA, "profiles": {}}
                if idx.get("schema") != INDEX_SCHEMA:
                    raise StoreError(f"profile index schema {idx.get('schema')!r} is not {INDEX_SCHEMA}")
                self._index, self._loaded_at = idx, time.monotonic()
            return self._index

    def names(self) -> list[str]:
        return sorted(self.index()["profiles"])

    def resolve(self, ref: str, *, fresh: bool = False) -> Resolved | None:
        """`name` → its default version; `name@version` → that version.

        None when the store does not know the name, or does not hold the pinned
        version, so a baked profile can still answer (an older image's pin).
        """
        name, _, want = ref.partition("@")
        entry = self.index(fresh=fresh)["profiles"].get(name)
        if entry is None:
            return None
        version = want or entry.get("default")
        v = entry["versions"].get(version) if version else None
        if v is None and want:
            # A pin recorded under another name for the same content (aliases:).
            for real, rec in sorted(entry["versions"].items()):
                if want in ((rec.get("descriptor") or {}).get("aliases") or []):
                    version, v = real, rec
                    break
        if v is None and want and not fresh:
            # Published moments ago? Look once more before giving up.
            return self.resolve(ref, fresh=True)
        if v is None:
            return None
        try:
            verdict = enforce_signature(name, version, v)
        except SignatureError:
            if fresh:
                raise
            # Signed moments ago (publish-profile.sh --sign-only)? The cached index
            # may predate it: look once more before refusing.
            return self.resolve(ref, fresh=True)
        return Resolved(name, version, v["digest"], v["key"], v, self.src.url, verdict)

    def materialize(self, r: Resolved) -> str:
        """The bundle's directory in the local cache, fetched and verified once."""
        hexd = r.digest.split(":", 1)[1]
        final = os.path.join(self.cache_dir, hexd)
        if os.path.isdir(final):
            return final
        os.makedirs(self.cache_dir, exist_ok=True)
        data = self.src.get(r.key)
        if data is None:
            raise StoreError(f"bundle for {r.name}@{r.version} is missing at {r.key}")
        tmp = tempfile.mkdtemp(dir=self.cache_dir, prefix=".fetch-")
        try:
            _safe_unpack(data, tmp)
            got = dir_digest(tmp)
            if got != r.digest:
                raise StoreError(f"bundle for {r.name}@{r.version} hashes to {got}, "
                                 f"but the index says {r.digest}; refusing to build with it")
            try:
                os.replace(tmp, final)
            except OSError:
                if not os.path.isdir(final):   # anything but "another run got there first"
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return final

    # ── publishing (operator only; see abcprint.publish) ───────────────────────

    def publish(self, root: str, descriptor: dict, *, promote: bool,
                signature: dict | None = None) -> Resolved:
        name, version = descriptor["name"], descriptor["version"]
        digest = dir_digest(root)
        if signature is not None:
            verdict = check_signature(name, version, {"digest": digest, "signature": signature})
            if verdict != "valid":
                raise StoreError(f"{name}@{version}: the signature given is {verdict} for "
                                 f"{digest}; nothing published")
        elif signature_policy() == "require":
            raise StoreError(f"{name}@{version}: unsigned, and {SIG_POLICY_VAR}=require; "
                             f"nothing published")
        idx_now = self.index(fresh=True)["profiles"].get(name) or {}
        for real, rec in (idx_now.get("versions") or {}).items():
            clash = set(descriptor.get("aliases") or []) & (
                {real} | set((rec.get("descriptor") or {}).get("aliases") or []))
            if real != version and clash:
                raise StoreError(f"{name}@{version}: alias {sorted(clash)} already names "
                                 f"{name}@{real}")
        hexd = digest.split(":", 1)[1]
        key = f"bundles/{name}/{version}/{hexd}.tar.gz"
        idx = self.index(fresh=True)
        entry = idx["profiles"].setdefault(name, {"default": None, "versions": {}})
        have = entry["versions"].get(version)
        if have is not None:
            if have["digest"] != digest:
                raise StoreError(
                    f"{name}@{version} is already published with {have['digest']}; a version "
                    f"never changes — publish this content as a new version")
            changed = False
            if signature is not None and have.get("signature") != signature:
                have["signature"] = signature        # verified above, over this same digest
                changed = True
            if promote and entry.get("default") != version:
                entry["default"] = version
                changed = True
            if changed:
                self._write_index(idx)
            return Resolved(name, version, digest, have["key"], have, self.src.url)
        self.src.put(key, pack(root))
        # Read back and verify before the index points at it.
        check = tempfile.mkdtemp(prefix="abcprint-verify-")
        try:
            _safe_unpack(self.src.get(key) or b"", check)
            if dir_digest(check) != digest:
                raise StoreError("the uploaded bundle does not verify; index left unchanged")
        finally:
            shutil.rmtree(check, ignore_errors=True)
        rec = {"digest": digest, "key": key,
               "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "engine": descriptor["engine"], "schema_version": descriptor.get("schema_version", 1),
               "descriptor": descriptor}
        if signature is not None:
            rec["signature"] = signature
        entry["versions"][version] = rec
        if promote or not entry.get("default"):
            entry["default"] = version
        self._write_index(idx)
        return Resolved(name, version, digest, key, rec, self.src.url)

    def mirror_into(self, r: Resolved, mirror: "Store") -> None:
        """Copy one version into a mirror the worker can read without a network.

        The worker job runs with no network at all, so it cannot reach the store.
        The front door (which can) copies each version it admits a run for into a
        `file://` mirror on the volume both share; the worker resolves and
        verifies against that. The mirror holds only versions that were admitted.
        """
        with mirror._lock:
            raw = mirror.src.get(INDEX)
            idx = json.loads(raw) if raw else {"schema": INDEX_SCHEMA, "profiles": {}}
            entry = idx["profiles"].setdefault(r.name, {"default": None, "versions": {}})
            have = entry["versions"].get(r.version)
            if have is not None and have["digest"] == r.digest and mirror.src.get(r.key) is not None:
                fresh_sig = r.entry.get("signature")
                if (entry.get("default") is not None and have.get("source")
                        and have.get("signature") == fresh_sig):
                    return
                have.setdefault("source", r.entry.get("source") or self.src.url)
                if fresh_sig:
                    have["signature"] = fresh_sig      # signed after it was first mirrored
                entry["default"] = entry.get("default") or r.version
            else:
                data = self.src.get(r.key)
                if data is None:
                    raise StoreError(f"bundle for {r.name}@{r.version} is missing at {r.key}")
                mirror.src.put(r.key, data)
                # The mirror is a copy; a run must still record where the
                # version was published, not the copy it was read from.
                entry["versions"][r.version] = {**r.entry, "source": r.entry.get("source") or self.src.url}
            store_default = self.index()["profiles"].get(r.name, {}).get("default")
            if store_default == r.version or entry.get("default") is None:
                entry["default"] = r.version
            idx["schema"] = INDEX_SCHEMA
            mirror.src.put(INDEX, json.dumps(idx, indent=1, sort_keys=True).encode())
            mirror._index = None

    def sign(self, name: str, version: str, signature: dict) -> None:
        """Attach a signature to a version already published (it covers the digest,
        which never changes, so signing later is as good as signing at publish)."""
        idx = self.index(fresh=True)
        rec = ((idx["profiles"].get(name) or {}).get("versions") or {}).get(version)
        if rec is None:
            raise StoreError(f"{name}@{version} is not published")
        verdict = check_signature(name, version, {**rec, "signature": signature})
        if verdict != "valid":
            raise StoreError(f"{name}@{version}: the signature given is {verdict}; not attached")
        rec["signature"] = signature
        self._write_index(idx)

    def promote(self, name: str, version: str) -> None:
        idx = self.index(fresh=True)
        entry = idx["profiles"].get(name)
        if entry is None or version not in entry["versions"]:
            raise StoreError(f"{name}@{version} is not published")
        entry["default"] = version
        self._write_index(idx)

    def _write_index(self, idx: dict) -> None:
        idx["schema"] = INDEX_SCHEMA
        idx["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.src.put(INDEX, json.dumps(idx, indent=1, sort_keys=True).encode())
        with self._lock:
            self._index, self._loaded_at = idx, time.monotonic()


MIRROR_VAR = "ABCPRINT_PROFILE_MIRROR"

_DEFAULT: Store | None = None
_DEFAULT_URL: str | None = None
_MIRROR: Store | None = None


def mirror() -> Store | None:
    """The front door's mirror for the worker (ABCPRINT_PROFILE_MIRROR); None when unset."""
    global _MIRROR
    url = os.environ.get(MIRROR_VAR, "").strip()
    if not url:
        return None
    if _MIRROR is None or _MIRROR.url != url:
        _MIRROR = Store(url, ttl_s=0)
    return _MIRROR


def default() -> Store | None:
    """The store this process uses, from ABCPRINT_PROFILE_SOURCE; None when unset."""
    global _DEFAULT, _DEFAULT_URL
    url = os.environ.get(SOURCE_VAR, "").strip()
    if not url:
        return None
    if _DEFAULT is None or _DEFAULT_URL != url:
        _DEFAULT = Store(url, cache_dir=os.environ.get(CACHE_VAR) or None,
                         ttl_s=float(os.environ.get(TTL_VAR, "60")))
        _DEFAULT_URL = url
    return _DEFAULT
