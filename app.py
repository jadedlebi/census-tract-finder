import json
import logging
import os
import threading
import time

import boto3
import requests as http_requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, abort, request
from flask_compress import Compress
from google.cloud import bigquery
from google.oauth2 import service_account

load_dotenv()

app = Flask(__name__)
Compress(app)

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "hdma1-242116")
MAPBOX_TOKEN = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
MAPBOX_STYLE = os.environ.get("MAPBOX_STYLE", "")
MAPBOX_SECRET_TOKEN = os.environ.get("MAPBOX_SECRET_TOKEN", "")
MAPBOX_USERNAME = os.environ.get("MAPBOX_USERNAME", "jedlebi")

BANK_CONFIG = {
    "hsbc": {
        "name": "HSBC",
        "table": "hcn.fef26_hsbc_eligible_tracts",
        "tileset": "jedlebi.fef-hsbc-tracts",
        "source_layer": "fef-hsbc-tracts",
    },
    "keybank": {
        "name": "KeyBank",
        "table": "hcn.fef26_keybank_eligible_tracts",
        "tileset": "jedlebi.fef-keybank-tracts",
        "source_layer": "fef-keybank-tracts",
    },
}

CACHE_TTL = 3600
_tract_id_cache: dict[str, tuple[float, list[str]]] = {}


def _build_credentials():
    """Build SA credentials from individual env vars. Returns None on Cloud
    Run where the default service account is used instead."""
    client_email = os.environ.get("GCP_SA_CLIENT_EMAIL")
    private_key = os.environ.get("GCP_SA_PRIVATE_KEY")
    if not client_email or not private_key:
        return None

    info = {
        "type": os.environ.get("GCP_SA_TYPE", "service_account"),
        "project_id": os.environ.get("GCP_SA_PROJECT_ID", PROJECT_ID),
        "private_key_id": os.environ.get("GCP_SA_PRIVATE_KEY_ID", ""),
        "private_key": private_key.replace("\\n", "\n"),
        "client_email": client_email,
        "client_id": os.environ.get("GCP_SA_CLIENT_ID", ""),
        "auth_uri": os.environ.get("GCP_SA_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
        "token_uri": os.environ.get("GCP_SA_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        "auth_provider_x509_cert_url": os.environ.get("GCP_SA_AUTH_PROVIDER_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
        "client_x509_cert_url": os.environ.get("GCP_SA_CLIENT_CERT_URL", ""),
        "universe_domain": os.environ.get("GCP_SA_UNIVERSE_DOMAIN", "googleapis.com"),
    }
    return service_account.Credentials.from_service_account_info(info)


_credentials = _build_credentials()


def _get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID, credentials=_credentials)


def _query_tract_ids(bank: str) -> list[str]:
    """Return only the eligible tract IDs for a bank (no geometry)."""
    now = time.time()
    if bank in _tract_id_cache:
        ts, ids = _tract_id_cache[bank]
        if now - ts < CACHE_TTL:
            return ids

    table = BANK_CONFIG[bank]["table"]
    query = f"""
        SELECT LPAD(CAST(geoid10 AS STRING), 11, '0') AS geoid10
        FROM `{PROJECT_ID}.{table}`
    """
    client = _get_bq_client()
    rows = list(client.query(query).result())
    ids = [row.geoid10 for row in rows]
    _tract_id_cache[bank] = (now, ids)
    return ids


def _prewarm_cache():
    """Pre-warm the tract ID cache in a background thread at startup."""
    def _warm():
        for bank in BANK_CONFIG:
            try:
                _query_tract_ids(bank)
            except Exception as exc:
                logger.warning("Pre-warm failed for %s: %s", bank, exc)
    threading.Thread(target=_warm, daemon=True).start()


_prewarm_cache()


@app.after_request
def set_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://ncrc.org https://*.ncrc.org"
    )
    return response


# ── Page routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return (
        '<!DOCTYPE html><html><head><title>FEF Eligibility</title>'
        '<style>body{font-family:sans-serif;display:flex;justify-content:center;'
        'align-items:center;height:100vh;margin:0;background:#f5f5f5}'
        '.links{text-align:center} a{display:block;margin:12px 0;font-size:20px;'
        'color:#3498db;text-decoration:none} a:hover{text-decoration:underline}'
        '</style></head><body><div class="links">'
        '<h2>Program Eligibility Checker</h2>'
        '<a href="/hsbc">HSBC Eligibility Checker</a>'
        '<a href="/keybank">KeyBank Eligibility Checker</a>'
        '</div></body></html>'
    )


@app.route("/<bank>")
def eligibility_page(bank: str):
    if bank not in BANK_CONFIG:
        abort(404)
    cfg = BANK_CONFIG[bank]
    embed = request.args.get("embed", "").lower() == "true"
    return render_template(
        "check-eligibility.html",
        bank_name=cfg["name"],
        bank_slug=bank,
        mapbox_token=MAPBOX_TOKEN,
        mapbox_style=MAPBOX_STYLE,
        tileset_id=cfg["tileset"],
        source_layer=cfg["source_layer"],
        embed=embed,
    )


# ── API routes ─────────────────────────────────────────────────

@app.route("/api/tracts/<bank>")
def get_tracts(bank: str):
    """Return only the list of eligible tract IDs (no geometry)."""
    if bank not in BANK_CONFIG:
        return jsonify({"error": "Unknown bank"}), 404

    ids = _query_tract_ids(bank)
    resp = jsonify(ids)
    resp.headers["Cache-Control"] = f"public, max-age={CACHE_TTL}"
    return resp


@app.route("/api/geocode")
def geocode_proxy():
    """Server-side proxy for Census Bureau geocoder, avoiding browser CORS."""
    address = request.args.get("address", "")
    if not address:
        return jsonify({"error": "address parameter required"}), 400

    census_url = (
        "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
        f"?address={http_requests.utils.quote(address)}"
        "&benchmark=Public_AR_Current"
        "&vintage=Census2010_Current"
        "&format=json"
    )
    try:
        r = http_requests.get(census_url, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/fcc")
def fcc_proxy():
    """Server-side proxy for FCC census area API, avoiding browser CORS."""
    lat = request.args.get("lat", "")
    lon = request.args.get("lon", "")
    if not lat or not lon:
        return jsonify({"error": "lat and lon parameters required"}), 400

    fcc_url = (
        f"https://geo.fcc.gov/api/census/area"
        f"?lat={lat}&lon={lon}&censusYear=2010&format=json"
    )
    try:
        r = http_requests.get(fcc_url, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ── Admin: one-time tileset upload to Mapbox ───────────────────

@app.route("/admin/upload-tiles/<bank>")
def upload_tiles(bank: str):
    """Query BigQuery for full-resolution tracts and upload as a Mapbox
    tileset. Requires MAPBOX_SECRET_TOKEN with uploads:write scope."""
    if bank not in BANK_CONFIG:
        return jsonify({"error": "Unknown bank"}), 404
    if not MAPBOX_SECRET_TOKEN:
        return jsonify({"error": "MAPBOX_SECRET_TOKEN not configured"}), 500

    cfg = BANK_CONFIG[bank]
    tileset_name = cfg["tileset"].split(".", 1)[1]

    table = cfg["table"]
    query = f"""
        SELECT
            LPAD(CAST(g.geoid10 AS STRING), 11, '0') AS geoid10,
            ST_ASGEOJSON(g.geometry) AS geojson_geom
        FROM `{PROJECT_ID}.geo.census10_geo` g
        INNER JOIN `{PROJECT_ID}.{table}` t
            ON LPAD(CAST(g.geoid10 AS STRING), 11, '0')
             = LPAD(CAST(t.geoid10 AS STRING), 11, '0')
    """
    client = _get_bq_client()
    rows = list(client.query(query).result())

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"geoid10": row.geoid10},
                "geometry": json.loads(row.geojson_geom),
            }
            for row in rows
        ],
    }

    geojson_bytes = json.dumps(geojson).encode("utf-8")

    creds_url = f"https://api.mapbox.com/uploads/v1/{MAPBOX_USERNAME}/credentials"
    creds_resp = http_requests.get(
        creds_url, params={"access_token": MAPBOX_SECRET_TOKEN}
    )
    creds_resp.raise_for_status()
    creds = creds_resp.json()

    s3 = boto3.client(
        "s3",
        aws_access_key_id=creds["accessKeyId"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
        region_name="us-east-1",
    )
    s3.put_object(Bucket=creds["bucket"], Key=creds["key"], Body=geojson_bytes)

    upload_resp = http_requests.post(
        f"https://api.mapbox.com/uploads/v1/{MAPBOX_USERNAME}",
        params={"access_token": MAPBOX_SECRET_TOKEN},
        json={
            "url": creds["url"],
            "tileset": cfg["tileset"],
            "name": tileset_name,
        },
    )
    upload_resp.raise_for_status()
    result = upload_resp.json()

    return jsonify({
        "status": "processing",
        "upload_id": result.get("id"),
        "tileset": cfg["tileset"],
        "features": len(rows),
        "message": "Tileset is being processed by Mapbox. This may take a few minutes.",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
