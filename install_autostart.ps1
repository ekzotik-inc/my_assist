# install_autostart.ps1
# Регистрирует бота как задачу Планировщика Windows:
#   - запуск при старте сервера (даже без входа в систему),
#   - автоперезапуск, если процесс упал.
#
# Запускать в PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА из папки проекта:
#   powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1

$ErrorActionPreference = "Stop"

$TaskName  = "DenisBot"
$ProjDir   = $PSScriptRoot
$RunBat    = Join-Path $ProjDir "run.bat"

if (-not (Test-Path $RunBat)) {
    throw "Не найден run.bat в $ProjDir"
}

# Действие: запустить run.bat в папке проекта.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$RunBat`"" -WorkingDirectory $ProjDir

# Триггер: при загрузке системы.
$trigger = New-ScheduledTaskTrigger -AtStartup

# От имени SYSTEM, чтобы работало без входа пользователя.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
    -LogonType ServiceAccount -RunLevel Highest

# Настройки: не глушить по времени, перезапускать при сбое каждые 1 мин.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 9999

# Перерегистрируем, если задача уже была.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "AI-ассистент Telegram Business (DenisBot)" | Out-Null

Write-Host "Задача '$TaskName' создана." -ForegroundColor Green

# Запустить прямо сейчас, не дожидаясь перезагрузки.
Start-ScheduledTask -TaskName $TaskName
Write-Host "Бот запущен. Проверь логи / статус командой:" -ForegroundColor Green
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
