# One-off repair: restart the Windows Audio services.
#
# Audio endpoints get stuck in "Unknown" state, which makes every application
# report that no output device exists. Restarting Windows Audio Endpoint
# Builder re-creates them. Audio will blip for a few seconds.
#
# Run elevated. Nothing here changes any user setting.

$ErrorActionPreference = 'Continue'
$log = Join-Path $env:TEMP 'fix_audio_endpoints.log'
"started $(Get-Date -Format o)" | Set-Content $log

function Note($m) { $m | Add-Content $log }

# AudioSrv depends on AudioEndpointBuilder, so it has to go first.
Note 'stopping AudioSrv'
Stop-Service -Name AudioSrv -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Note 'stopping AudioEndpointBuilder'
Stop-Service -Name AudioEndpointBuilder -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Note 'starting AudioEndpointBuilder'
Start-Service -Name AudioEndpointBuilder -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Note 'starting AudioSrv'
Start-Service -Name AudioSrv -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

foreach ($name in 'AudioEndpointBuilder', 'AudioSrv') {
    $svc = Get-Service -Name $name
    Note "$name -> $($svc.Status)"
}
Note 'done'
