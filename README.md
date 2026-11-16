# DALI Toolkit

让 AI 通过 MCP 调试 DALI 灯具，或通过 Computer Use 操作 DALI Config Tool。

**当前稳定运行版本：0.45，135 个 MCP 工具、两个 Skill。适用 Windows + Python 3.12。**

## 直接交给 Agent

首次安装，复制这句话：

> 请安装 https://github.com/appleyancheng-art/dali-toolkit 。先读仓库根目录 AGENTS.md，按 INSTALL.md 安装稳定版 MCP 和两个 Skill，完成离线验证并接入当前客户端。已有配置要保留，安装时不要操作灯具。

后续更新，复制这句话：

> 请按 https://github.com/appleyancheng-art/dali-toolkit 的 UPDATE.md 检查并更新稳定版。保留现场配置、状态记录、备份及其他 MCP；相同版本不重复安装，新版本验证成功后再切换，失败恢复旧版本。

**Agent 阅读入口：**[AGENTS.md](AGENTS.md) → [安装](INSTALL.md) 或 [更新](UPDATE.md)。下载地址、SHA256、版本和启动参数集中在 [release.json](release.json)。普通安装不需要阅读全部源码或开发历史。

## 安装内容

| 内容 | 用途 |
| --- | --- |
| MCP 服务 | 搜索网关、读取设备、预览亮度色温、配置场景及已支持参数 |
| `dali-mcp-control` | 指导 Agent 直接通过网关调试 |
| `dali-config-computer-use` | 指导 Agent 通过上位机界面操作；客户端需另有 Windows Computer Use 能力 |

支持本地 stdio MCP 的客户端可接入。WorkBuddy 和 Codex 的接入方式见 [INSTALL.md](INSTALL.md)。客户端授权、Python 和网络环境仍需满足；仅提供链接不能让不支持 MCP 或本机执行的 Agent 获得这些能力。

## 下载与使用

- [稳定版下载](https://github.com/appleyancheng-art/dali-toolkit/releases/latest)：主安装 ZIP 已包含两个 Skill，也提供独立 Skill ZIP。
- 新现场先搜索并选定网关；设计场景先实时预览，再按用户要求保存。
- 地址 **0–63**、场景 **0–15**；“场景 8”就是场景 8。
- 本版本现场验证基于固件 1.6.9 的 DT8 测试板，不是通用厂家网关驱动。完整工程实板恢复尚未验收，0.45 原生底图恢复准备有已知限制，见 [版本说明](RELEASE-0.45.md)。

## 需要深入了解时

[协议](docs/PROTOCOL.md) · [验收范围](docs/VALIDATION.md) · [开发历史与旧使用说明](DEVELOPMENT-HISTORY.md)

本次只简化安装与更新入口，运行代码和已发布的 v0.45 安装包保持不变。硬件功能开发仍暂停。后续已验收版本通过 Release 发布，并同步更新 release.json；不要把 main 上的任意提交当作新版安装包。
