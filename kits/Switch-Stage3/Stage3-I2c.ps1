<#
.SYNOPSIS
  Stage 3 on the Switch's Windows (Administrator). Needs UEFI R370 (DSDT I2C1/I2C5).

  -Mode Install : pnputil /add-driver tegra210i2c.inf /install, then report
  -Mode Status  : report the I2C devices (ACPI\SWOA0201) and the driver trace
  -Mode Remove  : remove the driver package

  Log: C:\SwitchWOA-Stage3.txt
#>
param([Parameter(Mandatory = $true)][ValidateSet('Install', 'Status', 'Remove')][string]$Mode)

$ErrorActionPreference = 'Continue'
# Windows PowerShell on Windows 10 ARM64 is x86: reach System32 via Sysnative.
$Sys = Join-Path $env:windir 'Sysnative'
if (-not (Test-Path (Join-Path $Sys 'pnputil.exe'))) { $Sys = Join-Path $env:windir 'System32' }
$Pnputil = Join-Path $Sys 'pnputil.exe'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = 'C:\SwitchWOA-Stage3.txt'
function Log([string]$s) { Write-Host $s; Add-Content -Path $Log -Value $s -Encoding UTF8 }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host 'Run this from an elevated (Administrator) prompt.'; exit 1 }
Log ''
Log "=== Stage3 $Mode  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

function Show-Status {
  $dev = @(Get-CimInstance Win32_PnPEntity -Filter "PNPDeviceID LIKE 'ACPI\\SWOA0201%'" -ErrorAction SilentlyContinue)
  if ($dev.Count -eq 0) { Log 'I2C devices: NOT FOUND (ACPI\SWOA0201). Is the Switch booted with UEFI R370?' }
  foreach ($d in $dev) {
    Log ("I2C device: {0}  name='{1}' status={2} problem={3} service={4}" -f $d.PNPDeviceID, $d.Name, $d.Status, $d.ConfigManagerErrorCode, $d.Service)
  }
  Log ('sc query tegra210i2c: ' + ((& sc.exe query tegra210i2c 2>&1) -join ' ' -replace '\s+', ' '))
  $p = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\tegra210i2c\Parameters' -ErrorAction SilentlyContinue
  if ($p) {
    $buses = $p.PSObject.Properties | ForEach-Object {
      if ($_.Name -match '^I2C_([0-9A-F]{4})_') { $Matches[1] }
    } | Sort-Object -Unique
    foreach ($bus in $buses) {
      $prefix = "I2C_${bus}_"
      $vals = $p.PSObject.Properties | Where-Object { $_.Name.StartsWith($prefix) } |
        ForEach-Object {
          $name = $_.Name.Substring($prefix.Length)
          $value = [long]$_.Value -band 0xFFFFFFFFL
          if ($name -eq 'SelfTest_MAX77621_Vout' -and $value -eq 0xC00000BBL) {
            'SelfTest_MAX77621_Vout=skipped (board-specific regulator)'
          } else { '{0}=0x{1:X}' -f $name, $value }
        }
      if ($vals) { Log ("trace I2C@7000${bus}: " + ($vals -join ' ')) }
    }
  } else { Log 'driver trace: (no Parameters key)' }
  Log 'SelfTest values: 0x1XX = read OK with value XX; 0xC0000xxx = NTSTATUS error (C000000E = no ACK, C00000B5 = timeout).'
}

switch ($Mode) {
  'Install' {
    $inf = Join-Path $Here 'tegra210i2c.inf'
    Log "pnputil /add-driver $inf /install"
    (& $Pnputil /add-driver $inf /install 2>&1) | ForEach-Object { Log "  $_" }
    Log "  exit=$LASTEXITCODE"
    Start-Sleep -Seconds 4
    Show-Status
  }
  'Status' { Show-Status }
  'Remove' {
    $oem = & $Pnputil /enum-drivers 2>&1 | Out-String
    foreach ($x in [regex]::Matches($oem, '(?ms)Published Name\s*:\s*(oem\d+\.inf)\s*\r?\n\s*Original Name\s*:\s*tegra210i2c\.inf')) {
      $name = $x.Groups[1].Value
      Log "pnputil /delete-driver $name /uninstall /force"
      (& $Pnputil /delete-driver $name /uninstall /force 2>&1) | ForEach-Object { Log "  $_" }
    }
    Show-Status
  }
}
Log "Log: $Log"
