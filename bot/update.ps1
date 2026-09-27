# Updates the bot on the Windows server in place from GitHub.
# Keeps .env, data\ (database, scanner session, backups) and .venv untouched.
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/vovaclub1/leadsGen/v0/leadhunter-bot/bot/update.ps1 | iex

$ErrorActionPreference = "Stop"
$Branch = if ($env:LH_BRANCH) { $env:LH_BRANCH } else { "v0/leadhunter-bot" }
$ZipUrl = "https://github.com/vovaclub1/leadsGen/archive/refs/heads/$Branch.zip"

# The bot folder is the one that already has a filled .env next to main.py.
$BotDir = $env:LH_BOT_DIR
if (-not $BotDir) {
    $found = Get-ChildItem -Path "C:\Users\Administrator", "C:\" -Filter ".env" -Recurse -Force -Depth 4 -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.DirectoryName "main.py") } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $found) { throw "Bot folder with .env and main.py not found. Set `$env:LH_BOT_DIR and run again." }
    $BotDir = $found.DirectoryName
}
Write-Host "Bot folder: $BotDir"

Write-Host "Stopping running bot..."
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match "main\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$Tmp = Join-Path $env:TEMP "lh-update"
if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
New-Item -ItemType Directory -Path $Tmp | Out-Null
Write-Host "Downloading $Branch..."
Invoke-WebRequest -Uri $ZipUrl -OutFile (Join-Path $Tmp "src.zip") -UseBasicParsing
Expand-Archive -Path (Join-Path $Tmp "src.zip") -DestinationPath $Tmp -Force
$NewBot = Get-ChildItem -Path $Tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "bot\main.py") } | Select-Object -First 1
if (-not $NewBot) { throw "bot\main.py not found in downloaded archive." }
$NewBot = Join-Path $NewBot.FullName "bot"

Write-Host "Copying new code (keeping .env, data, .venv)..."
robocopy $NewBot $BotDir /E /XD data .venv __pycache__ /XF .env *.session /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }

Set-Location $BotDir
if (-not (Test-Path ".venv")) { python -m venv .venv }
Write-Host "Installing dependencies..."
& ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

Write-Host "Starting bot in a new window..."
Start-Process -FilePath (Join-Path $BotDir ".venv\Scripts\python.exe") -ArgumentList "main.py" -WorkingDirectory $BotDir
Remove-Item $Tmp -Recurse -Force
Write-Host "Done. Bot updated and started. Check /start in Telegram."
