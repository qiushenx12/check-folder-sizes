# check_folder_sizes.ps1
# 列出当前命令行所在目录下所有文件夹的大小

$TargetDir = if ($args.Count -gt 0) { $args -join ' ' } else { Get-Location }

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
