$paths = @(
  'C:\Users\30919\AppData\Local\Docker',
  'C:\Users\30919\AppData\Local\Packages\CanonicalGroupLimited.Ubuntu22.04LTS_79rhkp1fndgsc'
)
foreach ($p in $paths) {
  Write-Host "===== $p ====="
  Write-Host "--- Top 15 largest files ---"
  Get-ChildItem $p -Recurse -File -Force -ErrorAction SilentlyContinue |
    Sort-Object Length -Descending |
    Select-Object -First 15 @{N='SizeGB';E={[math]::Round($_.Length/1GB,2)}}, FullName |
    Format-Table -AutoSize | Out-String -Width 300 | Write-Host
  Write-Host "--- Subfolder totals (top level) ---"
  Get-ChildItem $p -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    [PSCustomObject]@{ SizeGB = [math]::Round($size/1GB,2); Folder = $_.FullName }
  } | Sort-Object SizeGB -Descending | Format-Table -AutoSize | Out-String -Width 300 | Write-Host
}
