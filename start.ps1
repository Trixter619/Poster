$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:UV_UNMANAGED_INSTALL = Join-Path $PSScriptRoot '.launcher'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot '.runtime\python'
$env:UV_NO_MODIFY_PATH = '1'
$env:VK_POSTER_UV = Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'
try {
    if (-not (Test-Path -LiteralPath $env:VK_POSTER_UV)) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        New-Item -ItemType Directory -Force -Path $env:UV_UNMANAGED_INSTALL | Out-Null
        $installer = Join-Path $env:UV_UNMANAGED_INSTALL 'install.ps1'
        Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/install.ps1' -OutFile $installer
        & $installer
        if (-not (Test-Path -LiteralPath $env:VK_POSTER_UV)) { throw 'Could not install uv.' }
    }
    & $env:VK_POSTER_UV run --no-project --no-config --managed-python --python 3.12 launcher.py
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message
    exit 1
}
