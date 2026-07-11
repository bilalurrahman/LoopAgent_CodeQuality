# run-local.ps1 — start the quality loop + dashboard on Windows.
#
#   .\run-local.ps1                 # SIMULATED demo (no LLM/git), opens the UI
#   .\run-local.ps1 -Real -Repo C:\path\to\HattanMedicalHistory -OpenPr
#
# The dashboard is served at http://127.0.0.1:8787
param(
  [switch]$Real,
  [string]$Repo = $env:REPO_PATH,
  [switch]$OpenPr,
  [int]$Step = 10,
  [int]$SimRuns = 8,
  [double]$Speed = 2.0,
  [int]$Port = 8787,
  [string]$Model = "glm-5.2:cloud",
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$agent = Join-Path $PSScriptRoot "agent\loop_agent.py"

if (-not $NoBrowser) {
  Start-Job -ScriptBlock { Start-Sleep 2; Start-Process "http://127.0.0.1:$using:Port" } | Out-Null
}

if ($Real) {
  if (-not $Repo) { throw "In -Real mode you must pass -Repo <path> (or set REPO_PATH)." }
  $args = @("--repo", $Repo, "--step", $Step, "--model", $Model, "--port", $Port, "--keep-alive")
  if ($OpenPr) { $args += "--open-pr" }
  python $agent @args
} else {
  python $agent "--simulate" "--sim-runs" $SimRuns "--speed" $Speed "--step" $Step "--port" $Port "--keep-alive"
}
