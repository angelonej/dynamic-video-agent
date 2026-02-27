"""
app.py — Coordinator Lambda Handler
=====================================
Entry-point for all API Gateway HTTP API routes:
  GET  /health        → liveness probe
  POST /create-agent  → bootstrap a new specialized LangGraph agent
  POST /chat          → send a message, get back text + video URL

Design decisions (2026 PoC):
  • In-memory session store (SESSIONS dict) — trivial swap to DynamoDB later.
  • All heavy imports happen at module level (Lambda warm-start reuse).
  • CORS headers are added by API Gateway (SAM CorsConfiguration) but we
    also inject them here for sam local start-api compatibility.
  • All secrets come from environment variables set in template.yaml.
"""

import json
import logging
import os
import time
import traceback
import uuid
from typing import Any

from agents import AgentRegistry, create_agent, run_agent
from video import generate_tts_audio, generate_did_video, upload_audio_to_s3_or_base64

# ── Add this helper function near the top of your file ──────────────

import re
import urllib.request
import urllib.parse

SPARK_PROXY_URL = "https://nh8k05dcy6.execute-api.us-east-1.amazonaws.com/prod/spark"

def _fetch_spark_market_data(zip_code: str) -> dict | None:
    from datetime import datetime, timedelta
    import urllib.request, urllib.parse, json

    # Spark requires date without microseconds
    ninety_days_ago = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")

    closed_filter = (
        f"PostalCode eq '{zip_code}' and StandardStatus eq 'Closed' "
        f"and PropertySubType eq 'Single Family Residence' "
        f"and CloseDate ge {ninety_days_ago}"
    )
    params = urllib.parse.urlencode({
        "$filter": closed_filter,
        "$orderby": "CloseDate desc",
        "$top": "100",
        "$select": "ClosePrice,CloseDate,OriginalListPrice,DaysOnMarket,LivingArea,PropertySubType",
    })
    url = f"https://nh8k05dcy6.execute-api.us-east-1.amazonaws.com/prod/spark/Version/3/Reso/OData/Property?{params}"

    print(f"[Spark] Fetching: {url}")  # ← check CloudWatch logs for this

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        props = [
            p for p in (data.get("value") or [])
            if 100000 <= (p.get("ClosePrice") or 0) <= 2000000
        ]
        print(f"[Spark] Got {len(data.get('value', []))} total, {len(props)} valid SFR props for {zip_code}")

        if not props:
            return None

        prices = sorted(p["ClosePrice"] for p in props)
        median = prices[len(prices) // 2]
        average = round(sum(prices) / len(prices))
        dom_vals = [p["DaysOnMarket"] for p in props if (p.get("DaysOnMarket") or 0) > 0]
        avg_dom = round(sum(dom_vals) / len(dom_vals)) if dom_vals else 0
        lsr_vals = [
            (p["ClosePrice"] / p["OriginalListPrice"]) * 100
            for p in props if (p.get("OriginalListPrice") or 0) > 0
        ]
        avg_lsr = round(sum(lsr_vals) / len(lsr_vals), 1) if lsr_vals else 100.0

        return {
            "zip_code": zip_code,
            "total_sales": len(props),
            "median_price": median,
            "average_price": average,
            "avg_days_on_market": avg_dom,
            "list_to_sale_ratio": avg_lsr,
        }
    except Exception as exc:
        print(f"[Spark] ERROR for {zip_code}: {exc}")
        return None

def _fetch_spark_home_valuation(zip_code: str, city: str, bedrooms: str = None, 
                                 bathrooms: str = None, sqft: str = None, 
                                 year_built: str = None) -> dict | None:
    from datetime import datetime, timedelta
    import urllib.request, urllib.parse, json

    one_year_ago = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%dT00:00:00Z")

    filters = [
        f"PostalCode eq '{zip_code}'",
        "StandardStatus eq 'Closed'",
        "PropertySubType eq 'Single Family Residence'",
        f"CloseDate ge {one_year_ago}",
    ]

    if bedrooms:
        filters.append(f"BedroomsTotal eq {bedrooms}")
    if bathrooms:
        filters.append(f"BathroomsTotalInteger eq {bathrooms}")
    if sqft:
        n = int(sqft)
        filters.append(f"LivingArea ge {round(n * 0.8)} and LivingArea le {round(n * 1.2)}")
    if year_built:
        y = int(year_built)
        filters.append(f"YearBuilt ge {y - 10} and YearBuilt le {y + 10}")

    params = urllib.parse.urlencode({
        "$filter": " and ".join(filters),
        "$orderby": "CloseDate desc",
        "$top": "20",
    })
    url = f"https://nh8k05dcy6.execute-api.us-east-1.amazonaws.com/prod/spark/Version/3/Reso/OData/Property?{params}"

    print(f"[Spark] Home valuation fetch: {url}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        props = [p for p in (data.get("value") or []) if (p.get("ClosePrice") or 0) > 50000]
        print(f"[Spark] Got {len(props)} comparable sales for {city} {zip_code}")

        if not props:
            return None

        prices = sorted(p["ClosePrice"] for p in props)
        median = prices[len(prices) // 2]
        sample = props[0]

        return {
            "zip_code":        zip_code,
            "city":            city,
            "estimated_value": median,
            "value_low":       round(median * 0.90),
            "value_high":      round(median * 1.10),
            "comparable_count": len(prices),
            "confidence":      "high" if len(prices) >= 5 else "medium",
            "property_tax":    round(sample.get("TaxAnnualAmount") or 0) or None,
            "tax_year":        sample.get("TaxYear"),
            "school_district": sample.get("SchoolDistrict"),
        }
    except Exception as exc:
        print(f"[Spark] Home valuation ERROR for {city} {zip_code}: {exc}")
        return None

# City → default zip fallback for South Florida cities (used when no zip in message)
_CITY_ZIP_FALLBACK = {
    "Hollywood":       "33020",
    "Pembroke Pines":  "33024",
    "Fort Lauderdale": "33301",
    "Miami":           "33101",
    "Miramar":         "33025",
    "Davie":           "33314",
    "Weston":          "33326",
    "Cooper City":     "33026",
    "Sunrise":         "33351",
    "Plantation":      "33317",
}

def _geocode_address_to_zip(address: str) -> str | None:
    """
    Use the free US Census Geocoder to resolve a street address to a zip code.
    Returns the zip code string or None on failure.
    """
    try:
        params = urllib.parse.urlencode({
            "address": address,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "all",
            "format": "json",
        })
        url = f"https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            return None
        # Pull zip from the matched address components
        addr_components = matches[0].get("addressComponents", {})
        return addr_components.get("zip") or None
    except Exception as exc:
        print(f"[Geocode] Census geocoder failed for '{address}': {exc}")
        return None


def _enrich_message_with_spark(message: str, address: str = None) -> str:
    import re

    needs_market = bool(re.search(
        r"market|trends|inventory|days on market|how.s the market|selling",
        message, re.IGNORECASE
    ))
    needs_value = bool(re.search(
        r"worth|value|how much|cma|estimate|comparable|home value|valuation|report",
        message, re.IGNORECASE
    ))

    zip_match  = re.search(r"\b(\d{5})\b", message)
    city_match = re.search(
        r"\b(pembroke pines|hollywood|fort lauderdale|miami|miramar|davie|weston|cooper city|sunrise|plantation)\b",
        message, re.IGNORECASE
    )

    # Detect a street address pattern (e.g. "3250 Grant St, Hollywood, FL")
    street_match = re.search(
        r"\b\d+\s+[A-Za-z0-9 .]+(?:St|Ave|Blvd|Dr|Rd|Ln|Way|Ct|Pl|Ter|Cir|Loop|Pkwy|Hwy)[\w .]*",
        message, re.IGNORECASE
    )

    if not zip_match and not city_match and not street_match:
        return message  # no location found, no enrichment

    zip_code = zip_match.group(1) if zip_match else None
    city     = city_match.group(1).title() if city_match else None
    # Capture the matched street address text for use in AI context
    street_address = street_match.group(0).strip() if street_match else None

    # If we have a street address but no zip, try to geocode it
    if street_match and not zip_code:
        geocoded_zip = _geocode_address_to_zip(message.strip())
        if geocoded_zip:
            print(f"[Geocode] Resolved '{message[:80]}' → zip {geocoded_zip}")
            zip_code = geocoded_zip
        elif city:
            # Fall back to city's primary zip
            zip_code = _CITY_ZIP_FALLBACK.get(city)
            print(f"[Geocode] Geocode failed, using city fallback zip {zip_code} for {city}")

    # If still no zip but city is known, use city fallback
    if not zip_code and city:
        zip_code = _CITY_ZIP_FALLBACK.get(city)
        print(f"[Geocode] No zip in message, using city fallback zip {zip_code} for {city}")

    # Instruction header tells the AI to use the data silently (never echo it)
    data_instruction = (
        "INTERNAL CONTEXT — do NOT repeat, quote, or mention these data blocks in your reply. "
        "Use the numbers naturally in your response as if you already knew them.\n\n"
    )

    prefix = ""

    if needs_value and zip_code:
        effective_city = city or "the area"
        subject_property = street_address or f"{effective_city}, FL {zip_code}"
        data = _fetch_spark_home_valuation(zip_code, effective_city)
        if data:
            prefix += (
                f"[MLS CMA DATA — internal use only, do not echo]\n"
                f"Subject property: {subject_property}\n"
                f"This is a Comparative Market Analysis (CMA) for the SPECIFIC PROPERTY above, not a general area report.\n"
                f"Present the numbers as the estimated value FOR THIS PROPERTY based on comparable sales.\n"
                f"Comparable sales analyzed: {data['comparable_count']} recent sales in zip {zip_code}\n"
                f"Estimated value for this property: ${data['estimated_value']:,}\n"
                f"Value range: ${data['value_low']:,} – ${data['value_high']:,}\n"
                f"Confidence: {data['confidence']}\n"
            )
            if data.get("property_tax"):
                prefix += f"Annual property tax (area avg): ${data['property_tax']:,}\n"
            if data.get("school_district"):
                prefix += f"School district: {data['school_district']}\n"
            prefix += "[END VALUATION DATA]\n\n"
        else:
            # Valuation comps unavailable — try market stats as fallback
            print(f"[Spark] Valuation returned no comps for {zip_code}, trying market stats fallback")
            mdata = _fetch_spark_market_data(zip_code)
            if mdata:
                prefix += (
                    f"[MLS MARKET DATA — internal use only, do not echo]\n"
                    f"Location: {effective_city} {zip_code}\n"
                    f"Sales last 90 days: {mdata['total_sales']} Single Family homes\n"
                    f"Median price: ${mdata['median_price']:,}\n"
                    f"Average price: ${mdata['average_price']:,}\n"
                    f"Avg days on market: {mdata['avg_days_on_market']}\n"
                    f"List-to-sale ratio: {mdata['list_to_sale_ratio']}%\n"
                    f"[END MARKET DATA]\n\n"
                )
            else:
                # No data at all — tell AI explicitly so it doesn't hallucinate
                prefix += (
                    f"[MLS DATA UNAVAILABLE — internal use only, do not echo]\n"
                    f"No recent MLS sales data was found for {effective_city} {zip_code}.\n"
                    f"Do NOT invent or estimate any prices or values.\n"
                    f"Tell the user you don't have current MLS data for that address and suggest\n"
                    f"they contact John directly at 954-612-0935 for a personalized CMA.\n"
                    f"[END UNAVAILABLE NOTICE]\n\n"
                )

    elif needs_market and zip_code:
        data = _fetch_spark_market_data(zip_code)
        if data:
            prefix += (
                f"[MLS MARKET DATA — internal use only, do not echo]\n"
                f"Location: zip {zip_code}\n"
                f"Sales last 90 days: {data['total_sales']} Single Family homes\n"
                f"Median price: ${data['median_price']:,}\n"
                f"Average price: ${data['average_price']:,}\n"
                f"Avg days on market: {data['avg_days_on_market']}\n"
                f"List-to-sale ratio: {data['list_to_sale_ratio']}%\n"
                f"[END MARKET DATA]\n\n"
            )

    if prefix:
        return data_instruction + prefix + message
    return message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory session store
# Structure:
#   SESSIONS[session_id] = {
#       "agent_id": str,
#       "history": [ {"role": "user"|"assistant", "content": str}, ... ]
#   }
#
# NOTE: Lambda instances are recycled after ~15 min idle.
#       For production, replace with DynamoDB (see README §Future Scaling).
# ---------------------------------------------------------------------------
SESSIONS: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Agent registry (module-level singleton, persists across warm invocations)
# ---------------------------------------------------------------------------
AGENT_REGISTRY = AgentRegistry()

# ---------------------------------------------------------------------------
# CORS headers — mirrored here for sam local compatibility
# ---------------------------------------------------------------------------
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Session-ID",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Helper: build a standard API Gateway response
# ---------------------------------------------------------------------------
def _response(status: int, body: dict | list) -> dict:
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def _error(status: int, message: str, detail: str = "") -> dict:
    logger.error("HTTP %s — %s | %s", status, message, detail)
    payload = {"error": message}
    if detail and LOG_LEVEL == "DEBUG":
        payload["detail"] = detail
    return _response(status, payload)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def handle_health(_event: dict, _ctx: Any) -> dict:
    """GET /health — quick liveness probe used by front-end + load balancers."""
    return _response(200, {
        "status": "healthy",
        "timestamp": time.time(),
        "agents_loaded": len(AGENT_REGISTRY),
        "sessions_active": len(SESSIONS),
    })


def handle_create_agent(event: dict, _ctx: Any) -> dict:
    """
    POST /create-agent
    Request body:
        {
          "name":          "Rome Historian",          // display name
          "system_prompt": "You are a world-class...", // full persona prompt
          "voice_id":      "21m00Tcm4TlvDq8ikWAM",   // optional override
          "avatar_id":     "amy-jcu8MFXSuU"           // optional override
        }
    Response:
        {
          "agent_id":   "<uuid>",
          "name":       "Rome Historian",
          "created_at": 1234567890.123
        }
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    name: str = (body.get("name") or "").strip()
    system_prompt: str = (body.get("system_prompt") or "").strip()

    if not name:
        return _error(400, "Field 'name' is required")
    if not system_prompt or len(system_prompt) < 10:
        return _error(400, "Field 'system_prompt' must be at least 10 characters")

    # Optional per-agent overrides (voice & avatar)
    voice_id = body.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID", "")
    avatar_id = body.get("avatar_id") or os.environ.get("DID_PRESENTER_ID", "")

    agent_id = str(uuid.uuid4())
    created_at = time.time()

    # Register the agent in our in-memory registry
    AGENT_REGISTRY.register(
        agent_id=agent_id,
        name=name,
        system_prompt=system_prompt,
        voice_id=voice_id,
        avatar_id=avatar_id,
        created_at=created_at,
    )

    # Eagerly compile the LangGraph for this agent (warm-start advantage)
    create_agent(AGENT_REGISTRY.get(agent_id))

    logger.info("Agent created: id=%s name=%s", agent_id, name)
    return _response(201, {
        "agent_id": agent_id,
        "name": name,
        "created_at": created_at,
        "voice_id": voice_id,
        "avatar_id": avatar_id,
    })


def handle_chat(event: dict, _ctx: Any) -> dict:
    """
    POST /chat
    Request body:
        {
          "agent_id":   "<uuid>",
          "session_id": "<uuid or omit to start new>",
          "message":    "Tell me about Julius Caesar",
          "generate_video": true    // default: true — set false to skip D-ID
        }
    Response:
        {
          "session_id":  "<uuid>",
          "reply":       "Julius Caesar was...",
          "video_url":   "https://d-id.com/...",   // null if generate_video=false
          "audio_url":   "data:audio/mpeg;base64,..." // null if video generated
          "usage":       { "prompt_tokens": 120, "completion_tokens": 80 }
        }
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    agent_id: str = (body.get("agent_id") or "").strip()
    message: str = (body.get("message") or "").strip()
    session_id: str = body.get("session_id") or str(uuid.uuid4())
    generate_video: bool = body.get("generate_video", True)

    if not agent_id:
        return _error(400, "Field 'agent_id' is required")
    if not message:
        return _error(400, "Field 'message' is required")

    # Lookup agent
    agent_meta = AGENT_REGISTRY.get(agent_id)
    if agent_meta is None:
        return _error(404, f"Agent '{agent_id}' not found. Call /create-agent first.")

    # Get or create session history
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {"agent_id": agent_id, "history": []}

    session = SESSIONS[session_id]

    # Guard: don't mix agents in one session
    if session["agent_id"] != agent_id:
        return _error(400, "session_id belongs to a different agent")

    history = session["history"]
    history.append({"role": "user", "content": message})

    # ----- Step 1: LangGraph agent generates text reply -----
    logger.info("[%s] Running agent %s, message=%r", session_id, agent_id, message[:80])
    try:
        reply_text, usage = run_agent(
            agent_meta=agent_meta,
            history=history,
        )
    except Exception as exc:
        tb = traceback.format_exc()
        return _error(502, "Agent inference failed", tb)

    history.append({"role": "assistant", "content": reply_text})
    logger.info("[%s] Agent reply (%d chars)", session_id, len(reply_text))

    video_url = None
    audio_b64_url = None

    if generate_video:
        # ----- Step 2: ElevenLabs TTS — text → audio bytes -----
        logger.info("[%s] Generating TTS audio...", session_id)
        try:
            audio_bytes = generate_tts_audio(
                text=reply_text,
                voice_id=agent_meta["voice_id"],
            )
        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning("[%s] TTS failed, falling back to text only: %s", session_id, exc)
            audio_bytes = None

        if audio_bytes:
            # ----- Step 3: Upload audio then SUBMIT D-ID job (non-blocking) -----
            # We do NOT poll here to stay within the 29-second API Gateway limit.
            # The client receives a `talk_id` and polls GET /video-status/{talk_id}.
            # We ALSO return the audio_url so the client can play TTS immediately
            # while D-ID renders in the background — this cuts perceived latency.
            logger.info("[%s] Submitting D-ID talk job (async)...", session_id)
            try:
                audio_url = upload_audio_to_s3_or_base64(audio_bytes)
                from video import _create_did_talk
                talk_id = _create_did_talk(
                    audio_url=audio_url,
                    presenter_id=agent_meta["avatar_id"],
                )
                video_url = f"__did_pending__{talk_id}"   # sentinel for client
                audio_b64_url = audio_url  # return audio so client plays it NOW
            except Exception as exc:
                tb = traceback.format_exc()
                logger.warning("[%s] D-ID submit failed: %s", session_id, exc)
                # Fall back to returning raw audio only
                try:
                    audio_b64_url = upload_audio_to_s3_or_base64(audio_bytes)
                except Exception:
                    pass
    else:
        # Video skipped — just return TTS for the client to play
        try:
            audio_bytes = generate_tts_audio(
                text=reply_text,
                voice_id=agent_meta["voice_id"],
            )
            if audio_bytes:
                audio_b64_url = upload_audio_to_s3_or_base64(audio_bytes)
        except Exception as exc:
            logger.warning("[%s] TTS-only mode failed: %s", session_id, exc)

    return _response(200, {
        "session_id": session_id,
        "reply": reply_text,
        "video_url": video_url,
        "audio_url": audio_b64_url,
        "history_length": len(history),
        "usage": usage,
    })


# ---------------------------------------------------------------------------
# D-ID Streaming proxy handlers
# The browser cannot call D-ID directly (API key exposure), so these
# thin Lambda endpoints proxy each WebRTC negotiation step.
# ---------------------------------------------------------------------------

def _did_auth_header() -> str:
    api_key = os.environ.get("DID_API_KEY", "")
    if api_key.startswith("Basic ") or api_key.startswith("Bearer "):
        return api_key
    return f"Basic {api_key}"


DID_SOURCE_URL = (
    "s3://d-id-images-prod/google-oauth2|108660659165268832994"
    "/img_xEm3RGPOxfw-EUiwAJ5HF/alyssa.png"
)
DID_BACKGROUND_URL = (
    "https://clips-presenters.d-id.com/v2/Alyssa_NoHands_RedSuite_Lobby"
    "/qtzjxMSwEa/ypTds_0CK3/thumbnail.png"
)


def handle_stream_start(event: dict, _ctx: Any) -> dict:
    """
    POST /stream/start
    Creates a D-ID WebRTC streaming session.
    Returns: { stream_id, offer (SDP), ice_servers }
    """
    import requests as _req
    headers = {
        "Authorization": _did_auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = _req.post(
        "https://api.d-id.com/talks/streams",
        json={
            "source_url": DID_SOURCE_URL,
            "background": {"source_url": DID_BACKGROUND_URL},
        },
        headers=headers,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        return _error(502, f"D-ID stream start failed: {resp.status_code}", resp.text[:300])
    data = resp.json()
    # session_id is the raw Set-Cookie header value (AWS ALB sticky session)
    # We must echo it back as a Cookie on every subsequent D-ID request
    raw_cookie = resp.headers.get("Set-Cookie", data.get("session_id", ""))
    return _response(200, {
        "stream_id": data["id"],
        "offer": data["offer"],
        "ice_servers": data.get("ice_servers", []),
        "session_id": raw_cookie,
    })


def handle_stream_sdp(event: dict, _ctx: Any) -> dict:
    """
    POST /stream/sdp
    Body: { stream_id, answer (SDP object), session_id }
    Sends the browser's SDP answer back to D-ID to complete WebRTC handshake.
    """
    import requests as _req
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON")
    stream_id = body.get("stream_id", "")
    answer = body.get("answer")
    session_cookie = body.get("session_id", "")
    if not stream_id or not answer:
        return _error(400, "stream_id and answer required")
    headers = {
        "Authorization": _did_auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session_cookie:
        # Extract just the cookie value pairs (strip Expires/Path/etc)
        cookie_val = ";".join(
            p.strip() for p in session_cookie.split(";")
            if p.strip() and not any(p.strip().lower().startswith(k) for k in ("expires","path","samesite","secure","httponly","domain","max-age"))
        )
        headers["Cookie"] = cookie_val
    resp = _req.post(
        f"https://api.d-id.com/talks/streams/{stream_id}/sdp",
        json={"answer": answer, "session_id": session_cookie},
        headers=headers,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        return _error(502, f"D-ID SDP failed: {resp.status_code}", resp.text[:300])
    return _response(200, {"ok": True})


def handle_stream_ice(event: dict, _ctx: Any) -> dict:
    """
    POST /stream/ice
    Body: { stream_id, candidate (RTCIceCandidate), session_id }
    Forwards an ICE candidate from the browser to D-ID.
    """
    import requests as _req
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON")
    stream_id = body.get("stream_id", "")
    candidate = body.get("candidate")
    session_cookie = body.get("session_id", "")
    if not stream_id:
        return _error(400, "stream_id required")
    headers = {
        "Authorization": _did_auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session_cookie:
        cookie_val = ";".join(
            p.strip() for p in session_cookie.split(";")
            if p.strip() and not any(p.strip().lower().startswith(k) for k in ("expires","path","samesite","secure","httponly","domain","max-age"))
        )
        headers["Cookie"] = cookie_val
    resp = _req.post(
        f"https://api.d-id.com/talks/streams/{stream_id}/ice",
        json={"candidate": candidate, "session_id": session_cookie},
        headers=headers,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        return _error(502, f"D-ID ICE failed: {resp.status_code}", resp.text[:300])
    return _response(200, {"ok": True})


def handle_stream_talk(event: dict, _ctx: Any) -> dict:
    """
    POST /stream/talk
    Body: { stream_id, session_id, text, voice_id? }
    Sends text to an active stream — D-ID generates speech + video live.
    Optionally runs LangChain agent first if agent_id + message provided.
    """
    import requests as _req
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON")

    stream_id = body.get("stream_id", "")
    did_session_id = body.get("session_id", "")
    text = (body.get("text") or "").strip()

    # Optional: run through agent first
    agent_id = body.get("agent_id", "")
    chat_session_id = body.get("chat_session_id", "")
    message = (body.get("message") or "").strip()

    if not stream_id:
        return _error(400, "stream_id required")

    reply_text = text

    # If agent_id + message provided, generate LLM reply first
    if agent_id and message:
        agent_meta = AGENT_REGISTRY.get(agent_id)
        if agent_meta is None:
            return _error(404, f"Agent '{agent_id}' not found")
        if chat_session_id not in SESSIONS:
            SESSIONS[chat_session_id] = {"agent_id": agent_id, "history": []}
        session = SESSIONS[chat_session_id]
        session["history"].append({"role": "user", "content": message})
        enriched_message = _enrich_message_with_spark(message)          # ← ADD THIS
        print(f"[Spark] enriched={enriched_message[:200]}")  # add this
        try:
            reply_text, usage = run_agent(
                agent_meta=agent_meta,
                history=session["history"][:-1] + [{"role": "user", "content": enriched_message}],  # ← CHANGE THIS
            )
        except Exception as exc:
            return _error(502, "Agent inference failed", traceback.format_exc())
        session["history"].append({"role": "assistant", "content": reply_text})

        # Truncate for TTS
        max_chars = 800
        if len(reply_text) > max_chars:
            reply_text = reply_text[:max_chars] + "…"

    if not reply_text:
        return _error(400, "text or (agent_id + message) required")

    voice_id = body.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

    headers = {
        "Authorization": _did_auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if did_session_id:
        cookie_val = ";".join(
            p.strip() for p in did_session_id.split(";")
            if p.strip() and not any(p.strip().lower().startswith(k) for k in ("expires","path","samesite","secure","httponly","domain","max-age"))
        )
        headers["Cookie"] = cookie_val
    payload = {
        "script": {
            "type": "text",
            "input": reply_text,
            "provider": {
                "type": "elevenlabs",
                "voice_id": voice_id,
            },
        },
        "config": {"fluent": True, "pad_audio": 0.0},
        "session_id": did_session_id,
    }

    resp = _req.post(
        f"https://api.d-id.com/talks/streams/{stream_id}",
        json=payload,
        headers=headers,
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        return _error(502, f"D-ID talk stream failed: {resp.status_code}", resp.text[:400])

    result = {"ok": True}
    if agent_id and message:
        result["reply"] = reply_text
        result["session_id"] = chat_session_id
    return _response(200, result)


def handle_stream_end(event: dict, _ctx: Any) -> dict:
    """
    DELETE /stream/end
    Body: { stream_id, session_id }
    Closes the D-ID WebRTC streaming session.
    """
    import requests as _req
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON")
    stream_id = body.get("stream_id", "")
    session_cookie = body.get("session_id", "")
    if not stream_id:
        return _error(400, "stream_id required")
    headers = {
        "Authorization": _did_auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session_cookie:
        cookie_val = ";".join(
            p.strip() for p in session_cookie.split(";")
            if p.strip() and not any(p.strip().lower().startswith(k) for k in ("expires","path","samesite","secure","httponly","domain","max-age"))
        )
        headers["Cookie"] = cookie_val
    _req.delete(
        f"https://api.d-id.com/talks/streams/{stream_id}",
        json={"session_id": session_cookie},
        headers=headers,
        timeout=10,
    )
    return _response(200, {"ok": True})


def handle_video_status(event: dict, _ctx: Any) -> dict:
    """
    GET /video-status/{talk_id}
    Polls D-ID for the status of a pending talk job.
    Response:
        { "status": "created"|"started"|"done"|"error",
          "video_url": "<mp4 url or null>" }
    """
    path = event.get("rawPath") or event.get("path") or ""
    # extract talk_id from path segment
    parts = [p for p in path.split("/") if p and p not in ("dynamic-video-agents-poc-sam", "Prod", "prod", "dev")]
    talk_id = parts[-1] if parts else ""
    if not talk_id or not talk_id.startswith("tlk_"):
        return _error(400, "Missing or invalid talk_id in path")

    try:
        from video import _poll_did_talk_status  # single-shot status check
        status, result_url = _poll_did_talk_status(talk_id)
    except Exception as exc:
        return _error(502, f"D-ID status check failed: {exc}")

    return _response(200, {"status": status, "video_url": result_url})


# ---------------------------------------------------------------------------
# Main Lambda handler — routes by HTTP method + path
# ---------------------------------------------------------------------------

# Route table: (method, path) → handler function
ROUTES = {
    ("GET",    "/health"):       handle_health,
    ("POST",   "/create-agent"): handle_create_agent,
    ("POST",   "/chat"):         handle_chat,
    # D-ID WebRTC streaming proxy
    ("POST",   "/stream/start"): handle_stream_start,
    ("POST",   "/stream/sdp"):   handle_stream_sdp,
    ("POST",   "/stream/ice"):   handle_stream_ice,
    ("POST",   "/stream/talk"):  handle_stream_talk,
    ("DELETE", "/stream/end"):   handle_stream_end,
}

# Regex-matched routes for paths with parameters
import re as _re
PARAM_ROUTES = [
    (_re.compile(r"^/video-status/(?P<talk_id>tlk_[^/]+)$"), "GET", handle_video_status),
]


def handler(event: dict, context: Any) -> dict:
    """
    AWS Lambda entry-point.
    API Gateway HTTP API sends events with:
      event["requestContext"]["http"]["method"]  — HTTP method
      event["rawPath"]                           — path (e.g. /chat)
    """
    method = (
        event.get("requestContext", {})
             .get("http", {})
             .get("method", "")
             .upper()
    )
    # API Gateway HTTP API uses rawPath; REST API uses path
    path = event.get("rawPath") or event.get("path") or ""

    # Strip the stage prefix if present (e.g. /dynamic-video-agents-poc-sam/chat → /chat)
    # SAM local doesn't add stage prefix, deployed does — handle both
    for prefix in ("/dynamic-video-agents-poc-sam", "/Prod", "/prod", "/dev"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break

    logger.debug("Routing: %s %s", method, path)

    # Handle CORS pre-flight
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    route_fn = ROUTES.get((method, path))
    if route_fn is None:
        # Try regex param routes
        for pattern, allowed_method, fn in PARAM_ROUTES:
            if method == allowed_method and pattern.match(path):
                route_fn = fn
                break

    if route_fn is None:
        return _error(404, f"Route not found: {method} {path}")

    try:
        return route_fn(event, context)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.critical("Unhandled exception: %s\n%s", exc, tb)
        return _error(500, "Internal server error", tb)
