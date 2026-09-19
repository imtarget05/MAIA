"""Storage backends for the video pipeline (T2).

Two implementations behind one narrow interface:

- ``LocalStorage``  — folder under ``VID_STORAGE_DIR`` (offline/dev/tests).
- ``MinioStorage``  — S3-compatible via the ``minio`` SDK (integrated mode),
  with Pre-signed URLs for upload/download (security requirement, Giai doan 4).

Import of ``minio`` is deferred so the module works without the dependency.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from maia.video.settings import Settings


class StorageError(RuntimeError):
    """Raised when an upload/download/presign operation fails."""


class Storage(Protocol):
    """Minimal object-storage interface used by the worker and API."""

    def upload(self, local_path: str | Path, key: str) -> str: ...
    def download(self, uri: str, local_path: str | Path) -> Path: ...
    def presign_download(self, key: str, *, expires_sec: int = 900) -> str: ...
    def presign_upload(self, key: str, *, expires_sec: int = 900) -> str: ...
    def delete(self, key: str) -> None: ...
    def list_output_keys(self, job_id: str) -> list[str]: ...


def _bucket_key(uri: str) -> tuple[str | None, str]:
    """Split ``s3://bucket/key`` into (bucket, key); raw key -> (None, key)."""
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        return bucket or None, key
    return None, uri


class LocalStorage:
    """Folder backend rooted at ``VID_STORAGE_DIR`` — offline/dev only."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # prevent path traversal: keys are relative names only
        rel = key.lstrip("/").replace("..", "_")
        return self.root / rel

    def upload(self, local_path: str | Path, key: str) -> str:
        src = Path(local_path)
        dst = self._resolve(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return str(dst)

    def download(self, uri: str, local_path: str | Path) -> Path:
        if uri.startswith("s3://"):
            raise StorageError("local backend cannot download s3:// uris")
        src = Path(uri) if Path(uri).is_absolute() else self._resolve(uri)
        if not src.exists():
            raise StorageError(f"not found in local storage: {uri}")
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return dst

    def presign_download(self, key: str, *, expires_sec: int = 900) -> str:
        # Local backend has no signed URLs; path-based uri only (offline mode).
        _, rel = _bucket_key(key)
        return f"local://{quote(rel)}"

    def presign_upload(self, key: str, *, expires_sec: int = 900) -> str:
        raise NotImplementedError("local backend does not support presigned upload")

    def delete(self, key: str) -> None:
        target = self._resolve(key)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)

    def list_output_keys(self, job_id: str) -> list[str]:
        """All output object keys for a job (dir for HLS, file for MP4)."""
        base = self.root / "outputs" / job_id
        keys: list[str] = []
        if base.is_dir():
            keys.extend(
                str(p.relative_to(self.root))
                for p in sorted(base.rglob("*")) if p.is_file()
            )
        for suffix in (".mp4", ".m4a"):
            f = self.root / "outputs" / f"{job_id}{suffix}"
            if f.is_file():
                keys.append(str(f.relative_to(self.root)))
        return sorted(keys)



class MinioStorage:
    """MinIO / S3-compatible backend (integrated mode)."""

    def __init__(self, settings: Settings) -> None:
        try:
            from minio import Minio  # deferred import
        except ImportError as exc:  # pragma: no cover - env-specific
            raise StorageError(
                "storage_backend='minio' requires the 'minio' package "
                "(pip install minio)"
            ) from exc
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.bucket_in = settings.minio_bucket_in
        self.bucket_out = settings.minio_bucket_out
        for bucket in (self.bucket_in, self.bucket_out):
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

    def upload(self, local_path: str | Path, key: str) -> str:
        src = Path(local_path)
        bucket = self.bucket_out
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    self.client.fput_object(
                        bucket, f"{key}/{f.relative_to(src)}", str(f)
                    )
        else:
            self.client.fput_object(bucket, key, str(src))
        return f"s3://{bucket}/{key}"

    def download(self, uri: str, local_path: str | Path) -> Path:
        bucket, key = _bucket_key(uri)
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.client.fget_object(bucket or self.bucket_in, key, str(dst))
        return dst

    def presign_download(self, key: str, *, expires_sec: int = 900) -> str:
        from datetime import timedelta

        # Accept raw key, `<bucket>/<key>`, or full `s3://bucket/key` uri —
        # the API passes the stored output_uri straight through.
        bucket, k = _bucket_key(key)
        if bucket is None and k.startswith(f"{self.bucket_out}/"):
            k = k[len(self.bucket_out) + 1:]
        return self.client.presigned_get_object(
            bucket or self.bucket_out, k, expires=timedelta(seconds=expires_sec)
        )

    def presign_upload(self, key: str, *, expires_sec: int = 900) -> str:
        from datetime import timedelta

        return self.client.presigned_put_object(
            self.bucket_in, key, expires=timedelta(seconds=expires_sec)
        )

    def delete(self, key: str) -> None:
        from minio.deleteobjects import DeleteObject

        bucket, k = _bucket_key(key)
        b = bucket or self.bucket_out
        objects = [
            DeleteObject(obj.object_name or "")
            for obj in self.client.list_objects(b, prefix=k, recursive=True)
        ]
        for err in self.client.remove_objects(b, objects):
            raise StorageError(f"delete failed: {err}")

    def list_output_keys(self, job_id: str) -> list[str]:
        # prefix without a trailing slash matches both `outputs/<id>.mp4`
        # (single-file outputs) and `outputs/<id>/...` (HLS segment trees).
        return [
            obj.object_name or ""
            for obj in self.client.list_objects(
                self.bucket_out, prefix=f"outputs/{job_id}", recursive=True
            )
            if obj.object_name
        ]


def build_storage(settings: Settings) -> Storage:
    """Factory honoring ``VID_STORAGE_BACKEND``."""
    if settings.storage_backend == "minio":
        return MinioStorage(settings)
    return LocalStorage(settings.storage_dir)
