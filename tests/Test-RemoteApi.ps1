param([Parameter(Mandatory)][string]$ConnectionPath,[Parameter(Mandatory)][string]$ImagePath,[Parameter(Mandatory)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$connection = Get-Content -LiteralPath $ConnectionPath -Raw | ConvertFrom-Json
$headers = @{ Authorization = 'Bearer ' + $connection.api_key }
$base = $connection.base_url.TrimEnd('/')
$report = [ordered]@{ time = (Get-Date).ToUniversalTime().ToString('o'); origin = 'Windows LAN client'; base_url = $base }
$unauthorized = Invoke-WebRequest -Uri ($base + '/models') -SkipHttpErrorCheck -TimeoutSec 20
if ($unauthorized.StatusCode -ne 401) { throw 'Unauthenticated request was not rejected' }
$report.unauthenticated_status = 401
$models = Invoke-RestMethod -Uri ($base + '/models') -Headers $headers -TimeoutSec 20
if ($models.data[0].id -ne $connection.model) { throw 'Unexpected served model name' }
$report.model = $models.data[0].id
$payload = @{ model = $connection.model; temperature = 0; max_tokens = 48; messages = @(@{ role = 'user'; content = 'What is 6 times 7? Reply only with the number.' }) }
$result = Invoke-RestMethod -Uri ($base + '/chat/completions') -Method Post -Headers $headers -ContentType 'application/json' -Body ($payload | ConvertTo-Json -Depth 10 -Compress) -TimeoutSec 240
if ($result.choices[0].message.content -notmatch '42') { throw 'Text inference verification failed' }
$report.text_inference = @{ answer = $result.choices[0].message.content; usage = $result.usage }
$image64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($ImagePath))
$payload.messages[0].content = @(@{ type = 'text'; text = 'Read the four-digit number at the top of this image. Reply with only the number.' }, @{ type = 'image_url'; image_url = @{ url = 'data:image/png;base64,' + $image64 } })
$result = Invoke-RestMethod -Uri ($base + '/chat/completions') -Method Post -Headers $headers -ContentType 'application/json' -Body ($payload | ConvertTo-Json -Depth 10 -Compress) -TimeoutSec 240
if ($result.choices[0].message.content -notmatch '7319') { throw 'Vision inference verification failed' }
$report.vision_inference = @{ answer = $result.choices[0].message.content; usage = $result.usage }
$report.status = 'passed'
$report | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $OutputPath -Encoding utf8
Write-Output 'PASS: Windows LAN authentication, model listing, text and image inference'
