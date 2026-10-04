<#
Deploys overload-xray to AWS. Needs Docker running, AWS credentials, and Terraform in infra\.bin.

    .\deploy.ps1                 # Terraform asks you to type "yes" before it changes anything
    .\deploy.ps1 -AutoApprove    # only after you have read `terraform plan`

Teardown:  .bin\terraform.exe destroy -var image_tag=x
#>
param([switch]$AutoApprove)
$ErrorActionPreference = "Stop"
$terraform = Join-Path $PSScriptRoot ".bin\terraform.exe"
$repoRoot = Split-Path $PSScriptRoot -Parent
$tag = Get-Date -Format "yyyyMMdd-HHmmss"          # image tags are immutable: every deploy gets a new one

# The outer @( ) matters. `if` hands back a one-item array as a bare string, and splatting a string passes it one
# CHARACTER at a time ("-", "a", "u", "t", "o"...): Terraform then complained "Too many command line arguments".
$approve = @(if ($AutoApprove) { "-auto-approve" })

# $ErrorActionPreference = "Stop" does NOT stop on a failing program, only on a failing PowerShell command. Without the
# exit-code check a failed `terraform apply` would carry straight on to building and pushing an image.
#
# The opposite trap: docker writes its build progress to stderr. When the output is captured (a log file, CI),
# Windows PowerShell 5.1 turns every stderr line into an error record, and "Stop" then aborts the script on the first
# progress line ("#0 building with ...") although nothing has failed. So the program runs with "Continue", its lines
# become plain text again, and the exit code is the only success signal. (Reproduced and checked: a chatty program that
# succeeds survives, one that exits non-zero still stops the script.)
function Step($description, [scriptblock]$command) {
    Write-Host "`n=== $description" -ForegroundColor Cyan
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $command 2>&1 | ForEach-Object { "$_" } }
    finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "FAILED (exit code $LASTEXITCODE): $description. Nothing after this step was run." }
}

Push-Location $PSScriptRoot
try {
    Step "terraform init" { & $terraform init -input=false }

    # 1. The registry has to exist before an image can be pushed to it.
    Step "create the container registry" {
        & $terraform apply @approve -input=false "-target=aws_ecr_repository.web" "-target=aws_ecr_lifecycle_policy.web" -var "image_tag=$tag"
    }
    $repo = & $terraform output -raw ecr_repository_url
    if ($LASTEXITCODE -ne 0 -or -not $repo) { throw "could not read the repository address" }
    $registry = $repo.Split("/")[0]            # <account id>.dkr.ecr.us-east-1.amazonaws.com
    $region = $registry.Split(".")[3]          # us-east-1: take it from the address, not from whatever the CLI defaults to

    # 2. Build the Lambda image and push it. --provenance=false: Lambda cannot read the extra attestation manifests
    #    that Docker's builder adds by default. The login goes through cmd: in Windows PowerShell 5.1 a pipe into
    #    `docker login --password-stdin` adds stray bytes and the registry answers "400 Bad Request".
    Step "log in to the registry" { cmd /c "aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry" }
    Step "build the image ($tag)" { docker build --provenance=false -f "$repoRoot\Dockerfile.lambda" -t "${repo}:$tag" $repoRoot }
    Step "push the image" { docker push "${repo}:$tag" }

    # 3. Everything else.
    Step "create the rest (function, API, CloudFront)" { & $terraform apply @approve -input=false -var "image_tag=$tag" }

    # 4. CloudFront keeps the page, scripts and styles for 5 minutes and each file expires on its own clock, so right
    #    after a deploy a visitor could get the new HTML with the old JavaScript. Clearing the cache costs nothing here.
    $url = & $terraform output -raw url
    if ($LASTEXITCODE -ne 0 -or -not $url) { throw "could not read the public address" }
    $domain = $url -replace "^https://", ""
    $distribution = aws cloudfront list-distributions --query "DistributionList.Items[?DomainName=='$domain'].Id | [0]" --output text
    if ($LASTEXITCODE -ne 0 -or -not $distribution -or $distribution -eq "None") { throw "could not find the CloudFront distribution for $domain" }
    Step "clear the CloudFront cache" { aws cloudfront create-invalidation --distribution-id $distribution --paths "/*" --query "Invalidation.Status" --output text }

    # 5. Smoke test the public address.
    Write-Host "`n=== smoke test" -ForegroundColor Cyan
    "Health check: " + (Invoke-RestMethod "$url/healthz").status
    "Live at:      $url"
}
finally {
    Pop-Location
}
