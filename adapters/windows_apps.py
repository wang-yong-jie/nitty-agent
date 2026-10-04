"""Windows 应用目录查询；固定只读脚本通过显式注入的命令执行器运行。"""

import base64
import json
from pathlib import Path
from typing import Callable

from contracts import AgentCancelled
from validation import validate_app_name


# 名称以 UTF-8 Base64 数据传入，不能拼成 PowerShell 代码或通配表达式。
_PREAMBLE = r"""
$appQuery = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__QUERY__'))
function Normalize-AppName([string]$value) {
    return [regex]::Replace($value.ToLowerInvariant(), '[^\p{L}\p{Nd}]', '')
}
$appNeedle = Normalize-AppName $appQuery
function Test-AppName([string]$value) {
    return (Normalize-AppName $value).Contains($appNeedle)
}
$appRows = [Collections.Generic.List[object]]::new()
$appErrors = [Collections.Generic.List[string]]::new()
"""

_SOURCES = {
    "start_apps": r"""
Get-StartApps -ErrorAction Stop | ForEach-Object {
    if ((Test-AppName $_.Name) -or (Test-AppName $_.AppID)) {
        $appRows.Add([pscustomobject]@{name=$_.Name; app_id=$_.AppID})
    }
}
""",
    "appx": r"""
Get-AppxPackage -ErrorAction Stop | ForEach-Object {
    if (Test-AppName $_.Name) {
        $appRows.Add([pscustomobject]@{
            name=$_.Name; package_family_name=$_.PackageFamilyName
            install_location=$_.InstallLocation; package_status=[string]$_.Status
        })
    }
}
""",
    "shortcuts": r"""
$appRoots = @(
    [Environment]::GetFolderPath('Programs'),
    [Environment]::GetFolderPath('CommonPrograms'),
    [Environment]::GetFolderPath('DesktopDirectory'),
    [Environment]::GetFolderPath('CommonDesktopDirectory')
) | Where-Object { $_ } | Select-Object -Unique
foreach ($appRoot in $appRoots) {
    try {
        if (-not (Test-Path -LiteralPath $appRoot -ErrorAction Stop)) { continue }
        Get-ChildItem -LiteralPath $appRoot -Recurse -File -Filter '*.lnk' -ErrorAction Stop |
            ForEach-Object {
                if (Test-AppName $_.BaseName) {
                    $appRows.Add([pscustomobject]@{name=$_.BaseName; shortcut_path=$_.FullName})
                }
            }
    } catch { $appErrors.Add("${appRoot}: $($_.Exception.Message)") }
}
""",
    "uninstall": r"""
$appRoots = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
)
foreach ($appRoot in $appRoots) {
    try {
        if (-not (Test-Path -LiteralPath $appRoot -ErrorAction Stop)) { continue }
        Get-ChildItem -LiteralPath $appRoot -ErrorAction Stop | ForEach-Object {
            $appKey = $_.PSPath
            try {
                $appInfo = Get-ItemProperty -LiteralPath $appKey -ErrorAction Stop
                if ($appInfo.DisplayName -and (Test-AppName $appInfo.DisplayName)) {
                    $appRows.Add([pscustomobject]@{
                        name=$appInfo.DisplayName; install_location=$appInfo.InstallLocation
                        registry_key=$appKey
                    })
                }
            } catch { $appErrors.Add("${appKey}: $($_.Exception.Message)") }
        }
    } catch { $appErrors.Add("${appRoot}: $($_.Exception.Message)") }
}
""",
}

_EPILOGUE = r"""
[pscustomobject]@{
    matches=@($appRows | Select-Object -First 30)
    errors=@($appErrors | Select-Object -First 10)
    truncated=($appRows.Count -gt 30 -or $appErrors.Count -gt 10)
} | ConvertTo-Json -Depth 5 -Compress
"""


class WindowsApplicationCatalog:
    """按来源查询，单个失败不遮蔽其他来源；不会扫描全盘或自动启动程序。"""

    def __init__(
        self, run_command: Callable[..., dict], *, timeout: int = 10,
        cancel_check: Callable[[], None] | None = None,
    ):
        if not callable(run_command):
            raise ValueError("应用查询必须提供命令执行器。")
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise ValueError("每个来源的查询超时须为 1～30 秒。")
        self.run_command = run_command
        self.timeout = timeout
        self.cancel_check = cancel_check or (lambda: None)

    def find(self, name: str) -> dict:
        name = validate_app_name(name)
        encoded = base64.b64encode(name.encode("utf-8")).decode("ascii")
        matches, checked, errors = [], [], []
        for source, query in _SOURCES.items():
            self.cancel_check()
            script = _PREAMBLE.replace("__QUERY__", encoded) + query + _EPILOGUE
            try:
                result = self.run_command(script, workdir=str(Path.home()), timeout=self.timeout)
                problems = []
                if result.get("timed_out"):
                    problems.append("查询超时，检查未完成")
                if result.get("exit_code") != 0:
                    problems.append(f"命令退出码：{result.get('exit_code')}")
                if result.get("truncated"):
                    problems.append("命令输出被截断")
                if result.get("stderr", "").strip():
                    problems.append(result["stderr"][:1000])
                # 不把超时或损坏的输出当作完整的空结果。
                if problems:
                    raise ValueError("；".join(problems))
                payload = json.loads(result["stdout"])
                if not isinstance(payload, dict):
                    raise ValueError("查询结果不是对象")
                rows, source_errors = payload.get("matches"), payload.get("errors")
                if (not isinstance(rows, list) or not isinstance(source_errors, list)
                        or not all(isinstance(e, str) for e in source_errors)
                        or type(payload.get("truncated")) is not bool):
                    raise ValueError("查询结果缺少 matches/errors/truncated")
                if any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                       or not row["name"].strip() for row in rows):
                    raise ValueError("应用候选缺少有效名称")
                # 同名不同来源的记录保留，以便模型交叉核实；不自动选中或启动。
                for row in rows[:30]:
                    candidate = {**row, "source": source}
                    if candidate not in matches:
                        matches.append(candidate)
                errors.extend({"source": source, "message": e[:1000]} for e in source_errors[:10])
                truncated = payload["truncated"] or len(rows) > 30 or len(source_errors) > 10
                if truncated:
                    errors.append({"source": source, "message": "候选或错误过多，结果被截断；请缩小名称范围"})
                if not source_errors and not truncated:
                    checked.append(source)
            except AgentCancelled:
                raise
            except Exception as error:
                errors.append({"source": source, "message": f"{type(error).__name__}: {error}"[:1500]})
            self.cancel_check()
        return {
            "query": name,
            "status": "found" if matches else ("incomplete" if errors else "not_found"),
            "matches": matches,
            "checked_sources": checked,
            "attempted_sources": list(_SOURCES),
            "complete": not errors,
            "errors": errors,
            "scope": "当前用户开始菜单应用、AppX 包、用户/公共快捷方式和卸载注册信息；不覆盖所有便携应用或其他用户。",
        }
