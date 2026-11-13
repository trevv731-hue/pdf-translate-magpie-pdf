# 翻译多个 PDF（默认串行逐个翻译，仅输出中文版）
# 稳定性机制：限速防限流 / 失败自动重试 / 批间冷却 / 日志落盘 / 清理残留进程 / 单文件超时保护
param(
    [int]$MaxConcurrent = 1
)
$Files = @($args)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = if (Test-Path (Join-Path $root 'venv\Scripts\pdf2zh_next.exe')) { Join-Path $root 'venv' }
        elseif (Test-Path (Join-Path $root 'pdf2zh-venv\Scripts\pdf2zh_next.exe')) { Join-Path $root 'pdf2zh-venv' }
        else { $null }
if (-not $venv) { Write-Host "[错误] 未找到翻译程序（venv 或 pdf2zh-venv 目录缺失）" -ForegroundColor Red; exit 1 }
# 不直接用 pdf2zh_next.exe launcher：它是 embeddable Python 的 console launcher，
# 不读 PYTHONHOME、按相对路径解析标准库，在 PDF 目录启动会 fatal exit、日志为 0 字节。
# 改用 venv python.exe 直接调 cli() 入口。
$exe = Join-Path $venv 'Scripts\python.exe'
$pyCode = 'from pdf2zh_next.main import cli; cli()'

# 便携包形态（venv 与 python 平级）时，把 pyvenv.cfg 的 home 修正为绝对路径，
# 否则解释器按"当前工作目录"解析相对 home，PDF 不在包目录时就会启动失败。
$portablePython = Join-Path $root 'python\python.exe'
if ((Test-Path -LiteralPath $portablePython) -and $venv.EndsWith('venv')) {
    $cfg = Join-Path $venv 'pyvenv.cfg'
    $absHome = Join-Path $root 'python'
    if (Test-Path -LiteralPath $cfg) {
        $cfgText = [IO.File]::ReadAllText($cfg)
        if (-not $cfgText.Contains("home = $absHome")) {
            $cfgText = [Text.RegularExpressions.Regex]::Replace($cfgText, '(?m)^home = .*$', "home = $absHome")
            [IO.File]::WriteAllText($cfg, $cfgText, [Text.Encoding]::UTF8)
            Write-Host ("[便携包] 已修正 Python 路径 -> {0}" -f $absHome) -ForegroundColor Cyan
        }
    }
}
$env:HF_ENDPOINT = 'https://hf-mirror.com'
$env:HF_HOME = Join-Path $env:USERPROFILE '.cache\huggingface'
# 便携 Python 的 sys.path 用相对路径(.\lib, .\DLLs)，必须显式指定 PYTHONHOME，
# 否则从 PDF 所在目录启动时找不到 encodings 等标准库，进程 fatal exit、日志为 0 字节。
$env:PYTHONHOME = Join-Path $root 'python'
# 清除外部(Doubao/其他 Python 发行版等)可能注入的 PYTHONPATH，避免覆盖 venv 路径
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

# 启动前清理残留的翻译进程（上次崩溃/中断遗留），防止占内存、锁缓存、堆积
$stale = @()
Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('pdf2zh_next.exe','python.exe') -and ($_.ExecutablePath -like "$root*" -or $_.ExecutablePath -like 'D:\Tools\pdf2zh-venv*' -or $_.ExecutablePath -like 'D:\Tools\PDF翻译工具-便携版*') } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    $stale += "$($_.ProcessId):$($_.Name)"
}
if ($stale.Count -gt 0) { Write-Host ("[清理] 已终止 {0} 个残留翻译进程" -f $stale.Count) -ForegroundColor Yellow }
Start-Sleep -Seconds 1

# 翻译限速参数：给免费接口留余量，避免 429 限流积压（稳定性关键）
$rateArgs = '--qps 8 --pool-max-workers 8 --term-qps 5 --term-pool-max-workers 4'

# 单文件最长允许时间（分钟），超过则强制终止并按失败重试
$fileTimeoutMin = 45
# 失败重试次数（总尝试 = 重试次数 + 1）
$maxRetries = 3
# 批间冷却秒数
$coolDownSec = 15

$results = [System.Collections.ArrayList]::new()
$current = $null

foreach ($f in $Files) {
    if (-not $f.EndsWith('.pdf', [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Host ("[跳过] 非PDF文件: {0}" -f (Split-Path -Leaf $f)) -ForegroundColor Yellow
        continue
    }
    $dir = Split-Path -Parent $f
    $base = [System.IO.Path]::GetFileNameWithoutExtension($f)
    $mono = Join-Path $dir ($base + '.zh.mono.pdf')
    $log  = Join-Path $dir ($base + '.zh.log')

    if (Test-Path -LiteralPath $mono) {
        Write-Host ("[完成] {0}（已有输出，跳过）" -f (Split-Path -Leaf $f)) -ForegroundColor Green
        $results.Add([pscustomobject]@{ Name = Split-Path -Leaf $f; File = $f; Ok = $true; Log = $null }) | Out-Null
        continue
    }

    # 批间冷却：让接口和缓存喘息
    if ($null -ne $current) {
        Write-Host ("[冷却] 等待 {0} 秒后处理下一个..." -f $coolDownSec) -ForegroundColor Gray
        Start-Sleep -Seconds $coolDownSec
    }
    $current = $f

    $attempt = 0
    $ok = $false
    while ($attempt -le $maxRetries -and -not $ok) {
        $attempt++
        if ($attempt -gt 1) {
            $waitSec = 30 * $attempt
            Write-Host ("[重试] {0}（第 {1} 次尝试），等待 {2} 秒避峰..." -f (Split-Path -Leaf $f), $attempt, $waitSec) -ForegroundColor Yellow
            Start-Sleep -Seconds $waitSec
        } else {
            Write-Host ("[开始] {0}" -f (Split-Path -Leaf $f)) -ForegroundColor Cyan
        }

        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
        # 后台启动翻译，完整日志写入 PDF 旁的 xxx.zh.log（失败时可查原因）
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = 'cmd.exe'
        $inner = ('"{0}" -c "{1}" "{2}" --no-dual {3} > "{4}" 2>&1' -f $exe, $pyCode, $f, $rateArgs, $log)
        $psi.Arguments = ('/d /s /c "' + $inner + '"')
        $psi.WorkingDirectory = $dir
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        try {
            $p = [System.Diagnostics.Process]::Start($psi)
        } catch {
            Write-Host ("[失败] {0}: 启动错误 {1}" -f (Split-Path -Leaf $f), $_.Exception.Message) -ForegroundColor Red
            continue
        }

        # 轮询等待，带超时保护
        $deadline = (Get-Date).AddMinutes($fileTimeoutMin)
        $timedOut = $false
        while (-not $p.HasExited) {
            Start-Sleep -Seconds 20
            if ((Get-Date) -gt $deadline) {
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                $timedOut = $true
                break
            }
        }
        $p.WaitForExit() | Out-Null

        if ($timedOut) {
            Write-Host ("[超时] {0} 超过 {1} 分钟，已终止" -f (Split-Path -Leaf $f), $fileTimeoutMin) -ForegroundColor Yellow
        } elseif (Test-Path -LiteralPath $mono) {
            $ok = $true
            Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue   # 成功则清理日志
            Write-Host ("[完成] {0}" -f (Split-Path -Leaf $f)) -ForegroundColor Green
        } else {
            Write-Host ("[失败] {0} 未生成输出，见日志: {1}" -f (Split-Path -Leaf $f), $log) -ForegroundColor Yellow
        }
    }
    $results.Add([pscustomobject]@{ Name = Split-Path -Leaf $f; File = $f; Ok = $ok; Log = $log }) | Out-Null
}

Write-Host ""
Write-Host "========== 翻译结果汇总 ==========" -ForegroundColor Green
$anyOpen = $false
foreach ($r in $results) {
    if ($r.Ok) {
        $dir = Split-Path -Parent $r.File
        $mono = Join-Path $dir ([System.IO.Path]::GetFileNameWithoutExtension($r.File) + '.zh.mono.pdf')
        Write-Host ("[完成] {0}" -f $r.Name) -ForegroundColor Green
        if (-not $anyOpen) {
            Start-Process explorer.exe -ArgumentList ('/select,"{0}"' -f $mono)
            $anyOpen = $true
        }
    } else {
        Write-Host ("[失败] {0}  日志: {1}" -f $r.Name, $r.Log) -ForegroundColor Red
    }
}
Write-Host "==================================" -ForegroundColor Green
