$ErrorActionPreference = 'Stop'

function Read-KeaAuthorization($Node) {
    $credential = Get-Credential -Message "Nhap mat khau API Kea cua node $Node"
    if (-not $credential) { throw "Chua nhap thong tin API cua node $Node." }
    $pair = $credential.UserName + ':' + $credential.GetNetworkCredential().Password
    'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
}

try {
    $env:KEA_A_URL = Read-Host 'URL Control Agent node A'
    $env:KEA_B_URL = Read-Host 'URL Control Agent node B'
    $env:KEA_DIRECT = '0'
    $env:KEA_ENABLE_WRITES = '0'
    $env:KEA_A_AUTH = Read-KeaAuthorization 'A'
    $env:KEA_B_AUTH = Read-KeaAuthorization 'B'
    python (Join-Path $PSScriptRoot 'app.py')
    if ($LASTEXITCODE -ne 0) { throw "Python dung voi ma loi $LASTEXITCODE." }
} finally {
    Remove-Item Env:KEA_A_AUTH,Env:KEA_B_AUTH -ErrorAction SilentlyContinue
}
