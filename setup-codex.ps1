$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Find the TokenRouter API key (.env or opencode.json)
$apiKey = $null
$envFile = Join-Path $repoRoot ".env"
if (Test-Path $envFile) {
    $match = Get-Content $envFile | Select-String -Pattern '^API_KEY=' | Select-Object -First 1
    if ($match) { $apiKey = $match.Line.Substring("API_KEY=".Length).Trim() }
}
if (-not $apiKey) {
    $opencode = Join-Path $repoRoot "opencode.json"
    if (Test-Path $opencode) {
        $apiKey = ((Get-Content $opencode -Raw | ConvertFrom-Json).provider.tokenrouter.options.apiKey)
    }
}
if (-not $apiKey) {
    Write-Error "No API key found. Set API_KEY in .env, or keep the tokenrouter provider block in opencode.json."
    exit 1
}

[Environment]::SetEnvironmentVariable("TOKENROUTER_API_KEY", $apiKey, "User")
$env:TOKENROUTER_API_KEY = $apiKey

# 2. Codex CLI required
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host "Installing OpenAI Codex CLI..."
    npm install -g @openai/codex
}

# 3. Write the Codex profile.
#    Codex removed wire_api="chat" (Feb 2026), so the profile points at the
#    local bridge (agent/codex_bridge.py) which speaks the Responses API and
#    forwards to TokenRouter's chat/completions. The Bearer token the profile
#    sends (env_key below) is relayed straight to TokenRouter by the bridge.
$codexHome = Join-Path $env:USERPROFILE ".codex"
if (-not (Test-Path $codexHome)) { New-Item -ItemType Directory -Path $codexHome | Out-Null }

# Patch any pre-existing provider block in the base config.toml: a stale
# wire_api="chat" / direct-tokenrouter entry breaks config loading even for
# --profile runs (the base file must parse cleanly first).
$baseCfg = Join-Path $codexHome "config.toml"
if (Test-Path $baseCfg) {
    $text = Get-Content $baseCfg -Raw
    if ($text -match 'wire_api = "chat"' -or $text -match 'base_url = "https://api.tokenrouter.com/v1"') {
        $text = $text.Replace('wire_api = "chat"', 'wire_api = "responses"')
        $text = $text.Replace('base_url = "https://api.tokenrouter.com/v1"', 'base_url = "http://127.0.0.1:8787/v1"')
        [System.IO.File]::WriteAllText($baseCfg, $text)
        Write-Host "Patched existing $baseCfg (old tokenrouter/chat provider updated)."
    }
}

$toml = @'
model = "qwen/qwen3.8-max-free"
model_provider = "tokenrouter"
model_context_window = 200000

[model_providers.tokenrouter]
name = "TokenRouter via Agent-Legacy bridge"
base_url = "http://127.0.0.1:8787/v1"
env_key = "TOKENROUTER_API_KEY"
wire_api = "responses"
'@
Set-Content -Path (Join-Path $codexHome "tokenrouter.config.toml") -Value $toml -Encoding utf8NoBOM

Write-Host "Codex profile written: $codexHome\tokenrouter.config.toml"
Write-Host "TOKENROUTER_API_KEY set for this shell and future shells."
Write-Host ""
Write-Host "Usage (from $repoRoot):"
Write-Host "  .\codex.bat                      # starts bridge + interactive codex"
Write-Host "  .\codex.bat exec ""task here""    # non-interactive"
