param(
    [switch]$Init,
    [string]$Version,
    [long]$Size,
    [string]$Sha256,
    [string]$Notes = '',
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$keyName = 'UmaRU Launcher Release 1'
$provider = [System.Security.Cryptography.CngProvider]::MicrosoftSoftwareKeyStorageProvider

function Get-ReleaseKey {
    if (-not [System.Security.Cryptography.CngKey]::Exists($keyName, $provider)) {
        throw "Signing key '$keyName' does not exist. Run -Init first."
    }
    return [System.Security.Cryptography.CngKey]::Open($keyName, $provider)
}

if ($Init) {
    if ($Version -or $Sha256 -or $OutputPath) { throw '-Init cannot sign a release.' }
    if (-not [System.Security.Cryptography.CngKey]::Exists($keyName, $provider)) {
        $parameters = [System.Security.Cryptography.CngKeyCreationParameters]::new()
        $parameters.Provider = $provider
        $parameters.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::None
        $parameters.KeyUsage = [System.Security.Cryptography.CngKeyUsages]::Signing
        $parameters.KeyCreationOptions = [System.Security.Cryptography.CngKeyCreationOptions]::None
        $parameters.Parameters.Add([System.Security.Cryptography.CngProperty]::new('Length', [BitConverter]::GetBytes([int]3072), [System.Security.Cryptography.CngPropertyOptions]::None))
        $key = [System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::Rsa, $keyName, $parameters)
    } else {
        $key = Get-ReleaseKey
    }
    try {
        $rsa = [System.Security.Cryptography.RSACng]::new($key)
        try {
            $public = $rsa.ExportParameters($false)
            # Only public material is printed. Private CNG key is nonexportable.
            [pscustomobject]@{
                modulus = [Convert]::ToBase64String($public.Modulus)
                exponent = [Convert]::ToBase64String($public.Exponent)
            } | ConvertTo-Json -Compress
        } finally { $rsa.Dispose() }
    } finally { $key.Dispose() }
    return
}

if ($Version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
    $Version.Length -gt 24 -or $Size -le 0 -or $Size -gt 1073741824 -or
    $Sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Notes.Length -gt 2000 -or
    [string]::IsNullOrWhiteSpace($OutputPath)) {
    throw 'Use -Version major.minor -Size bytes -Sha256 lowercase-hex -Notes text -OutputPath launcher.json.'
}
$payload = [ordered]@{
    schema = 1
    version = $Version
    size = $Size
    sha256 = $Sha256
    notes = $Notes
} | ConvertTo-Json -Compress -Depth 2
$bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($payload)
$key = Get-ReleaseKey
try {
    $rsa = [System.Security.Cryptography.RSACng]::new($key)
    try {
        $signature = $rsa.SignData($bytes, [System.Security.Cryptography.HashAlgorithmName]::SHA256, [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    } finally { $rsa.Dispose() }
} finally { $key.Dispose() }
$manifest = [ordered]@{
    payload = [Convert]::ToBase64String($bytes)
    signature = [Convert]::ToBase64String($signature)
} | ConvertTo-Json -Compress -Depth 2
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($OutputPath), $manifest + "`n", [System.Text.UTF8Encoding]::new($false))
