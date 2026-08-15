from io import BytesIO
from pathlib import Path
from datetime import timedelta
try:
    from minio import Minio
except ImportError:  # local Mock Platform tests do not require MinIO client
    Minio = None
from app.core.config import settings
from app.integrations.secrets import SecretRef, SecretResolver


class ObjectStorage:
    def __init__(self):
        if Minio is None:
            raise RuntimeError("MINIO_CLIENT_UNAVAILABLE")
        access_key = SecretResolver.resolve(
            SecretRef(value=settings.minio_access_key, file=settings.minio_access_key_file, env=settings.minio_access_key_env),
            name="MINIO_ACCESS_KEY", required=True,
        )
        secret_key = SecretResolver.resolve(
            SecretRef(value=settings.minio_secret_key, file=settings.minio_secret_key_file, env=settings.minio_secret_key_env),
            name="MINIO_SECRET_KEY", required=True,
        )
        self.client=Minio(settings.minio_endpoint, access_key=access_key, secret_key=secret_key, secure=settings.minio_secure)
        self.bucket=settings.minio_bucket

    def ensure_bucket(self):
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def _retry_write(self, fn, *, attempts: int = 3, label: str = 'object'):
        """Retry an idempotent MinIO write on transient network errors.

        MinIO writes (analysis results, evidence blobs) can hit transient connection
        drops; a retry keeps the evidence from being lost. Confirmed-immutable writes
        are safe to retry because the object key is content-addressed / unique.
        """
        import time as _t
        last: Exception | None = None
        for i in range(attempts):
            try:
                return fn()
            except Exception as exc:
                last = exc
                if i < attempts - 1:
                    _t.sleep(1.0 * (i + 1))
                    continue
        raise last

    def put_bytes(self, object_key:str, data:bytes, content_type='application/octet-stream'):
        self.ensure_bucket()
        self._retry_write(lambda: self.client.put_object(
            self.bucket, object_key, BytesIO(data), length=len(data), content_type=content_type))

    def put_file(self, object_key:str, file_path:str|Path, content_type='application/octet-stream'):
        self.ensure_bucket()
        self._retry_write(lambda: self.client.fput_object(
            self.bucket, object_key, str(file_path), content_type=content_type))

    def get_to_file(self, object_key:str, file_path:str|Path):
        self.ensure_bucket()
        self.client.fget_object(self.bucket, object_key, str(file_path))

    def get_bytes(self, object_key:str) -> bytes:
        self.ensure_bucket()
        response=self.client.get_object(self.bucket, object_key)
        try:
            return response.read()
        finally:
            response.close(); response.release_conn()


    def iter_object(self, object_key:str, chunk_size:int=1024*1024):
        self.ensure_bucket()
        response=self.client.get_object(self.bucket, object_key)
        try:
            while True:
                chunk=response.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close(); response.release_conn()

    def presigned_get(self, object_key:str, expires:timedelta|None=None):
        self.ensure_bucket()
        return self.client.presigned_get_object(self.bucket, object_key, expires=expires or timedelta(minutes=settings.artifact_url_ttl_minutes))

    def remove(self, object_key: str) -> None:
        self.client.remove_object(self.bucket, object_key)

    def probe(self, *, read_write: bool = False) -> dict:
        """Probe bucket access without exposing configured credentials."""
        self.ensure_bucket()
        result={"status":"ok","endpoint":settings.minio_endpoint,"bucket":self.bucket,"secure":bool(settings.minio_secure)}
        if not read_write:
            return result
        import uuid
        key=f"_health/probe-{uuid.uuid4().hex}.txt"
        payload=b"voip-storage-probe"
        try:
            self.put_bytes(key,payload,"text/plain")
            if self.get_bytes(key) != payload:
                raise RuntimeError("MINIO_PROBE_READ_MISMATCH")
            result["read_write"] = True
            return result
        finally:
            try:
                self.remove(key)
            except Exception:
                pass


class FilesystemObjectStorage:
    """Immutable local object-storage backend for Phase-C Mock Platform tests/dev.

    It implements the ObjectStorage method surface used by the reproduction evidence
    pipeline, but never pretends to be production MinIO. EC-02/production deployment
    can switch the configured backend without changing evidence semantics.
    """
    def __init__(self, root: str|Path|None=None):
        self.root=Path(root or settings.reproduction_object_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        key=Path(str(object_key).lstrip('/'))
        path=(self.root/key).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError('OBJECT_KEY_PATH_INVALID')
        return path

    def ensure_bucket(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, object_key:str, data:bytes, content_type='application/octet-stream'):
        path=self._path(object_key); path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            existing=path.read_bytes()
            if existing != data:
                raise ValueError('IMMUTABLE_OBJECT_CONFLICT')
            return
        tmp=path.with_suffix(path.suffix+'.tmp')
        tmp.write_bytes(data); tmp.replace(path)

    def put_file(self, object_key:str, file_path:str|Path, content_type='application/octet-stream'):
        src=Path(file_path); data=src.read_bytes(); self.put_bytes(object_key,data,content_type)

    def get_to_file(self, object_key:str, file_path:str|Path):
        src=self._path(object_key); dst=Path(file_path); dst.parent.mkdir(parents=True,exist_ok=True); dst.write_bytes(src.read_bytes())

    def get_bytes(self, object_key:str) -> bytes:
        return self._path(object_key).read_bytes()

    def iter_object(self, object_key:str, chunk_size:int=1024*1024):
        with self._path(object_key).open('rb') as fh:
            while True:
                chunk=fh.read(chunk_size)
                if not chunk: break
                yield chunk

    def presigned_get(self, object_key:str, expires:timedelta|None=None):
        # Test/dev representation only; API consumers should not treat this as a public URL.
        return self._path(object_key).as_uri()

    def remove(self, object_key: str) -> None:
        path=self._path(object_key)
        if path.exists(): path.unlink()

    def probe(self, *, read_write: bool = False) -> dict:
        self.ensure_bucket()
        return {"status":"ok","backend":"filesystem","root":str(self.root),"read_write":bool(read_write)}


def reproduction_object_storage():
    mode=str(settings.reproduction_storage_mode).lower()
    if mode=='local':
        return FilesystemObjectStorage()
    if mode=='minio':
        return ObjectStorage()
    raise ValueError(f'REPRODUCTION_STORAGE_MODE_INVALID:{settings.reproduction_storage_mode}')
