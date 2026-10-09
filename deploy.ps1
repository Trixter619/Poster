param([switch]$Public)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Run-Docker([string[]]$Arguments) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker failed ($LASTEXITCODE)" }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop with Compose first.' }
Run-Docker @('info')
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$compose = @('compose', '-f', 'compose.yaml')
if ($Public) { $compose += @('-f', 'compose.public.yaml') }
Run-Docker ($compose + @('config', '--quiet'))
Run-Docker ($compose + @('build', 'app'))
Run-Docker ($compose + @('run', '--rm', '--no-deps', 'app', 'python', 'bootstrap_admin.py'))
Run-Docker ($compose + @('up', '-d', '--wait', '--wait-timeout', '180'))
Write-Host 'VK Poster started: http://localhost:8787. Public URL uses DOMAIN from .env.'
