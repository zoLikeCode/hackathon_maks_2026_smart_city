$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot

try {
    docker compose up --build -d
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed to start."
    }

    docker compose ps
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read Docker Compose status."
    }
}
finally {
    Pop-Location
}
