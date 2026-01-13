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
from flask import Flask, render_template, request, jsonify, session, Response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import pandas as pd
import uuid

# Load environment variables
load_dotenv()

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper())
logger = logging.getLogger(__name__)

# Simple in-process cache for Ninja OAuth tokens
_NINJA_TOKEN_CACHE = {
    'access_token': None,
    'expires_at': 0,
    'api_url': None,
}

# In-process cache for Active Directory device snapshots received from Ninja
# Key: (client_name, days) -> {path, count, received_at}
_AD_CACHE = {}

# AD inventory storage (written by the Ninja-run PowerShell script)
_AD_CUSTOM_FIELD_NAME = 'ADInventoryJson'

# One-time nonces for AD intake (legacy; kept for backwards compatibility)
# Key: nonce -> {client, days, signing_key, expires_at}
_AD_INTAKE_NONCES = {}
_AD_INTAKE_NONCE_TTL_SECONDS = 15 * 60


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
    replacements = {
        'â€™': "'",
        'â€˜': "'",
        'â€œ': '"',
        'â€': '"',
        'â€"': '-',
        'â€"': '-',
        'Ã©': 'é',
        'Ã¨': 'è',
        'Ã¡': 'á',
        'Ã ': 'à',
    }
    for bad, good in replacements.items():
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

# Use a stable secret key if provided; falls back to a random key for local dev.
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY') or os.urandom(24)

# Session cookie hardening (recommended for LAN/prod)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', '0') in ('1', 'true', 'True')


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

    files = session.get('files')
    if isinstance(files, dict):
        session['files'] = {str(k): v for k, v in files.items()}


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

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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
    """Return (headers, auth) for NinjaRMM calls; caches OAuth token when used."""
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    api_key = os.getenv('NINJARMM_API_KEY')
    api_secret = os.getenv('NINJARMM_API_SECRET')

    if (client_id and client_secret):
        now = time.time()
        if (
            _NINJA_TOKEN_CACHE.get('access_token')
            and _NINJA_TOKEN_CACHE.get('api_url') == api_url
            and _NINJA_TOKEN_CACHE.get('expires_at', 0) > (now + 30)
        ):
            return ({'Accept': 'application/json', 'Authorization': f"Bearer {_NINJA_TOKEN_CACHE['access_token']}"}, None)

        token_response = requests.post(
            f'{api_url}/oauth/token',
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret,
                'scope': os.getenv('NINJARMM_OAUTH_SCOPE', 'monitoring management')
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )

        if token_response.status_code != 200:
            raise ValueError(f"NinjaRMM OAuth error: {token_response.status_code} {token_response.text[:200]}")

        token_json = token_response.json()
        access_token = token_json.get('access_token')
        expires_in = int(token_json.get('expires_in') or 3600)
        if not access_token:
            raise ValueError('NinjaRMM OAuth error: missing access_token')

        _NINJA_TOKEN_CACHE.update({
            'access_token': access_token,
            'expires_at': now + expires_in,
            'api_url': api_url,
        })

        return ({'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}, None)

    if (api_key and api_secret):
        return ({'Accept': 'application/json'}, (api_key, api_secret))

    raise ValueError('NinjaRMM API credentials not configured.')


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


def _format_ninja_parameters_kv_lines(params: dict) -> str:
    # Common format expected by some Ninja script runners: key=value, one per line.
    lines = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        lines.append(f"{k}={v}")
    return "\n".join(lines)


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


def _format_ninja_parameters_space_separated(params: dict) -> str:
    # Space-separated without dashes: ParamName "value" ParamName2 value2
    parts = []
    for k, v in (params or {}).items():
        if v is None:
            continue
        # Quote string values, don't quote numbers
        if isinstance(v, str):
            parts.append(f'{k} "{v}"')
        else:
            parts.append(f'{k} {v}')
    return " ".join(parts)


def _extract_ninja_custom_field(device_data, field_name: str):
    """Best-effort extraction of a custom field value from a Ninja device payload."""
    if not isinstance(device_data, dict) or not field_name:
        return None

    # Direct key (rare)
    for k, v in device_data.items():
        if isinstance(k, str) and k.lower() == field_name.lower():
            return v

    for key in ('customFields', 'custom_fields', 'fields', 'properties'):
        blob = device_data.get(key)
        if isinstance(blob, dict):
            for k, v in blob.items():
                if isinstance(k, str) and k.lower() == field_name.lower():
                    return v
        elif isinstance(blob, list):
            for it in blob:
                if not isinstance(it, dict):
                    continue
                name = it.get('name') or it.get('fieldName') or it.get('key') or it.get('label')
                if isinstance(name, str) and name.lower() == field_name.lower():
                    return it.get('value')

    # One-level deep scan
    for v in device_data.values():
        if isinstance(v, dict):
            found = _extract_ninja_custom_field(v, field_name)
            if found is not None:
                return found

    return None


def _get_ninja_device_custom_field(api_url, headers, auth, device_id: int, field_name: str):
    endpoints = [
        f'{api_url}/api/v2/device/{device_id}',
        f'{api_url}/v2/device/{device_id}',
        f'{api_url}/v2/devices/{device_id}',
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
        f'{api_url}/api/v2/organization/{org_id}',
        f'{api_url}/v2/organization/{org_id}',
        f'{api_url}/v2/organizations/{org_id}',
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
    session.setdefault('csrf_token', uuid.uuid4().hex)
    return render_template('index.html', csrf_token=session['csrf_token'])


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/upload', methods=['POST'])
def upload_file():
    """Handle file upload and return column information."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    file_id = str(request.form.get('file_id', '1'))
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type. Please upload .xlsx or .xls files'}), 400
    
    try:
        # Generate unique filename
        unique_id = str(uuid.uuid4())
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_{filename}")
        file.save(filepath)
        
        # Read Excel and get sheet/column info
        xl = read_excel_file(filepath)
        sheets = xl.sheet_names
        
        # Get columns for first sheet by default
        df = pd.read_excel(xl, sheet_name=sheets[0])
        columns = df.columns.tolist()
        
        # Store filepath in session
        if 'files' not in session:
            session['files'] = {}
        session['files'][file_id] = filepath
        session.modified = True
        
        return jsonify({
            'success': True,
            'filename': filename,
            'sheets': sheets,
            'columns': columns,
            'row_count': len(df)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/get_columns', methods=['POST'])
def get_columns():
    """Get columns for a specific sheet."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json
    file_id = data.get('file_id')
    file_id = str(file_id) if file_id is not None else None
    sheet_name = data.get('sheet_name')
    
    if 'files' not in session or file_id not in session['files']:
        return jsonify({'error': 'File not found. Please upload again.'}), 400
    
    try:
        filepath = session['files'][file_id]
        df = pd.read_excel(filepath, sheet_name=sheet_name)
        columns = df.columns.tolist()
        
        return jsonify({
            'success': True,
            'columns': columns,
            'row_count': len(df)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/preview_column', methods=['POST'])
def preview_column():
    """Get preview data for a specific column."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json
    file_id = data.get('file_id')
    file_id = str(file_id) if file_id is not None else None
    sheet_name = data.get('sheet_name')
    column_name = data.get('column_name')
    
    if 'files' not in session or file_id not in session['files']:
        return jsonify({'error': 'File not found. Please upload again.'}), 400
    
    try:
        filepath = session['files'][file_id]
        df = pd.read_excel(filepath, sheet_name=sheet_name)
        
        if column_name not in df.columns:
            return jsonify({'error': 'Column not found.'}), 400
        
        # Get column data and clean it
        col_data = df[column_name].dropna().astype(str).tolist()
        col_data = [fix_encoding(v).strip() for v in col_data if str(v).strip()]
        
        # Get first 5 unique values as preview
        preview_values = []
        seen = set()
        for value in col_data:
            if value not in seen and len(preview_values) < 5:
                preview_values.append(value)
                seen.add(value)
        
        return jsonify({
            'success': True,
            'preview': preview_values,
            'total_count': len(col_data),
            'unique_count': len(set(col_data))
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/compare', methods=['POST'])
def compare_columns():
    """Compare selected columns from both files using set-based comparison."""
    csrf_err = _require_csrf()
    if csrf_err:
        return csrf_err

    data = request.json

    file1_id = str(data.get('file1_id', '1'))
    file2_id = str(data.get('file2_id', '2'))

    file1_sheet = data.get('file1_sheet')
    file1_column = data.get('file1_column')
    file2_sheet = data.get('file2_sheet')
    file2_column = data.get('file2_column')

    if 'files' not in session:
        return jsonify({'error': 'Files not found. Please upload again.'}), 400

    if file1_id not in session['files'] or file2_id not in session['files']:
        return jsonify({'error': 'Both files must be uploaded.'}), 400

    try:
        # Read both files
        df1 = pd.read_excel(session['files'][file1_id], sheet_name=file1_sheet)
        df2 = pd.read_excel(session['files'][file2_id], sheet_name=file2_sheet)
        
        # Get the columns and filter out empty values
        col1_data = df1[file1_column].dropna().astype(str).tolist()
        col2_data = df2[file2_column].dropna().astype(str).tolist()
        
        # Fix encoding and filter out empty strings
        col1_data = [fix_encoding(v).strip() for v in col1_data if str(v).strip()]
        col2_data = [fix_encoding(v).strip() for v in col2_data if str(v).strip()]
        
        # Create mappings: normalized -> original values
        # Keep the first original value seen for each normalized form
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
        
        # Prefix matching for 15-char truncation (NinjaRMM limitation)
        # Check if any unmatched item from file1 matches the first 15 chars of an unmatched item from file2, or vice versa
        # Minimum length threshold prevents false positives on very short hostnames
        prefix_matches = []  # List of (file1_value, file2_value) tuples
        matched_from_file1 = set()
        matched_from_file2 = set()
        min_prefix_length = 10  # Avoid matching very short names that could be coincidental
        
        for norm1 in list(only_in_file1_norm):
            orig1 = norm_to_orig1[norm1]
            prefix1 = norm1[:15] if len(norm1) > 15 else norm1
            
            for norm2 in list(only_in_file2_norm):
                if norm2 in matched_from_file2:
                    continue
                orig2 = norm_to_orig2[norm2]
                prefix2 = norm2[:15] if len(norm2) > 15 else norm2
                
                # Only match if both names are long enough to avoid false positives
                if len(norm1) >= min_prefix_length and len(norm2) >= min_prefix_length:
                    # Check if one is a prefix of the other (handles truncation)
                    if prefix1 == prefix2 or norm1.startswith(norm2) or norm2.startswith(norm1):
                        prefix_matches.append({
                            'file1': orig1,
                            'file2': orig2,
                            'matched_on': 'prefix'
                        })
                        matched_from_file1.add(norm1)
                        matched_from_file2.add(norm2)
                        logger.debug(f"Prefix match: '{orig1}' <-> '{orig2}' (normalized: '{norm1}' <-> '{norm2}')")
                        break
        
        # Remove prefix-matched items from only_in lists
        only_in_file1_norm -= matched_from_file1
        only_in_file2_norm -= matched_from_file2
        
        # Map back to original values for display
        only_in_file1 = sorted([norm_to_orig1[n] for n in only_in_file1_norm])
        only_in_file2 = sorted([norm_to_orig2[n] for n in only_in_file2_norm])
        in_both = sorted([norm_to_orig1[n] for n in in_both_norm])
        
        # Calculate statistics
        total_file1 = len(col1_data)
        total_file2 = len(col2_data)
        unique_file1 = len(set1_norm)
        unique_file2 = len(set2_norm)
        
        return jsonify({
            'success': True,
            'only_in_file1': only_in_file1,
            'only_in_file2': only_in_file2,
            'in_both': in_both,
            'prefix_matches': prefix_matches,
            'stats': {
                'total_file1': total_file1,
                'total_file2': total_file2,
                'unique_file1': unique_file1,
                'unique_file2': unique_file2,
                'only_in_file1_count': len(only_in_file1),
                'only_in_file2_count': len(only_in_file2),
                'common_count': len(in_both),
                'prefix_match_count': len(prefix_matches),
                'match_percentage': round((len(in_both) + len(prefix_matches)) / max(unique_file1, unique_file2, 1) * 100, 1)
            }
        })
    
    except KeyError as e:
        return jsonify({'error': f'Column not found: {str(e)}'}), 400
    except Exception as e:
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
                computer_name = agent.get('computerName') or agent.get('networkInterfaces', [{}])[0].get('name')
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
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # Save to Excel
        df.to_excel(filepath, index=False, engine='openpyxl')
        
        # Store filepath in session
        if 'files' not in session:
            session['files'] = {}
        session['files'][file_id] = filepath
        session.modified = True
        
        return jsonify({
            'success': True,
            'filename': 'SentinelOne Endpoints',
            'sheets': ['Sheet1'],
            'columns': ['Endpoint Name'],
            'row_count': len(endpoint_names)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ninjarmm/test', methods=['GET'])
def test_ninjarmm_auth():
    """Test different authentication methods for NinjaRMM."""
    if os.getenv('ENABLE_NINJARMM_TEST', '0') not in ('1', 'true', 'True'):
        return jsonify({'error': 'Not found'}), 404

    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')
    api_key = os.getenv('NINJARMM_API_KEY')
    api_secret = os.getenv('NINJARMM_API_SECRET')
    
    results = []
    
    # Test 1: Basic Auth with requests auth parameter
    try:
        response = requests.get(
            f'{api_url}/v2/organizations',
            auth=(api_key, api_secret),
            headers={'Accept': 'application/json'},
            timeout=10
        )
        results.append({
            'method': 'requests auth parameter',
            'status': response.status_code,
            'response': response.text[:200]
        })
    except Exception as e:
        results.append({'method': 'requests auth parameter', 'error': str(e)})
    
    # Test 2: Manual Base64 Authorization header
    try:
        credentials = f"{api_key}:{api_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        response = requests.get(
            f'{api_url}/v2/organizations',
            headers={
                'Accept': 'application/json',
                'Authorization': f'Basic {encoded}'
            },
            timeout=10
        )
        results.append({
            'method': 'Manual Basic Auth header',
            'status': response.status_code,
            'response': response.text[:200]
        })
    except Exception as e:
        results.append({'method': 'Manual Basic Auth header', 'error': str(e)})
    
    # Test 3: Bearer token with API key
    try:
        response = requests.get(
            f'{api_url}/v2/organizations',
            headers={
                'Accept': 'application/json',
                'Authorization': f'Bearer {api_key}'
            },
            timeout=10
        )
        results.append({
            'method': 'Bearer with API key',
            'status': response.status_code,
            'response': response.text[:200]
        })
    except Exception as e:
        results.append({'method': 'Bearer with API key', 'error': str(e)})
    
    # Test 4: No auth (to see default error)
    try:
        response = requests.get(
            f'{api_url}/v2/organizations',
            headers={'Accept': 'application/json'},
            timeout=10
        )
        results.append({
            'method': 'No authentication',
            'status': response.status_code,
            'response': response.text[:200]
        })
    except Exception as e:
        results.append({'method': 'No authentication', 'error': str(e)})
    
    return jsonify({
        'api_url': api_url,
        'api_key_length': len(api_key) if api_key else 0,
        'tests': results
    })


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
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # Save to Excel
        df.to_excel(filepath, index=False, engine='openpyxl')
        
        # Store filepath in session
        if 'files' not in session:
            session['files'] = {}
        session['files'][file_id] = filepath
        session.modified = True
        
        return jsonify({
            'success': True,
            'filename': 'NinjaRMM Devices',
            'sheets': ['Sheet1'],
            'columns': ['Device Name'],
            'row_count': len(device_names)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/clients/unified', methods=['GET'])
def get_unified_clients():
    """Fetch and match clients from both SentinelOne and NinjaRMM APIs."""
    s1_url = os.getenv('SENTINELONE_API_URL')
    s1_token = os.getenv('SENTINELONE_API_TOKEN')
    ninja_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')
    ninja_client_id = os.getenv('NINJARMM_CLIENT_ID')
    ninja_client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    ninja_api_key = os.getenv('NINJARMM_API_KEY')
    ninja_api_secret = os.getenv('NINJARMM_API_SECRET')
    
    s1_available = bool(s1_url and s1_token)
    ninja_available = bool((ninja_client_id and ninja_client_secret) or (ninja_api_key and ninja_api_secret))
    
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
            'id': script_id,
            'type': 'SCRIPT',
            'parameters': ninja_parameters
        }
        if script_uid:
            payload['uid'] = script_uid

        run_as = (os.getenv('NINJARMM_SCRIPT_RUN_AS') or '').strip()
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

    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')

    # Clean client name: remove emojis/non-ASCII, extra whitespace, newlines
    clean_client = ' '.join(client.split())
    clean_client = clean_client.encode('ascii', 'ignore').decode('ascii').strip()

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

        # Try to get previous custom field values (non-critical, continue on failure)
        previous_org_value = None
        previous_device_value = None
        try:
            previous_org_value = _get_ninja_organization_custom_field(api_url, headers, auth, org_id, _AD_CUSTOM_FIELD_NAME)
            previous_device_value = _get_ninja_device_custom_field(api_url, headers, auth, device_id, _AD_CUSTOM_FIELD_NAME)
        except Exception as e:
            logger.warning('Failed to get previous custom field values: %s', str(e))

        # Try to lookup script UID (non-critical, continue on failure)
        script_uid = None
        try:
            script_uid = _lookup_ninja_script_uid(api_url, headers, auth, script_id, device_id=device_id)
            logger.info('Script UID lookup: script_id=%s uid=%s', script_id, script_uid)
        except Exception as e:
            logger.warning('Script UID lookup failed: %s', str(e), exc_info=True)

        # Only use runAs if explicitly configured via environment variable
        # Otherwise, let NinjaRMM use its default (typically SYSTEM)
        run_as = (os.getenv('NINJARMM_SCRIPT_RUN_AS') or '').strip()

        # Build payload - no parameters, let the script use defaults
        payload = {
            'id': script_id,
            'type': 'SCRIPT',
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
            # Poll the organization custom field for results with adaptive intervals
            try:
                timeout_s = int(os.getenv('AD_CUSTOM_FIELD_POLL_TIMEOUT_SECONDS', '120'))
            except Exception:
                timeout_s = 120
            
            # Adaptive polling: start fast, then slow down (2s, 2s, 4s, 4s, 8s, 8s...)
            poll_attempt = 0
            deadline = time.time() + timeout_s
            last_seen = None

            while time.time() < deadline:
                # Calculate adaptive interval: 2s for first 2 attempts, then exponential
                poll_interval_s = min(2 ** (poll_attempt // 2), 8)  # Cap at 8 seconds
                time.sleep(poll_interval_s)
                poll_attempt += 1

                # Prefer org-scoped field, but fall back to device-scoped field.
                org_value = _get_ninja_organization_custom_field(api_url, headers, auth, org_id, _AD_CUSTOM_FIELD_NAME)
                device_value = _get_ninja_device_custom_field(api_url, headers, auth, device_id, _AD_CUSTOM_FIELD_NAME)

                candidates = []
                if org_value and (previous_org_value is None or org_value != previous_org_value):
                    candidates.append(org_value)
                if device_value and (previous_device_value is None or device_value != previous_device_value):
                    candidates.append(device_value)

                if not candidates:
                    continue

                # Avoid re-processing the same candidate value.
                value = candidates[0]
                if last_seen is not None and value == last_seen:
                    continue
                last_seen = value

                if '...TRUNCATED...' in value:
                    return jsonify({'error': f"AD payload in Ninja custom field '{_AD_CUSTOM_FIELD_NAME}' was truncated."}), 500

                try:
                    parsed = json.loads(value)
                except Exception:
                    continue

                if not isinstance(parsed, dict):
                    continue

                if str(parsed.get('runId') or '').strip() != run_id:
                    continue

                gen = str(parsed.get('generatedAtUtc') or '').strip()
                try:
                    gen_dt = datetime.fromisoformat(gen.replace('Z', '+00:00'))
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                    if gen_dt.timestamp() < (started_at - 5):
                        continue
                except Exception:
                    continue

                try:
                    parsed_days = int(parsed.get('days') or 0)
                except Exception:
                    parsed_days = 0

                if parsed_days != days:
                    continue

                items = parsed.get('workstations') or parsed.get('computers') or []
                if not isinstance(items, list):
                    continue

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

                logger.info(f"AD inventory received after {poll_attempt} poll attempts")
                return jsonify({'success': True, 'message': 'AD inventory received', 'count': len(names)})

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


@app.route('/ad/debug', methods=['GET'])
def ad_debug():
    """Diagnostic endpoint to validate AD trigger configuration (device_id, script_id)."""
    device_id = request.args.get('device_id')
    script_id = request.args.get('script_id')

    result = {
        'device_id': device_id,
        'script_id': script_id,
        'device_valid': False,
        'script_valid': False,
        'device_info': None,
        'script_info': None,
        'device_error': None,
        'script_error': None,
        'oauth_token_info': None,
        'ready': False
    }

    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')

    try:
        headers, auth = _get_ninja_auth(api_url)
        
        # Decode OAuth token to check permissions (if using OAuth)
        auth_header = headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            import base64
            token = auth_header[7:]  # Remove "Bearer " prefix
            try:
                # JWT tokens have 3 parts separated by dots: header.payload.signature
                token_parts = token.split('.')
                if len(token_parts) >= 2:
                    # Decode the payload (second part)
                    # Add padding if needed
                    payload = token_parts[1]
                    padding = 4 - len(payload) % 4
                    if padding != 4:
                        payload += '=' * padding
                    decoded = base64.urlsafe_b64decode(payload)
                    token_claims = json.loads(decoded)
                    result['oauth_token_info'] = {
                        'scopes': token_claims.get('scope', '').split() if token_claims.get('scope') else [],
                        'expires_at': token_claims.get('exp'),
                        'subject': token_claims.get('sub'),
                        'audience': token_claims.get('aud'),
                        'token_type': 'Bearer/JWT'
                    }
            except Exception as e:
                result['oauth_token_info'] = {'error': f'Failed to decode token: {str(e)}'}
    except Exception as e:
        return jsonify({'error': f'Auth failed: {e}', **result}), 400

    # Validate device_id
    if device_id:
        try:
            device_id_int = int(device_id)
            # Try /api/v2 first, then /v2
            device_data = None
            for endpoint in [f'{api_url}/api/v2/device/{device_id_int}', f'{api_url}/v2/device/{device_id_int}']:
                try:
                    r = requests.get(endpoint, headers=headers, auth=auth, timeout=10)
                    if r.status_code == 200:
                        device_data = r.json()
                        result['device_valid'] = True
                        result['device_info'] = {
                            'id': device_data.get('id'),
                            'systemName': device_data.get('systemName'),
                            'dnsName': device_data.get('dnsName'),
                            'organizationId': device_data.get('organizationId'),
                            'lastContact': device_data.get('lastContact'),
                        }
                        break
                    elif r.status_code == 404:
                        continue
                    else:
                        result['device_error'] = f'{endpoint} returned {r.status_code}: {r.text[:100]}'
                        break
                except Exception as e:
                    result['device_error'] = str(e)
            if not device_data and not result['device_error']:
                result['device_error'] = 'Device not found (404 on all endpoints)'
        except ValueError:
            result['device_error'] = 'device_id must be an integer'
    
    # Fetch scripting options for the device (if device is valid)
    if result['device_valid'] and device_id:
        try:
            device_id_int = int(device_id)
            scripting_opts_endpoint = f'{api_url}/v2/device/{device_id_int}/scripting/options'
            r = requests.get(scripting_opts_endpoint, headers=headers, auth=auth, timeout=10)
            if r.status_code == 200:
                opts = r.json()
                # Handle credentials - API may return nested structure with 'roles' and 'credentials' keys
                credentials_data = opts.get('credentials') or {}
                run_as_options = []
                
                # If it's a dict with nested structure, extract the actual credentials
                if isinstance(credentials_data, dict):
                    # Get roles
                    for role in (credentials_data.get('roles') or []):
                        if isinstance(role, dict):
                            run_as_options.append(role.get('name') or role.get('id') or str(role))
                        else:
                            run_as_options.append(str(role))
                    # Get named credentials
                    for cred in (credentials_data.get('credentials') or []):
                        if isinstance(cred, dict):
                            run_as_options.append(cred.get('name') or cred.get('id') or str(cred))
                        else:
                            run_as_options.append(str(cred))
                elif isinstance(credentials_data, list):
                    for c in credentials_data:
                        if isinstance(c, dict):
                            run_as_options.append(c.get('name') or c.get('id') or str(c))
                        else:
                            run_as_options.append(str(c))
                
                result['scripting_options'] = {
                    'runAsOptions': run_as_options,
                    'scriptsAvailable': len(opts.get('scripts') or []),
                    'actionsAvailable': len(opts.get('actions') or []),
                    'raw_response_keys': list(opts.keys()) if isinstance(opts, dict) else str(type(opts)),
                    'credentials_type': str(type(credentials_data)),
                    'credentials_keys': list(credentials_data.keys()) if isinstance(credentials_data, dict) else None,
                }
            else:
                result['scripting_options_error'] = f'Failed to fetch: {r.status_code}'
        except Exception as e:
            result['scripting_options_error'] = str(e)

    # Validate script_id
    if script_id:
        try:
            script_id_int = int(script_id)
            # Fetch scripts list and look for matching ID
            script_data = None
            for endpoint in [f'{api_url}/v2/automation/scripts', f'{api_url}/v2/queries/scripts', f'{api_url}/v2/scripts']:
                try:
                    r = requests.get(endpoint, headers=headers, auth=auth, timeout=15)
                    if r.status_code != 200:
                        continue
                    scripts = r.json()
                    if isinstance(scripts, dict):
                        scripts = scripts.get('data') or scripts.get('items') or scripts.get('scripts') or []
                    for s in (scripts or []):
                        if isinstance(s, dict) and int(s.get('id', -1)) == script_id_int:
                            script_data = s
                            result['script_valid'] = True
                            result['script_info'] = {
                                'id': s.get('id'),
                                'uid': s.get('uid') or s.get('scriptUid'),
                                'name': s.get('name'),
                                'language': s.get('scriptType') or s.get('language'),
                            }
                            break
                    if script_data:
                        break
                except Exception:
                    continue
            if not script_data and not result['script_error']:
                result['script_error'] = 'Script not found in any scripts endpoint'
        except ValueError:
            result['script_error'] = 'script_id must be an integer'

    result['ready'] = result['device_valid'] and result['script_valid']
    
    # Optional: actually test the script run API call (dry run)
    test_run = request.args.get('test_run', '').lower() == 'true'
    logger.info('ad_debug: test_run=%s ready=%s', test_run, result['ready'])
    if test_run and result['ready']:
        try:
            # Re-get auth for the test call
            test_headers, test_auth = _get_ninja_auth(api_url)
            test_headers = {**test_headers, 'Content-Type': 'application/json'}
            
            device_id_int = int(device_id)
            script_id_int = int(script_id)
            
            # Try the simplest possible payload
            test_payload = {
                'id': script_id_int,
                'type': 'SCRIPT'
            }
            
            logger.info('ad_debug: attempting test run with payload=%s', test_payload)
            
            test_results = []
            endpoints = [
                f'{api_url}/v2/device/{device_id_int}/script/run',
                f'{api_url}/api/v2/device/{device_id_int}/script/run',
            ]
            
            for endpoint in endpoints:
                try:
                    logger.info('ad_debug: calling endpoint=%s', endpoint)
                    r = requests.post(endpoint, json=test_payload, headers=test_headers, auth=test_auth, timeout=15)
                    logger.info('ad_debug: response status=%s', r.status_code)
                    test_results.append({
                        'endpoint': endpoint,
                        'status': r.status_code,
                        'body': r.text[:500],
                        'request_payload': test_payload,
                        'response_headers': dict(r.headers),
                    })
                    # If success, break
                    if r.status_code in (200, 204):
                        break
                except Exception as e:
                    logger.warning('ad_debug: endpoint error: %s', str(e))
                    test_results.append({
                        'endpoint': endpoint,
                        'error': str(e)
                    })
            
            result['test_run_results'] = test_results
        except Exception as e:
            logger.error('ad_debug: test_run error: %s', str(e))
            result['test_run_error'] = str(e)
    
    return jsonify(result)


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

    if 'files' not in session:
        session['files'] = {}

    session['files'][file_id] = entry['path']
    session.modified = True

    return jsonify({
        'success': True,
        'filename': 'Active Directory',
        'sheets': ['Sheet1'],
        'columns': ['Computer Name'],
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

    # 1) Remove session-tracked files (only when CSRF token is valid)
    if csrf_ok and 'files' in session:
        for file_id, filepath in session['files'].items():
            try:
                abs_path = os.path.abspath(filepath)
                if abs_path.startswith(uploads_root) and os.path.exists(abs_path):
                    os.remove(abs_path)
            except Exception:
                pass
        session.pop('files', None)

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

    return jsonify({'success': True, 'retention_hours': retention_hours})


if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0') in ('1', 'true', 'True')
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '5000'))
    logger.info(f"Endpoint Comparison Tool starting (http://{host}:{port})")
    app.run(host=host, debug=debug, port=port)


