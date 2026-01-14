"""
Excel Column Comparison Tool
Compares selected columns between two Excel files and displays differences.
"""

import os
import re
import time
import logging
import json
import requests
from datetime import datetime, timezone
import base64
import secrets
import hmac
import hashlib
from urllib.parse import urlencode
from flask import Flask, render_template, request, jsonify, session, Response, redirect, url_for, send_from_directory, make_response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import pandas as pd
import uuid

# Load environment variables
load_dotenv()

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper())
logger = logging.getLogger(__name__)

# Simple in-process cache for Ninja OAuth tokens
TOKEN_CACHE_FILE = '.ninja_token_cache.json'
_NINJA_TOKEN_CACHE = {
    'access_token': None,
    'refresh_token': None,
    'expires_at': 0,
    'api_url': None,
    'grant_type': None,
}

def _save_ninja_token_cache():
    try:
        with open(TOKEN_CACHE_FILE, 'w') as f:
            json.dump(_NINJA_TOKEN_CACHE, f)
    except Exception as e:
        logger.warning('Failed to save Ninja token cache: %s', e)

def _load_ninja_token_cache():
    if os.path.exists(TOKEN_CACHE_FILE):
        try:
            with open(TOKEN_CACHE_FILE, 'r') as f:
                data = json.load(f)
                _NINJA_TOKEN_CACHE.update(data)
                logger.info('Loaded Ninja token cache from %s', TOKEN_CACHE_FILE)
        except Exception as e:
            logger.warning('Failed to load Ninja token cache: %s', e)

# Initial load
_load_ninja_token_cache()
# Global store for file paths, keyed by frontend clientId.
# This acts as a backup to the Flask session, which can be unstable in some environments.
_CLIENT_FILE_STORE = {}

def _get_client_id(data=None):
    """Extract client_id from request JSON or fallback to session."""
    if not data:
        data = request.get_json(silent=True) or {}
    return data.get('client_id')

def _set_session_file(file_id, filepath, client_id=None):
    """Store filepath in both session and global store."""
    file_id = str(file_id)
    if 'files' not in session:
        session['files'] = {}
    session['files'][file_id] = filepath
    session.modified = True
    
    if client_id:
        if client_id not in _CLIENT_FILE_STORE:
            _CLIENT_FILE_STORE[client_id] = {'files': {}, 'last_access': time.time()}
        _CLIENT_FILE_STORE[client_id]['files'][file_id] = filepath
        _CLIENT_FILE_STORE[client_id]['last_access'] = time.time()
        logger.info(f'Stored file {file_id} in global store for client {client_id}')

def _get_session_files(client_id=None):
    """Get files from session, falling back to global store if missing."""
    files = session.get('files', {})

    if client_id and client_id in _CLIENT_FILE_STORE:
        store_files = _CLIENT_FILE_STORE[client_id].get('files', {})
        if store_files:
            _CLIENT_FILE_STORE[client_id]['last_access'] = time.time()
            if not files:
                files = dict(store_files)
                session['files'] = files
                session.modified = True
                logger.info(f'Healed session for client {client_id} from global store')
            elif isinstance(files, dict):
                merged = {**store_files, **files}
                if merged != files:
                    session['files'] = merged
                    session.modified = True
                    files = merged
                    logger.info(f'Merged session files for client {client_id} from global store')

    return files


# In-process cache for Active Directory device snapshots received from Ninja
# Key: (client_name, days) -> {path, count, received_at}
_AD_CACHE = {}

# AD inventory storage (written by the Ninja-run PowerShell script)
_AD_CUSTOM_FIELD_NAME = 'ADInventoryJson'

# One-time nonces for AD intake (legacy; kept for backwards compatibility)
# Key: nonce -> {client, days, signing_key, expires_at}
_AD_INTAKE_NONCES = {}
_AD_INTAKE_NONCE_TTL_SECONDS = 15 * 60


def _get_ninja_api_url():
    """Return the NinjaRMM API base URL from environment or default."""
    return os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')


def _sanitize_client_name(client):
    """Clean client name: remove emojis/non-ASCII, extra whitespace, newlines."""
    if not client:
        return ""
    clean = ' '.join(client.split())
    clean = clean.encode('ascii', 'ignore').decode('ascii').strip()
    return clean


def _prune_ad_intake_nonces(now=None):
    now = now or time.time()
    expired = [t for t, v in _AD_INTAKE_NONCES.items() if v.get('expires_at', 0) <= now]
    for t in expired:
        _AD_INTAKE_NONCES.pop(t, None)


def _fetch_with_retry(url, headers=None, auth=None, params=None, timeout=30, max_retries=3):
    """
    Fetch URL with retry logic and exponential backoff.
    Handles transient failures and rate limiting.
    """
    response = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, auth=auth, params=params, timeout=timeout)
            
            # Success
            if response.status_code == 200:
                return response
            
            # Rate limited - use exponential backoff
            if response.status_code == 429:
                wait_time = (2 ** attempt) * 1  # 1s, 2s, 4s
                logger.warning(f"Rate limited on {url}, waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                    continue
            
            # Other errors - return immediately on last attempt
            if attempt == max_retries - 1:
                return response
            
            # Retry on 5xx errors
            if response.status_code >= 500:
                wait_time = (2 ** attempt) * 0.5  # 0.5s, 1s, 2s
                logger.warning(f"Server error {response.status_code} on {url}, retrying in {wait_time}s")
                time.sleep(wait_time)
                continue
            
            # Don't retry on 4xx errors (except 429)
            return response
            
        except requests.exceptions.Timeout:
            if attempt == max_retries - 1:
                raise
            wait_time = (2 ** attempt) * 0.5
            logger.warning(f"Timeout on {url}, retrying in {wait_time}s")
            time.sleep(wait_time)
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise
            wait_time = (2 ** attempt) * 0.5
            logger.warning(f"Request error on {url}: {e}, retrying in {wait_time}s")
            time.sleep(wait_time)
    
    # Should not reach here, but return last response if we do
    return response


def fix_encoding(value):
    """
    Fix common encoding issues in text values.
    """
    if not value:
        return ''
    
    # Ensure proper string encoding
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    value = str(value)
    
    # Fix common encoding issues (smart quotes to regular quotes)
    value = value.replace('\u2019', "'").replace('\u2018', "'")  # ' and '
    value = value.replace('\u201c', '"').replace('\u201d', '"')  # " and "
    value = value.replace('\u2013', '-').replace('\u2014', '-')  # – and —
    
    # Fix mojibake patterns (common misencoded UTF-8)
    # Use list of tuples to ensure proper ordering (longest patterns first to avoid substring issues)
    replacements = [
        ('â€™', "'"),
        ('â€˜', "'"),
        ('â€œ', '"'),
        ('â€"', '-'),
        ('â€"', '-'),
        ('Ã©', 'é'),
        ('Ã¨', 'è'),
        ('Ã¡', 'á'),
        ('Ã ', 'à'),
    ]
    for bad, good in replacements:
        value = value.replace(bad, good)
    
    return value


def normalize_value(value, log_transformations=False):
    """
    Normalize a value for comparison by:
    - Converting to lowercase
    - Removing common suffixes (.local, .lan, .home, etc.)
    - Replacing spaces, hyphens, underscores with nothing
    - Removing apostrophes and special characters
    
    Args:
        value: The value to normalize
        log_transformations: If True, log normalization steps for debugging
    """
    if not value:
        return ''
    
    original = value
    
    # Fix encoding first
    value = fix_encoding(value)
    if log_transformations and value != original:
        logger.debug(f"Encoding fix: '{original}' -> '{value}'")
    
    # Convert to lowercase
    normalized = value.lower().strip()
    if log_transformations and normalized != value:
        logger.debug(f"Lowercase: '{value}' -> '{normalized}'")
    
    # Remove common network suffixes
    suffixes = ['.local', '.lan', '.home', '.internal', '.localdomain', '.domain']
    for suffix in suffixes:
        if normalized.endswith(suffix):
            before = normalized
            normalized = normalized[:-len(suffix)]
            if log_transformations:
                logger.debug(f"Suffix removal ({suffix}): '{before}' -> '{normalized}'")
            break
    
    # Remove apostrophes and common special characters
    before = normalized
    normalized = re.sub(r"['\"`]", '', normalized)
    if log_transformations and normalized != before:
        logger.debug(f"Special char removal: '{before}' -> '{normalized}'")
    
    # Replace spaces, hyphens, underscores, dots with nothing (normalize separators)
    before = normalized
    normalized = re.sub(r'[\s\-_\.]+', '', normalized)
    if log_transformations and normalized != before:
        logger.debug(f"Separator removal: '{before}' -> '{normalized}'")
    
    if log_transformations and normalized != original:
        logger.info(f"Normalization complete: '{original}' -> '{normalized}'")
    
    return normalized

app = Flask(__name__)

# Use a stable secret key if provided; falls back to a stable default for local dev stability.
# NOTE: In production, always set FLASK_SECRET_KEY in your .env file.
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY') or 'comparison-tool-stable-dev-key-8b92'

# Session configuration - optimized for local HTTP development
app.config['PERMANENT_SESSION_LIFETIME'] = 7200  # 2 hours
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['SESSION_COOKIE_DOMAIN'] = None


def _require_basic_auth():
    if os.getenv('ENABLE_BASIC_AUTH', '0') not in ('1', 'true', 'True'):
        return None

    username = os.getenv('BASIC_AUTH_USERNAME')
    password = os.getenv('BASIC_AUTH_PASSWORD')
    if not username or not password:
        return jsonify({'error': 'Server auth not configured'}), 500

    auth = request.authorization
    if not auth or auth.username != username or auth.password != password:
        return Response('Authentication required', 401, {'WWW-Authenticate': 'Basic realm="Comparison"'})

    return None


@app.before_request
def _normalize_session_files_keys():
    auth_err = _require_basic_auth()
    if auth_err:
        return auth_err

    # Make session permanent to persist across requests
    session.permanent = True
    
    # Log session info for debugging
    session_cookie = request.cookies.get('session', 'NO_COOKIE')
    session_id = session_cookie[:20] if session_cookie != 'NO_COOKIE' else 'NO_COOKIE'
    all_cookies = list(request.cookies.keys())
    logger.info(f'Request {request.path} - Session ID: {session_id} - Files: {list(session.get("files", {}).keys())} - All cookies: {all_cookies}')
    
    # Log session creation
    if 'session_created' not in session:
        logger.info(f'New session created for {request.path}')
        session['session_created'] = True

    # Use the diagnostic client_id if available to heal session
    client_id = request.get_json(silent=True).get('client_id') if request.is_json else None
    files = _get_session_files(client_id)
    
    if isinstance(files, dict):
        session['files'] = {str(k): v for k, v in files.items()}
        session.modified = True


@app.after_request
def _set_security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    # SPA uses inline <style>/<script>, so CSP must allow 'unsafe-inline' unless refactored to nonces.
    resp.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    return resp

app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def _require_csrf():
    if request.method != 'POST':
        return None

    token = request.headers.get('X-CSRF-Token')
    if not token:
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            token = payload.get('csrf_token')
        else:
            token = request.form.get('csrf_token')

    if not token or token != session.get('csrf_token'):
        return jsonify({'error': 'CSRF token missing or invalid'}), 403
    return None


def _get_ninja_auth(api_url):
    """Return (headers, auth) for NinjaRMM calls using authorization_code OAuth flow."""
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')

    if not (client_id and client_secret):
        raise ValueError('NinjaRMM OAuth credentials not configured. Set NINJARMM_CLIENT_ID and NINJARMM_CLIENT_SECRET.')

    now = time.time()

    # Check if we have a valid cached token
    if (
        _NINJA_TOKEN_CACHE.get('access_token')
        and _NINJA_TOKEN_CACHE.get('api_url') == api_url
        and _NINJA_TOKEN_CACHE.get('expires_at', 0) > (now + 30)
    ):
        return ({'Accept': 'application/json', 'Authorization': f"Bearer {_NINJA_TOKEN_CACHE['access_token']}"}, None)

    # Try to refresh if we have a refresh token
    if _NINJA_TOKEN_CACHE.get('refresh_token'):
        try:
            return _refresh_ninja_token(api_url, client_id, client_secret)
        except Exception as e:
            logger.warning('NinjaRMM token refresh failed: %s', e)
            # Clear cache so user re-authorizes
            _NINJA_TOKEN_CACHE.update({
                'access_token': None,
                'refresh_token': None,
                'expires_at': 0,
                'api_url': None,
                'grant_type': None,
            })

    raise ValueError('NinjaRMM authorization required. Visit /ninjarmm/oauth/authorize to connect.')


def _refresh_ninja_token(api_url, client_id, client_secret):
    """Refresh the OAuth token using the stored refresh token."""
    refresh_token = _NINJA_TOKEN_CACHE.get('refresh_token')
    if not refresh_token:
        raise ValueError('No refresh token available')

    # Token refresh happens on app.ninjarmm.com (central OAuth server)
    oauth_url = os.getenv('NINJARMM_OAUTH_URL', 'https://app.ninjarmm.com')

    token_response = requests.post(
        f'{oauth_url}/ws/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client_id,
            'client_secret': client_secret,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30
    )

    if token_response.status_code != 200:
        raise ValueError(f"NinjaRMM token refresh failed: {token_response.status_code}")

    token_json = token_response.json()
    access_token = token_json.get('access_token')
    new_refresh_token = token_json.get('refresh_token', refresh_token)
    expires_in = int(token_json.get('expires_in') or 3600)

    if not access_token:
        raise ValueError('NinjaRMM token refresh failed: missing access_token')

    now = time.time()
    _NINJA_TOKEN_CACHE.update({
        'access_token': access_token,
        'refresh_token': new_refresh_token,
        'expires_at': now + expires_in,
        'api_url': api_url,
    })
    _save_ninja_token_cache()

    logger.info('NinjaRMM OAuth token refreshed successfully')
    return ({'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}, None)


def _lookup_ninja_script_uid(api_url, headers, auth, script_id, device_id=None):
    """Best-effort lookup of a script UID for a numeric script_id.

    Some Ninja endpoints require/accept a `uid` field when running scripts; providing it can avoid
    server-side errors for certain accounts/scripts.
    """
    possible_endpoints = [
        f'{api_url}/v2/automation/scripts',
        f'{api_url}/v2/queries/scripts',
        f'{api_url}/v2/scripts'
    ]

    for endpoint in possible_endpoints:
        try:
            r = requests.get(endpoint, headers=headers, auth=auth, timeout=15)
            logger.debug('Script UID lookup: endpoint=%s status=%s', endpoint, r.status_code)
            if r.status_code != 200:
                continue

            data = r.json()
            if isinstance(data, dict):
                # Some APIs wrap results (best-effort).
                data = (
                    data.get('data')
                    or data.get('items')
                    or data.get('results')
                    or data.get('scripts')
                    or []
                )

            if not isinstance(data, list):
                logger.debug('Script UID lookup: endpoint=%s returned non-list data type=%s', endpoint, type(data))
                continue

            logger.debug('Script UID lookup: endpoint=%s returned %d scripts', endpoint, len(data))
            for s in data:
                if not isinstance(s, dict):
                    continue
                sid = s.get('id')
                try:
                    if int(sid) != int(script_id):
                        continue
                except Exception:
                    continue

                uid = s.get('uid') or s.get('scriptUid')
                if uid:
                    logger.info('Script UID found via %s: script_id=%s uid=%s', endpoint, script_id, uid)
                    return uid
                else:
                    # Found the script but it has no UID - log available fields
                    logger.warning('Script found but no UID: script_id=%s available_fields=%s', script_id, list(s.keys()))
        except Exception as e:
            logger.debug('Script UID lookup: endpoint=%s error=%s', endpoint, str(e))
            continue

    # Alternative: try device-specific scripting options endpoint if device_id provided
    if device_id:
        try:
            scripting_opts_endpoint = f'{api_url}/v2/device/{device_id}/scripting/options'
            r = requests.get(scripting_opts_endpoint, headers=headers, auth=auth, timeout=15)
            logger.debug('Script UID lookup via scripting options: status=%s', r.status_code)
            if r.status_code == 200:
                opts = r.json()
                for s in (opts.get('scripts') or []):
                    if not isinstance(s, dict):
                        continue
                    sid = s.get('id')
                    try:
                        if int(sid) != int(script_id):
                            continue
                    except Exception:
                        continue
                    uid = s.get('uid') or s.get('scriptUid')
                    if uid:
                        logger.info('Script UID found via scripting options: script_id=%s uid=%s', script_id, uid)
                        return uid
                    else:
                        logger.warning('Script in scripting options has no UID: script_id=%s fields=%s', script_id, list(s.keys()))
        except Exception as e:
            logger.debug('Script UID lookup via scripting options error: %s', str(e))

    logger.warning('Script UID not found for script_id=%s (tried all endpoints)', script_id)
    return None


def _format_ninja_parameters_powershell(params: dict) -> str:
    # PowerShell-style parameters: -ParamName "value" all on one line
    parts = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        # Quote string values, don't quote numbers
        if isinstance(v, str):
            parts.append(f'-{k} "{v}"')
        else:
            parts.append(f'-{k} {v}')
    return " ".join(parts)


def _extract_ninja_custom_field(device_data, field_name: str):
    """Best-effort extraction of a custom field value from a Ninja device payload."""
    if not field_name:
        return None

    # Case 1: device_data is the exact value (rare)
    if isinstance(device_data, (str, int, float, bool)) and not isinstance(device_data, dict):
        # We can't know the name if it's just a value, so skip
        pass

    # Case 2: device_data is a dictionary
    if isinstance(device_data, dict):
        # Direct key match (case-insensitive)
        for k, v in device_data.items():
            if k.lower() == field_name.lower():
                # If the value is a dict with a 'value' key, it's Ninja's wrapper object
                if isinstance(v, dict) and 'value' in v:
                    return v['value']
                return v

        # Look in known nested keys
        for key in ('customFields', 'custom_fields', 'fields', 'properties'):
            blob = device_data.get(key)
            if blob:
                found = _extract_ninja_custom_field(blob, field_name)
                if found is not None:
                    return found

        # One-level deep scan of all other dictionary values
        for v in device_data.values():
            if isinstance(v, (dict, list)):
                found = _extract_ninja_custom_field(v, field_name)
                if found is not None:
                    return found

    # Case 3: device_data is a list
    if isinstance(device_data, list):
        for it in device_data:
            if isinstance(it, dict):
                # Try common list item formats: {name: '...', value: '...'} or {fieldName: '...', value: '...'}
                name = it.get('name') or it.get('fieldName') or it.get('key') or it.get('label')
                if isinstance(name, str) and name.lower() == field_name.lower():
                    val = it.get('value')
                    if isinstance(val, dict) and 'value' in val:
                        return val['value']
                    return val
                # Recurse if the item itself contains fields
                found = _extract_ninja_custom_field(it, field_name)
                if found is not None:
                    return found

    return None


def _get_ninja_device_custom_field(api_url, headers, auth, device_id: int, field_name: str):
    endpoints = [
        f'{api_url}/v2/device/{device_id}/custom-fields',
        f'{api_url}/v2/device/{device_id}',
        f'{api_url}/api/v2/device/{device_id}',
    ]
    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, headers=headers, auth=auth, timeout=15)
            if r.status_code != 200:
                continue
            payload = r.json()
            value = _extract_ninja_custom_field(payload, field_name)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)
        except Exception:
            continue
    return None


def _get_ninja_organization_custom_field(api_url, headers, auth, org_id: int, field_name: str):
    endpoints = [
        f'{api_url}/v2/organization/{org_id}/custom-fields',
        f'{api_url}/v2/organization/{org_id}',
        f'{api_url}/api/v2/organization/{org_id}',
    ]
    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, headers=headers, auth=auth, timeout=15)
            if r.status_code != 200:
                continue
            payload = r.json()
            value = _extract_ninja_custom_field(payload, field_name)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return str(value)
        except Exception:
            continue
    return None


def _get_ninja_all_custom_fields(api_url, headers, auth, entity_type, entity_id):
    """Fetch and extract all custom fields for an org or device."""
    if entity_type == 'org':
        endpoints = [
            f'{api_url}/v2/organization/{entity_id}/custom-fields',
            f'{api_url}/v2/organization/{entity_id}',
            f'{api_url}/api/v2/organization/{entity_id}'
        ]
    else:
        endpoints = [
            f'{api_url}/v2/device/{entity_id}/custom-fields',
            f'{api_url}/v2/device/{entity_id}',
            f'{api_url}/api/v2/device/{entity_id}'
        ]

    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, headers=headers, auth=auth, timeout=15)
            if r.status_code == 200:
                payload = r.json()
                results = {}
                
                # If it's already a flat dict of field_name: value
                if isinstance(payload, dict) and not any(k in payload for k in ('customFields', 'fields', 'properties', 'id', 'systemName')):
                    return payload
                
                # If it's a list of field objects (returned by some /custom-fields versions)
                if isinstance(payload, list):
                    for it in payload:
                        if isinstance(it, dict):
                            name = it.get('name') or it.get('fieldName') or it.get('key') or it.get('label')
                            if name:
                                results[name] = it.get('value')
                    if results: return results

                # Otherwise, look in known nested keys
                for key in ('customFields', 'custom_fields', 'fields', 'properties'):
                    blob = payload.get(key)
                    if isinstance(blob, dict):
                        results.update(blob)
                    elif isinstance(blob, list):
                        for it in blob:
                            if isinstance(it, dict):
                                name = it.get('name') or it.get('fieldName') or it.get('key') or it.get('label')
                                if name:
                                    results[name] = it.get('value')
                
                if results: return results
                
                # If we're at the root and it looks like a device object but no fields found yet
                # results is still empty, let it try next endpoint
        except Exception:
            continue
    return None


def _repair_json(val_str):
    """Attempt several strategies to repair malformed or loose JSON (commonly from PowerShell)."""
    if not val_str or not isinstance(val_str, str):
        return None
        
    s = val_str.strip()
    
    # Strategy 1: Simple single-to-double quote swap (already done in some places, but good to have here)
    if "'" in s and '"' not in s: # Looks like single-quote JSON
        try:
            return json.loads(s.replace("'", '"'))
        except Exception:
            pass

    # Strategy 2: Loose keys (unquoted keys like { days: 30 })
    try:
        # Match words followed by colon: ^  days: -> "days":
        # and unquoted string values that look like UUIDs or ISO dates
        import re
        repaired = s
        # Quote keys
        repaired = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', repaired)
        # Quote unquoted string values (basic check for word-only values that aren't numbers/bools)
        # This regex looks for :  VALUE where VALUE is word/dash/period but not a number
        repaired = re.sub(r'(:\s*)([a-zA-Z][a-zA-Z0-9\-\._]*)\s*([,}])', r'\1"\2"\3', repaired)
        return json.loads(repaired)
    except Exception:
        pass

    # Strategy 3: Handle truncation
    if s.endswith('...TRUNCATED...') or (s.count('{') > s.count('}')):
        try:
            # Try to close the objects/arrays
            temp = s.replace('...TRUNCATED...', '').strip()
            # Remove trailing comma if any
            temp = re.sub(r',\s*$', '', temp)
            # Add closing braces until it might parse or we hit a limit
            for _ in range(5):
                try:
                    return json.loads(temp + '}')
                except Exception:
                    temp += '}'
        except Exception:
            pass

    return None


def _regex_extract_ad_data(val_str):
    """Pure regex fallback to get key data if JSON parsing fails completely."""
    import re
    data = {}
    
    # Extract generatedAtUtc
    m = re.search(r'generatedAtUtc\s*[:=]\s*["\']?([\d\-T:\.Z\+ ]+)["\']?', val_str, re.I)
    if m:
        data['generatedAtUtc'] = m.group(1).strip()
    else:
        # Fallback timestamp if missing, so we don't reject the data
        from datetime import datetime
        data['generatedAtUtc'] = datetime.utcnow().isoformat() + 'Z'
        
    # Extract days
    m = re.search(r'days\s*[:=]\s*(\d+)', val_str, re.I)
    if m:
        data['days'] = int(m.group(1))

    # Extract names
    names = []
    # If we see workstations: [ ... ] or workstations: { ... }
    m_arr = re.search(r'(?:workstations|computers|data)\s*[:=]\s*[\[\{](.*?)[\}\]]', val_str, re.S | re.I)
    if m_arr:
        # Match contents separated by commas (handles quoted AND unquoted)
        # Split by comma or semicolon
        raw_items = re.split(r'[,;]', m_arr.group(1))
        for it in raw_items:
            clean = it.strip().strip('"\' \t\n\r')
            if clean and len(clean) > 2:
                names.append(clean)
    else:
        # Last resort: find all quoted strings that look like computer names
        all_quoted = re.findall(r'["\']([^"\'\[\],:{}]+)["\']', val_str)
        exclude = {'generatedAtUtc', 'days', 'runId', 'workstations', 'computers', 'data', 'client'}
        names = [n.strip() for n in all_quoted if n.strip() not in exclude and len(n.strip()) > 2]

    data['workstations'] = names
    return data


def _extract_and_validate_ad_data(val_str):
    """Unified robust extraction from a custom field string."""
    if not val_str or not isinstance(val_str, str) or not val_str.strip():
        return None
        
    val_str = val_str.strip()
    parsed = None
    
    # 1. Normal JSON load
    try:
        parsed = json.loads(val_str)
    except Exception:
        # 2. Repair attempt
        parsed = _repair_json(val_str)
        
    # 3. Regex Fallback
    if not parsed or not isinstance(parsed, dict) or 'workstations' not in parsed:
        # If workstations is missing or it's not a dict, try regex
        parsed = _regex_extract_ad_data(val_str)

    if not parsed or not isinstance(parsed, dict):
        return None
        
    # Ensure workstations/computers key exists
    if 'workstations' not in parsed and 'computers' in parsed:
        parsed['workstations'] = parsed['computers']
        
    # Final check for workstations
    if 'workstations' not in parsed:
        return None
        
    # Ensure timestamp exists
    if 'generatedAtUtc' not in parsed:
        from datetime import datetime
        parsed['generatedAtUtc'] = datetime.utcnow().isoformat() + 'Z'
        
    return parsed


def read_excel_file(filepath):
    """Read Excel file and return dataframe with sheet info."""
    try:
        # Try reading with openpyxl first (for .xlsx)
        xl = pd.ExcelFile(filepath, engine='openpyxl')
    except Exception:
        try:
            # Fall back to xlrd for older .xls files
            xl = pd.ExcelFile(filepath, engine='xlrd')
        except Exception as e:
            raise ValueError(f"Could not read Excel file: {str(e)}")
    
    return xl


@app.route('/')
def index():
    """Render the main page."""
    # Note: Session files are NOT cleared on page load to preserve ongoing work
    # Files are naturally cleaned up by age-based pruning in /cleanup
    
    # Generate CSRF token and ensure it's in session
    csrf_token = session.get('csrf_token')
    if not csrf_token:
        csrf_token = uuid.uuid4().hex
        session['csrf_token'] = csrf_token
        session.modified = True
    
    return render_template('index.html', csrf_token=csrf_token)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.svg', mimetype='image/svg+xml')


@app.route('/compare', methods=['POST'])
def compare_columns():
    """Compare selected columns from both files using set-based comparison."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json

    file1_id = str(data.get('file1_id', '1'))
    file2_id = str(data.get('file2_id', '2'))
    file3_id = str(data.get('file3_id', '')) # Optional AD file

    file1_sheet = data.get('file1_sheet')
    file1_column = data.get('file1_column')
    file2_sheet = data.get('file2_sheet')
    file2_column = data.get('file2_column')

    client_id = _get_client_id(data)
    files = _get_session_files(client_id)

    logger.info(f'Compare request: file1_id={file1_id}, file2_id={file2_id}, file3_id={file3_id}, client_id={client_id}')
    logger.info(f'Files available: {list(files.keys())}')

    if not files:
        logger.error(f'No files found for client {client_id} (Session: {list(session.get("files", {}).keys())})')
        return jsonify({'error': 'Files not found. Please upload again.'}), 400

    if file1_id not in files or file2_id not in files:
        logger.error(f'Missing files. Requested: {file1_id}, {file2_id}. Available: {list(files.keys())}')
        return jsonify({'error': 'Both files must be uploaded.'}), 400

    # Log the actual paths being used
    file1_path = files[file1_id]
    file2_path = files[file2_id]
    logger.info(f'Reading file1 from: {file1_path}')
    logger.info(f'Reading file2 from: {file2_path}')
    logger.info(f'File1 exists: {os.path.exists(file1_path)}')
    logger.info(f'File2 exists: {os.path.exists(file2_path)}')

    try:
        # Read files
        df1 = pd.read_excel(file1_path, sheet_name=file1_sheet)
        df2 = pd.read_excel(file2_path, sheet_name=file2_sheet)
        
        # Load AD data if provided
        set3_norm = set()
        if file3_id and file3_id in files:
            try:
                df3 = pd.read_excel(files[file3_id]) # AD files are always flat-ish
                # AD schema uses 'Name' column from _save_ad_inventory_to_excel
                col3_name = 'Name' if 'Name' in df3.columns else df3.columns[0]
                col3_data = df3[col3_name].dropna().astype(str).tolist()
                set3_norm = {normalize_value(v) for v in col3_data if normalize_value(v)}
                logger.info('Loaded AD data for comparison: %d items', len(set3_norm))
            except Exception as e:
                logger.warning('Failed to load AD data for comparison: %s', str(e))
        
        # Get the columns and filter out empty values
        col1_data = df1[file1_column].dropna().astype(str).tolist()
        col2_data = df2[file2_column].dropna().astype(str).tolist()
        
        # Fix encoding and filter out empty strings
        col1_data = [fix_encoding(v).strip() for v in col1_data if str(v).strip()]
        col2_data = [fix_encoding(v).strip() for v in col2_data if str(v).strip()]
        
        # Create mappings: normalized -> original values
        norm_to_orig1 = {}
        for v in col1_data:
            norm = normalize_value(v)
            if norm and norm not in norm_to_orig1:
                norm_to_orig1[norm] = v
        
        norm_to_orig2 = {}
        for v in col2_data:
            norm = normalize_value(v)
            if norm and norm not in norm_to_orig2:
                norm_to_orig2[norm] = v
        
        # Compare using normalized values
        set1_norm = set(norm_to_orig1.keys())
        set2_norm = set(norm_to_orig2.keys())
        
        # Find differences using normalized comparison
        only_in_file1_norm = set1_norm - set2_norm
        only_in_file2_norm = set2_norm - set1_norm
        in_both_norm = set1_norm & set2_norm
        
        # Prefix matching for 15-char truncation
        prefix_matches = []
        matched_from_file1 = set()
        matched_from_file2 = set()
        min_prefix_length = 10
        
        for norm1 in list(only_in_file1_norm):
            orig1 = norm_to_orig1[norm1]
            prefix1 = norm1[:15] if len(norm1) > 15 else norm1
            
            for norm2 in list(only_in_file2_norm):
                if norm2 in matched_from_file2:
                    continue
                orig2 = norm_to_orig2[norm2]
                prefix2 = norm2[:15] if len(norm2) > 15 else norm2
                
                if len(norm1) >= min_prefix_length and len(norm2) >= min_prefix_length:
                    if prefix1 == prefix2 or norm1.startswith(norm2) or norm2.startswith(norm1):
                        prefix_matches.append({
                            'file1': orig1,
                            'file2': orig2,
                            'matched_on': 'prefix',
                            'in_ad': norm1 in set3_norm or norm2 in set3_norm if set3_norm else None
                        })
                        matched_from_file1.add(norm1)
                        matched_from_file2.add(norm2)
                        break
        
        # Remove prefix-matched items
        only_in_file1_norm -= matched_from_file1
        only_in_file2_norm -= matched_from_file2
        
        # Construct result objects with AD status
        def build_result_list(norms, map_orig):
            res = []
            for n in sorted(norms):
                item = {'name': map_orig[n]}
                if set3_norm:
                    item['in_ad'] = n in set3_norm
                res.append(item)
            return res

        only_in_file1 = build_result_list(only_in_file1_norm, norm_to_orig1)
        only_in_file2 = build_result_list(only_in_file2_norm, norm_to_orig2)
        in_both = build_result_list(in_both_norm, norm_to_orig1)

        # Computers only in AD (not in S1 or Ninja)
        only_in_ad = []
        if set3_norm:
            only_in_ad_norm = set3_norm - set1_norm - set2_norm - matched_from_file1 - matched_from_file2
            only_in_ad = [{'name': n, 'in_ad': True, 's1_missing': True, 'ninja_missing': True} for n in sorted(only_in_ad_norm)]
        
        # Calculate statistics
        unique_file1 = len(set1_norm)
        unique_file2 = len(set2_norm)
        unique_ad = len(set3_norm)
        
        # Calculate match percentage with proper handling of empty files
        total_matches = len(in_both) + len(prefix_matches)
        max_unique = max(unique_file1, unique_file2)
        if max_unique > 0:
            match_percentage = round(total_matches / max_unique * 100, 1)
        else:
            # Both files are empty
            match_percentage = 0.0 if total_matches == 0 else 100.0
        
        return jsonify({
            'success': True,
            'only_in_file1': [x['name'] for x in only_in_file1],
            'only_in_file2': [x['name'] for x in only_in_file2],
            'in_both': [x['name'] for x in in_both],
            'only_in_ad': [x['name'] for x in only_in_ad],
            'results_file1': only_in_file1,
            'results_file2': only_in_file2,
            'results_common': in_both,
            'results_ad': only_in_ad,
            'prefix_matches': prefix_matches,
            'ad_attached': bool(set3_norm),
            'stats': {
                'total_file1': len(col1_data),
                'total_file2': len(col2_data),
                'unique_file1': unique_file1,
                'unique_file2': unique_file2,
                'unique_ad': unique_ad,
                'only_in_file1_count': len(only_in_file1),
                'only_in_file2_count': len(only_in_file2),
                'only_in_ad_count': len(only_in_ad),
                'common_count': len(in_both),
                'prefix_match_count': len(prefix_matches),
                'match_percentage': match_percentage
            }
        })
    
    except KeyError as e:
        logger.error(f'Column not found error in compare: {str(e)}', exc_info=True)
        return jsonify({'error': f'Column not found: {str(e)}'}), 400
    except Exception as e:
        logger.error(f'Comparison error: {str(e)}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/sentinelone/sites', methods=['GET'])
def get_sentinelone_sites():
    """Fetch list of sites from SentinelOne API."""
    api_url = os.getenv('SENTINELONE_API_URL')
    api_token = os.getenv('SENTINELONE_API_TOKEN')
    
    if not api_url or not api_token:
        return jsonify({
            'error': 'SentinelOne API credentials not configured. Please set SENTINELONE_API_URL and SENTINELONE_API_TOKEN in .env file'
        }), 400
    
    try:
        headers = {
            'Authorization': f'ApiToken {api_token}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(
            f'{api_url}/web/api/v2.1/sites',
            headers=headers,
            params={'limit': 1000},
            timeout=30
        )
        
        if response.status_code != 200:
            logger.warning("SentinelOne sites error status=%s", response.status_code)
            return jsonify({
                'error': f'SentinelOne API error: {response.status_code}'
            }), response.status_code
        
        data = response.json()
        sites = data.get('data', {}).get('sites', [])
        
        # Format sites for dropdown
        site_list = [
            {'id': site['id'], 'name': site['name']} 
            for site in sites
        ]
        
        return jsonify({
            'success': True,
            'sites': site_list
        })
    
    except requests.exceptions.Timeout:
        return jsonify({'error': 'SentinelOne API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'SentinelOne API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/sentinelone/endpoints', methods=['GET'])
def get_sentinelone_endpoints():
    """Fetch endpoint names and last active dates from SentinelOne API."""
    api_url = os.getenv('SENTINELONE_API_URL')
    api_token = os.getenv('SENTINELONE_API_TOKEN')
    site_id = request.args.get('site_id')
    if site_id in [None, '', 'all', 'null', 'undefined']:
        site_id = None
    
    logger.debug("SentinelOne endpoints request site_id=%s", site_id)
    if not api_url or not api_token:
        return jsonify({
            'error': 'SentinelOne API credentials not configured. Please set SENTINELONE_API_URL and SENTINELONE_API_TOKEN in .env file'
        }), 400
    
    try:
        headers = {
            'Authorization': f'ApiToken {api_token}',
            'Content-Type': 'application/json'
        }
        
        endpoints = []
        cursor = None
        
        # Paginate through all endpoints
        while True:
            params = {'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            if site_id:
                params['siteIds'] = site_id
            
            response = requests.get(
                f'{api_url}/web/api/v2.1/agents',
                headers=headers,
                params=params,
                timeout=30
            )
            
            if response.status_code != 200:
                logger.warning("SentinelOne agents error status=%s", response.status_code)
                return jsonify({
                    'error': f'SentinelOne API error: {response.status_code}'
                }), response.status_code
            
            data = response.json()
            agents = data.get('data', [])
            
            # Extract computer names and last active dates
            for agent in agents:
                computer_name = agent.get('computerName')
                if not computer_name:
                    # Safely access networkInterfaces array
                    interfaces = agent.get('networkInterfaces', [])
                    if interfaces and isinstance(interfaces, list):
                        computer_name = interfaces[0].get('name')
                if computer_name:
                    endpoints.append({
                        'name': fix_encoding(computer_name),
                        'lastActive': agent.get('lastActiveDate')  # ISO 8601 format
                    })
            
            # Check for more pages
            pagination = data.get('pagination', {})
            if not pagination.get('nextCursor'):
                break
            cursor = pagination['nextCursor']
        
        # Remove duplicates by name (keep first occurrence with lastActive)
        seen = {}
        for ep in endpoints:
            if ep['name'] not in seen:
                seen[ep['name']] = ep
        endpoints = sorted(seen.values(), key=lambda x: x['name'])
        
        return jsonify({
            'success': True,
            'endpoints': endpoints,
            'count': len(endpoints)
        })
    
    except requests.exceptions.Timeout:
        return jsonify({'error': 'SentinelOne API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'SentinelOne API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/sentinelone/upload', methods=['POST'])
def upload_sentinelone_data():
    """Create a virtual file from SentinelOne endpoint data."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json
    file_id = str(data.get('file_id', '1'))
    endpoints = data.get('endpoints', [])
    
    if not endpoints:
        return jsonify({'error': 'No endpoints provided'}), 400
    
    try:
        # Extract names from device objects (keep all devices, no filtering)
        endpoint_names = [ep['name'] if isinstance(ep, dict) else ep for ep in endpoints]
        
        # Create a DataFrame from endpoints
        df = pd.DataFrame({'Endpoint Name': endpoint_names})
        
        # Generate unique filename
        unique_id = str(uuid.uuid4())
        filename = f'sentinelone_endpoints_{unique_id}.xlsx'
        filepath = os.path.abspath(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        
        # Save to Excel
        df.to_excel(filepath, index=False, engine='openpyxl')
        
        # Verify file was created
        if not os.path.exists(filepath):
            logger.error(f'File creation failed: {filepath}')
            return jsonify({'error': 'File creation failed'}), 500
        
        logger.info(f'Created SentinelOne file: {filename} ({len(endpoint_names)} endpoints)')
        
        # Delete old file if it exists in session
        if 'files' in session and file_id in session['files']:
            old_filepath = session['files'][file_id]
            try:
                if os.path.exists(old_filepath):
                    os.remove(old_filepath)
                    logger.info(f'Removed old file: {old_filepath}')
            except Exception as e:
                logger.warning(f'Failed to remove old file {old_filepath}: {e}')
        
        # Store filepath in both session and global store for resilience
        _set_session_file(file_id, filepath, _get_client_id(data))
        
        logger.info(f'Session files after upload: {session.get("files", {})}')
        
        return jsonify({
            'success': True,
            'filename': 'SentinelOne Endpoints',
            'sheets': ['Sheet1'],
            'columns': ['Endpoint Name'],
            'row_count': len(endpoint_names)
        })
    
    except Exception as e:
        logger.error(f'SentinelOne upload error: {str(e)}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# =============================================================================
# NinjaRMM OAuth Authorization Code Flow Routes
# =============================================================================

@app.route('/ninjarmm/oauth/authorize', methods=['GET'])
def ninjarmm_oauth_authorize():
    """Initiate OAuth authorization code flow - redirects user to NinjaRMM login."""
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    redirect_uri = os.getenv('NINJARMM_OAUTH_REDIRECT_URI')
    # OAuth authorization happens on app.ninjarmm.com (central OAuth server)
    oauth_url = os.getenv('NINJARMM_OAUTH_URL', 'https://app.ninjarmm.com')
    scope = os.getenv('NINJARMM_OAUTH_SCOPE', 'monitoring management offline_access')

    if not client_id:
        return jsonify({'error': 'NINJARMM_CLIENT_ID not configured'}), 500
    if not redirect_uri:
        return jsonify({'error': 'NINJARMM_OAUTH_REDIRECT_URI not configured'}), 500

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    session['ninja_oauth_state'] = {
        'state': state,
        'created_at': time.time(),
        'redirect_uri': redirect_uri,
    }

    # Build authorization URL with proper encoding
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': scope,
        'state': state,
    }
    auth_url = f"{oauth_url}/ws/oauth/authorize?{urlencode(params)}"

    logger.info('Redirecting to NinjaRMM OAuth authorization: %s', auth_url)
    return redirect(auth_url)


@app.route('/ninjarmm/oauth/callback', methods=['GET'])
def ninjarmm_oauth_callback():
    """Handle OAuth callback - exchange authorization code for tokens."""
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description', '')

    if error:
        logger.error('NinjaRMM OAuth error: %s - %s', error, error_description)
        return jsonify({'error': f'OAuth error: {error}', 'description': error_description}), 400

    if not code:
        return jsonify({'error': 'Missing authorization code'}), 400

    saved_state_data = session.pop('ninja_oauth_state', None)
    if not state or not saved_state_data or state != saved_state_data.get('state'):
        saved_state = saved_state_data.get('state') if saved_state_data else 'MISSING'
        logger.warning('OAuth State Mismatch - Received: %s, Saved: %s', state, saved_state)
        # Check if saved_state_data exists at all
        if not saved_state_data:
            logger.warning('No state found in session. This usually happens if the server restarted during authorization or if cookies are blocked.')
        
        return jsonify({
            'error': 'Invalid or expired state parameter.',
            'details': 'Please ensure you are using a stable FLASK_SECRET_KEY and that your browser allows cookies. Try starting the authorization flow again.',
            'received': state,
            'expected': saved_state
        }), 400

    # Ensure state is not too old (10 mins)
    if time.time() - saved_state_data.get('created_at', 0) > 600:
        return jsonify({'error': 'OAuth authorization timed out. Please try again.'}), 400

    redirect_uri = saved_state_data['redirect_uri']

    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    api_url = os.getenv('NINJARMM_API_URL', 'https://app.ninjarmm.com')
    # Token exchange must happen on app.ninjarmm.com (central OAuth server)
    oauth_url = os.getenv('NINJARMM_OAUTH_URL', 'https://app.ninjarmm.com')

    if not client_id or not client_secret:
        return jsonify({'error': 'OAuth credentials not configured'}), 500

    # Exchange code for tokens
    try:
        token_response = requests.post(
            f'{oauth_url}/ws/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
                'client_id': client_id,
                'client_secret': client_secret,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )

        if token_response.status_code != 200:
            logger.error('NinjaRMM token exchange failed: %s %s', token_response.status_code, token_response.text[:200])
            return jsonify({'error': f'Token exchange failed: {token_response.status_code}'}), 400

        token_json = token_response.json()
        access_token = token_json.get('access_token')
        refresh_token = token_json.get('refresh_token')
        expires_in = int(token_json.get('expires_in') or 3600)

        if not access_token:
            return jsonify({'error': 'No access token in response'}), 400

        now = time.time()
        _NINJA_TOKEN_CACHE.update({
            'access_token': token_json['access_token'],
            'refresh_token': token_json.get('refresh_token'),
            'api_url': api_url,
            'expires_at': time.time() + token_json['expires_in'],
            'grant_type': 'authorization_code',
        })
        _save_ninja_token_cache()
        
        logger.info('NinjaRMM OAuth authorization successful')
        return jsonify({
            'success': True,
            'message': 'NinjaRMM connected successfully',
            'has_refresh_token': bool(refresh_token),
            'expires_in': expires_in,
        })

    except requests.RequestException as e:
        logger.exception('NinjaRMM token exchange request failed')
        return jsonify({'error': f'Token exchange request failed: {str(e)}'}), 500


@app.route('/ninjarmm/oauth/status', methods=['GET'])
def ninjarmm_oauth_status():
    """Check the current NinjaRMM OAuth connection status."""
    now = time.time()

    connected = bool(
        _NINJA_TOKEN_CACHE.get('access_token')
        and _NINJA_TOKEN_CACHE.get('expires_at', 0) > now
    )

    return jsonify({
        'connected': connected,
        'has_refresh_token': bool(_NINJA_TOKEN_CACHE.get('refresh_token')),
        'expires_in': max(0, int(_NINJA_TOKEN_CACHE.get('expires_at', 0) - now)) if connected else 0,
    })


@app.route('/ninjarmm/oauth/disconnect', methods=['POST'])
def ninjarmm_oauth_disconnect():
    """Clear the cached OAuth tokens (disconnect from NinjaRMM)."""
    _NINJA_TOKEN_CACHE.update({
        'access_token': None,
        'refresh_token': None,
        'expires_at': 0,
        'api_url': None,
    })
    _save_ninja_token_cache()
    logger.info('NinjaRMM OAuth tokens cleared')
    return jsonify({'success': True, 'message': 'Disconnected from NinjaRMM'})




@app.route('/ninjarmm/organizations', methods=['GET'])
def get_ninjarmm_organizations():
    """Fetch list of organizations from NinjaRMM API."""
    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')

    try:
        headers, auth = _get_ninja_auth(api_url)
        response = requests.get(
            f'{api_url}/v2/organizations',
            headers=headers,
            auth=auth,
            timeout=30
        )

        if response.status_code != 200:
            logger.warning("NinjaRMM organizations error status=%s", response.status_code)
            return jsonify({'error': f'NinjaRMM API error: {response.status_code}'}), response.status_code

        orgs = response.json()

        org_list = [{'id': org['id'], 'name': org['name']} for org in orgs]
        org_list.sort(key=lambda x: x['name'])

        return jsonify({'success': True, 'organizations': org_list})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ninjarmm/devices', methods=['GET'])
def get_ninjarmm_devices():
    """Fetch device names from NinjaRMM API."""
    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')
    org_id = request.args.get('org_id')
    if org_id in [None, '', 'all', 'null', 'undefined']:
        org_id = None

    try:
        headers, auth = _get_ninja_auth(api_url)

        devices = []
        page_num = 0
        page_size = 1000

        while True:
            params = {'pageSize': page_size, 'page': page_num}
            endpoint = f'{api_url}/v2/organization/{org_id}/devices' if org_id else f'{api_url}/v2/devices'

            response = requests.get(
                endpoint,
                headers=headers,
                auth=auth,
                params=params,
                timeout=30
            )

            if response.status_code != 200:
                logger.warning("NinjaRMM devices error status=%s", response.status_code)
                return jsonify({'error': f'NinjaRMM API error: {response.status_code}'}), response.status_code

            data = response.json()
            if not data:
                break

            for device in data:
                device_name = device.get('systemName') or device.get('dnsName')
                if device_name:
                    devices.append({
                        'name': fix_encoding(device_name),
                        'id': device.get('id'),
                        'lastContact': device.get('lastContact')
                    })

            if len(data) < page_size:
                break
            page_num += 1

        seen = {}
        for dev in devices:
            if dev['name'] not in seen:
                seen[dev['name']] = dev
        devices = sorted(seen.values(), key=lambda x: x['name'])

        return jsonify({'success': True, 'devices': devices, 'count': len(devices)})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ninjarmm/upload', methods=['POST'])
def upload_ninjarmm_data():
    """Create a virtual file from NinjaRMM device data."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    # Log session cookie details for debugging
    session_cookie = request.cookies.get('session', 'NO_COOKIE')
    logger.info(f'Ninja upload - Session cookie: {session_cookie[:20] if session_cookie != "NO_COOKIE" else "NO_COOKIE"}')
    logger.info(f'Ninja upload - Session files before: {list(session.get("files", {}).keys())}')

    data = request.json
    file_id = str(data.get('file_id', '1'))
    devices = data.get('devices', [])
    
    if not devices:
        return jsonify({'error': 'No devices provided'}), 400
    
    try:
        # Extract names from device objects (keep all devices, no filtering)
        device_names = [dev['name'] if isinstance(dev, dict) else dev for dev in devices]
        
        # Create a DataFrame from devices
        df = pd.DataFrame({'Device Name': device_names})
        
        # Generate unique filename
        unique_id = str(uuid.uuid4())
        filename = f'ninjarmm_devices_{unique_id}.xlsx'
        filepath = os.path.abspath(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        
        # Save to Excel
        df.to_excel(filepath, index=False, engine='openpyxl')
        
        # Verify file was created
        if not os.path.exists(filepath):
            logger.error(f'File creation failed: {filepath}')
            return jsonify({'error': 'File creation failed'}), 500
        
        logger.info(f'Created NinjaRMM file: {filename} ({len(device_names)} devices)')
        
        # Delete old file if it exists in session
        if 'files' in session and file_id in session['files']:
            old_filepath = session['files'][file_id]
            try:
                if os.path.exists(old_filepath):
                    os.remove(old_filepath)
                    logger.info(f'Removed old file: {old_filepath}')
            except Exception as e:
                logger.warning(f'Failed to remove old file {old_filepath}: {e}')
        
        # Store filepath in both session and global store for resilience
        _set_session_file(file_id, filepath, _get_client_id(data))
        
        logger.info(f'Session files after upload: {session.get("files", {})}')
        
        return jsonify({
            'success': True,
            'filename': 'NinjaRMM Devices',
            'sheets': ['Sheet1'],
            'columns': ['Device Name'],
            'row_count': len(device_names)
        })
    
    except Exception as e:
        logger.error(f'NinjaRMM upload error: {str(e)}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/clients/unified', methods=['GET'])
def get_unified_clients():
    """Fetch and match clients from both SentinelOne and NinjaRMM APIs."""
    s1_url = os.getenv('SENTINELONE_API_URL')
    s1_token = os.getenv('SENTINELONE_API_TOKEN')
    ninja_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')
    ninja_client_id = os.getenv('NINJARMM_CLIENT_ID')
    ninja_client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    
    s1_available = bool(s1_url and s1_token)
    ninja_available = bool(ninja_client_id and ninja_client_secret)
    
    
    s1_sites = []
    ninja_orgs = []
    
    # Fetch SentinelOne sites
    if s1_available:
        try:
            headers = {
                'Authorization': f'ApiToken {s1_token}',
                'Content-Type': 'application/json'
            }
            response = requests.get(
                f'{s1_url}/web/api/v2.1/sites',
                headers=headers,
                params={'limit': 1000},
                timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                s1_sites = [{'id': site['id'], 'name': site['name']} for site in data.get('data', {}).get('sites', [])]
        except Exception as e:
            logger.warning("Error fetching S1 sites: %s", e)
    
    # Fetch NinjaRMM organizations
    if ninja_available:
        try:
            headers, auth = _get_ninja_auth(ninja_url)
            response = requests.get(
                f'{ninja_url}/v2/organizations',
                headers=headers,
                auth=auth,
                timeout=30
            )
            if response.status_code == 200:
                ninja_orgs = [{'id': org['id'], 'name': org['name']} for org in response.json()]
            else:
                logger.warning("Error fetching Ninja orgs: status=%s", response.status_code)
        except Exception as e:
            logger.warning("Error fetching Ninja orgs: %s", e)
    
    # Match clients by normalized name using the same normalization logic as device comparison
    # This ensures consistent matching behavior across the application
    # Create lookup maps
    s1_by_norm = {normalize_value(site['name']): site for site in s1_sites}
    ninja_by_norm = {normalize_value(org['name']): org for org in ninja_orgs}
    
    # Match clients
    matched_clients = []
    unmatched_s1 = []
    unmatched_ninja = []
    
    all_norm_names = set(s1_by_norm.keys()) | set(ninja_by_norm.keys())
    
    for norm_name in sorted(all_norm_names):
        s1_site = s1_by_norm.get(norm_name)
        ninja_org = ninja_by_norm.get(norm_name)
        
        if s1_site and ninja_org:
            # Matched - both exist
            matched_clients.append({
                'name': s1_site['name'],  # Use S1 name as primary
                's1_id': s1_site['id'],
                's1_name': s1_site['name'],
                'ninja_id': ninja_org['id'],
                'ninja_name': ninja_org['name'],
                'matched': True
            })
        elif s1_site:
            unmatched_s1.append({
                'name': s1_site['name'],
                's1_id': s1_site['id'],
                's1_name': s1_site['name'],
                'ninja_id': None,
                'ninja_name': None,
                'matched': False
            })
        elif ninja_org:
            unmatched_ninja.append({
                'name': ninja_org['name'],
                's1_id': None,
                's1_name': None,
                'ninja_id': ninja_org['id'],
                'ninja_name': ninja_org['name'],
                'matched': False
            })
    
    # Combine: matched first, then unmatched
    all_clients = matched_clients + unmatched_s1 + unmatched_ninja
    
    return jsonify({
        'success': True,
        'clients': all_clients,
        's1_available': s1_available,
        'ninja_available': ninja_available,
        'stats': {
            'matched': len(matched_clients),
            'only_s1': len(unmatched_s1),
            'only_ninja': len(unmatched_ninja),
            'total': len(all_clients)
        }
    })


@app.route('/ninjarmm/scripts', methods=['GET'])
def get_ninjarmm_scripts():
    """Fetch available scripts from NinjaRMM API."""
    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')

    try:
        headers, auth = _get_ninja_auth(api_url)

        possible_endpoints = [
            f'{api_url}/v2/automation/scripts',
            f'{api_url}/v2/queries/scripts',
            f'{api_url}/v2/scripts'
        ]

        scripts_data = None
        last_error = None

        for endpoint in possible_endpoints:
            try:
                response = requests.get(endpoint, headers=headers, auth=auth, timeout=30)
                if response.status_code == 200:
                    scripts_data = response.json()
                    break
                last_error = f"{response.status_code}"
            except Exception as e:
                last_error = str(e)

        if scripts_data is None:
            return jsonify({'error': f'Could not fetch scripts. Last error: {last_error}'}), 404

        scripts = []
        for script in scripts_data:
            scripts.append({
                'id': script.get('id'),
                'name': script.get('name'),
                'description': script.get('description', ''),
                'category': script.get('category', 'Uncategorized'),
                'language': script.get('scriptType', 'Unknown')
            })

        return jsonify({'success': True, 'scripts': scripts, 'count': len(scripts)})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ninjarmm/run-script', methods=['POST'])
def run_ninjarmm_script():
    """Trigger a script to run on a specific NinjaRMM device."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')

    # Get request parameters
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    device_id = data.get('device_id')
    script_id = data.get('script_id')
    script_params = data.get('parameters', {})  # Optional script parameters

    if device_id in (None, ''):
        return jsonify({'error': 'device_id is required'}), 400
    if script_id in (None, ''):
        return jsonify({'error': 'script_id is required'}), 400

    try:
        device_id = int(device_id)
        script_id = int(script_id)
    except Exception:
        return jsonify({'error': 'device_id and script_id must be integers'}), 400

    if script_params is None:
        script_params = {}

    if isinstance(script_params, dict):
        ninja_parameters = json.dumps(script_params, separators=(',', ':'))
    elif isinstance(script_params, str):
        ninja_parameters = script_params
    else:
        return jsonify({'error': 'parameters must be an object or string'}), 400

    allowed = os.getenv('NINJARMM_ALLOWED_SCRIPT_IDS', '').strip()
    if allowed:
        try:
            allowed_ids = {int(x) for x in re.split(r'[\s,]+', allowed) if x}
        except Exception:
            return jsonify({'error': 'Server allowlist is misconfigured'}), 500
        if script_id not in allowed_ids:
            return jsonify({'error': 'Script not allowed'}), 403

    logger.info("Triggering NinjaRMM script_id=%s device_id=%s from=%s", script_id, device_id, request.remote_addr)

    try:
        headers, auth = _get_ninja_auth(api_url)

        require_online = os.getenv('NINJARMM_REQUIRE_DEVICE_ONLINE', '1') in ('1', 'true', 'True')
        if require_online:
            try:
                max_age = int(os.getenv('NINJARMM_ONLINE_MAX_AGE_SECONDS', '300'))
            except Exception:
                max_age = 300

            device_data = None
            for endpoint in (f'{api_url}/v2/device/{device_id}', f'{api_url}/v2/devices/{device_id}'):
                try:
                    r = requests.get(endpoint, headers=headers, auth=auth, timeout=10)
                    if r.status_code == 200:
                        device_data = r.json()
                        break
                except Exception:
                    pass

            if device_data is None:
                # Fallback: query list endpoint and search
                try:
                    r = requests.get(f'{api_url}/v2/devices', headers=headers, auth=auth, timeout=10)
                    if r.status_code == 200:
                        for d in (r.json() or []):
                            if d.get('id') == device_id:
                                device_data = d
                                break
                except Exception:
                    pass

            if not device_data:
                return jsonify({'error': 'Device not found'}), 404

            last_contact = device_data.get('lastContact')
            if not last_contact:
                return jsonify({'error': 'Device online status unavailable'}), 403

            try:
                last_contact = float(last_contact)
                if last_contact > 1e12:
                    last_contact = last_contact / 1000.0
                age = time.time() - last_contact
                if age > max_age:
                    return jsonify({'error': f'Device not online (last contact {int(age)}s ago)'}), 403
            except Exception:
                return jsonify({'error': 'Device online status invalid'}), 403

        headers = {**headers, 'Content-Type': 'application/json'}
        
        # Prepare script execution payload
        # Ninja expects fields like: id/type/uid/runAs/parameters (not scriptId)
        # Note: Ninja expects parameters as a string.
        script_uid = _lookup_ninja_script_uid(api_url, headers, auth, script_id, device_id=device_id)

        payload = {
            'id': int(script_id),
            'type': 'SCRIPT',
            'parameters': ninja_parameters
        }
        if script_uid:
            payload['uid'] = script_uid

        run_as = (os.getenv('NINJARMM_SCRIPT_RUN_AS') or 'system').strip()
        if run_as:
            payload['runAs'] = run_as
        
        # Execute script on device (try /api/v2 first, fallback to /v2)
        endpoints_to_try = [
            f'{api_url}/api/v2/device/{device_id}/script/run',
            f'{api_url}/v2/device/{device_id}/script/run',
        ]
        response = None
        endpoint = None
        for endpoint in endpoints_to_try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                auth=auth,
                timeout=30
            )
            if response.status_code in (200, 204):
                break
            if response.status_code != 404:
                break  # Non-404 error, don't retry
        if response.status_code == 204:
            # Success (no content returned)
            return jsonify({
                'success': True,
                'message': f'Script {script_id} triggered successfully on device {device_id}'
            })
        elif response.status_code == 200:
            # Success with response data
            return jsonify({
                'success': True,
                'message': f'Script {script_id} triggered successfully on device {device_id}',
                'data': response.json()
            })
        else:
            return jsonify({
                'error': f'NinjaRMM API error: {response.status_code} {response.text[:200]}'
            }), response.status_code
    
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _save_ad_inventory_to_excel(parsed_data, client, days, clean_client):
    """Process AD inventory JSON data and save to Excel cache."""
    items = parsed_data.get('workstations') or parsed_data.get('computers') or []
    if not isinstance(items, list):
        return None

    names = []
    for it in items:
        if isinstance(it, dict):
            name = it.get('name')
        else:
            name = it
        if name:
            names.append(fix_encoding(name).strip())
    names = [n for n in names if n]

    uploads_root = os.path.abspath(app.config['UPLOAD_FOLDER'])
    filename = f'ad_computers_{uuid.uuid4().hex}.xlsx'
    filepath = os.path.join(uploads_root, filename)

    df = pd.DataFrame({'Computer Name': names})
    df.to_excel(filepath, index=False, engine='openpyxl')

    entry = {
        'path': filepath,
        'count': len(names),
        'received_at': int(time.time()),
    }
    _AD_CACHE[(client, days)] = entry
    if clean_client and clean_client != client:
        _AD_CACHE[(clean_client, days)] = entry
    
    return len(names)


@app.route('/ad/trigger', methods=['POST'])
def trigger_ad_inventory():
    """Trigger a NinjaRMM script to inventory Active Directory computers, then pull results from a custom field."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    client = (data.get('client') or '').strip()
    days = data.get('days')
    org_id = data.get('org_id')
    device_id = data.get('device_id')
    script_id = data.get('script_id')

    if not client:
        return jsonify({'error': 'client is required'}), 400

    try:
        days = int(days)
    except Exception:
        return jsonify({'error': 'days must be an integer'}), 400

    if days not in (30, 60, 90):
        return jsonify({'error': 'days must be one of: 30, 60, 90'}), 400

    try:
        org_id = int(org_id)
        device_id = int(device_id)
        script_id = int(script_id)
    except Exception:
        return jsonify({'error': 'org_id, device_id, and script_id must be integers'}), 400

    api_url = _get_ninja_api_url()

    # Clean client name
    clean_client = _sanitize_client_name(client)

    run_id = uuid.uuid4().hex
    started_at = time.time()

    script_params = {
        'Days': days,
        'RunId': run_id,
    }

    if client != clean_client:
        logger.info('Client name sanitized: "%s" -> "%s"', client, clean_client)
    logger.info('Triggering AD inventory via Ninja: client=%s days=%s org_id=%s device_id=%s script_id=%s', clean_client, days, org_id, device_id, script_id)

    try:
        # Get authentication headers
        try:
            logger.info('Attempting NinjaRMM auth to: %s', api_url)
            headers, auth = _get_ninja_auth(api_url)
            logger.info('NinjaRMM auth successful')
        except Exception as e:
            logger.error('Failed to get NinjaRMM auth: %s', str(e), exc_info=True)
            return jsonify({'error': f'NinjaRMM authentication failed: {str(e)}'}), 500
        
        headers = {**headers, 'Content-Type': 'application/json'}

        # Try to get previous device custom field value (non-critical, continue on failure)
        previous_device_value = None
        try:
            previous_device_value = _get_ninja_device_custom_field(api_url, headers, auth, device_id, _AD_CUSTOM_FIELD_NAME)
            
            # CHECK FOR RECENT DATA (10 minutes)
            if previous_device_value:
                try:
                    parsed = json.loads(previous_device_value)
                    if not isinstance(parsed, dict):
                        logger.debug('Cache check: Device field is not a JSON object')
                    else:
                        # Verify days and age
                        gen = str(parsed.get('generatedAtUtc') or '').strip()
                        if gen:
                            gen_dt = datetime.fromisoformat(gen.replace('Z', '+00:00'))
                            if gen_dt.tzinfo is None:
                                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                                
                            age_seconds = time.time() - gen_dt.timestamp()
                            parsed_days = int(parsed.get('days') or 0)
                            
                            logger.debug('Cache check (device): age=%ds days=%d (requested %d)', int(age_seconds), parsed_days, days)
                            
                            if age_seconds < 600 and parsed_days == days:
                                logger.info('Reusing existing AD inventory data from device (age: %ds)', int(age_seconds))
                                count = _save_ad_inventory_to_excel(parsed, client, days, clean_client)
                                if count is not None:
                                    return jsonify({
                                        'success': True, 
                                        'message': f'Reused recent AD inventory from Ninja device field', 
                                        'count': count,
                                        'cached': True
                                    })
                except Exception as e:
                    logger.debug('Cache check (device) failed: %s', str(e))
        except Exception as e:
            logger.warning('Failed to get previous custom field values: %s', str(e))

        # Try to lookup script UID (non-critical, continue on failure)
        script_uid = None
        try:
            script_uid = _lookup_ninja_script_uid(api_url, headers, auth, script_id, device_id=device_id)
            logger.info('Script UID lookup: script_id=%s uid=%s', script_id, script_uid)
        except Exception as e:
            logger.warning('Script UID lookup failed: %s', str(e), exc_info=True)

        # Default to runAs system if no specific credential is provided
        run_as = (os.getenv('NINJARMM_SCRIPT_RUN_AS') or 'system').strip()

        # Build payload with parameters (Days and RunId) so polling can validate results
        # Format parameters as PowerShell-style: -Days 60 -RunId "abc123"
        ninja_parameters = _format_ninja_parameters_powershell(script_params)
        logger.info('Formatted script parameters: %s', ninja_parameters)
        
        payload = {
            'id': int(script_id),
            'type': 'SCRIPT',
            'parameters': ninja_parameters
        }
        if script_uid:
            payload['uid'] = script_uid
        if run_as:
            payload['runAs'] = run_as

        # Use the documented endpoint
        endpoint = f'{api_url}/v2/device/{device_id}/script/run'
        
        # Ensure Content-Type is set
        request_headers = {**headers, 'Content-Type': 'application/json'}
        
        logger.info('Triggering script: endpoint=%s payload=%s', endpoint, payload)
        try:
            last_resp = requests.post(endpoint, json=payload, headers=request_headers, auth=auth, timeout=30)
            logger.info('Script run response: status=%s body=%s', last_resp.status_code, last_resp.text[:500])
        except requests.exceptions.RequestException as e:
            logger.error('Script run request failed: %s', str(e), exc_info=True)
            return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500

        if last_resp.status_code in (200, 204):
            # 1. OPTIONAL: Monitor Job Success (New behavior)
            job_id = None
            if last_resp.status_code == 200:
                try:
                    job_data = last_resp.json()
                    job_id = job_data.get('id') or job_data.get('uid')
                except Exception:
                    pass

            if job_id:
                logger.info('Monitoring NinjaRMM job status: job_id=%s', job_id)
                job_deadline = time.time() + 120 # 2 minute limit for job status check
                job_success = False
                
                while time.time() < job_deadline:
                    try:
                        # Poll device jobs endpoint
                        jobs_url = f'{api_url}/v2/device/{device_id}/jobs'
                        jr = requests.get(jobs_url, headers=headers, auth=auth, timeout=10)
                        if jr.status_code == 200:
                            active_jobs = jr.json()
                            # Check both id and uid
                            target_job = next((j for j in active_jobs if j.get('id') == job_id or j.get('uid') == job_id), None)
                            
                            if target_job:
                                status = target_job.get('jobStatus') or target_job.get('status')
                                result = target_job.get('jobResult') or target_job.get('result')
                                logger.debug('Job %s current status: %s (result: %s)', job_id, status, result)
                                
                                if status == 'COMPLETED':
                                    if result == 'SUCCESS':
                                        job_success = True
                                        logger.info('NinjaRMM job %s completed successfully.', job_id)
                                        break
                                    else:
                                        logger.error('NinjaRMM job %s failed with result: %s', job_id, result)
                                        return jsonify({'error': f'NinjaRMM script failed: {result}'}), 500
                                elif status in ('CANCELLED', 'FAILED'):
                                    logger.error('NinjaRMM job %s reached terminal state: %s', job_id, status)
                                    return jsonify({'error': f'NinjaRMM script execution {status.lower()}'}), 500
                            else:
                                # If job is not in active jobs list, it might have finished and moved to history
                                # Before we give up, we'll wait a bit and check activities or just proceed to custom field polling
                                logger.debug('Job %s not found in active jobs list; proceeding to custom field check.', job_id)
                                job_success = True # Assume it might have finished extremely fast
                                break
                    except Exception as e:
                        logger.warning('Error checking job status: %s', str(e))
                    
                    time.sleep(3)
                
                if not job_success and time.time() >= job_deadline:
                    logger.warning('Job %s did not reach COMPLETED state within timeout; proceeding to check custom fields anyway.', job_id)

            # 2. Poll the device custom field for results with adaptive intervals
            try:
                timeout_s = int(os.getenv('AD_CUSTOM_FIELD_POLL_TIMEOUT_SECONDS', '300'))
            except Exception:
                timeout_s = 300
            
            # Adaptive polling: start fast, then slow down
            poll_attempt = 0
            deadline = time.time() + timeout_s
            last_seen = None

            logger.info('Starting device custom field polling for AD results...')

            while time.time() < deadline:
                # Calculate adaptive interval: 2s for first 2 attempts, then exponential
                poll_interval_s = min(2 ** (poll_attempt // 2), 8)  # Cap at 8 seconds
                time.sleep(poll_interval_s)
                poll_attempt += 1

                # Strictly poll device-scoped field.
                device_value = _get_ninja_device_custom_field(api_url, headers, auth, device_id, _AD_CUSTOM_FIELD_NAME)

                if not device_value:
                    continue
                
                # Check for change
                if previous_device_value is not None and device_value == previous_device_value:
                    continue

                # Avoid re-processing if it hasn't changed since last poll iteration
                value = device_value
                if last_seen is not None and value == last_seen:
                    continue
                last_seen = value

                if '...TRUNCATED...' in value:
                    return jsonify({'error': f"AD payload in Ninja custom field '{_AD_CUSTOM_FIELD_NAME}' was truncated."}), 500

                parsed = _extract_and_validate_ad_data(value)
                if not parsed:
                    continue

                # Validate timestamp age (must be recent)
                gen = str(parsed.get('generatedAtUtc') or '').strip()
                is_recent = False
                try:
                    gen_dt = datetime.fromisoformat(gen.replace('Z', '+00:00'))
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                    age_seconds = time.time() - gen_dt.timestamp()
                    # Data must be generated after we started (with 10s grace period for clock skew)
                    if gen_dt.timestamp() >= (started_at - 10):
                        is_recent = True
                    else:
                        logger.debug('Polling: data is too old (age: %ds)', int(age_seconds))
                except Exception as e:
                    logger.debug('Polling: timestamp validation failed: %s', str(e))

                # Validate days parameter
                days_match = False
                try:
                    parsed_days = int(parsed.get('days') or 0)
                    if parsed_days == days:
                        days_match = True
                    else:
                        logger.debug('Polling: days mismatch (got %d, expected %d)', parsed_days, days)
                except Exception:
                    pass

                # runId check (preferred but not required - some Ninja environments may not pass parameters)
                runid_match = False
                parsed_runid = str(parsed.get('runId') or '').strip()
                if parsed_runid and parsed_runid == run_id:
                    runid_match = True
                    logger.debug('Polling: runId match confirmed')
                elif parsed_runid:
                    logger.debug('Polling: runId mismatch (got %s, expected %s)', parsed_runid, run_id)

                # Accept data if: (runId matches) OR (recent timestamp AND days match)
                # This handles both cases: parameters passed correctly, or fallback to timestamp+days validation
                if runid_match or (is_recent and days_match):
                    count = _save_ad_inventory_to_excel(parsed, client, days, clean_client)
                    if count is not None:
                        validation_method = 'runId' if runid_match else 'timestamp+days'
                        logger.info(f"AD inventory received after {poll_attempt} poll attempts (validated by {validation_method})")
                        return jsonify({'success': True, 'message': 'AD inventory received', 'count': count})
                else:
                    logger.debug('Polling: data validation failed (runId=%s, recent=%s, days_match=%s)', runid_match, is_recent, days_match)
                    continue

            logger.warning(f"AD inventory timed out after {poll_attempt} poll attempts over {timeout_s}s")
            return jsonify({'error': 'Timed out waiting for AD results in Ninja custom field'}), 504

        # Script run failed
        return jsonify({
            'error': f'NinjaRMM API error: {last_resp.status_code} {last_resp.text[:200]}'
        }), last_resp.status_code

    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500




@app.route('/ninjarmm/devices/all', methods=['GET'])
def get_all_ninjarmm_devices():
    """Fetch all devices (for AD device picker) with minimal info, optionally filtered by org_id."""
    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')
    org_id = request.args.get('org_id')
    if org_id in [None, '', 'all', 'null', 'undefined']:
        org_id = None

    try:
        headers, auth = _get_ninja_auth(api_url)

        devices = []
        page_num = 0
        page_size = 1000

        while True:
            params = {'pageSize': page_size, 'page': page_num}
            # Use org-specific endpoint if org_id provided
            endpoint = f'{api_url}/v2/organization/{org_id}/devices' if org_id else f'{api_url}/v2/devices'

            response = requests.get(endpoint, headers=headers, auth=auth, params=params, timeout=30)
            if response.status_code != 200:
                return jsonify({'error': f'NinjaRMM API error: {response.status_code}'}), response.status_code

            data = response.json()
            if not data:
                break

            for device in data:
                device_name = device.get('systemName') or device.get('dnsName')
                device_org_id = device.get('organizationId')
                if device_name:
                    devices.append({
                        'id': device.get('id'),
                        'name': fix_encoding(device_name),
                        'organizationId': device_org_id,
                    })

            if len(data) < page_size:
                break
            page_num += 1

        devices.sort(key=lambda x: x['name'])
        return jsonify({'success': True, 'devices': devices, 'count': len(devices)})

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ad/intake', methods=['POST'])
def ad_intake():
    """Receive AD computer inventory from a Ninja-run script."""
    _prune_ad_intake_nonces()

    # Preferred: HMAC-signed intake using a one-time nonce.
    intake_nonce = request.headers.get('X-AD-Intake-Nonce')
    intake_sig = request.headers.get('X-AD-Intake-Signature')

    # Legacy: bearer token (static env token).
    provided = request.headers.get('X-AD-Intake-Token')

    raw_body = request.get_data(cache=True) or b''
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    client = (payload.get('client') or payload.get('clientName') or '').strip()
    days = payload.get('days')
    items = payload.get('workstations') or payload.get('computers') or []

    if not client:
        return jsonify({'error': 'client is required'}), 400

    try:
        days = int(days)
    except Exception:
        return jsonify({'error': 'days must be an integer'}), 400

    if days not in (30, 60, 90):
        return jsonify({'error': 'days must be one of: 30, 60, 90'}), 400

    # Auth: if HMAC headers are present, validate signature using the per-request signing key.
    if intake_nonce and intake_sig:
        entry = _AD_INTAKE_NONCES.get(intake_nonce)
        if not entry or entry.get('client') != client or int(entry.get('days') or 0) != days or entry.get('expires_at', 0) <= time.time():
            return jsonify({'error': 'Unauthorized'}), 401

        signing_key = entry.get('signing_key') or ''
        expected = hmac.new(signing_key.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(intake_sig).strip()):
            return jsonify({'error': 'Unauthorized'}), 401

        _AD_INTAKE_NONCES.pop(intake_nonce, None)

    else:
        # Legacy bearer token (manual/legacy use)
        current = os.getenv('AD_INTAKE_TOKEN_CURRENT')
        previous = os.getenv('AD_INTAKE_TOKEN_PREVIOUS')
        if not provided or provided not in {t for t in (current, previous) if t}:
            return jsonify({'error': 'Unauthorized'}), 401

    if not isinstance(items, list):
        return jsonify({'error': 'workstations must be a list'}), 400

    names = []
    for it in items:
        if isinstance(it, dict):
            name = it.get('name')
        else:
            name = it
        if name:
            names.append(fix_encoding(name).strip())

    names = [n for n in names if n]

    # Save as a virtual Excel file to reuse existing compare flow
    uploads_root = os.path.abspath(app.config['UPLOAD_FOLDER'])
    filename = f'ad_computers_{uuid.uuid4().hex}.xlsx'
    filepath = os.path.join(uploads_root, filename)

    df = pd.DataFrame({'Computer Name': names})
    df.to_excel(filepath, index=False, engine='openpyxl')

    _AD_CACHE[(client, days)] = {
        'path': filepath,
        'count': len(names),
        'received_at': int(time.time()),
    }

    logger.info('Received AD inventory: client=%s days=%s count=%s from=%s', client, days, len(names), request.remote_addr)

    return jsonify({'success': True, 'client': client, 'days': days, 'count': len(names)})


@app.route('/ad/sync', methods=['POST'])
def sync_ad_inventory():
    """Manually sync AD inventory data from Ninja custom fields without triggering a new script."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json or {}
    client = data.get('client')
    org_id = data.get('org_id')
    device_id = data.get('device_id')
    days = int(data.get('days') or 30)

    if not client or not org_id or not device_id:
        return jsonify({'error': 'Missing client, org_id, or device_id'}), 400

    try:
        org_id = int(org_id)
        device_id = int(device_id)
    except Exception:
        return jsonify({'error': 'org_id and device_id must be integers'}), 400

    clean_client = _sanitize_client_name(client)
    api_url = _get_ninja_api_url()
    logger.info('Syncing AD inventory: client=%s org_id=%s device_id=%s field=%s', clean_client, org_id, device_id, _AD_CUSTOM_FIELD_NAME)

    try:
        headers, auth = _get_ninja_auth(api_url)
        headers = {**headers, 'Content-Type': 'application/json'}

        # Get value only from device as requested
        device_value = _get_ninja_device_custom_field(api_url, headers, auth, device_id, _AD_CUSTOM_FIELD_NAME)
        
        logger.debug('Device field value: %s', (device_value[:100] + '...') if device_value else 'None')

        latest_parsed = _extract_and_validate_ad_data(device_value)

        if latest_parsed:
            comp_count = len(latest_parsed.get('workstations') or [])
            logger.info('Parsed AD data from device: found %d computers. Keys: %s', comp_count, list(latest_parsed.keys()))

        if latest_parsed:
            count = _save_ad_inventory_to_excel(latest_parsed, client, days, clean_client)
            if count is not None:
                gen_str = latest_parsed.get('generatedAtUtc', 'unknown time')
                return jsonify({
                    'success': True,
                    'message': f'Synced AD inventory from Ninja (Generated at: {gen_str})',
                    'count': count,
                    'cached': True
                })
        
        return jsonify({'error': 'No AD inventory data found in custom fields for this organization or device.'}), 404

    except Exception as e:
        logger.error('AD sync failed: %s', str(e), exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/ad/debug/inspect-field', methods=['POST'])
def inspect_ninja_field():
    """Debug endpoint to pull raw custom field data for a specific ID and type."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json or {}
    type_ = data.get('type')  # 'org' or 'device'
    id_ = data.get('id')
    field = data.get('field') or _AD_CUSTOM_FIELD_NAME

    if not id_ or not type_:
        return jsonify({'error': 'id and type are required'}), 400

    api_url = _get_ninja_api_url()

    try:
        headers, auth = _get_ninja_auth(api_url)
        headers = {**headers, 'Content-Type': 'application/json'}

        if type_ == 'org':
            value = _get_ninja_organization_custom_field(api_url, headers, auth, int(id_), field)
        else:
            value = _get_ninja_device_custom_field(api_url, headers, auth, int(id_), field)

        if value is None:
            logger.info('Inspect Field: Field "%s" not found for %s %s', field, type_, id_)
            return jsonify({'success': True, 'found': False, 'message': f'Field "{field}" is empty or not found'})

        logger.info('Inspect Field: Found field "%s" for %s %s. Length: %d', field, type_, id_, len(value))
        
        try:
            parsed = json.loads(value)
            return jsonify({'success': True, 'found': True, 'raw': value, 'parsed': parsed})
        except Exception as e:
            logger.warning('Inspect Field: Failed to parse field "%s" for %s %s: %s', field, type_, id_, str(e))
            # Try repair for the debug view as well
            try:
                repaired = value.replace("'", '"')
                parsed = json.loads(repaired)
                logger.info('Inspect Field: JSON repaired for debug view.')
                return jsonify({'success': True, 'found': True, 'raw': value, 'parsed': parsed, 'was_repaired': True})
            except Exception:
                pass
            return jsonify({'success': True, 'found': True, 'raw': value, 'parsed': None, 'parse_error': str(e)})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ad/debug/list-fields', methods=['POST'])
def list_ninja_fields():
    """Debug endpoint to list all available custom fields for a specific ID and type."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json or {}
    type_ = data.get('type')
    id_ = data.get('id')

    if not id_ or not type_:
        return jsonify({'error': 'id and type are required'}), 400

    api_url = _get_ninja_api_url()

    try:
        headers, auth = _get_ninja_auth(api_url)
        fields = _get_ninja_all_custom_fields(api_url, headers, auth, type_, id_)
        
        if not fields:
            return jsonify({'success': True, 'fields': {}, 'message': 'No custom fields found or entity not reachable'})
            
        return jsonify({'success': True, 'fields': fields})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ad/tokens/generate', methods=['POST'])
def ad_generate_intake_token():
    """Generate a new AD intake token for rotation.

    Note: this does not persist changes to environment variables; it returns a suggested token
    that you should set as AD_INTAKE_TOKEN_CURRENT (and move the previous current to PREVIOUS).
    """
    if os.getenv('ENABLE_BASIC_AUTH', '0') not in ('1', 'true', 'True'):
        return jsonify({'error': 'Not available unless Basic Auth is enabled'}), 403

    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    token = secrets.token_hex(32)
    return jsonify({'success': True, 'token': token})


@app.route('/ad/status', methods=['GET'])
def ad_status():
    client = (request.args.get('client') or '').strip()
    try:
        days = int(request.args.get('days') or 30)
    except Exception:
        days = 30

    entry = _AD_CACHE.get((client, days)) if client else None
    if not entry:
        return jsonify({'success': True, 'available': False})

    return jsonify({
        'success': True,
        'available': True,
        'client': client,
        'days': days,
        'count': entry.get('count', 0),
        'received_at': entry.get('received_at')
    })


@app.route('/ad/attach', methods=['POST'])
def ad_attach():
    """Attach a previously received AD snapshot to the current session as file_id=3."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON body'}), 400

    client = (data.get('client') or '').strip()
    days = data.get('days')
    file_id = str(data.get('file_id', '3'))

    if not client:
        return jsonify({'error': 'client is required'}), 400

    try:
        days = int(days)
    except Exception:
        return jsonify({'error': 'days must be an integer'}), 400

    entry = _AD_CACHE.get((client, days))
    if not entry:
        return jsonify({'error': 'AD snapshot not available yet'}), 404

    logger.info(f'AD attach - session files BEFORE: {session.get("files", {})}')

    # Store filepath in both session and global store for resilience
    _set_session_file(file_id, entry['path'], _get_client_id(data))
    
    logger.info(f'AD attach - session files AFTER: {session.get("files", {})}')

    return jsonify({
        'success': True,
        'filename': 'Active Directory',
        'sheets': ['Sheet1'],
        'columns': ['Computer Name'],
        'count': entry.get('count', 0),
        'row_count': entry.get('count', 0)
    })


@app.route('/cleanup', methods=['POST'])
def cleanup():
    """Clean up uploaded files.

    - Removes files referenced by the current session.
    - Prunes old files from uploads/ to avoid orphan buildup.

    Note: this endpoint is called from a `beforeunload` handler where CSRF/session
    data can be missing; we treat that case as a no-op for session cleanup.
    """
    csrf_err = _require_csrf()
    csrf_ok = not bool(csrf_err)

    uploads_root = os.path.abspath(app.config['UPLOAD_FOLDER'])

    # 1) Clear session file references (but don't delete files immediately)
    # Files will be cleaned up by age-based pruning below
    # This prevents premature deletion when browsers trigger pagehide unexpectedly
    # Note: We no longer clear session files here to prevent premature loss of references
    # during transient browser events like pagehide. Disk cleanup still occurs below.
    # if csrf_ok and 'files' in session:
    #     session.pop('files', None)

    # 2) Prune old/orphan uploads (age-based)
    try:
        retention_hours = int(os.getenv('UPLOAD_RETENTION_HOURS', '24'))
    except Exception:
        retention_hours = 24

    retention_seconds = max(0, retention_hours) * 3600
    now = time.time()

    try:
        for name in os.listdir(uploads_root):
            path = os.path.abspath(os.path.join(uploads_root, name))
            if not path.startswith(uploads_root):
                continue
            if not os.path.isfile(path):
                continue

            try:
                age_seconds = now - os.path.getmtime(path)
                if age_seconds >= retention_seconds:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass

    # 3) Prune global _CLIENT_FILE_STORE (age-based)
    try:
        current_store_keys = list(_CLIENT_FILE_STORE.keys())
        for cid in current_store_keys:
            if cid in _CLIENT_FILE_STORE:
                age = now - _CLIENT_FILE_STORE[cid].get('last_access', 0)
                if age >= retention_seconds:
                    del _CLIENT_FILE_STORE[cid]
                    logger.info(f'Pruned client {cid} from global store (age: {int(age/3600)}h)')
    except Exception as e:
        logger.warning(f'Global store pruning failed: {e}')

    return jsonify({'success': True, 'retention_hours': retention_hours})


if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0') in ('1', 'true', 'True')
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '5000'))
    logger.info(f"Endpoint Comparison Tool starting (http://{host}:{port})")
    app.run(host=host, debug=debug, port=port)


