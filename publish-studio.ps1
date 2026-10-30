# Phat hanh ban moi cua yt-dlp Studio: chi can push thay doi app len nhanh studio.
# GitHub Actions (.github/workflows/studio-release.yml) tu build exe va tao Release; ytdlp.luangroup.asia/latest.json
# doc Release moi nhat nen moi may dang dung app tu tai ban moi khi mo app (hoac toi da 1 tieng neu app dang mo san).
# Script nay chi giup commit + push + cho ban build xong. Push tu may khac / tu Codex cung tu phat hanh y nhu vay.
#
#   .\publish-studio.ps1                          # tu tang ban va (1.0.0 -> 1.0.1)
#   .\publish-studio.ps1 -Notes "Sua loi tai TikTok"
#   .\publish-studio.ps1 -Version 1.2.0 -Notes "..."   # doi so lon
param(
    [string]$Version,
    [string]$Notes = ''
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$utf8 = New-Object Text.UTF8Encoding $false

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE) { throw "$what that bai (ma $LASTEXITCODE)" } }

if ((git branch --show-current) -ne 'studio') { throw 'Hay chuyen sang nhanh studio truoc: git checkout studio' }

if ($Version) {
    $vf = Join-Path $PSScriptRoot 'gui\studio_version.py'
    $src = [IO.File]::ReadAllText($vf)
    [IO.File]::WriteAllText($vf, ($src -replace "STUDIO_VERSION = '[\d.]+'", "STUDIO_VERSION = '$Version'"), $utf8)
}

Step 'Day len GitHub'
git add gui site publish-studio.ps1 yt-dlp-studio.cmd .gitignore .github/workflows/studio-release.yml; Check 'git add'
git diff --cached --quiet
if ($LASTEXITCODE) { git commit -q -m $(if ($Notes) { $Notes } else { 'Studio: cap nhat' }); Check 'git commit' }
$sha = git rev-parse HEAD
git push -q origin studio; Check 'git push'

Step 'Cho GitHub build va phat hanh'
$run = $null
for ($i = 0; $i -lt 20 -and -not $run; $i++) {
    Start-Sleep 3
    $run = gh run list --workflow studio-release.yml --commit $sha --json databaseId --jq '.[0].databaseId'
}
if (-not $run) { Write-Host 'Lan push nay khong doi code app nen khong phat hanh ban moi.'; return }
gh run watch $run --exit-status; Check 'Build tren GitHub'

Step "Xong. Ban moi: $((Invoke-RestMethod "https://ytdlp.luangroup.asia/latest.json?t=$(Get-Date -UFormat %s)").version)"
