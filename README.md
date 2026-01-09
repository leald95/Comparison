# Endpoint Comparison Tool

A modern web application to compare endpoints between SentinelOne (EDR) and NinjaRMM (RMM) platforms. Identify discrepancies, offline devices, and automatically remediate missing SentinelOne installations.

## Features

- **Unified Client Selection**: Automatically match and select clients across both platforms
- **SentinelOne Integration**: Fetch live endpoint data from your SentinelOne console
- **NinjaRMM Integration**: Fetch live device data from your NinjaRMM account
- **Automated Comparison**: Instant side-by-side comparison with intelligent matching
- **Comprehensive Testing View**:
  - Row-by-row device comparison showing matches and differences
  - Offline device detection (> 30 days)
  - Smart prefix matching for truncated hostnames
- **🆕 Automated Remediation**:
  - One-click SentinelOne installation on missing/offline devices
  - Configurable remediation scripts via Settings
  - Automatic online status checks before script execution
- **Statistics Dashboard**: Quick overview of match rates and counts
- **Smart Encoding Fix**: Automatically fixes character encoding issues
- **Beautiful UI**: Modern, dark-themed interface with smooth animations
- **Comparison History**: Track and reload previous comparisons

## Installation

1. **Create a virtual environment** (recommended):
   ```bash
   python -m venv venv
   
   # Windows
   .\venv\Scripts\activate
   
   # macOS/Linux
   source venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure API Credentials**:
   ```bash
   # Copy the example .env file
   cp .env.example .env
   
   # Edit .env and add your credentials
   ```

   **SentinelOne API:**
   - `SENTINELONE_API_URL`: Your SentinelOne console URL
   - `SENTINELONE_API_TOKEN`: Generate from Settings → Users → API Token Operations

   **NinjaRMM API:**
   - `NINJARMM_API_URL`: Usually `https://api.ninjarmm.com` (US) or `https://eu-api.ninjarmm.com` (EU)
   - Choose ONE authentication method:
     - **Option 1 (Recommended)**: OAuth Client App API
       - `NINJARMM_CLIENT_ID` and `NINJARMM_CLIENT_SECRET`
       - Generate from Administration → Apps → API → Add (select "Client App")
     - **Option 2**: Legacy API (Basic Auth)
       - `NINJARMM_API_KEY` and `NINJARMM_API_SECRET`
       - Generate from Administration → Apps → API → Add (select "Legacy")

## Usage

1. **Start the application**:
   ```bash
   python app.py
   ```

2. **Open your browser** and navigate to:
   ```
   http://localhost:5000
   ```

3. **Configure Remediation (Optional)**:
   - Click the ⚙️ **Settings** button in the header
   - Select a NinjaRMM script for SentinelOne installation/repair
   - This enables the "Fix S1" button in the Testing View

4. **Select a client**:
   - Click "Choose Client" to see all matched clients across both platforms
   - The app automatically fetches and compares data

5. **Review results**:
   - **Testing View**: See all devices with status indicators
   - **Remediation**: Click "🛠️ Fix S1" to run the configured script on devices where:
     - Device is online in NinjaRMM (< 30 days)
     - SentinelOne is missing or offline
   - Switch between tabs to see different views:
     - Only in SentinelOne
     - Only in NinjaRMM
     - In Both Systems
     - Prefix Matches (truncated hostnames)
     - Offline Devices (> 30 days)

6. **Export or run again**:
   - Export results to HTML for reporting
   - Click "Back to Client Selection" to compare another client
   - See values unique to each file
   - Find common values between files

## How It Works

The comparison works by:
1. Reading the selected column from each data source (Excel or SentinelOne)
2. Fixing common encoding issues (smart quotes, special characters)
3. Normalizing values (removing .local/.lan suffixes, standardizing separators)
4. Finding:
   - **Row-by-row matches**: Direct position comparison
   - **Set differences**: Unique values in each file
   - **Set intersection**: Values appearing in both files

## Project Structure

```
Comparison/
├── app.py              # Flask backend application
├── requirements.txt    # Python dependencies
├── README.md          # This file
├── .env.example       # SentinelOne API config template
├── templates/
│   └── index.html     # Web interface
└── uploads/           # Temporary file storage (auto-created)
```

## Technology Stack

- **Backend**: Python 3, Flask
- **Excel Processing**: pandas, openpyxl, xlrd
- **API Integration**: requests, python-dotenv
- **Frontend**: HTML5, CSS3, Vanilla JavaScript
- **Design**: Custom CSS with modern dark theme

## SentinelOne API

The SentinelOne integration uses the `/web/api/v2.1/agents` endpoint to fetch all endpoint computer names. The data is:
- Automatically paginated (fetches all endpoints)
- Cleaned for encoding issues
- De-duplicated and sorted
- Loaded into memory as a virtual Excel file for comparison

## Security Note

Never commit your `.env` file with real credentials to version control. The `.env.example` file is provided as a template only.


