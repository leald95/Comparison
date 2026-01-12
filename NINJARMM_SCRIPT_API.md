# NinjaRMM Script Execution API

This document describes how to use the new NinjaRMM script execution endpoints added to the application.

## Overview

The application now supports triggering scripts on specific NinjaRMM devices through the API. This is useful for automation tasks like remediation, updates, or data collection.

## Prerequisites

1. **NinjaRMM API Credentials** configured in `.env`:
   - OAuth (Client App API): `NINJARMM_CLIENT_ID` and `NINJARMM_CLIENT_SECRET`
   - OR Legacy API: `NINJARMM_API_KEY` and `NINJARMM_API_SECRET`

2. **API URL** configured in `.env`:
   ```
   NINJARMM_API_URL=https://api.ninjarmm.com
   ```
   (Use `https://eu-api.ninjarmm.com` for EU region)

## Endpoints

### 1. List Available Scripts

**Endpoint:** `GET /ninjarmm/scripts`

**Description:** Fetches all available scripts from your NinjaRMM account.

**Response:**
```json
{
  "success": true,
  "scripts": [
    {
      "id": 123,
      "name": "Windows Update Check",
      "description": "Checks for available Windows updates",
      "category": "Maintenance",
      "language": "PowerShell"
    },
    {
      "id": 456,
      "name": "Disk Cleanup",
      "description": "Performs disk cleanup operations",
      "category": "Maintenance",
      "language": "Batch"
    }
  ],
  "count": 2
}
```

**Example Usage:**
```javascript
fetch('/ninjarmm/scripts')
  .then(response => response.json())
  .then(data => {
    console.log('Available scripts:', data.scripts);
  });
```

### 2. Run Script on Device

**Endpoint:** `POST /ninjarmm/run-script`

**Description:** Triggers a script to run on a specific NinjaRMM device.

**Request Body:**
```json
{
  "device_id": 789,
  "script_id": 123,
  "parameters": {
    "param1": "value1",
    "param2": "value2"
  }
}
```

**Parameters:**
- `device_id` (required): The NinjaRMM device ID (obtained from `/ninjarmm/devices`)
- `script_id` (required): The script ID (obtained from `/ninjarmm/scripts`)
- `parameters` (optional): Object containing script parameters as key-value pairs

**Response (Success):**
```json
{
  "success": true,
  "message": "Script 123 triggered successfully on device 789"
}
```

**Response (Error):**
```json
{
  "error": "NinjaRMM API error: 404",
  "details": "Device not found"
}
```

**Example Usage:**
```javascript
fetch('/ninjarmm/run-script', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    device_id: 789,
    script_id: 123,
    parameters: {
      updateType: 'security',
      rebootAllowed: 'true'
    }
  })
})
  .then(response => response.json())
  .then(data => {
    if (data.success) {
      console.log('Script executed:', data.message);
    } else {
      console.error('Error:', data.error);
    }
  });
```

### 3. Get Devices with IDs

**Endpoint:** `GET /ninjarmm/devices?org_id={org_id}`

**Description:** Fetches all devices from NinjaRMM. Now includes device IDs needed for script execution.

**Response:**
```json
{
  "success": true,
  "devices": [
    {
      "name": "WORKSTATION-01",
      "id": 789,
      "lastContact": 1704844800
    },
    {
      "name": "SERVER-DC01",
      "id": 790,
      "lastContact": 1704931200
    }
  ],
  "count": 2
}
```

**Note:** The response now includes the `id` field which is required for script execution.

## Complete Workflow Example

Here's a complete example of running a script on a specific device:

```javascript
// Step 1: Get available scripts
const scriptsResponse = await fetch('/ninjarmm/scripts');
const { scripts } = await scriptsResponse.json();

// Find the script you want to run
const targetScript = scripts.find(s => s.name === 'Windows Update Check');
console.log('Found script:', targetScript.id);

// Step 2: Get devices
const devicesResponse = await fetch('/ninjarmm/devices?org_id=123');
const { devices } = await devicesResponse.json();

// Find the device you want to run the script on
const targetDevice = devices.find(d => d.name === 'WORKSTATION-01');
console.log('Found device:', targetDevice.id);

// Step 3: Run the script
const runResponse = await fetch('/ninjarmm/run-script', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    device_id: targetDevice.id,
    script_id: targetScript.id,
    parameters: {
      // Add any required script parameters here
      rebootAllowed: 'false'
    }
  })
});

const result = await runResponse.json();
if (result.success) {
  console.log('✅ Script triggered successfully!');
} else {
  console.error('❌ Error:', result.error);
}
```

## Error Handling

Common errors and their meanings:

| Error | Meaning | Solution |
|-------|---------|----------|
| `device_id is required` | Missing device_id in request | Include device_id in request body |
| `script_id is required` | Missing script_id in request | Include script_id in request body |
| `NinjaRMM API credentials not configured` | Missing .env credentials | Add credentials to .env file |
| `NinjaRMM OAuth error: 401` | Invalid credentials | Check client_id/client_secret in .env |
| `NinjaRMM API error: 404` | Device or script not found | Verify IDs are correct |
| `NinjaRMM API request timed out` | Network timeout | Check network connection and API URL |

## Security Notes

1. **Never expose API credentials** in frontend code
2. All NinjaRMM API calls are **proxied through the backend** for security
3. Script execution requires proper **NinjaRMM permissions** (ensure your API credentials have script execution rights)
4. **Online status verification**: The frontend verifies devices are currently online (last contact within 5 minutes) before allowing script execution
5. Consider implementing **rate limiting** for production use
6. Log all script executions for **audit trails**

## NinjaRMM API Reference

For more details on the NinjaRMM API:
- **API Documentation:** https://app.ninjarmm.com/apidocs-beta/
- **Script Run Endpoint:** `/v2/device/{deviceId}/script/run`
- **Scripts Query:** `/v2/queries/scripts`

## Active Directory Inventory (via Ninja)

This project can optionally use a NinjaRMM-run PowerShell script to query Active Directory for computers that have been active within the last N days, then send that list back to the webapp.

### Flow
1. Webapp triggers your Ninja script via `POST /ad/trigger`.
2. Ninja runs the script on the configured device (ideally a DC).
3. Script POSTs results to the webapp at `POST /ad/intake`.
4. Browser attaches the received snapshot into the session via `POST /ad/attach`, then you can run comparisons.

### Script parameters (expected)
Your Ninja PowerShell script should accept these parameters:
- `Days` (30/60/90)
- `ClientName` (display name only)
- `CallbackUrl` (e.g. `https://yourapp/ad/intake`)
- `Token` (shared secret)

### Script callback
The script should POST JSON like:
```json
{
  "client": "Client A",
  "days": 30,
  "workstations": [
    {"name": "PC-01", "lastLogonTimestamp": 1700000000}
  ]
}
```
With header:
- `X-AD-Intake-Token: <Token>`

### Token rotation
Configure tokens on the webapp:
- `AD_INTAKE_TOKEN_CURRENT`
- `AD_INTAKE_TOKEN_PREVIOUS` (kept temporarily during rotation)

## Version History

- **v2.1** (2026-01-09): Added script execution functionality
  - New endpoint: `/ninjarmm/scripts`
  - New endpoint: `/ninjarmm/run-script`
  - Modified: `/ninjarmm/devices` now returns device IDs
