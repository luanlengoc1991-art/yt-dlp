# Phat hanh ban moi cua yt-dlp Studio: build exe -> GitHub (push + Release) -> Cloudflare (ytdlp.luangroup.asia).
# Moi may dang dung app se tu tai ban moi trong vong vai phut sau khi mo app (hoac toi da 6 tieng neu app dang mo san).
#
#   .\publish-studio.ps1                          # tu tang ban va (1.0.0 -> 1.0.1)
#   .\publish-studio.ps1 -Notes "Sua loi tai TikTok"
#   .\publish-studio.ps1 -Version 1.2.0 -Notes "..."
param(
    [string]$Version,
    [string]$Notes = ''
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$utf8 = New-Object Text.UTF8Encoding $false

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE) { throw "$what that bai (ma $LASTEXITCODE)" } }

if ((git branch --show-current) -ne 'studio') { throw 'Hay chuyen sang nhanh studio truoc: git checkout studio' }

# 1. So phien ban
$vf = Join-Path $PSScriptRoot 'gui\studio_version.py'
$src = [IO.File]::ReadAllText($vf)
$cur = [regex]::Match($src, "STUDIO_VERSION = '([\d.]+)'").Groups[1].Value
$published = $null
try { $published = (Invoke-RestMethod "https://ytdlp.luangroup.asia/latest.json?t=$(Get-Date -UFormat %s)" -TimeoutSec 15).version } catch {}
if (-not $Version) {
    if ($published -and $published -eq $cur) {
        $p = $cur.Split('.'); $p[2] = [int]$p[2] + 1; $Version = $p -join '.'
    } else { $Version = $cur }
}
if ($published -and ([version]$Version -le [version]$published)) { throw "Ban $Version khong moi hon ban dang phat hanh ($published)" }
[IO.File]::WriteAllText($vf, ($src -replace "STUDIO_VERSION = '[\d.]+'", "STUDIO_VERSION = '$Version'"), $utf8)
if (-not $Notes) { $Notes = "Phien ban $Version" }
Step "Phat hanh yt-dlp Studio $Version"

# 2. Build exe
Step 'Build exe'
& $py devscripts/make_lazy_extractors.py; Check 'make_lazy_extractors'
& $py -m PyInstaller --noconfirm --onefile --windowed --log-level WARN --name yt-dlp-studio `
    --icon "$PSScriptRoot\devscripts\logo.ico" `
    --additional-hooks-dir="$PSScriptRoot\yt_dlp\__pyinstaller" `
    --add-data "$PSScriptRoot\gui\index.html:gui" --add-data "$PSScriptRoot\gui\login.html:gui" `
    --exclude-module setuptools --distpath dist --workpath build/studio --specpath build/studio gui/server.py
Check 'PyInstaller'
$exe = Join-Path $PSScriptRoot 'dist\yt-dlp-studio.exe'

# 3. File cap nhat cho trang web
Step 'Tao latest.json'
Copy-Item $exe 'site\public\yt-dlp-studio.exe' -Force
$manifest = [ordered]@{
    version = $Version
    url     = 'https://ytdlp.luangroup.asia/yt-dlp-studio.exe'
    sha256  = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
    size    = (Get-Item $exe).Length
    notes   = $Notes
    date    = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'site\public\latest.json'), ($manifest | ConvertTo-Json), $utf8)

# 4. GitHub
Step 'Day len GitHub'
git add gui site publish-studio.ps1 yt-dlp-studio.cmd .gitignore; Check 'git add'
git diff --cached --quiet
if ($LASTEXITCODE) { git commit -q -m "Studio $Version" -m $Notes; Check 'git commit' }
git push -q origin studio; Check 'git push'
gh release create "studio-v$Version" $exe --target studio --title "yt-dlp Studio $Version" --notes $Notes; Check 'gh release'

# 5. Cloudflare: dua latest.json len cuoi cung de cac may chi thay ban moi khi moi thu da san sang
Step 'Deploy Cloudflare'
Push-Location site
try { npx --yes wrangler deploy; Check 'wrangler deploy' } finally { Pop-Location }

Step "Xong. Ban $Version da len https://ytdlp.luangroup.asia"
