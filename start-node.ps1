$ErrorActionPreference = 'Stop'
$nodeArguments = @($args)
$nodeRoot = $PSScriptRoot
$nodeBootstrap = Join-Path $nodeRoot 'scripts\node_bootstrap.py'
$nodeVersionProbe = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
$nodeCandidates = @(
    @{ File = (Join-Path $nodeRoot '.venv\Scripts\python.exe'); Prefix = @() },
    @{ File = 'py'; Prefix = @('-3') },
    @{ File = 'py'; Prefix = @('-3.12') },
    @{ File = 'python'; Prefix = @() }
)
$nodePython = $null
$nodePythonPrefix = @()
foreach ($nodeCandidate in $nodeCandidates) {
    if (-not (Get-Command $nodeCandidate.File -ErrorAction SilentlyContinue)) {
        continue
    }
    try {
        $nodeProbePrefix = @($nodeCandidate.Prefix)
        & $nodeCandidate.File @nodeProbePrefix -c $nodeVersionProbe *> $null
        if ($LASTEXITCODE -eq 0) {
            $nodePython = $nodeCandidate.File
            $nodePythonPrefix = $nodeProbePrefix
            break
        }
    } catch {
        # Try the next installed interpreter; do not install or alter system Python.
    }
}
if (-not $nodePython) {
    [Console]::Error.WriteLine('Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/ with the Python launcher or PATH enabled, then run start-node.cmd again.')
    exit 1
}
if (-not (Test-Path -LiteralPath $nodeBootstrap -PathType Leaf)) {
    [Console]::Error.WriteLine('scripts/node_bootstrap.py is missing. Restore the complete project folder and try again.')
    exit 1
}
try {
    & $nodePython @nodePythonPrefix $nodeBootstrap @nodeArguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine(('Unable to start the node manager: ' + $_.Exception.Message))
    exit 1
}
