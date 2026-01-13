<#
NinjaRMM PowerShell Script: Active Directory Computer Inventory

Purpose:
- Query Active Directory for enabled computer accounts that have been active within the last N days
  using lastLogonTimestamp (replicated, approximate).
- Write results to a NinjaOne custom field.

Parameters:
- Days (30/60/90)
- RunId (unique run identifier passed by the webapp)

Custom field:
- Hardcoded to 'ADInventoryJson'

Notes:
- Must run on a domain-joined host with RSAT AD module available (or a DC).
- Uses lastLogonTimestamp which can lag; best used for 30/60/90-day windows.
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory=$false)]
  [int]$Days = 30,

  [Parameter(Mandatory=$false)]
  [string]$RunId = ''
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
  Write-Error $Message
  exit 1
}

$NinjaPropertySet = Get-Command -Name 'Ninja-Property-Set' -ErrorAction SilentlyContinue
if (-not $NinjaPropertySet) {
  Fail "Ninja-Property-Set is not available in this environment."
}

# Some runners may pass Days as empty/0; enforce the hard default.
if (-not $Days) { $Days = 30 }

if ($Days -notin @(30,60,90)) { Fail "Days must be one of: 30, 60, 90" }
if ([string]::IsNullOrWhiteSpace($RunId)) { $RunId = [guid]::NewGuid().ToString('N') }

# Target NinjaOne custom field name (hardcoded)
$CustomFieldName = 'ADInventoryJson'

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

# Store only computer names to keep the payload small enough for Ninja custom field limits.
$results = New-Object System.Collections.Generic.List[string]

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
    $results.Add([string]$name)
  }
}

$payload = [pscustomobject]@{
  days = $Days
  runId = $RunId
  generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
  workstations = $results
}

$json = $payload | ConvertTo-Json -Depth 6

# Ninja custom fields can have size limits; keep the payload safe.
# Many tenants enforce ~10,000 characters on text fields.
$maxLen = 9500
$valueToStore = $json
if ($valueToStore.Length -gt $maxLen) {
  $valueToStore = ($valueToStore.Substring(0, $maxLen) + "\n...TRUNCATED...")
}

Ninja-Property-Set -Name $CustomFieldName -Value $valueToStore -ErrorAction Stop | Out-Null
Write-Host "AD inventory stored in NinjaOne custom field '$CustomFieldName': $($results.Count) computers (>= $Days days)"
