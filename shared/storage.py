"""
shared/storage.py — Object Storage Backend
===========================================

In dev (MINIO_ENABLED=False): files stored locally via Django's default FileSystemStorage.
In production (MINIO_ENABLED=True): files stored in MinIO (S3-compatible, Ghana-resident).

TODO: Configure django-storages S3 backend for production.
Reference: NBES System Architecture §11.1 — MinIO / S3-compatible object storage
"""

from django.conf import settings


def get_storage_backend():
    """
    Returns the appropriate storage backend based on settings.
    TODO: Return MinIO-backed S3 storage when MINIO_ENABLED=True.
    """
    if settings.MINIO_ENABLED:
        raise NotImplementedError(
            "MinIO storage not yet configured. "
            "See django-storages S3 backend documentation and set "
            "AWS_S3_ENDPOINT_URL = settings.MINIO_ENDPOINT."
        )
    # Default: Django FileSystemStorage (local dev)
    from django.core.files.storage import default_storage
    return default_storage
