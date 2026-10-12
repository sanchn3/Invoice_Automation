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
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ── Per-process download safety counter ───────────────────────────────────────
# Resets every hour.  Limits the blast radius of any future render-loop bug.
# Override via env vars PDF_SAFETY_MAX_DOWNLOADS / PDF_SAFETY_MAX_MB.

_safety_lock            = threading.Lock()
_safety_window_start: float = 0.0
_safety_download_count: int = 0
_safety_download_bytes: int = 0
_SAFETY_MAX_DOWNLOADS   = int(os.environ.get("PDF_SAFETY_MAX_DOWNLOADS", "300"))
_SAFETY_MAX_MB          = float(os.environ.get("PDF_SAFETY_MAX_MB", "400"))


def _check_download_safety() -> bool:
    """
    Gate every download through a rolling-hour counter.
    Returns False (and logs a warning) once the hourly ceiling is exceeded.
    """
    global _safety_window_start, _safety_download_count, _safety_download_bytes
    with _safety_lock:
        now = time.monotonic()
        if now - _safety_window_start > 3600:          # new hour — reset
            _safety_window_start  = now
            _safety_download_count = 0
            _safety_download_bytes = 0
        _safety_download_count += 1
        if _safety_download_count > _SAFETY_MAX_DOWNLOADS:
            logger.warning(
                "pdf_storage: safety limit — %d download attempts in the current "
                "hour window (max %d). Skipping. Set PDF_SAFETY_MAX_DOWNLOADS to raise.",
                _safety_download_count, _SAFETY_MAX_DOWNLOADS,
            )
            return False
        mb = _safety_download_bytes / (1024 * 1024)
        if mb > _SAFETY_MAX_MB:
            logger.warning(
                "pdf_storage: safety limit — %.1f MB downloaded in the current "
                "hour window (max %.0f MB). Skipping. Set PDF_SAFETY_MAX_MB to raise.",
                mb, _SAFETY_MAX_MB,
            )
            return False
    return True

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
    if not _check_download_safety():
        return None
    url = f"{_base()}/storage/v1/object/{bucket}/{storage_key}"
    try:
        resp = httpx.get(url, headers=_auth_headers(), timeout=60)
        if resp.status_code == 200:
            data = resp.content
            with _safety_lock:
                global _safety_download_bytes
                _safety_download_bytes += len(data)
            return data
        if resp.status_code in (400, 404):
            # Object does not exist in this bucket — not an application error.
            logger.debug(
                "pdf_storage._download: object not found (HTTP %d) key=%s bucket=%s",
                resp.status_code, storage_key, bucket,
            )
            return None
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


def overwrite_provider_pdf(pdf_local_path: str, pdf_bytes: bytes) -> None:
    """
    Replace the provider PDF with new bytes (e.g. after baking in worker photos).
    Overwrites the local file (if it exists) and re-uploads to 'unprocessed-invoices'.
    """
    path = Path(pdf_local_path)
    if path.exists():
        path.write_bytes(pdf_bytes)
    key     = path.name
    url     = f"{_base()}/storage/v1/object/{_BUCKET_UNPROCESSED}/{key}"
    headers = {**_auth_headers(), "Content-Type": "application/pdf", "x-upsert": "true"}
    try:
        resp = httpx.put(url, headers=headers, content=pdf_bytes, timeout=60)
        if resp.status_code in (200, 201):
            logger.info("pdf_storage: overwritten provider PDF %s", key)
        else:
            logger.error("pdf_storage.overwrite_provider_pdf HTTP %d: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("pdf_storage.overwrite_provider_pdf exception: %s", e)


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
