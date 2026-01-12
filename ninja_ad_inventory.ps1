<#
NinjaRMM PowerShell Script: Active Directory Computer Inventory

Purpose:
- Query Active Directory for enabled computer accounts that have been active within the last N days
  using lastLogonTimestamp (replicated, approximate).
- POST results back to the webapp /ad/intake endpoint OR store in a NinjaOne custom field.

Expected parameters (as sent by the webapp):
- Days (30/60/90)
- ClientName (string label)
- CallbackUrl (e.g. https://yourapp/ad/intake)
- Nonce (one-time nonce)
- SigningKey (one-time signing key; used to compute X-AD-Intake-Signature)
- Token (legacy optional; sent as X-AD-Intake-Token)

NinjaOne-only mode parameters:
- CustomField (name of NinjaOne custom field to store results in)

Notes:
- Must run on a domain-joined host with RSAT AD module available (or a DC).
- Uses lastLogonTimestamp which can lag; best used for 30/60/90-day windows.
- The script supports multiple modes: webhook-only, CustomField-only, or both simultaneously.
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [int]$Days,

  [Parameter(Mandatory=$false)]
  [string]$ClientName,

  [Parameter(Mandatory=$false)]
  [string]$CallbackUrl,

  [Parameter(Mandatory=$false)]
  [string]$Nonce,

  [Parameter(Mandatory=$false)]
  [string]$SigningKey,

  [Parameter(Mandatory=$false)]
  [string]$Token,

  [Parameter(Mandatory=$false)]
  [string]$CustomField
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
  Write-Error $Message
  exit 1
}

if ($Days -notin @(30,60,90)) { Fail "Days must be one of: 30, 60, 90" }

# Validate webhook parameters only if CustomField is not provided
if ([string]::IsNullOrWhiteSpace($CustomField)) {
  if ([string]::IsNullOrWhiteSpace($ClientName)) { Fail "ClientName is required when CustomField is not provided" }
  if ([string]::IsNullOrWhiteSpace($CallbackUrl)) { Fail "CallbackUrl is required when CustomField is not provided" }
  if ([string]::IsNullOrWhiteSpace($Nonce)) { Fail "Nonce is required when CustomField is not provided" }
  if ([string]::IsNullOrWhiteSpace($SigningKey)) { Fail "SigningKey is required when CustomField is not provided" }
}

# If any webhook parameters are provided, ensure all required ones are present
$hasAnyWebhookParam = (-not [string]::IsNullOrWhiteSpace($ClientName)) -or 
                      (-not [string]::IsNullOrWhiteSpace($CallbackUrl)) -or 
                      (-not [string]::IsNullOrWhiteSpace($Nonce)) -or 
                      (-not [string]::IsNullOrWhiteSpace($SigningKey))

if ($hasAnyWebhookParam) {
  if ([string]::IsNullOrWhiteSpace($ClientName)) { Fail "ClientName is required when using webhook parameters" }
  if ([string]::IsNullOrWhiteSpace($CallbackUrl)) { Fail "CallbackUrl is required when using webhook parameters" }
  if ([string]::IsNullOrWhiteSpace($Nonce)) { Fail "Nonce is required when using webhook parameters" }
  if ([string]::IsNullOrWhiteSpace($SigningKey)) { Fail "SigningKey is required when using webhook parameters" }
}

# Validate CallbackUrl if provided
if (-not [string]::IsNullOrWhiteSpace($CallbackUrl)) {
  try {
    # Ensure URL is sane
    $u = [Uri]$CallbackUrl
    if ($u.Scheme -notin @('http','https')) { Fail "CallbackUrl must be http/https" }
  } catch {
    Fail "CallbackUrl is not a valid URI"
  }
}

# Try to load AD module
try {
  Import-Module ActiveDirectory -ErrorAction Stop
} catch {
  Fail "ActiveDirectory module not available. Install RSAT (or run on a DC)."
}

# Determine domain root DN dynamically (top level of the domain)
$rootDn = $null
try {
  $rootDn = (Get-ADDomain).DistinguishedName
} catch {
  try {
    $rootDn = ([ADSI]"LDAP://RootDSE").defaultNamingContext
  } catch {
    Fail "Failed to determine domain root DN"
  }
}

if ([string]::IsNullOrWhiteSpace($rootDn)) { Fail "Domain root DN was empty" }

$cutoff = (Get-Date).AddDays(-$Days)

# Enabled computers only:
# userAccountControl disable bit (2) NOT set
$ldapEnabledComputers = '(&(objectCategory=computer)(objectClass=computer)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))'

# Fetch computers (page through large domains)
$computers = @()
try {
  $computers = Get-ADComputer -LDAPFilter $ldapEnabledComputers -SearchBase $rootDn -SearchScope Subtree -Properties lastLogonTimestamp -ResultPageSize 2000 -ResultSetSize $null
} catch {
  Fail "Get-ADComputer failed: $($_.Exception.Message)"
}

$results = New-Object System.Collections.Generic.List[object]

foreach ($c in $computers) {
  $name = $c.Name
  if ([string]::IsNullOrWhiteSpace($name)) { continue }

  $llt = $c.lastLogonTimestamp
  if (-not $llt) { continue }

  try {
    $dt = [DateTime]::FromFileTimeUtc([Int64]$llt)
  } catch {
    continue
  }

  if ($dt -ge $cutoff) {
    $results.Add([pscustomobject]@{
      name = $name
      lastLogonTimestamp = [Int64]$llt
      lastSeenUtc = $dt.ToString('o')
    })
  }
}

$payload = [pscustomobject]@{
  client = if ([string]::IsNullOrWhiteSpace($ClientName)) { $null } else { $ClientName }
  days = $Days
  workstations = $results
}

$json = $payload | ConvertTo-Json -Depth 6

# If CustomField is provided, set the NinjaOne custom field
if (-not [string]::IsNullOrWhiteSpace($CustomField)) {
  try {
    Ninja-Property-Set $CustomField $json
    Write-Host "AD inventory stored in NinjaOne custom field '$CustomField': $($results.Count) computers (within last $Days days)"
  } catch {
    Fail "Failed to set NinjaOne custom field '$CustomField': $($_.Exception.Message)"
  }
}

# If CallbackUrl, SigningKey, and Nonce are provided, POST to the webhook
if (-not [string]::IsNullOrWhiteSpace($CallbackUrl) -and -not [string]::IsNullOrWhiteSpace($SigningKey) -and -not [string]::IsNullOrWhiteSpace($Nonce)) {
  $hmac = New-Object System.Security.Cryptography.HMACSHA256 ([Text.Encoding]::UTF8.GetBytes($SigningKey))
  $sigBytes = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($json))
  $signature = ($sigBytes | ForEach-Object { $_.ToString('x2') }) -join ''

  $headers = @{ 
    'X-AD-Intake-Nonce' = $Nonce
    'X-AD-Intake-Signature' = $signature
  }

  # Legacy optional header
  if (-not [string]::IsNullOrWhiteSpace($Token)) {
    $headers['X-AD-Intake-Token'] = $Token
  }

  try {
    # If your environment has strict TLS defaults, uncomment the next line:
    # [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    Invoke-RestMethod -Method POST -Uri $CallbackUrl -Headers $headers -ContentType 'application/json' -Body $json -TimeoutSec 60 | Out-Null
    Write-Host "AD inventory sent successfully: $($results.Count) computers (within last $Days days)"
  } catch {
    Fail "Failed to POST to CallbackUrl: $($_.Exception.Message)"
  }
}
