# check_folder_sizes.ps1
# 列出指定目录下所有文件夹的大小
# -TopFiles N: 额外递归列出每个目录下最大的 N 个文件（排查 WSL/Docker 等占空间大户）
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Paths,
    [int]$TopFiles = 0
)

if (-not $Paths) { $Paths = @((Get-Location).Path) }

foreach ($TargetDir in $Paths) {
    Write-Host "扫描目录: $TargetDir" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host ""

    $folders = Get-ChildItem $TargetDir -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $size = (Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum -ErrorAction SilentlyContinue).Sum
        [PSCustomObject]@{
            Name   = $_.Name
            SizeGB = [math]::Round(($size / 1GB), 2)
            SizeMB = [math]::Round(($size / 1MB), 2)
        }
    }

    $folders | Sort-Object SizeGB -Descending | ForEach-Object {
        $color = if ($_.SizeGB -ge 10) { "Red" } elseif ($_.SizeGB -ge 1) { "Yellow" } else { "Green" }
        Write-Host ("{0,-40} {1,10} GB ({2,10} MB)" -f $_.Name, $_.SizeGB, $_.SizeMB) -ForegroundColor $color
    }

    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host ("共 {0} 个文件夹" -f $folders.Count) -ForegroundColor Cyan

    if ($TopFiles -gt 0) {
        Write-Host ""
        Write-Host ("--- Top {0} largest files ---" -f $TopFiles) -ForegroundColor Cyan
        Get-ChildItem $TargetDir -Recurse -File -Force -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending |
            Select-Object -First $TopFiles @{N='SizeGB';E={[math]::Round($_.Length/1GB,2)}}, FullName |
            Format-Table -AutoSize | Out-String -Width 300 | Write-Host
    }
}

