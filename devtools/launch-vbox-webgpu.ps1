param(
    [string]$PythonExe = "python.exe",
    [string]$ChromeExe = "",
    [string]$ChromeProfile = "",
    [switch]$BrowserFilePicker,
    [ValidateRange(1, 65535)]
    [int]$DevToolsPort = 0
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $repoRoot "src"

$pythonCommand = Get-Command $PythonExe -CommandType Application -ErrorAction Stop
$pythonPath = $pythonCommand.Source

if ([string]::IsNullOrWhiteSpace($ChromeExe)) {
    $chromeCandidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    $ChromeExe = $chromeCandidates | Select-Object -First 1
}
if (-not $ChromeExe -or -not (Test-Path -LiteralPath $ChromeExe -PathType Leaf)) {
    throw "Google Chrome was not found. Pass its executable path with -ChromeExe."
}

if ([string]::IsNullOrWhiteSpace($ChromeProfile)) {
    $ChromeProfile = Join-Path $env:LOCALAPPDATA "MM-FEM\VirtualBox-WebGPU-Chrome"
}
if ($DevToolsPort -gt 0) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $DevToolsPort)
    try {
        $listener.Start()
    }
    catch {
        throw "Local Chrome DevTools port $DevToolsPort is already in use."
    }
    finally {
        $listener.Stop()
    }
}

$oldPath = $env:PATH
$oldPythonPath = $env:PYTHONPATH
$oldBrowser = $env:NGAPP_BROWSER
$oldChromeProfile = $env:NGAPP_CHROME_PROFILE
$oldFilePicker = $env:NGSOLVE_GUI_FILE_PICKER
$chromeDirectory = Split-Path -Parent $ChromeExe
$env:PATH = "$chromeDirectory$([System.IO.Path]::PathSeparator)$oldPath"
$env:PYTHONPATH = if ($oldPythonPath) {
    "$sourceRoot$([System.IO.Path]::PathSeparator)$oldPythonPath"
} else {
    $sourceRoot
}

# ngapp appends these switches to its normal --app=<local-url> Chrome command.
# They exist only in this launcher process; no global setting, production code,
# browser policy, sandbox setting, or installed package is changed.
$browserArguments = @(
    "chrome.exe",
    "--enable-unsafe-webgpu",
    "--use-webgpu-adapter=swiftshader",
    "--no-first-run"
)
if ($DevToolsPort -gt 0) {
    $browserArguments += @(
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=$DevToolsPort"
    )
}
$env:NGAPP_BROWSER = $browserArguments -join " "
$env:NGAPP_CHROME_PROFILE = $ChromeProfile
if ($BrowserFilePicker) {
    $env:NGSOLVE_GUI_FILE_PICKER = "browser"
}

$pythonArguments = @("-m", "ngsolve_gui")

Write-Host "Starting the source checkout in a Chromium app window with VM-only SwiftShader WebGPU."
Write-Host "Chrome profile: $ChromeProfile"
if ($DevToolsPort -gt 0) {
    Write-Host "Local DevTools enabled on 127.0.0.1:$DevToolsPort for verification."
}

Push-Location $repoRoot
$exitCode = 0
try {
    & $pythonPath @pythonArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    $env:PATH = $oldPath
    $env:PYTHONPATH = $oldPythonPath
    $env:NGAPP_BROWSER = $oldBrowser
    $env:NGAPP_CHROME_PROFILE = $oldChromeProfile
    $env:NGSOLVE_GUI_FILE_PICKER = $oldFilePicker
}
exit $exitCode
