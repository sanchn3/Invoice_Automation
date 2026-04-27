"""
pdf_storage.py
==============
Manages provider invoice PDFs in Supabase Storage across two buckets:

  unprocessed-invoices  — PDF received from provider, not yet invoiced
  processed-invoices    — PDF moved here once the client invoice is generated

All operations use the service-role key (bypasses RLS).
httpx is used directly — no supabase SDK required.
"""

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_BUCKET_UNPROCESSED = "unprocessed-invoices"
_BUCKET_PROCESSED   = "processed-invoices"


def _base() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL must be set in .env")
    return url


def _auth_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY must be set in .env")
    return {
        "apikey"       : key,
        "Authorization": f"Bearer {key}",
    }


# ── Internal primitives ───────────────────────────────────────────────────────

def _upload(local_path: str, bucket: str) -> bool:
    path = Path(local_path)
    if not path.exists():
        logger.error("pdf_storage._upload: file not found: %s", local_path)
        return False
    url     = f"{_base()}/storage/v1/object/{bucket}/{path.name}"
    headers = {**_auth_headers(), "Content-Type": "application/pdf", "x-upsert": "true"}
    try:
        resp = httpx.put(url, headers=headers, content=path.read_bytes(), timeout=60)
        if resp.status_code in (200, 201):
            logger.info("pdf_storage: uploaded %s → %s", path.name, bucket)
            return True
        logger.error("pdf_storage._upload HTTP %d: %s", resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.error("pdf_storage._upload exception: %s", e)
        return False


def _download(storage_key: str, bucket: str) -> bytes | None:
    url = f"{_base()}/storage/v1/object/{bucket}/{storage_key}"
    try:
        resp = httpx.get(url, headers=_auth_headers(), timeout=60)
        if resp.status_code == 200:
            return resp.content
        logger.error(
            "pdf_storage._download HTTP %d key=%s bucket=%s: %s",
            resp.status_code, storage_key, bucket, resp.text,
        )
        return None
    except Exception as e:
        logger.error("pdf_storage._download exception: %s", e)
        return None


def _delete(storage_key: str, bucket: str) -> None:
    url = f"{_base()}/storage/v1/object/{bucket}/{storage_key}"
    try:
        resp = httpx.delete(url, headers=_auth_headers(), timeout=30)
        if resp.status_code in (200, 204):
            logger.info("pdf_storage: deleted %s from %s", storage_key, bucket)
        else:
            logger.warning(
                "pdf_storage._delete HTTP %d key=%s bucket=%s",
                resp.status_code, storage_key, bucket,
            )
    except Exception as e:
        logger.error("pdf_storage._delete exception: %s", e)


# ── Public API ────────────────────────────────────────────────────────────────

def upload_pdf(local_path: str) -> bool:
    """
    Upload a newly-received provider invoice PDF to 'unprocessed-invoices'.
    The storage key is the plain filename of local_path.
    Returns True on success.
    """
    return _upload(local_path, _BUCKET_UNPROCESSED)


def move_to_processed(pdf_local_path: str) -> bool:
    """
    Move a PDF from 'unprocessed-invoices' to 'processed-invoices'.
    Called when the client invoice is generated (status → 'invoiced').
    Returns True if the move completed successfully.
    """
    key  = Path(pdf_local_path).name
    data = _download(key, _BUCKET_UNPROCESSED)

    if data is None:
        # May already be in processed, or never uploaded — not a hard failure
        logger.warning("pdf_storage.move_to_processed: %s not found in unprocessed bucket", key)
        return False

    # Upload to processed bucket
    url     = f"{_base()}/storage/v1/object/{_BUCKET_PROCESSED}/{key}"
    headers = {**_auth_headers(), "Content-Type": "application/pdf", "x-upsert": "true"}
    try:
        resp = httpx.put(url, headers=headers, content=data, timeout=60)
        if resp.status_code not in (200, 201):
            logger.error("pdf_storage.move_to_processed upload HTTP %d: %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("pdf_storage.move_to_processed upload exception: %s", e)
        return False

    # Delete from unprocessed bucket
    _delete(key, _BUCKET_UNPROCESSED)
    logger.info("pdf_storage: moved %s → processed-invoices", key)
    return True


def upload_photo(job_id: str, filename: str, photo_bytes: bytes) -> str | None:
    """
    Upload a worker photo to 'unprocessed-invoices' under photos/{job_id}/{filename}.
    Returns the storage key on success, None on failure.
    """
    key          = f"photos/{job_id}/{filename}"
    content_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    url          = f"{_base()}/storage/v1/object/{_BUCKET_UNPROCESSED}/{key}"
    headers      = {**_auth_headers(), "Content-Type": content_type, "x-upsert": "true"}
    try:
        resp = httpx.put(url, headers=headers, content=photo_bytes, timeout=60)
        if resp.status_code in (200, 201):
            logger.info("pdf_storage: uploaded photo → %s/%s", _BUCKET_UNPROCESSED, key)
            return key
        logger.error("pdf_storage.upload_photo HTTP %d: %s", resp.status_code, resp.text)
        return None
    except Exception as e:
        logger.error("pdf_storage.upload_photo exception: %s", e)
        return None


def download_photo(key: str) -> bytes | None:
    """
    Download a worker photo by storage key.
    Checks unprocessed bucket first, then processed (in case the job was invoiced).
    """
    data = _download(key, _BUCKET_UNPROCESSED)
    if data is not None:
        return data
    return _download(key, _BUCKET_PROCESSED)


def upload_pdf_bytes(key: str, pdf_bytes: bytes) -> bool:
    """
    Upload raw PDF bytes to 'processed-invoices' under the given key.
    Used for generated client invoice PDFs (no local file involved).
    Returns True on success.
    """
    url     = f"{_base()}/storage/v1/object/{_BUCKET_PROCESSED}/{key}"
    headers = {**_auth_headers(), "Content-Type": "application/pdf", "x-upsert": "true"}
    try:
        resp = httpx.put(url, headers=headers, content=pdf_bytes, timeout=60)
        if resp.status_code in (200, 201):
            logger.info("pdf_storage: uploaded bytes → processed-invoices/%s", key)
            return True
        logger.error("pdf_storage.upload_pdf_bytes HTTP %d: %s", resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.error("pdf_storage.upload_pdf_bytes exception: %s", e)
        return False


def fetch_pdf_bytes(storage_key: str) -> bytes | None:
    """
    Download PDF bytes by key — checks processed bucket first, then unprocessed.
    Returns None if not found in either.
    """
    data = _download(storage_key, _BUCKET_PROCESSED)
    if data is not None:
        return data
    return _download(storage_key, _BUCKET_UNPROCESSED)


def get_pdf_bytes(pdf_local_path: str) -> bytes | None:
    """
    Return PDF bytes for display, regardless of deployment environment.
    1. Try local file (fast in dev).
    2. Check processed-invoices bucket.
    3. Check unprocessed-invoices bucket.
    Returns None if the PDF cannot be found anywhere.
    """
    if pdf_local_path:
        local = Path(pdf_local_path)
        if local.exists():
            return local.read_bytes()
        key = local.name
        if key:
            data = fetch_pdf_bytes(key)
            if data is not None:
                return data
    logger.warning("pdf_storage.get_pdf_bytes: not found: %s", pdf_local_path)
    return None
