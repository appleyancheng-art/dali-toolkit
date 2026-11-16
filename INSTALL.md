# 给 Agent 的快速安装流程

目标：安装稳定版 MCP 与两个 Skill，并让当前客户端识别。默认不做设备测试，不跑完整回归。先读同目录 `release.json`；本流程不要求先分析源码。

## 1. 确定版本与本机路径

检查 Windows、Python 3.12、网络下载能力，以及客户端是否支持本地 stdio MCP。非 Windows 或无本机执行能力时报告不满足条件，不猜测替代安装。

查当前客户端中已有的 `dali_config` MCP、同用途的 DALI 服务和两个同名 Skill。已有安装转 [UPDATE.md](UPDATE.md)，不要新增重复服务。

读取 [GitHub Latest](https://api.github.com/repos/appleyancheng-art/dali-toolkit/releases/latest)：必须非 draft、非 prerelease，tag 与 `release.json` 一致。冲突时刷新两者一次；仍不一致则报告发布元数据尚未同步，不猜版本。只下载其中 `release.json.package.name` 指定的主安装包，校验 SHA256；主包已经包含两个 Skill，不必再下载源码包和独立 Skill 包。

建议新装目录（可按用户要求调整）：

```text
%LOCALAPPDATA%/DALI-Toolkit/
  versions/v0.45/DALI-Toolkit-0.45/   # 主 ZIP 解压后的程序
  data/default/config.json          # 与程序分离的现场配置
  data/default/state/               # 服务使用中的记录
  data/default/gateways/            # 搜索并选择网关后的配置与记录
  backups/                          # 更新前备份
  installed.json                    # 本机安装记录
```

下载和解压到新的目录，不覆盖已有目录。在运行文件前核对 ZIP 哈希；解压后以包内 `MANIFEST.sha256.json` 逐文件验证。主 ZIP 另有安装说明与接入示例，正式程序根目录以 `release.json.package.root` 为准。

## 2. 安装依赖并做离线检查

以下路径为示例，替换成已经确定的绝对路径。PowerShell 每条外部命令退出码非零就停止。

```powershell
$DaliRoot = Join-Path $env:LOCALAPPDATA 'DALI-Toolkit'
$DaliRelease = Join-Path $DaliRoot 'versions/v0.45/DALI-Toolkit-0.45'
$DaliMcp = Join-Path $DaliRelease 'mcp'
$DaliPython = Join-Path $DaliMcp '.venv/Scripts/python.exe'
$DaliConfig = Join-Path $DaliRoot 'data/default/config.json'

py -3.12 --version
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required' }
py -3.12 -m venv (Join-Path $DaliMcp '.venv')
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
& $DaliPython -m pip install -r (Join-Path $DaliMcp 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
& $DaliPython (Join-Path $DaliMcp 'tests/smoke_stdio.py')
if ($LASTEXITCODE -ne 0) { throw 'offline MCP check failed' }
```

0.45 成功标记为 `Stdio OK: 135 tools`。测试会检查非法参数拒绝，预期错误不等于最终失败。没有 `py` 时用已确认的 Python 3.12 可执行文件替代，不能用不明版本。

普通安装不要运行 `mcp/install.ps1`：0.45 的该脚本会运行完整离线回归，耗时较长。无需删测试或改服务器来提速；`smoke_stdio.py` 已提供安装检查。依赖失败时保留错误并检查包源，不取消锁定版本。

新装时创建配置父目录，仅在目标不存在时从 `mcp/config.example.json` 复制配置。示例不预设网关，存储写入关闭。已有配置不覆盖、不删除字段。服务的 `state/`、`gateways/` 跟随配置所在目录，不能只备份一个 JSON。

## 3. 注册 MCP

使用 `release.json.mcp` 中的 command/args 模板替换 `{release_root}`、`{config_path}`，全部填写绝对路径。`stdio` 由客户端拉起，不要另开重复服务。

通用 JSON 模板（合并同名条目，保留其他服务器）：

```json
{
  "mcpServers": {
    "dali_config": {
      "command": "C:/实际路径/mcp/.venv/Scripts/python.exe",
      "args": ["C:/实际路径/mcp/run_server.py", "--config", "C:/实际数据目录/config.json"]
    }
  }
}
```

**WorkBuddy：**优先使用其自定义连接器配置入口导入上述 command/args；若本机已存在并确认使用 `%USERPROFILE%/.workbuddy/mcp.json`，可先备份，再合并其中 `mcpServers`。该路径在本项目测试机存在，不代表所有版本统一使用它；不要套用 CodeBuddy Code 的目录。需要客户端“信任/启用”时完成其正常授权，再核对工具加载。

**Codex：**合并到已确认的用户配置 `~/.codex/config.toml`，保留其他内容；已有同名段就更新，不能重复追加：

```toml
[mcp_servers.dali_config]
command = 'C:/实际路径/mcp/.venv/Scripts/python.exe'
args = ['C:/实际路径/mcp/run_server.py', '--config', 'C:/实际数据目录/config.json']
startup_timeout_sec = 60
tool_timeout_sec = 2400
enabled = true
```

其他客户端按其支持格式导入，不把上面的 JSON/TOML 直接覆盖整个客户端配置。

## 4. 安装两个 Skill

从同一主包复制完整的 `skills/dali-mcp-control/` 和 `skills/dali-config-computer-use/`，包括 references。已有同名 Skill 先备份，不能混合不同版本。

WorkBuddy 可在“专家·技能·连接器”中上传本地技能包；需要 ZIP 时从主包分别打包这两个目录（顶层即技能目录）。如果当前客户端已明确提供可写技能目录，也可按其机制放置完整目录。Codex 新安装的个人 Skill 路径为 `~/.agents/skills/`；历史 `.codex/skills/` 已被识别时沿用现有位置，避免同名重复。

Computer Use Skill 不自带电脑控制工具或厂家上位机。没有这些能力时，说明“Skill 已安装，界面操作前置条件未满足”，不要冒称界面调试可用。

## 5. 验证并交付

重新加载 DALI MCP；在当前客户端确认工具列表及 `dali_status` 的版本为 `release.json.runtime_version`（0.45 共 135 工具）。`dali_status` 是本地状态，**不证明网关在线**。在 Skill 列表或新会话中确认两个名称可识别；当前会话不能刷新时明确标为待客户端重载验证。

在 `installed.json` 记录 runtime_version、release_tag、package_sha256、release_root、python_path、config_path、config_parent、客户端配置位置、两个 Skill 位置和验证结果。若采用用户自定目录，记录实际值，后续不要依赖默认路径猜测。

报告这五项即可：版本、程序/数据路径、MCP 加载结果、Skill 识别结果、未操作灯具。安装完成后停下；用户要求调试时再按 MCP Skill 搜索当前现场网关。

官方接入参考：[OpenAI MCP](https://learn.chatgpt.com/docs/extend/mcp) · [OpenAI Skill](https://learn.chatgpt.com/docs/build-skills) · [WorkBuddy 技能安装](https://cloud.tencent.com/document/product/1831/134432)。本流程未宣称已在所有客户端或全新电脑端到端实测。
