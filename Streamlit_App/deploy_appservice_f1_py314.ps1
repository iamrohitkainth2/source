$ErrorActionPreference = 'Stop'

$rg = 'rg-First-AI'
$app = 'FirstRKApp'
$plan = 'asp-firstai-f1'
$location = 'eastus'

# Load .env values into process environment if present (without overriding existing env vars).
# $envFile = Join-Path $PSScriptRoot '.env'
# if (Test-Path $envFile) {
#   Get-Content $envFile | ForEach-Object {
#     if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
#     $parts = $_ -split '=', 2
#     if ($parts.Count -eq 2) {
#       $name = $parts[0].Trim()
#       $value = $parts[1].Trim().Trim("\"").Trim("'")
#       $existing = (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue).Value
#       if (-not [string]::IsNullOrWhiteSpace($name) -and [string]::IsNullOrWhiteSpace($existing)) {
#         Set-Item -Path "Env:$name" -Value $value
#       }
#     }
#   }
# }

Write-Host 'Logging in to Azure...'
az login | Out-Null

Write-Host 'Creating resource group (if needed)...'
az group create --name $rg --location $location | Out-Null

Write-Host 'Creating Linux F1 App Service plan (if needed)...'
az appservice plan create --name $plan --resource-group $rg --sku F1 --is-linux | Out-Null

Write-Host 'Creating web app (if needed) with Python 3.11...'
az webapp create --resource-group $rg --plan $plan --name $app --runtime "PYTHON|3.11" | Out-Null

Write-Host 'Forcing runtime to Python 3.11...'
az webapp config set --resource-group $rg --name $app --linux-fx-version "PYTHON|3.11" | Out-Null

Write-Host 'Enabling build during deployment...'
az webapp config appsettings set --resource-group $rg --name $app --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true | Out-Null

Write-Host 'Setting Streamlit startup command...'
az webapp config set --resource-group $rg --name $app --startup-file "python -m streamlit run main.py --server.port 8000 --server.address 0.0.0.0" | Out-Null

if ($env:AZURE_OPENAI_ENDPOINT -and $env:AZURE_OPENAI_API_KEY) {
  Write-Host 'Applying OpenAI app settings from environment variables...'
  az webapp config appsettings set --resource-group $rg --name $app --settings AZURE_OPENAI_ENDPOINT="$env:AZURE_OPENAI_ENDPOINT" AZURE_OPENAI_API_KEY="$env:AZURE_OPENAI_API_KEY" OPENAI_API_KEY="$env:AZURE_OPENAI_API_KEY" OPENAI_API_BASE="$env:AZURE_OPENAI_ENDPOINT" OPENAI_API_TYPE="azure" OPENAI_API_VERSION="2023-05-15" | Out-Null
}

Write-Host 'Deploying current folder...'
az webapp up --name $app --resource-group $rg --runtime "PYTHON:3.11" --sku F1 --logs

Write-Host 'Verifying runtime...'
$runtime = az webapp config show --resource-group $rg --name $app --query linuxFxVersion -o tsv
Write-Host "Runtime: $runtime"

Write-Host "Done. URL: https://$app.azurewebsites.net"
