# Excel Column Comparator

A modern web application to compare columns between two Excel files and visualize the differences. Now with SentinelOne API integration!

## Features

- **Drag & Drop Upload**: Easily upload Excel files (.xlsx, .xls)
- **SentinelOne Integration**: Fetch live endpoint names directly from your SentinelOne console
- **Sheet Selection**: Choose specific sheets from multi-sheet workbooks
- **Column Selection**: Pick which column to compare from each file
- **Column Preview**: See sample values before comparing to ensure correct selection
- **Comprehensive Comparison**:
  - Row-by-row comparison showing matches and differences
  - Values unique to File 1
  - Values unique to File 2
  - Common values between both files
- **Statistics Dashboard**: Quick overview of match rates and counts
- **Smart Encoding Fix**: Automatically fixes character encoding issues (e.g., â€™ → ')
- **Beautiful UI**: Modern, dark-themed interface with smooth animations

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

3. **Configure SentinelOne API** (optional):
   ```bash
   # Copy the example .env file
   cp .env.example .env
   
   # Edit .env and add your SentinelOne credentials:
   # SENTINELONE_API_URL=https://your-tenant.sentinelone.net
   # SENTINELONE_API_TOKEN=your_api_token_here
   ```

   To generate a SentinelOne API token:
   - Log into your SentinelOne console
   - Go to Settings → Users → [Your User] → Options
   - Select "API Token Operations" → "Generate API Token"

## Usage

1. **Start the application**:
   ```bash
   python app.py
   ```

2. **Open your browser** and navigate to:
   ```
   http://localhost:5000
   ```

3. **Upload your data**:
   - **Option A**: Drag and drop or click to browse for your Excel file
   - **Option B**: Click "Fetch from SentinelOne" to load live endpoint data
   
4. **Select columns to compare**:
   - Choose the sheet and column from each file
   - View the column preview to ensure you selected the right data

5. **Click "Compare Columns"** to see the results:
   - View row-by-row comparison
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


