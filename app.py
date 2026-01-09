"""
Excel Column Comparison Tool
Compares selected columns between two Excel files and displays differences.
"""

import os
import re
import requests
import base64
from flask import Flask, render_template, request, jsonify, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import pandas as pd
import uuid

# Load environment variables
load_dotenv()


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


def normalize_value(value):
    """
    Normalize a value for comparison by:
    - Converting to lowercase
    - Removing common suffixes (.local, .lan, .home, etc.)
    - Replacing spaces, hyphens, underscores with nothing
    - Removing apostrophes and special characters
    """
    if not value:
        return ''
    
    # Fix encoding first
    value = fix_encoding(value)
    
    # Convert to lowercase
    normalized = value.lower().strip()
    
    # Remove common network suffixes
    suffixes = ['.local', '.lan', '.home', '.internal', '.localdomain', '.domain']
    for suffix in suffixes:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
    
    # Remove apostrophes and common special characters
    normalized = re.sub(r"['\"`]", '', normalized)
    
    # Replace spaces, hyphens, underscores, dots with nothing (normalize separators)
    normalized = re.sub(r'[\s\-_\.]+', '', normalized)
    
    return normalized

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    """Handle file upload and return column information."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    file_id = request.form.get('file_id', '1')
    
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
    data = request.json
    file_id = data.get('file_id')
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
    data = request.json
    file_id = data.get('file_id')
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
    data = request.json
    
    file1_sheet = data.get('file1_sheet')
    file1_column = data.get('file1_column')
    file2_sheet = data.get('file2_sheet')
    file2_column = data.get('file2_column')
    
    if 'files' not in session:
        return jsonify({'error': 'Files not found. Please upload again.'}), 400
    
    if '1' not in session['files'] or '2' not in session['files']:
        return jsonify({'error': 'Both files must be uploaded.'}), 400
    
    try:
        # Read both files
        df1 = pd.read_excel(session['files']['1'], sheet_name=file1_sheet)
        df2 = pd.read_excel(session['files']['2'], sheet_name=file2_sheet)
        
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
        prefix_matches = []  # List of (file1_value, file2_value) tuples
        matched_from_file1 = set()
        matched_from_file2 = set()
        
        for norm1 in list(only_in_file1_norm):
            orig1 = norm_to_orig1[norm1]
            prefix1 = norm1[:15] if len(norm1) > 15 else norm1
            
            for norm2 in list(only_in_file2_norm):
                if norm2 in matched_from_file2:
                    continue
                orig2 = norm_to_orig2[norm2]
                prefix2 = norm2[:15] if len(norm2) > 15 else norm2
                
                # Check if one is a prefix of the other (handles truncation)
                if prefix1 == prefix2 or norm1.startswith(norm2) or norm2.startswith(norm1):
                    prefix_matches.append({
                        'file1': orig1,
                        'file2': orig2,
                        'matched_on': 'prefix'
                    })
                    matched_from_file1.add(norm1)
                    matched_from_file2.add(norm2)
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
            return jsonify({
                'error': f'SentinelOne API error: {response.status_code} - {response.text}'
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
    
    print(f"DEBUG: SentinelOne Endpoints request. site_id: {site_id}")
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
                return jsonify({
                    'error': f'SentinelOne API error: {response.status_code} - {response.text}'
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
    data = request.json
    file_id = data.get('file_id', '1')
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
    
    # Check which auth method to use
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    api_key = os.getenv('NINJARMM_API_KEY')
    api_secret = os.getenv('NINJARMM_API_SECRET')
    
    print(f"DEBUG: API URL: {api_url}")
    print(f"DEBUG: Has client_id: {bool(client_id)}")
    print(f"DEBUG: Has api_key: {bool(api_key)}")
    
    if not (client_id and client_secret) and not (api_key and api_secret):
        return jsonify({
            'error': 'NinjaRMM API credentials not configured. Please set either NINJARMM_CLIENT_ID/CLIENT_SECRET (Client App API) or NINJARMM_API_KEY/API_SECRET (Legacy API) in .env file'
        }), 400
    
    try:
        headers = {'Accept': 'application/json'}
        auth = None
        
        # Use Client App API (OAuth) if credentials provided
        if client_id and client_secret:
            print("DEBUG: Using Client App API (OAuth)")
            # Get OAuth token - NinjaRMM expects form-encoded data, not JSON
            token_response = requests.post(
                f'{api_url}/oauth/token',
                data={  # Use 'data' for form-encoded, not 'json'
                    'grant_type': 'client_credentials',
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'scope': 'monitoring'
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30
            )
            
            print(f"DEBUG: OAuth response: {token_response.status_code}")
            
            if token_response.status_code != 200:
                return jsonify({
                    'error': f'NinjaRMM OAuth error: {token_response.status_code} - {token_response.text}'
                }), token_response.status_code
            
            access_token = token_response.json().get('access_token')
            headers['Authorization'] = f'Bearer {access_token}'
        elif api_key and api_secret:
            # Use Legacy API - Try WITHOUT Authorization header
            print("DEBUG: Using Legacy API")
            print(f"DEBUG: API Key: {api_key[:5]}...")
            # NinjaRMM Legacy might use query parameters or different auth
            # Let's try the requests auth parameter which should work
            auth = (api_key, api_secret)
            print("DEBUG: Using requests auth parameter (Basic Auth)")
        
        print(f"DEBUG: Calling {api_url}/v2/organizations")
        print(f"DEBUG: Headers: {headers}")
        print(f"DEBUG: Auth set: {bool(auth)}")
        
        response = requests.get(
            f'{api_url}/v2/organizations',
            headers=headers,
            auth=auth,
            timeout=30
        )
        
        print(f"DEBUG: Response status: {response.status_code}")
        print(f"DEBUG: Response body: {response.text[:200]}")
        
        if response.status_code != 200:
            return jsonify({
                'error': f'NinjaRMM API error: {response.status_code} - {response.text}'
            }), response.status_code
        
        orgs = response.json()
        
        # Format organizations for dropdown
        org_list = [
            {'id': org['id'], 'name': org['name']} 
            for org in orgs
        ]
        
        # Sort alphabetically
        org_list.sort(key=lambda x: x['name'])
        
        return jsonify({
            'success': True,
            'organizations': org_list
        })
    
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
        
    print(f"DEBUG: NinjaRMM Devices request. org_id: {org_id}")
    # Check which auth method to use
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    api_key = os.getenv('NINJARMM_API_KEY')
    api_secret = os.getenv('NINJARMM_API_SECRET')
    
    if not (client_id and client_secret) and not (api_key and api_secret):
        return jsonify({
            'error': 'NinjaRMM API credentials not configured.'
        }), 400
    
    try:
        headers = {'Accept': 'application/json'}
        auth = None
        
        # Use Client App API (OAuth) if credentials provided
        if client_id and client_secret:
            # Get OAuth token - NinjaRMM expects form-encoded data
            token_response = requests.post(
                f'{api_url}/oauth/token',
                data={  # Use 'data' for form-encoded, not 'json'
                    'grant_type': 'client_credentials',
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'scope': 'monitoring'
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30
            )
            
            if token_response.status_code != 200:
                return jsonify({
                    'error': f'NinjaRMM OAuth error: {token_response.status_code}'
                }), token_response.status_code
            
            access_token = token_response.json().get('access_token')
            headers['Authorization'] = f'Bearer {access_token}'
        elif api_key and api_secret:
            # Use Legacy API
            auth = (api_key, api_secret)
        
        devices = []
        page_num = 0
        page_size = 1000
        
        # Paginate through all devices
        while True:
            params = {
                'pageSize': page_size,
                'page': page_num
            }
            
            if org_id:
                endpoint = f'{api_url}/v2/organization/{org_id}/devices'
            else:
                endpoint = f'{api_url}/v2/devices'
            
            response = requests.get(
                endpoint,
                headers=headers,
                auth=auth,
                params=params,
                timeout=30
            )
            
            if response.status_code != 200:
                return jsonify({
                    'error': f'NinjaRMM API error: {response.status_code} - {response.text}'
                }), response.status_code
            
            data = response.json()
            
            if not data:
                break
            
            # Extract device names, IDs, and last contact times
            for device in data:
                device_name = device.get('systemName') or device.get('dnsName')
                if device_name:
                    devices.append({
                        'name': fix_encoding(device_name),
                        'id': device.get('id'),  # Device ID for script execution
                        'lastContact': device.get('lastContact')  # Unix timestamp (seconds)
                    })
            
            # Check if there are more pages
            if len(data) < page_size:
                break
            page_num += 1
        
        # Remove duplicates by name (keep first occurrence with lastContact)
        seen = {}
        for dev in devices:
            if dev['name'] not in seen:
                seen[dev['name']] = dev
        devices = sorted(seen.values(), key=lambda x: x['name'])
        
        return jsonify({
            'success': True,
            'devices': devices,
            'count': len(devices)
        })
    
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ninjarmm/upload', methods=['POST'])
def upload_ninjarmm_data():
    """Create a virtual file from NinjaRMM device data."""
    data = request.json
    file_id = data.get('file_id', '1')
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
            print(f"Error fetching S1 sites: {e}")
    
    # Fetch NinjaRMM organizations
    if ninja_available:
        try:
            headers = {'Accept': 'application/json'}
            auth = None
            
            if ninja_client_id and ninja_client_secret:
                token_response = requests.post(
                    f'{ninja_url}/oauth/token',
                    data={
                        'grant_type': 'client_credentials',
                        'client_id': ninja_client_id,
                        'client_secret': ninja_client_secret,
                        'scope': 'monitoring'
                    },
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    timeout=30
                )
                if token_response.status_code == 200:
                    access_token = token_response.json().get('access_token')
                    headers['Authorization'] = f'Bearer {access_token}'
            elif ninja_api_key and ninja_api_secret:
                auth = (ninja_api_key, ninja_api_secret)
            
            response = requests.get(
                f'{ninja_url}/v2/organizations',
                headers=headers,
                auth=auth,
                timeout=30
            )
            if response.status_code == 200:
                ninja_orgs = [{'id': org['id'], 'name': org['name']} for org in response.json()]
        except Exception as e:
            print(f"Error fetching Ninja orgs: {e}")
    
    # Match clients by normalized name
    def normalize_name(name):
        """Normalize name for matching: lowercase, remove special chars, trim whitespace."""
        normalized = name.lower().strip()
        normalized = re.sub(r'[^\w\s]', '', normalized)  # Remove special characters
        normalized = re.sub(r'\s+', ' ', normalized)  # Normalize whitespace
        return normalized
    
    # Create lookup maps
    s1_by_norm = {normalize_name(site['name']): site for site in s1_sites}
    ninja_by_norm = {normalize_name(org['name']): org for org in ninja_orgs}
    
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
    
    print(f"DEBUG: Fetching NinjaRMM scripts")
    
    # Check which auth method to use
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    api_key = os.getenv('NINJARMM_API_KEY')
    api_secret = os.getenv('NINJARMM_API_SECRET')
    
    if not (client_id and client_secret) and not (api_key and api_secret):
        return jsonify({
            'error': 'NinjaRMM API credentials not configured.'
        }), 400
    
    try:
        headers = {'Accept': 'application/json'}
        auth = None
        
        # Use Client App API (OAuth) if credentials provided
        if client_id and client_secret:
            # Get OAuth token
            token_response = requests.post(
                f'{api_url}/oauth/token',
                data={
                    'grant_type': 'client_credentials',
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'scope': 'monitoring'
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30
            )
            
            if token_response.status_code != 200:
                return jsonify({
                    'error': f'NinjaRMM OAuth error: {token_response.status_code}',
                    'details': token_response.text
                }), token_response.status_code
            
            access_token = token_response.json().get('access_token')
            headers['Authorization'] = f'Bearer {access_token}'
        elif api_key and api_secret:
            # Use Legacy API
            auth = (api_key, api_secret)
        
        # Fetch scripts - try multiple possible endpoints
        possible_endpoints = [
            f'{api_url}/v2/automation/scripts',
            f'{api_url}/v2/queries/scripts',
            f'{api_url}/v2/scripts'
        ]
        
        scripts_data = None
        last_error = None
        
        for endpoint in possible_endpoints:
            try:
                print(f"DEBUG: Trying scripts endpoint: {endpoint}")
                response = requests.get(
                    endpoint,
                    headers=headers,
                    auth=auth,
                    timeout=30
                )
                
                print(f"DEBUG: Response status: {response.status_code}")
                
                if response.status_code == 200:
                    scripts_data = response.json()
                    print(f"DEBUG: Successfully fetched {len(scripts_data)} scripts from {endpoint}")
                    break
                else:
                    last_error = f"{response.status_code}: {response.text[:200]}"
                    print(f"DEBUG: Failed with {last_error}")
            except Exception as e:
                last_error = str(e)
                print(f"DEBUG: Exception: {last_error}")
                continue
        
        if scripts_data is None:
            return jsonify({
                'error': f'Could not fetch scripts from any endpoint. Last error: {last_error}'
            }), 404
        
        # Format scripts for easier consumption
        scripts = []
        for script in scripts_data:
            scripts.append({
                'id': script.get('id'),
                'name': script.get('name'),
                'description': script.get('description', ''),
                'category': script.get('category', 'Uncategorized'),
                'language': script.get('scriptType', 'Unknown')
            })
        
        return jsonify({
            'success': True,
            'scripts': scripts,
            'count': len(scripts)
        })
    
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ninjarmm/run-script', methods=['POST'])
def run_ninjarmm_script():
    """Trigger a script to run on a specific NinjaRMM device."""
    api_url = os.getenv('NINJARMM_API_URL', 'https://api.ninjarmm.com')
    
    # Get request parameters
    data = request.get_json()
    device_id = data.get('device_id')
    script_id = data.get('script_id')
    script_params = data.get('parameters', {})  # Optional script parameters
    
    if not device_id:
        return jsonify({'error': 'device_id is required'}), 400
    if not script_id:
        return jsonify({'error': 'script_id is required'}), 400
    
    print(f"DEBUG: Running script {script_id} on device {device_id}")
    
    # Check which auth method to use
    client_id = os.getenv('NINJARMM_CLIENT_ID')
    client_secret = os.getenv('NINJARMM_CLIENT_SECRET')
    api_key = os.getenv('NINJARMM_API_KEY')
    api_secret = os.getenv('NINJARMM_API_SECRET')
    
    if not (client_id and client_secret) and not (api_key and api_secret):
        return jsonify({
            'error': 'NinjaRMM API credentials not configured.'
        }), 400
    
    try:
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        auth = None
        
        # Use Client App API (OAuth) if credentials provided
        if client_id and client_secret:
            # Get OAuth token
            token_response = requests.post(
                f'{api_url}/oauth/token',
                data={
                    'grant_type': 'client_credentials',
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'scope': 'monitoring'
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30
            )
            
            if token_response.status_code != 200:
                return jsonify({
                    'error': f'NinjaRMM OAuth error: {token_response.status_code}',
                    'details': token_response.text
                }), token_response.status_code
            
            access_token = token_response.json().get('access_token')
            headers['Authorization'] = f'Bearer {access_token}'
        elif api_key and api_secret:
            # Use Legacy API
            auth = (api_key, api_secret)
        
        # Prepare script execution payload
        payload = {
            'scriptId': script_id,
            'parameters': script_params
        }
        
        # Execute script on device
        endpoint = f'{api_url}/v2/device/{device_id}/script/run'
        response = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            auth=auth,
            timeout=30
        )
        
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
                'error': f'NinjaRMM API error: {response.status_code}',
                'details': response.text
            }), response.status_code
    
    except requests.exceptions.Timeout:
        return jsonify({'error': 'NinjaRMM API request timed out'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'NinjaRMM API connection error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/cleanup', methods=['POST'])
def cleanup():
    """Clean up uploaded files."""
    if 'files' in session:
        for file_id, filepath in session['files'].items():
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
        session.pop('files', None)
    
    return jsonify({'success': True})


if __name__ == '__main__':
    print("\n" + "="*60)
    print("  Endpoint Comparison Tool")
    print("  SentinelOne vs NinjaRMM")
    print("  Open http://localhost:5000 in your browser")
    print("="*60 + "\n")
    app.run(debug=True, port=5000)


