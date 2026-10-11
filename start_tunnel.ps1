# Opens a Cloudflare quick tunnel to the Idon-Lafia backend, points the
# dashboard at the new address and redeploys it to Firebase.
#
#   1. Start the backend first:   python phc_triage\backend\main.py
#   2. Then in another terminal:  powershell -ExecutionPolicy Bypass -File start_tunnel.ps1
#
# Keep this window open; closing it (or Ctrl+C) closes the tunnel.
# Options: -Port 5005 (backend port), -NoDeploy (update config.js only).
#
# Quick tunnels can be dropped by Cloudflare (for example after a network
# blip); cloudflared then logs "Tunnel not found" forever and the address is
# gone. This script notices that, opens a fresh tunnel and redeploys, so the
# dashboard keeps working. Update the Meta webhook when the address changes.

param(
    [int]$Port = 5005,
    [switch]$NoDeploy
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$config = Join-Path $root "phc_triage\dashboard\static\js\config.js"
$log = Join-Path $env:TEMP "idon-lafia-tunnel.log"

if (-not (Test-Path $cloudflared)) { throw "cloudflared not found at $cloudflared" }

try { Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5 | Out-Null }
catch { Write-Warning "Nothing is answering on port $Port yet. Start the backend: python phc_triage\backend\main.py" }

function Open-Tunnel {
    # cloudflared prints the public address to its log, not to stdout.
    if (Test-Path $log) { Remove-Item $log -Force }
    $p = Start-Process $cloudflared -ArgumentList "tunnel --no-autoupdate --url http://127.0.0.1:$Port --logfile `"$log`"" `
        -NoNewWindow -PassThru
    Write-Host "Opening tunnel..."
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if ($p.HasExited) { throw "cloudflared stopped. See $log" }
        if (Test-Path $log) {
            $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
            if ($m) { return @{ Proc = $p; Url = $m.Matches[0].Value } }
        }
    }
    Stop-Process -Id $p.Id -Force
    throw "No tunnel address after 30 seconds. See $log"
}

function Publish($url) {
    # Point the dashboard at the new address.
    $js = [IO.File]::ReadAllText($config)
    $js = [regex]::Replace($js, 'var API_URL = "[^"]*";', "var API_URL = `"$url`";")
    [IO.File]::WriteAllText($config, $js, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "config.js now points at $url"
    if (-not $NoDeploy) {
        Push-Location $root
        try { firebase deploy --only hosting } finally { Pop-Location }
    }
    Write-Host ""
    Write-Host "Backend address : $url" -ForegroundColor Green
    Write-Host "Health check    : $url/health"
    Write-Host "Meta webhook    : $url/whatsapp   (paste into the Meta developer dashboard)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Tunnel is open. Keep this window open; press Ctrl+C to close it."
}

function Test-Alive($url) {
    try { Invoke-RestMethod "$url/health" -TimeoutSec 15 | Out-Null; return $true } catch { return $false }
}

$t = $null
try {
    while ($true) {
        $t = Open-Tunnel
        # A fresh address takes a few seconds to resolve.
        for ($i = 0; $i -lt 15 -and -not (Test-Alive $t.Url); $i++) { Start-Sleep -Seconds 2 }
        Publish $t.Url

        # Watch it: restart when cloudflared dies or the address stops answering three times running.
        $fails = 0
        while (-not $t.Proc.HasExited -and $fails -lt 3) {
            Start-Sleep -Seconds 30
            if (Test-Alive $t.Url) { $fails = 0 } else { $fails++ }
        }
        Write-Warning "Tunnel $($t.Url) is down. Opening a new one..."
        if (-not $t.Proc.HasExited) { Stop-Process -Id $t.Proc.Id -Force }
    }
}
finally {
    if ($t -and -not $t.Proc.HasExited) { Stop-Process -Id $t.Proc.Id -Force }
}
