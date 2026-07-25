param(
    [Parameter(Mandatory = $true)]
    [int64]$TargetId,
    [int]$SecondsPerCenter = 60,
    [int]$StartMHz = 910,
    [int]$EndMHz = 920,
    [int]$StepMHz = 1,
    [int]$Port = 1234,
    [double]$Gain = 40.2
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$rtlTcpExe = Join-Path $root ".tools\rtl-sdr-blog-v1.3.6\x64\rtl_tcp.exe"
$rtlamrExe = Join-Path $root ".tools\rtlamr-v0.9.5\rtlamr.exe"
if (-not (Test-Path $rtlTcpExe)) { throw "Missing rtl_tcp.exe at $rtlTcpExe" }
if (-not (Test-Path $rtlamrExe)) { throw "Missing rtlamr.exe at $rtlamrExe" }

Get-Process rtl_tcp,rtlamr -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runDir = Join-Path $logDir "rtlamr_sweep_$stamp"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$summaryPath = Join-Path $runDir "summary.csv"
$metaPath = Join-Path $logDir "current_rtlamr_sweep.json"
$sampleRate = 524288
$symbolLength = 16

$centers = for ($mhz = $StartMHz + 0.5; $mhz -lt $EndMHz; $mhz += $StepMHz) {
    [int64]($mhz * 1000000)
}

$meta = [pscustomobject]@{
    started = (Get-Date).ToString("o")
    target = $TargetId
    seconds_per_center = $SecondsPerCenter
    start_mhz = $StartMHz
    end_mhz = $EndMHz
    step_mhz = $StepMHz
    run_dir = $runDir
    summary = $summaryPath
    sample_rate = $sampleRate
    symbol_length = $symbolLength
    status = "running"
    current_center_hz = $null
    current_index = 0
    total_centers = @($centers).Count
}
$meta | ConvertTo-Json | Set-Content -LiteralPath $metaPath

$rows = New-Object System.Collections.Generic.List[object]

function Stop-RtlTcp {
    param($Process)
    if ($null -ne $Process) {
        $p = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue
        if ($p) {
            Stop-Process -Id $Process.Id -Force
            Start-Sleep -Milliseconds 500
        }
    }
}

try {
    for ($i = 0; $i -lt @($centers).Count; $i++) {
        $center = $centers[$i]
        $mhzText = "{0:n1}" -f ($center / 1000000)
        $safeMhz = $mhzText.Replace(".", "_")

        $meta.current_center_hz = $center
        $meta.current_index = $i + 1
        $meta.status = "running $mhzText MHz"
        $meta | ConvertTo-Json | Set-Content -LiteralPath $metaPath

        $tcpOut = Join-Path $runDir "rtl_tcp_${safeMhz}MHz.out"
        $tcpErr = Join-Path $runDir "rtl_tcp_${safeMhz}MHz.err"
        $jsonOut = Join-Path $runDir "rtlamr_${safeMhz}MHz.jsonl"
        $amrErr = Join-Path $runDir "rtlamr_${safeMhz}MHz.err"

        $rtlTcp = $null
        try {
            $rtlTcp = Start-Process -FilePath $rtlTcpExe `
                -ArgumentList @("-a","127.0.0.1","-p",[string]$Port,"-f",[string]$center,"-s",[string]$sampleRate,"-g",[string]$Gain) `
                -WorkingDirectory (Split-Path $rtlTcpExe) `
                -PassThru `
                -RedirectStandardOutput $tcpOut `
                -RedirectStandardError $tcpErr `
                -WindowStyle Hidden

            Start-Sleep -Seconds 2

            $amrArgs = @(
                "-format=json",
                "-msgtype=scm,scm+,idm,netidm",
                "-duration=${SecondsPerCenter}s",
                "-server=127.0.0.1:$Port",
                "-centerfreq=$center",
                "-samplerate=$sampleRate",
                "-symbollength=$symbolLength",
                "-tunergainmode=true",
                "-tunergain=$Gain"
            )

            $rtlamr = Start-Process -FilePath $rtlamrExe `
                -ArgumentList $amrArgs `
                -WorkingDirectory (Split-Path $rtlamrExe) `
                -PassThru `
                -RedirectStandardOutput $jsonOut `
                -RedirectStandardError $amrErr `
                -WindowStyle Hidden

            Wait-Process -Id $rtlamr.Id
        }
        finally {
            Stop-RtlTcp -Process $rtlTcp
        }

        $events = @()
        if (Test-Path $jsonOut) {
            $events = @(Get-Content -LiteralPath $jsonOut |
                Where-Object { $_.TrimStart().StartsWith("{") } |
                ForEach-Object { $_ | ConvertFrom-Json })
        }

        $validEvents = @($events | Where-Object { $null -ne $_.Message.ID -and [int64]$_.Message.ID -ne 0 })
        $targetEvents = @($validEvents | Where-Object { [int64]$_.Message.ID -eq $TargetId })
        $nearEvents = @($validEvents | Where-Object { [Math]::Abs(([int64]$_.Message.ID) - $TargetId) -le 100 })

        $nearSummary = ""
        if ($nearEvents.Count -gt 0) {
            $nearSummary = (($nearEvents |
                Group-Object { [int64]$_.Message.ID } |
                ForEach-Object {
                    $group = @($_.Group | Sort-Object Time)
                    $id = [int64]$_.Name
                    "${id}:$($group.Count):$($group[-1].Message.Consumption)"
                }) -join ";")
        }

        $topSummary = ""
        if ($validEvents.Count -gt 0) {
            $topSummary = (($validEvents |
                Group-Object { [int64]$_.Message.ID } |
                Sort-Object Count -Descending |
                Select-Object -First 5 |
                ForEach-Object {
                    $group = @($_.Group | Sort-Object Time)
                    "$($_.Name):$($_.Count):$($group[-1].Message.Type):$($group[-1].Message.Consumption)"
                }) -join ";")
        }

        $row = [pscustomobject]@{
            CenterMHz = $mhzText
            CenterHz = $center
            Events = $events.Count
            ValidEvents = $validEvents.Count
            TargetMatches = $targetEvents.Count
            NearTargetSummary = $nearSummary
            TopIds = $topSummary
            JsonLog = $jsonOut
            RtlamrErr = $amrErr
            RtlTcpErr = $tcpErr
        }
        $rows.Add($row)
        $rows | Export-Csv -NoTypeInformation -LiteralPath $summaryPath
    }

    $meta.status = "complete"
}
catch {
    $meta.status = "failed: $($_.Exception.Message)"
    throw
}
finally {
    Get-Process rtl_tcp,rtlamr -ErrorAction SilentlyContinue | Stop-Process -Force
    $meta | Add-Member -NotePropertyName "finished" -NotePropertyValue (Get-Date).ToString("o") -Force
    $meta | ConvertTo-Json | Set-Content -LiteralPath $metaPath
}

Write-Output "summary=$summaryPath"
