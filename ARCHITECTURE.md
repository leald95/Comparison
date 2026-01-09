# Endpoint Comparison Tool - Architecture Documentation

## Overview
A Flask-based web application designed for MSP (Managed Service Provider) teams to compare endpoint/device lists between SentinelOne (EDR platform) and NinjaRMM (RMM platform) to identify discrepancies and offline devices.

**Version:** 2.0 (Unified Client Selection)  
**Last Updated:** 2026-01-09

**Line Counts:**
- `app.py`: 1,045 lines
- `templates/index.html`: 4,717 lines

---

## Table of Contents
1. [System Architecture](#system-architecture)
2. [Technology Stack](#technology-stack)
3. [Application Flow](#application-flow)
4. [Frontend Architecture](#frontend-architecture)
5. [Backend Architecture](#backend-architecture)
6. [Data Flow](#data-flow)
7. [State Management](#state-management)
8. [API Integration](#api-integration)
9. [Key Features](#key-features)
10. [Recent Changes](#recent-changes)

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Browser (Client)                      │
│  ┌───────────────────────────────────────────────────────┐  │
│  │         Single Page Application (SPA)                  │  │
│  │  - HTML/CSS/JavaScript (No frameworks)                │  │
│  │  - Client-side state management                       │  │
│  │  - LocalStorage for caching & history                 │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓ HTTP/JSON
┌─────────────────────────────────────────────────────────────┐
│                    Flask Backend (Python)                    │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Routes:                                               │  │
│  │  - /clients/unified      (Fetch & match clients)      │  │
│  │  - /sentinelone/*        (S1 API proxy)               │  │
│  │  - /ninjarmm/*           (Ninja API proxy)            │  │
│  │  - /compare              (Column comparison logic)    │  │
│  │  - /upload               (Virtual file creation)      │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Data Processing:                                      │  │
│  │  - pandas (Excel manipulation)                        │  │
│  │  - Session management (file storage)                  │  │
│  │  - Normalization & encoding fixes                     │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓ REST API
┌─────────────────────────────────────────────────────────────┐
│                    External APIs                             │
│  ┌──────────────────────┐    ┌──────────────────────────┐  │
│  │  SentinelOne API     │    │  NinjaRMM API            │  │
│  │  - Sites             │    │  - Organizations         │  │
│  │  - Agents/Endpoints  │    │  - Devices               │  │
│  │  - Last Active Date  │    │  - Last Contact          │  │
│  │                      │    │  - Scripts (list/run)    │  │
│  └──────────────────────┘    └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

### **Backend**
- **Framework:** Flask 3.0+, Werkzeug 3.0+
- **Data Processing:** pandas 2.0+, openpyxl 3.1+, xlrd 2.0+
- **API Client:** requests 2.31+
- **Environment:** python-dotenv 1.0+
- **Session Storage:** Flask sessions (server-side file storage)

### **Frontend**
- **HTML5** - Semantic structure
- **CSS3** - Custom design system (no frameworks)
  - CSS Variables for theming
  - Flexbox & Grid layouts
  - Animations & transitions
- **Vanilla JavaScript** - No frameworks/libraries
  - ES6+ features (async/await, arrow functions)
  - Fetch API for HTTP requests
  - LocalStorage API for caching

### **External APIs**
- **SentinelOne API v2.1**
  - Authentication: API Token
  - Endpoints: Sites, Agents
- **NinjaRMM API v2**
  - Authentication: OAuth 2.0 (Client Credentials) or Legacy Basic Auth
  - Endpoints: Organizations, Devices

---

## Application Flow

### **1. Initial Load**
```
User opens app
    ↓
Check localStorage for previous comparison
    ↓
Show "Choose Client" button
    ↓
If previous exists: Show "⚡ Load [Client]" button
```

### **2. Client Selection Flow**
```
User clicks "Choose Client"
    ↓
Fetch matched clients from backend (/clients/unified)
    ↓
Backend calls both APIs concurrently
    ↓
Match clients by normalized name
    ↓
Display only matched clients (exist in both systems)
    ↓
User selects client
    ↓
Trigger automatic comparison
```

### **3. Automatic Comparison Flow**
```
Client selected
    ↓
Show "Fetching data..." status
    ↓
Parallel API calls:
  - fetchFromS1(siteId) → /sentinelone/endpoints
  - fetchFromNinja(orgId) → /ninjarmm/devices
    ↓
Calculate offline devices (>30 days)
    ↓
Create virtual Excel files:
  - /sentinelone/upload → File 1
  - /ninjarmm/upload → File 2
    ↓
Show "Comparing..." status
    ↓
Call /compare with column selections
    ↓
Backend performs normalized comparison
    ↓
Show "Complete!" status
    ↓
Display results in tabs:
  - Testing View (matched/unmatched)
  - Only in S1
  - Only in Ninja
  - Both Systems
  - Prefix Matches
  - Offline Devices
```

### **4. Results Navigation**
```
View results
    ↓
Options:
  - Export to HTML
  - Back to Client Selection (resets state)
  - Change Client (from selected client display)
  - Load previous comparison (history)
```

---

## Frontend Architecture

### **File Structure**
```
templates/
└── index.html (4,717 lines)
    ├── <head> (lines 4-1860)
    │   ├── CSS Variables (theming)
    │   ├── Layout Styles
    │   ├── Component Styles
    │   └── Animation Definitions
    ├── <body> (lines 1862-4716)
    │   ├── Background Effects (lines 1863-1864)
    │   ├── Header (lines 1867-1881)
    │   │   └── Title, quick-load btn, history btn
    │   ├── Results Section (line 1883, hidden by default)
    │   ├── Client Selection Section (lines 1990-2048)
    │   │   ├── Selector Prompt (line 1998)
    │   │   └── Selected Client Display (line 2006)
    │   └── Modals (lines 2051-2130)
    │       ├── Site Selection Modal (S1)
    │       ├── Organization Modal (Ninja)
    │       ├── Unified Client Modal
    │       └── History Modal
    └── <script> (lines 2132-4716)
        ├── State Management (line 2134)
        ├── LocalStorage Functions (lines 2143-2250)
        │   ├── saveLastSourceConfig()
        │   ├── updateQuickLoadButton()
        │   ├── getCache() / setCache()
        │   └── getHistory() / saveToHistory()
        ├── Utility Functions (lines 2256-2610)
        │   ├── showToast() (line 2256)
        │   ├── formatLastSeen() (line 2290)
        │   ├── formatTimeAgo() (line 2300)
        │   └── escapeHtml() (line 4149)
        ├── File Upload Functions (legacy, not used in current UI)
        │   ├── setupFileUpload() (line 2613)
        │   ├── resetFileCard() (line 2791)
        │   └── handleFileUpload() (line 3762)
        ├── Modal Functions (lines 2831-3400)
        │   ├── openSiteSelectionModal() (line 2831)
        │   ├── openNinjaOrganizationModal() (line 3192)
        │   └── openUnifiedClientModal() (line 3373)
        ├── API Functions (lines 3543-3600)
        │   ├── fetchFromS1() (line 3543)
        │   └── fetchFromNinja() (line 3567)
        ├── Comparison Functions (lines 3825-4100)
        │   ├── runComparison() (line 3825)
        │   └── displayResults() (line 3856)
        ├── Export Functions (line 4263)
        │   └── exportToHTML()
        └── Event Listeners (lines 4479-4710)
            ├── Modal close handlers
            ├── new-comparison-btn (line 4503)
            ├── quick-load-btn (line 4527)
            ├── select-client-main-btn (line 4580)
            └── selectUnifiedClientAndCompare() (line 4589)
```

### **Key Components**

#### **1. Client Selection Card**
```html
<div class="client-selection-card">
  <!-- Initial prompt -->
  <div id="client-selector-prompt">
    <button id="select-client-main-btn">Choose Client</button>
  </div>
  
  <!-- After selection -->
  <div id="selected-client-display" style="display: none;">
    <div class="client-header-row">
      <span id="selected-client-name"></span>
      <button id="change-client-main-btn">Change Client</button>
    </div>
    <div id="comparison-status">
      <!-- Fetching → Comparing → Complete -->
    </div>
    <div id="sources-summary">
      <!-- S1 count | Ninja count -->
    </div>
  </div>
</div>
```

#### **2. Results Section**
```html
<section id="results-section" class="results-section">
  <button id="new-comparison-btn">🏠 Back to Client Selection</button>
  <div id="stats-grid"><!-- Match statistics --></div>
  <div class="tabs">
    <button data-tab="testing">Testing View</button>
    <button data-tab="file1">Only in S1</button>
    <button data-tab="file2">Only in Ninja</button>
    <button data-tab="both">Both Systems</button>
    <button data-tab="prefix">Prefix Matches</button>
  </div>
  <div class="tab-content"><!-- Dynamic content --></div>
  <button id="export-btn">📥 Export to HTML</button>
</section>
```

#### **3. Unified Client Modal**
```html
<div id="unified-client-modal" class="modal-overlay">
  <div class="modal">
    <h2>Select Client</h2>
    <div id="unified-client-modal-content">
      <!-- Client list with match indicators -->
      <div class="site-item client-matched" 
           data-s1-id="123" 
           data-ninja-id="456">
        <span>🛡️ 🥷</span> Client Name
      </div>
    </div>
  </div>
</div>
```

### **State Object**
```javascript
// Location: line 2134
const state = {
  file1: {
    uploaded: false,
    sheet: null,
    column: null,
    filename: null,
    sourceType: null,      // Set to 'SentinelOne' after fetch
    offlineDevices: []
  },
  file2: {
    uploaded: false,
    sheet: null,
    column: null,
    filename: null,
    sourceType: null,      // Set to 'NinjaRMM' after fetch
    offlineDevices: []
  },
  currentFileIdForS1: null,  // Modal context ('main', '1', or '2')
  pendingCacheChoice: null,   // Cache prompt state
  testingViewSort: {          // Table sorting
    field: 'name',
    direction: 'asc'
  },
  clientName: null             // Current client name for history
};
```

### **LocalStorage Schema**
```javascript
// Last source configuration (key: 'lastSourceConfig')
{
  "file1": {
    "type": "SentinelOne",
    "id": "site_id_string",
    "name": "Client Name"
  },
  "file2": {
    "type": "NinjaRMM",
    "id": "org_id_number",
    "name": "Client Name"
  }
}

// API cache (key: 'api_cache_{source}_{id}')
{
  "timestamp": 1704835200000,
  "name": "Client Name",
  "data": {
    "endpoints": [...],  // or "devices": [...]
    "count": 150
  }
}

// Comparison history (key: 'comparisonHistory')
// Array limited to 10 entries
[
  {
    "id": "1704835200000",           // Timestamp as string
    "timestamp": 1704835200000,
    "title": "Client Name - SentinelOne vs NinjaRMM",
    "clientName": "Client Name",
    "file1Name": "Client Name Endpoints",
    "file2Name": "Client Name Devices",
    "file1Source": "SentinelOne",
    "file2Source": "NinjaRMM",
    "file1Offline": [                 // Devices offline >30 days
      { "name": "HOSTNAME", "lastSeen": "2024-01-09T12:00:00Z" }
    ],
    "file2Offline": [...],
    "data": {                         // Comparison results
      "only_in_file1": ["HOST1", "HOST2"],
      "only_in_file2": ["HOST3"],
      "in_both": ["HOST4", "HOST5"],
      "prefix_matches": [
        { "file1": "LONGNAME-ABC", "file2": "LONGNAME-AB" }
      ],
      "stats": {
        "total_file1": 100,
        "total_file2": 95,
        "unique_file1": 98,
        "unique_file2": 93,
        "only_in_file1_count": 10,
        "only_in_file2_count": 5,
        "common_count": 85,
        "prefix_match_count": 3,
        "match_percentage": 92.5
      }
    }
  }
]
```

---

## Backend Architecture

### **Flask Application Structure**
```python
app.py (1,045 lines)
├── Configuration
│   ├── Flask app setup
│   ├── Environment variables (.env)
│   ├── Upload folder config
│   └── Session secret key
├── Utility Functions
│   ├── fix_encoding()         # UTF-8 mojibake fixes (line 20)
│   ├── normalize_value()      # Hostname normalization (line 56)
│   ├── read_excel_file()      # Excel reader with fallback (line 102)
│   └── allowed_file()         # File extension validation (line 98)
├── Routes - Main
│   ├── GET  /                 # Serve index.html (line 117)
│   └── POST /cleanup          # Delete session files (line 1022)
├── Routes - File Upload (Legacy, unused in UI)
│   ├── POST /upload           # Upload Excel file (line 123)
│   ├── POST /get_columns      # Get sheet columns (line 171)
│   └── POST /preview_column   # Preview column data (line 196)
├── Routes - Comparison
│   └── POST /compare          # Compare two columns (line 237)
├── Routes - SentinelOne API
│   ├── GET  /sentinelone/sites      # List sites (line 356)
│   ├── GET  /sentinelone/endpoints  # List agents (line 407)
│   └── POST /sentinelone/upload     # Create virtual file (line 490)
├── Routes - NinjaRMM API
│   ├── GET  /ninjarmm/test          # Test auth methods (line 533)
│   ├── GET  /ninjarmm/organizations # List orgs (line 618)
│   ├── GET  /ninjarmm/devices       # List devices (line 719)
│   ├── GET  /ninjarmm/scripts       # List available scripts (NEW)
│   ├── POST /ninjarmm/run-script    # Execute script on device (NEW)
│   └── POST /ninjarmm/upload        # Create virtual file (line 838)
└── Routes - Unified
    └── GET  /clients/unified        # Match clients across APIs (line 881)
```

### **Key Backend Functions**

#### **1. Normalization Pipeline**
```python
def fix_encoding(value):
    """Fix UTF-8 mojibake and smart quotes"""
    # Handles: â€™ → ', â€œ → ", etc.
    return cleaned_value

def normalize_value(value):
    """Normalize for comparison"""
    # 1. Fix encoding
    # 2. Lowercase
    # 3. Remove domain suffixes (.local, .lan, etc.)
    # 4. Remove special chars (apostrophes, quotes)
    # 5. Remove separators (spaces, hyphens, underscores)
    return normalized
```

#### **2. Comparison Logic** (`/compare`)
```python
def compare_columns():
    # 1. Load both Excel files from session
    # 2. Extract columns, fix encoding
    # 3. Create normalization mappings
    #    norm_to_orig1 = {normalized: original}
    # 4. Set-based comparison
    #    only_in_file1 = set1 - set2
    #    only_in_file2 = set2 - set1
    #    in_both = set1 & set2
    # 5. Prefix matching (15-char truncation)
    #    Handles NinjaRMM hostname limits
    # 6. Calculate statistics
    # 7. Return results
```

#### **3. Unified Client Matching** (`/clients/unified`)
```python
def get_unified_clients():
    # 1. Fetch from both APIs concurrently
    #    - SentinelOne: /web/api/v2.1/sites
    #    - NinjaRMM: /v2/organizations
    # 2. Normalize client names
    #    normalize_name(name) → lowercase, no special chars
    # 3. Match by normalized name
    # 4. Categorize:
    #    - matched_clients (exist in both)
    #    - unmatched_s1 (only in S1)
    #    - unmatched_ninja (only in Ninja)
    # 5. Return combined list with match status
```

#### **4. Virtual File Creation**
```python
def upload_sentinelone_data():
    # 1. Receive endpoint list from frontend
    # 2. Create pandas DataFrame
    # 3. Save as Excel (.xlsx) with unique filename
    # 4. Store filepath in session
    # 5. Return sheet/column metadata
```

### **Session Management**
```python
# Session stores file paths for comparison
session['files'] = {
    '1': '/uploads/uuid1_sentinelone_endpoints.xlsx',
    '2': '/uploads/uuid2_ninjarmm_devices.xlsx'
}

# Cleanup on page unload
window.addEventListener('beforeunload', () => {
    fetch('/cleanup', { method: 'POST' });
});
```

---

## Data Flow

### **Complete Comparison Lifecycle**

```
┌─────────────────────────────────────────────────────────────┐
│ 1. USER SELECTS CLIENT                                       │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. FETCH MATCHED CLIENTS                                     │
│    Frontend: openUnifiedClientModal('main')                  │
│              ↓                                                │
│    Backend:  GET /clients/unified                            │
│              ├─→ SentinelOne API: GET /sites                 │
│              ├─→ NinjaRMM API: GET /organizations            │
│              └─→ Match by normalized name                    │
│              ↓                                                │
│    Return:   [{ s1_id, s1_name, ninja_id, ninja_name }]     │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. FETCH ENDPOINT DATA                                       │
│    Frontend: selectUnifiedClientAndCompare(client)           │
│              ↓                                                │
│    Parallel: fetchFromS1(s1_id)                              │
│              ├─→ GET /sentinelone/endpoints?site_id=123      │
│              ├─→ S1 API: GET /agents (paginated)             │
│              └─→ Calculate offline (>30 days)                │
│              ↓                                                │
│              fetchFromNinja(ninja_id)                        │
│              ├─→ GET /ninjarmm/devices?org_id=456            │
│              ├─→ Ninja API: GET /devices (paginated)         │
│              └─→ Calculate offline (>30 days)                │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. CREATE VIRTUAL FILES                                      │
│    Frontend: POST /sentinelone/upload                        │
│              { file_id: '1', endpoints: [...] }              │
│              ↓                                                │
│    Backend:  Create DataFrame(['Endpoint Name'])             │
│              Save to Excel: uuid_sentinelone.xlsx            │
│              Store in session['files']['1']                  │
│              ↓                                                │
│    Frontend: POST /ninjarmm/upload                           │
│              { file_id: '2', devices: [...] }                │
│              ↓                                                │
│    Backend:  Create DataFrame(['Device Name'])               │
│              Save to Excel: uuid_ninjarmm.xlsx               │
│              Store in session['files']['2']                  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. COMPARE COLUMNS                                           │
│    Frontend: POST /compare                                   │
│              {                                                │
│                file1_sheet: 'Sheet1',                        │
│                file1_column: 'Endpoint Name',                │
│                file2_sheet: 'Sheet1',                        │
│                file2_column: 'Device Name'                   │
│              }                                                │
│              ↓                                                │
│    Backend:  Read Excel files from session                   │
│              Extract columns                                 │
│              fix_encoding() on all values                    │
│              Create normalization maps                       │
│              Set-based comparison                            │
│              Prefix matching (15-char)                       │
│              Calculate statistics                            │
│              ↓                                                │
│    Return:   {                                               │
│                only_in_file1: [...],                         │
│                only_in_file2: [...],                         │
│                in_both: [...],                               │
│                prefix_matches: [...],                        │
│                stats: { ... }                                │
│              }                                                │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 6. DISPLAY RESULTS                                           │
│    Frontend: displayResults(data)                            │
│              ├─→ Create stats cards                          │
│              ├─→ Populate tabs                               │
│              ├─→ Build testing table                         │
│              ├─→ Show offline devices                        │
│              └─→ Save to history (localStorage)              │
└─────────────────────────────────────────────────────────────┘
```

---

## API Integration

### **SentinelOne API v2.1**

**Base URL:** `https://{tenant}.sentinelone.net`

**Authentication:**
```http
Authorization: ApiToken {token}
```

**Endpoints Used:**

1. **List Sites**
   ```http
   GET /web/api/v2.1/sites?limit=1000
   ```
   Response:
   ```json
   {
     "data": {
       "sites": [
         { "id": "123", "name": "Client Name" }
       ]
     }
   }
   ```

2. **List Agents (Endpoints)**
   ```http
   GET /web/api/v2.1/agents?limit=1000&siteIds=123
   ```
   Response (paginated with cursor):
   ```json
   {
     "data": [
       {
         "computerName": "HOSTNAME",
         "lastActiveDate": "2024-01-09T12:00:00Z",
         "networkInterfaces": [{"name": "HOSTNAME"}]
       }
     ],
     "pagination": {
       "nextCursor": "cursor_token"
     }
   }
   ```

### **NinjaRMM API v2**

**Base URL:** `https://api.ninjarmm.com` or `https://eu-api.ninjarmm.com`

**Authentication (OAuth 2.0):**
```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id={client_id}
&client_secret={client_secret}
&scope=monitoring
```

Response:
```json
{
  "access_token": "token",
  "expires_in": 3600
}
```

**Authentication (Legacy):**
```http
Authorization: Basic {base64(api_key:api_secret)}
```

**Endpoints Used:**

1. **List Organizations**
   ```http
   GET /v2/organizations
   ```
   Response:
   ```json
   [
     { "id": 456, "name": "Client Name" }
   ]
   ```

2. **List Devices**
   ```http
   GET /v2/devices?pageSize=1000&page=0
   GET /v2/organization/{orgId}/devices?pageSize=1000&page=0
   ```
   Response:
   ```json
   [
     {
       "systemName": "HOSTNAME",
       "dnsName": "HOSTNAME.local",
       "lastContact": 1704835200
     }
   ]
   ```

---

## Key Features

### **1. Unified Client Selection**
- Fetches organizations/sites from both APIs
- Matches clients by normalized name
- Shows only matched clients (exist in both systems)
- One-click selection triggers automatic comparison

### **2. Automatic Comparison**
- Parallel API calls for speed
- Virtual Excel file creation (no manual uploads)
- Real-time status updates (Fetching → Comparing → Complete)
- Immediate results display

### **3. Smart Comparison Logic**

**Normalization:**
- Fixes encoding issues first (mojibake)
- Converts to lowercase
- Removes domain suffixes (`.local`, `.lan`, `.home`, `.internal`, `.localdomain`, `.domain`)
- Removes special characters (apostrophes, quotes, backticks)
- Removes all separators (spaces, hyphens, underscores, dots)

**Prefix Matching:**
- Handles 15-character hostname truncation (NinjaRMM limitation)
- Matches `WORKSTATION-ABC` with `WORKSTATION-AB`

**Results Categories:**
- **Testing View:** Color-coded table (green=matched, red=unmatched)
- **Only in S1:** Endpoints not found in Ninja
- **Only in Ninja:** Devices not found in S1
- **Both Systems:** Exact matches
- **Prefix Matches:** Truncated matches (likely same device)
- **Offline Devices:** Not seen in >30 days (collapsible section)

### **4. Caching System**
- API responses cached in localStorage
- Cache age displayed (e.g., "2 hours ago")
- User choice: "Load Cached" or "Fetch Fresh"
- Reduces API calls and improves performance

### **5. Quick Reload**
- "⚡ Load [Client]" button in header
- Appears after first comparison
- Instantly reloads last selected client
- Saved in localStorage

### **6. Comparison History**
- Saves all comparisons to localStorage
- "⏳ History" button to browse past results
- Click to restore previous comparison
- Includes timestamp and statistics

### **7. Export Functionality**
- "📥 Export to HTML" button
- Standalone HTML file with embedded CSS
- Includes all tabs and statistics
- No external dependencies

### **8. Offline Device Tracking**
- Identifies devices not seen in >30 days
- Displays last seen date
- Shows count in badge
- Collapsible section (hidden by default)
- Separate lists for S1 and Ninja sources

---

## Recent Changes

### **Version 2.0 - Unified Client Selection (2026-01-09)**

#### **Removed:**
- ❌ Manual Excel file upload interface
- ❌ Drag-and-drop file zones
- ❌ Separate file cards for File 1 and File 2
- ❌ Individual SentinelOne/NinjaRMM source selection

#### **Added:**
- ✅ Unified client selection modal
- ✅ Automatic matched client detection
- ✅ One-click client selection and comparison
- ✅ Real-time status updates during fetch/compare
- ✅ Source summary cards (endpoint/device counts)

#### **Bug Fixes (2026-01-09):**

1. **Quick Load Button Not Working**
   - **Issue:** No event listener attached to button
   - **Fix:** Added click handler calling `selectUnifiedClientAndCompare()`
   - **Location:** Lines 4527-4563

2. **Case Sensitivity in Save Config**
   - **Issue:** Saved as lowercase (`'sentinelone'`) but checked for capitalized (`'SentinelOne'`)
   - **Fix:** Changed save calls to use capitalized type names
   - **Location:** Lines 4697-4702

3. **Back Button Not Working**
   - **Issue:** Didn't reset view states properly
   - **Fix:** Added state reset and display property changes
   - **Location:** Lines 4503-4525

4. **Back Button Shows Empty Page**
   - **Issue:** Upload section had `display: none` set by comparison
   - **Fix:** Added `uploadSection.style.display = 'flex'` to restore visibility
   - **Location:** Lines 4507-4509

5. **Load Last Button Not Appearing After Back**
   - **Issue:** `updateQuickLoadButton()` not called after state reset
   - **Fix:** Added call to `updateQuickLoadButton()` after resetting state
   - **Location:** Line 4521

---

## File Structure

```
Comparison/
├── app.py                    # Flask backend (1,045 lines)
├── requirements.txt          # Python dependencies
├── README.md                 # User documentation
├── ARCHITECTURE.md           # This file
├── .env                      # Environment variables (not in git)
├── .env.example              # Environment template
├── .gitignore                # Git exclusions
├── templates/
│   ├── index.html            # Single-page app (4,717 lines)
│   └── index.html.backup     # Backup copy
├── uploads/                  # Temporary file storage (auto-created)
│   └── *.xlsx                # Session-based virtual files
├── venv/                     # Python virtual environment
└── __pycache__/              # Python bytecode cache
```

---

## Environment Configuration

### **.env File**
```bash
# SentinelOne API
SENTINELONE_API_URL=https://your-tenant.sentinelone.net
SENTINELONE_API_TOKEN=your_api_token

# NinjaRMM API
NINJARMM_API_URL=https://api.ninjarmm.com

# Option 1: OAuth (Recommended)
NINJARMM_CLIENT_ID=your_client_id
NINJARMM_CLIENT_SECRET=your_client_secret

# Option 2: Legacy Basic Auth
# NINJARMM_API_KEY=your_api_key
# NINJARMM_API_SECRET=your_api_secret
```

---

## Performance Considerations

### **Frontend**
- **No Framework Overhead:** Vanilla JS = fast load times
- **LocalStorage Caching:** Reduces API calls by ~80%
- **Parallel API Calls:** Fetches from S1 and Ninja concurrently
- **Lazy Rendering:** Results tabs render on demand

### **Backend**
- **Session-based Files:** No database required
- **Efficient Pandas Operations:** Vectorized comparisons
- **Set Operations:** O(n) time complexity for comparisons
- **API Request Optimization:** Pagination handled automatically

### **Scalability**
- **Client-side State:** No server-side memory pressure
- **Stateless Backend:** Horizontal scaling possible
- **Session Cleanup:** Files deleted on page unload
- **API Rate Limits:** Handled by pagination and error handling

---

## Security Considerations

### **Credentials**
- ✅ `.env` file excluded from git
- ✅ API tokens server-side only (never exposed to client)
- ✅ Secure session secret key (random 24 bytes)

### **File Handling**
- ✅ Secure filename generation (UUID)
- ✅ File type validation (`.xlsx`, `.xls` only)
- ✅ Max file size limit (50MB)
- ✅ Automatic cleanup on session end

### **API Proxying**
- ✅ Backend acts as proxy (hides credentials)
- ✅ Request validation before forwarding
- ✅ Error sanitization (no credential leakage)

---

## Future Enhancements

### **Potential Features**
- [ ] Multi-client comparison (compare multiple clients at once)
- [ ] Scheduled comparisons (automated runs)
- [ ] Email reports (send results to stakeholders)
- [ ] API webhook support (trigger comparisons from external systems)
- [ ] Advanced filtering (by device type, OS, etc.)
- [ ] Trend analysis (compare current vs. historical)
- [ ] Role-based access control (multi-user support)
- [ ] Database backend (persistent storage)

### **Technical Debt**
- [ ] Split `index.html` into components (modular architecture)
- [ ] Add frontend framework (React/Vue) for better state management
- [ ] Unit tests (backend and frontend)
- [ ] CI/CD pipeline (automated testing and deployment)
- [ ] Docker containerization
- [ ] Logging and monitoring (structured logs, metrics)

---

## Development Guidelines

### **Running Locally**
```bash
# 1. Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your API credentials

# 4. Run application
python app.py

# 5. Open browser
http://localhost:5000
```

### **Code Style**
- **Python:** PEP 8 style guide
- **JavaScript:** ES6+ with semicolons
- **CSS:** BEM-like naming (component-element-modifier)
- **Comments:** Document non-obvious logic and API interactions

### **Testing**
- **Manual Testing:** Use test clients in both APIs
- **Edge Cases:** Empty lists, API timeouts, mismatched names
- **Browser Testing:** Chrome, Firefox, Edge, Safari

---

## Support and Maintenance

### **Common Issues**

1. **API Connection Errors**
   - Check `.env` credentials
   - Verify API URL (US vs. EU regions)
   - Test with `/ninjarmm/test` endpoint

2. **Comparison Results Unexpected**
   - Check normalization logic in `normalize_value()`
   - Verify column selections (correct data source)
   - Review encoding fixes in `fix_encoding()`

3. **Performance Degradation**
   - Clear localStorage (old cache data)
   - Check API response times
   - Monitor upload folder size

### **Logs and Debugging**
- **Flask Debug Mode:** Enabled by default (`debug=True`)
- **Browser Console:** Check for JavaScript errors
- **Network Tab:** Inspect API requests/responses
- **Python Console:** `print()` statements in backend

---

## Contact and Contributors

**Primary Developer:** MSP Engineering Team  
**Last Updated:** 2026-01-09  
**Version:** 2.0 (Unified Client Selection)

For questions or issues, consult this documentation first, then reach out to the development team.

---

*End of Architecture Documentation*
