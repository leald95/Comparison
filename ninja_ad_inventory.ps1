<#
NinjaRMM PowerShell Script: Active Directory Computer Inventory

Purpose:
- Query Active Directory for enabled computer accounts that have been active within the last N days
  using lastLogonTimestamp (replicated, approximate).
- POST results back to the webapp /ad/intake endpoint.

Expected parameters (as sent by the webapp):
- Days (30/60/90)
- ClientName (string label)
- CallbackUrl (e.g. https://yourapp/ad/intake)
- Token (shared secret; sent as X-AD-Intake-Token)

Notes:
- Must run on a domain-joined host with RSAT AD module available (or a DC).
- Uses lastLogonTimestamp which can lag; best used for 30/60/90-day windows.
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [int]$Days,

  [Parameter(Mandatory=$true)]
  [string]$ClientName,

  [Parameter(Mandatory=$true)]
  [string]$CallbackUrl,

  [Parameter(Mandatory=$true)]
  [string]$Token
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
  Write-Error $Message
  exit 1
}

if ($Days -notin @(30,60,90)) { Fail "Days must be one of: 30, 60, 90" }
if ([string]::IsNullOrWhiteSpace($ClientName)) { Fail "ClientName is required" }
if ([string]::IsNullOrWhiteSpace($CallbackUrl)) { Fail "CallbackUrl is required" }
if ([string]::IsNullOrWhiteSpace($Token)) { Fail "Token is required" }

try {
  # Ensure URL is sane
  $u = [Uri]$CallbackUrl
  if ($u.Scheme -notin @('http','https')) { Fail "CallbackUrl must be http/https" }
} catch {
  Fail "CallbackUrl is not a valid URI"
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
  client = $ClientName
  days = $Days
  workstations = $results
}

$json = $payload | ConvertTo-Json -Depth 6

$headers = @{ 'X-AD-Intake-Token' = $Token }

try {
  # If your environment has strict TLS defaults, uncomment the next line:
  # [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

  Invoke-RestMethod -Method POST -Uri $CallbackUrl -Headers $headers -ContentType 'application/json' -Body $json -TimeoutSec 60 | Out-Null
  Write-Host "AD inventory sent successfully: $($results.Count) computers (>= $Days days)"
} catch {
  Fail "Failed to POST to CallbackUrl: $($_.Exception.Message)"
}
