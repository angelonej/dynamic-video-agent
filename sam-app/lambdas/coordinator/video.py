"""
video.py — ElevenLabs TTS + D-ID Video Generation
===================================================
Pipeline:
  1. generate_tts_audio()       — text → MP3 bytes via ElevenLabs API
  2. upload_audio_to_s3_or_base64() — package audio for D-ID consumption
  3. generate_did_video()        — submit D-ID Talk job → poll → return video URL

D-ID "Talks" API reference (2024):
  POST https://api.d-id.com/talks
  GET  https://api.d-id.com/talks/{id}

Notes:
  • D-ID accepts audio as a publicly accessible URL *or* as a base64 data URL.
    For the PoC we use base64 to avoid needing an S3 bucket, but the function
    `upload_audio_to_s3_or_base64` can be trivially swapped to S3 presigned URL.
  • D-ID polling: max 10 × 6 s = 60 s. Increase for longer replies.
  • All credentials come from environment variables.
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
DID_BASE_URL = "https://api.d-id.com"

# D-ID polling config
DID_POLL_INTERVAL_S = 5       # seconds between status checks
DID_MAX_POLLS = 12            # 12 × 5 s = 60 s max wait


# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

def generate_tts_audio(
    text: str,
    voice_id: Optional[str] = None,
    model_id: str = "eleven_turbo_v2_5",   # fastest + cheapest EL model (2025)
    output_format: str = "mp3_44100_128",
) -> bytes:
    """
    Convert `text` to MP3 audio bytes using ElevenLabs TTS API.

    Parameters
    ----------
    text        : The text to synthesise (max ~5000 chars on free tier)
    voice_id    : ElevenLabs voice ID. Falls back to env var ELEVENLABS_VOICE_ID.
    model_id    : ElevenLabs model. eleven_turbo_v2_5 is best for PoC.
    output_format : ElevenLabs output format string.

    Returns
    -------
    Raw MP3 bytes.

    Raises
    ------
    RuntimeError on non-2xx response from ElevenLabs.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key or api_key == "REPLACE_ME":
        raise RuntimeError("ELEVENLABS_API_KEY is not configured")

    resolved_voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

    # Truncate very long replies to avoid EL quota burn (adjust as needed)
    max_chars = 800
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
        logger.warning("TTS text truncated to %d chars", max_chars)

    url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{resolved_voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "output_format": output_format,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.8,
            "style": 0.2,
            "use_speaker_boost": True,
        },
    }

    logger.info("ElevenLabs TTS: voice=%s len=%d chars", resolved_voice_id, len(text))
    resp = requests.post(url, json=payload, headers=headers, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs API error {resp.status_code}: {resp.text[:300]}"
        )

    audio_bytes = resp.content
    logger.info("ElevenLabs TTS: received %d bytes", len(audio_bytes))
    return audio_bytes


# ---------------------------------------------------------------------------
# Audio packaging helper
# ---------------------------------------------------------------------------

def upload_audio_to_s3_or_base64(audio_bytes: bytes) -> str:
    """
    Upload audio to D-ID's /audios endpoint and return the hosted URL.
    D-ID rejects base64 data URIs larger than ~100KB, so we upload the file
    directly and use the returned HTTPS URL instead.

    Returns
    -------
    str — HTTPS URL of the uploaded audio on D-ID's CDN.
    """
    import io
    logger.info("Uploading audio to D-ID (%d bytes)", len(audio_bytes))
    resp = requests.post(
        f"{DID_BASE_URL}/audios",
        headers={
            "Authorization": _did_headers()["Authorization"],
            "Accept": "application/json",
        },
        files={"audio": ("tts.mp3", io.BytesIO(audio_bytes), "audio/mpeg")},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"D-ID audio upload error {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    audio_url = data.get("url")
    if not audio_url:
        raise RuntimeError(f"D-ID audio upload missing 'url': {data}")
    logger.info("D-ID audio uploaded: %s", audio_url[:80])
    return audio_url


# ---------------------------------------------------------------------------
# D-ID Talk Job
# ---------------------------------------------------------------------------

def _did_headers() -> dict:
    """Build D-ID Authorization header from env."""
    api_key = os.environ.get("DID_API_KEY", "")
    if not api_key or api_key == "REPLACE_ME":
        raise RuntimeError("DID_API_KEY is not configured")

    # D-ID supports two schemes:
    #   Basic  <base64(email:key)>  — used with api.d-id.com/auth/token
    #   Bearer <jwt>                — issued via the web UI
    # The simplest approach: the user sets DID_API_KEY to the full value
    # including the scheme prefix, e.g. "Basic dXNlckBleG..."
    if api_key.startswith("Basic ") or api_key.startswith("Bearer "):
        auth_value = api_key
    else:
        # Assume raw base64 credentials (legacy format) → wrap in Basic
        auth_value = f"Basic {api_key}"

    return {
        "Authorization": auth_value,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _create_did_talk(
    audio_url: str,
    presenter_id: str,
    driver_url: Optional[str] = None,
) -> str:
    """
    Submit a new D-ID Talk job and return the talk_id.

    D-ID Talk payload (2024 API):
    {
      "script": {
        "type": "audio",
        "audio_url": "<data URI or https URL>"
      },
      "presenter_id": "<stock or custom avatar ID>",
      "driver_url":   "<optional custom driver video URL>",
      "config": {
        "fluent": true,
        "pad_audio": 0.0
      }
    }
    """
    # D-ID requires source_url to reference an image previously uploaded via
    # POST /images.  The s3:// URI returned by that endpoint is accepted directly.
    # We uploaded an AI-generated avatar face once and hardcode its s3 path here.
    # The DID_PRESENTER_ID env var can override with any s3:// or https:// URL.
    # Alyssa — professional female presenter in red suit, lobby background
    # Uploaded via POST /images → s3 URI accepted directly by D-ID /talks
    DEFAULT_SOURCE = (
        "s3://d-id-images-prod/google-oauth2|108660659165268832994"
        "/img_xEm3RGPOxfw-EUiwAJ5HF/alyssa.png"
    )
    SOURCE_URL_MAP = {
        "alyssa":         DEFAULT_SOURCE,
        "amy-jcu8MFXSuU": DEFAULT_SOURCE,   # legacy alias
        "default":        DEFAULT_SOURCE,
    }
    if presenter_id and (presenter_id.startswith("https://") or presenter_id.startswith("s3://")):
        source_url = presenter_id
    else:
        source_url = SOURCE_URL_MAP.get(presenter_id or "", DEFAULT_SOURCE)

    payload: dict = {
        "script": {
            "type": "audio",
            "audio_url": audio_url,
        },
        "source_url": source_url,
        "config": {
            "fluent": True,
            "pad_audio": 0.0,
            "result_format": "mp4",
        },
    }

    # Optionally override the driver video (controls head/body movement)
    if driver_url:
        payload["driver_url"] = driver_url

    logger.info("D-ID: submitting talk job, presenter=%s", presenter_id)
    resp = requests.post(
        f"{DID_BASE_URL}/talks",
        json=payload,
        headers=_did_headers(),
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"D-ID create talk error {resp.status_code}: {resp.text[:400]}"
        )

    data = resp.json()
    talk_id = data.get("id")
    if not talk_id:
        raise RuntimeError(f"D-ID response missing 'id': {data}")

    logger.info("D-ID: talk job created, id=%s", talk_id)
    return talk_id


def _poll_did_talk_status(talk_id: str) -> tuple:
    """
    Single-shot status check for a D-ID talk job.
    Returns (status_str, result_url_or_None).
    Called by the /video-status endpoint — no sleep/looping.
    """
    url = f"{DID_BASE_URL}/talks/{talk_id}"
    headers = _did_headers()
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"D-ID poll error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    status = data.get("status", "unknown")
    result_url = data.get("result_url") if status == "done" else None
    if status == "error" or status == "rejected":
        raise RuntimeError(f"D-ID talk {status}: {data.get('error', {})}")
    return status, result_url


def _poll_did_talk(talk_id: str) -> str:
    """
    Poll D-ID until the talk job completes or fails.

    Returns
    -------
    str — the result_url (MP4 video URL) from D-ID CDN.

    Raises
    ------
    RuntimeError if the job fails or times out.
    """
    url = f"{DID_BASE_URL}/talks/{talk_id}"
    headers = _did_headers()

    for attempt in range(1, DID_MAX_POLLS + 1):
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(
                f"D-ID poll error {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        status = data.get("status", "unknown")
        logger.info("D-ID: poll %d/%d — status=%s", attempt, DID_MAX_POLLS, status)

        if status == "done":
            result_url = data.get("result_url")
            if not result_url:
                raise RuntimeError(f"D-ID done but no result_url: {data}")
            logger.info("D-ID: video ready → %s", result_url[:80])
            return result_url

        elif status in ("error", "rejected"):
            error_msg = data.get("error", {})
            raise RuntimeError(f"D-ID talk job {status}: {error_msg}")

        # Still processing — wait and retry
        if attempt < DID_MAX_POLLS:
            time.sleep(DID_POLL_INTERVAL_S)

    raise RuntimeError(
        f"D-ID talk job timed out after {DID_MAX_POLLS * DID_POLL_INTERVAL_S}s"
    )


def generate_did_video(
    audio_url: str,
    presenter_id: Optional[str] = None,
    driver_url: Optional[str] = None,
) -> str:
    """
    Full D-ID pipeline: submit → poll → return video URL.

    Parameters
    ----------
    audio_url    : Base64 data URI or public HTTPS URL of the TTS audio.
    presenter_id : D-ID presenter/avatar ID. Falls back to env DID_PRESENTER_ID.
    driver_url   : Optional custom driver video URL.

    Returns
    -------
    str — CDN URL of the rendered MP4 video (valid for ~24h).
    """
    resolved_presenter = presenter_id or os.environ.get("DID_PRESENTER_ID", "amy-jcu8MFXSuU")
    resolved_driver = driver_url or os.environ.get("DID_DRIVER_URL") or None

    talk_id = _create_did_talk(
        audio_url=audio_url,
        presenter_id=resolved_presenter,
        driver_url=resolved_driver,
    )

    video_url = _poll_did_talk(talk_id)
    return video_url
