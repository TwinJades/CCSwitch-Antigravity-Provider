param(
    [ValidateSet('Prospective', 'Index')]
    [string]$Mode = 'Prospective'
)

$ErrorActionPreference = 'Stop'

if ($Mode -eq 'Index') {
    $files = @(git diff --cached --name-only --diff-filter=ACMR)
} else {
    $files = @(git ls-files --cached --others --exclude-standard)
}

$files = @($files | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
$forbiddenNames = @(
    '(?i)(^|/)\.env(?:\.|$)',
    '(?i)(^|/)(?:client[_-]?secret|oauth|credential)[^/]*\.json$',
    '(?i)\.(?:key|pem)$'
)
$rules = [ordered]@{
    'private-key-header' = '-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
    'openai-style-secret' = '\bsk-[A-Za-z0-9_-]{20,}\b'
    'google-api-key' = '\bAIza[0-9A-Za-z_-]{30,}\b'
    'github-token' = '\bgh[pousr]_[A-Za-z0-9]{20,}\b'
    'aws-access-key' = '\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'
    'jwt' = '\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b'
    'oauth-client-id' = '\b[0-9]{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com\b'
    'literal-local-username' = '\b11077\b'
    'windows-user-path' = '(?i)[A-Z]:\\Users\\(?!user(?:name)?\\|<)[^\\\s]+'
    'long-literal-credential' = '(?i)(?:password|passphrase|api[_-]?key|secret|access[_-]?token|refresh[_-]?token|management[_-]?key)\s*[:=]\s*["''][^"'']{16,}["'']'
    'bearer-literal' = '(?i)Authorization\s*[:=]\s*["'']?Bearer\s+[A-Za-z0-9._~-]{16,}'
}
$allowed = @{
    'windows-user-path|tests/test_redaction.py' = $true
}
$findings = [System.Collections.Generic.List[object]]::new()

foreach ($file in $files) {
    $normalized = $file.Replace('\', '/')
    foreach ($pattern in $forbiddenNames) {
        if ($normalized -match $pattern -and $normalized -ne '.env.example') {
            $findings.Add([PSCustomObject]@{
                Rule = 'sensitive-filename'
                Path = $normalized
                Line = 0
            })
        }
    }
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $file) {
        $lineNumber += 1
        foreach ($entry in $rules.GetEnumerator()) {
            if ($line -match $entry.Value) {
                $allowKey = $entry.Key + '|' + $normalized
                if (-not $allowed.ContainsKey($allowKey)) {
                    $findings.Add([PSCustomObject]@{
                        Rule = $entry.Key
                        Path = $normalized
                        Line = $lineNumber
                    })
                }
            }
        }
        $emailMatches = [regex]::Matches(
            $line,
            '(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b'
        )
        foreach ($match in $emailMatches) {
            $domain = $match.Groups[1].Value.ToLowerInvariant()
            if ($domain -notin @('example.com', 'users.noreply.github.com')) {
                $findings.Add([PSCustomObject]@{
                    Rule = 'private-email'
                    Path = $normalized
                    Line = $lineNumber
                })
            }
        }
    }
}

if ($findings.Count -gt 0) {
    Write-Output ('PUBLIC_SECRET_SCAN=failed mode=' + $Mode + ' findings=' + $findings.Count)
    $findings | Sort-Object Rule, Path, Line | Format-Table Rule, Path, Line -AutoSize
    Write-Output 'Matched values are intentionally omitted.'
    exit 1
}

Write-Output ('PUBLIC_SECRET_SCAN=passed mode=' + $Mode + ' files=' + $files.Count)
