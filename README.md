# DALI Toolkit 0.13.2

一个MCP服务、两份独立Skill：通过网关直接调试，或用Computer Use操作上位机。当前45个MCP工具，247项维护源码测试通过。版本证据及历史验收范围见 [验收记录](docs/VALIDATION.md)。

0.13.2根据独立现场反馈改善CLI错误提示、结果摘要及离线状态标识，并给未开放的实验参数执行器加入同目录跨进程互斥和持久化一次性执行声明。未增加设备写入工具；多场景作业、布局交接及实时预检进度仍待完善。

0.13新增参数差异与工程选定参数恢复的只读预览，并通过单地址现场读取验证；没有开放参数写入接口。0.13.1修复标准客户端在服务启动失败或会话关闭失败时未完整记录错误的问题，保留已收到的结果、原任务ID和未确认请求，不自动重发。整板双参数预览已完成两条路线的效果验收，但严格同条件控制速度对照尚未通过。

一个基于 ConfigMaster 网关已观察协议的 MCP 服务，加两个 Codex Skill：直接通信调试，以及通过 Computer Use 操作 Windows 上位机。当前验证范围为网关固件 1.6.9 的 DT8 测试板；不是通用 DALI 网关兼容承诺。

目录：`mcp/` 服务、CLI 和测试；`skills/` 两条独立操作路线；`docs/` 协议范围和验收记录。

## 安装与启动

目前在 Windows、Python 3.12 上验证。依赖固定到本次已运行的版本。

```powershell
cd mcp
.\install.ps1 -Python python
Copy-Item config.example.json config.json
```

将 `config.json` 的示例 IP、网卡 IP 和 MAC 替换为实际目标，确认固件版本。示例地址不能用于现场连接。默认存储写入关闭；不要仅为解决报错而解除身份校验或开放写入。

```powershell
.\.venv\Scripts\python.exe run_server.py --config config.json
```

客户端注册时使用上面 Python、服务脚本及配置的绝对路径，transport 为 stdio。两份 Skill 可分别安装到所用 Codex 的技能目录；Computer Use 路线还需要当前环境提供可用的 Windows UI 工具。它不依赖 MCP 来代替界面操作。

普通命令也可通过标准 MCP CLI 执行：

```powershell
.\.venv\Scripts\python.exe client.py --config config.json --tool dali_status --output status-check.json
```

参数从 JSON 文件传入 `--arguments`。输出文件须是新路径，防止无意重复执行。CLI 不自动启用写入、不重发控制命令；保存任务会保持服务器存活并轮询到终态。

CLI预检服务、配置、参数文件及输出路径。错误默认显示简短提示，`--debug`才输出完整Python堆栈；错误记录仍保留完整工具响应和嵌套原因。正常stdout为JSON摘要，包含tool/state以及适用的rows_count、layouts_count、results_count和result_status_counts；退出码0为客户端正常且未发现已定义失败状态，2表示错误/不成功结果，130表示用户中断。摘要中的returned不等于设备验证成功。`dali_status`仅回显本地配置，connectivity=not_checked、network_traffic=false，不能作为网关在线证明。

客户端日志的`client_error`表示调用端失败，不等于设备没有变化。`failure_phase=connecting`且`request_intent_count=0`表示尚未准备任何工具请求；`calling_tool`的pending只表示已记录请求意图，不能证明已发送或已执行。调用关闭阶段失败仍保留已收到的result和calls，不能据此重发。保存任务查询出错时保留原job_id，先查原记录；不换ID重做。`returned`仅表示客户端正常返回，设备操作是否成功仍看result中的state和验证字段。若磁盘同时拒绝保存错误日志，原异常保留并附注日志错误；旧记录不能据此判定进程仍活着。


## 45 个 MCP 工具

| 用途 | 工具 |
|---|---|
| 布局与目标准备 | `dali_create_layout`, `dali_get_layout`, `dali_prepare_live_output`, `dali_compose_layout_output` |
| 状态与身份 | `dali_status`, `dali_discover` |
| 现有地址与分组 | `dali_scan_existing`, `dali_read_groups` |
| 读取 | `dali_read_actual`, `dali_read_lamp_profiles`, `dali_read_lamp_parameters`, `dali_read_scenes` |
| 参数只读预览与记录 | `dali_preview_parameter_changes`, `dali_preview_project_parameter_restore`, `dali_get_parameter_review` |
| 实时预览 | `dali_set_live_output`, `dali_set_channel_live_output` |
| 按组实时预览 | `dali_set_group_live_output` |
| 找灯与记录 | `dali_identify_lamp`, `dali_get_identification` |
| 调用已存场景 | `dali_recall_scene`, `dali_recall_group_scene`, `dali_get_group_recall` |
| 保存场景 | `dali_save_scene_batch`, `dali_preview_scene_batch`, `dali_get_scene_plan`, `dali_apply_scene_batch` |
| 工程快照与恢复计划 | `dali_capture_project`, `dali_get_project`, `dali_import_project`, `dali_diff_projects`, `dali_preview_project_scene_restore`, `dali_preview_project_group_restore` |
| 组成员事务 | `dali_prepare_group_membership`, `dali_apply_group_membership`, `dali_get_group_membership_plan`, `dali_prepare_group_restore` |
| 原生缓存 | `dali_import_configmaster_cache`, `dali_get_configmaster_cache` |
| 名称草稿 | `dali_prepare_configmaster_names`, `dali_get_configmaster_name_plan`, `dali_rebase_configmaster_names` |
| 长任务 | `dali_start_scene_save`, `dali_get_job`, `dali_cancel_job` |

通道1=A、2=B；地址0～63，场景0～15，直接传显示编号。原始亮度不是百分比，0是关闭、254是最大、255是场景MASK；色温65535是MASK。两种MASK均不是关灯命令。

先用实时输出预览，用户认可并要求保存后再存储。通道广播预览与dali_recall_scene作用于整条通道，不能替代子集操作；dali_recall_group_scene只作用于核对后的明确整组。组预览必须明确整组范围及预期完整成员，先重新扫描/读成员，范围不符即停止。它不修改组成员，不调用或保存场景；已完成当前测试板组1的实灯验收。服务串行请求；不要与上位机同时占用总线。

找灯是单个明确地址的短暗/短亮流程，随后恢复原亮度，各阶段读回。只用已验证的实时亮度命令，不改色温、场景、组或渐变；不声称复刻上位机专用闪烁协议。未知/长渐变或变化中的实时输出会在控制前拒绝。默认保持2秒，允许1～5秒。发生异常先停止进一步找灯，再尝试读回与恢复；检查`restore_verified`。进程强制退出或通信故障可能阻止恢复，记录可用`dali_get_identification`读取，不自动重播。磁盘在控制后失败时仍尝试原亮度恢复，并通过返回结果报告日志错误。

保存前读取全部目标原值，逐项写后读回，等效未变化项跳过。现场状态、日志和备份在配置同级 `state/`。`backup_seconds` 包括连接和备份准备；`timing_seconds` 区分写回执等待与读回，总时长还包含日志及其他工作。

场景保存/应用的results[].status契约：verified表示写入后读回符合；unchanged表示新读原值已符合、成功跳过写入；outcome_unknown表示结果尚未验证；verification_failed表示读回不符。整批成功须同时满足state=completed、结果数等于全部请求目标且每项为verified或unchanged。部分终态、缺失项和未知结果不可当作成功，也不可盲目重发。已确认效果并授权保存时优先dali_save_scene_batch，避免不必要的preview+apply重复预检；需要单独审阅差异时才拆开。当前每次最多64个地址/场景组合，配置范围的scene仍为单个编号；尚无六场景384项的一体化作业。

色温在已验证DT8设备上按整数mired量化：mired=1000000//requested_K，典型读回K=1000000//mired。例如3000→3003、3500→3508、4000→4000；边界/钳位仍取决于灯具。判断等效时先排除MASK等哨兵值，再比较1000000//requested_K与1000000//actual_K。独立读取不知道历史请求，不能凭读回伪造requested_kelvin。原始65535是场景MASK，不代入此公式。

保存任务使用持久化 request_id 防重复，取消在灯具操作之间生效，已发写入仍完成读回，不自动回滚。stdio进程退出会中断任务；重启标记 interrupted，不自动续写。同一状态目录只有一个任务进程所有者，但没有与上位机共享的跨程序锁。

网关回执只表示接收；检查 `receipt_status`、`verification_status` 和读回结果。抽查不能宣称全灯逐一通过。超时先核对现场，不盲目重发。

## 百分比与图案准备

优先用 `dali_prepare_live_output(channel,changes,scene?)` 把每灯 `percent` 与可选 `kelvin_raw` 转为原始目标。它只读设备能力，不点灯、不保存；返回 `live_arguments` 可交给 `dali_set_live_output`。非零百分比只支持已确认的标准对数曲线0，并核对配置亮度上下限；0是OFF，低于0.1%的非零值、其他曲线及越界值会拒绝，不静默截断。量化百分比不是实测照度。换灯或改参数后刷新 profiles，包括亮度限制。

编排图案先 `dali_get_layout` 查项目布局；无合适记录时，用用户已明确的地址矩阵调用 `dali_create_layout`。矩阵每行从左到右、各行从上到下，null为空位，地址不可重复；记录绑定当前配置网关与固件。它不会自动辨认实物排列，换项目不能硬套旧布局。

`dali_compose_layout_output` 接受布局ID、foreground_percent、可选色温、8×8内置glyph X/M/N/B，或自定义同尺寸0/1字符串行；glyph与pattern_rows二选一。background必须明确：off生成图案外已映射灯具的OFF目标，preserve省略背景。根据用户意图选择，不扩大到未映射地址；输出列出前景、背景及不操作地址。内置字形是当前模板，不保证与历史手工保存图案完全相同。

准备后按用户授权执行 live_arguments、检查预览效果，再按保存请求存储。可选scene直接传0～15；返回scene_changes只是目标清单，不是捕获当前输出，不会保存。省略的分量在场景中成为MASK（亮度255、色温65535），表示调用时保持。若用户要求保存实际当前效果，应核对当前值或最近已验证预览结果，不能把未知实时值当成这些目标。准备结果中的样本地址不等于全板验证，也不自动切换成广播。

全关场景无需绕过准备接口。例如 `{"channel":2,"changes":[{"address":0,"percent":0}],"scene":1}` 生成实时目标level_raw=0（不发色温），同时生成场景目标level_raw=0/kelvin_raw=65535。全64灯须明确列出0～63。准备输入省略色温，不能传65535：它是场景存储MASK而不是有效的实时色温。布局是现场资料，安装包故意不包含旧state；当前工程导出尚不包含布局，也没有人工核对确认接口。

独立现场日志的整通道双参数示例：首次服务总耗时82.016秒，其中预检75.235秒；热缓存7.031秒。另起会话全64实时读取25.749/25.881秒。仅作特定设备的量级参考，不承诺每次速度。实时预检目前没有CLI阶段进度，保持足够超时（默认单调用300秒），超时后先查记录；禁止用并发查询替代串行预检，此网关曾出现并发读丢结果。

## 工程快照、差异与场景恢复

`dali_capture_project(name,channel,addresses,scenes)` 串行新读指定1～64地址的已支持参数（含分组）和指定0～15场景，最多64×16项场景。它不搜索、不修改地址，不点灯。采集不是原子快照，记录各字段时间；当前未覆盖物理CT极限、扩展渐变、设备版本、PC备注/工程文件、网关设置、布局或实时输出。全通道全场景可能数分钟，保持stdio进程及连接；CLI可用 `--timeout-seconds 1800`。超时后先查记录，不自动重采。

`dali_get_project()` 列最近记录及pending；传project_id返回完整JSON，可保存为工程导出。读取记录不等待总线锁，但reading只表示最后保存的阶段，不能证明原进程存活。stopped_partial保留已采集字段和失败位置；不能用于导入、差异或恢复。没有记录不等于空工程。

`dali_import_project(document)` 只把完整工具包JSON导入为一个新的本地记录，不连接文件内的IP、不恢复设备。校验和用于发现意外损坏，不验证来源真实性；导入标为来源未验证。它不兼容上位机原生工程格式。导出包含网关和现场配置，只随用户指定的交付范围分享。

`dali_diff_projects(before_id,after_id)` 比较同网关IP/MAC/固件和同通道的精确参数与场景值；忽略读取时间、回执封装以及DT6无定义的色温字节。范围不同会给出not_captured_before/after，不能把缺项当成0、离线或删除要求。差异本身不授权改设备。

用户要求恢复时，用 `dali_preview_project_scene_restore(project_id,addresses,scenes)` 明确选择已采集的1～64个地址/场景组合。它新读能力限制与当前原值，生成现有的10分钟场景计划；仅支持同配置网关的DT8场景存储。原工程中超出现灯具限制的值会拒绝，不截断。取得用户对该范围的恢复授权后，使用已有dali_apply_scene_batch及任务专用写入范围；保持原值备份、写前冲突检查和写后逐灯读回。不会恢复参数、组成员、地址、标签、网关设置或实时灯光。用户已明确要求恢复的范围无需再次重复询问。

`dali_preview_project_group_restore(project_id,addresses,group_numbers)` 则只恢复明确地址及组号的成员位，其他当前成员保持不变。它要求完整工具包工程、同网关/固件/灯类型，重新读取成员与实时状态；全部匹配时返回already_matches、不生成执行计划。有差异时生成现有10分钟组计划，使用独立组写开关和精确操作范围授权后应用；写前任何组位或灯光漂移均会停止。源工程摘要和选择范围保存在计划内并重新验证，不接受原生缓存占位值。撤回本次恢复使用dali_prepare_group_restore，恢复本次操作前值。多地址写入和DT6仅模拟验证；不扩大为参数、地址或原生工程的整体恢复。

导出完整项目JSON后，可以离线生成中文摘要：

```powershell
.\.venv\Scripts\python.exe project_report.py --input project.json --output project-summary.txt
```

摘要保留原始值，区分关闭与MASK，不把存储场景当作当前输出；不连接设备。

## 验证与边界

`install.ps1` 只运行本地模拟网关测试及离线 stdio 检查，不扫描或控制真实设备。显式只读现场检查：

```powershell
.\.venv\Scripts\python.exe tests\smoke_stdio.py --live --config config.json --channel 2 --addresses 0 63
```

现有地址扫描用固定只读模式，最长可能约4分钟；成功返回本次网关扫描位图，超时/错误返回scan_unknown，不能解释成空通道。组查询返回所请求短地址的成员组，部分地址结果不能代表完整组成员。只读查询不诊断地址冲突和详细灯具故障。

组成员写入与恢复已接入，现场限单地址/组15试验；工程中的选定组位恢复和参数候选另有各自验证范围。参数写入、寻址、复位、升级、上位机文件备注回写及完整原生工程恢复仍未实现。原生缓存读取/名称草稿、工具包JSON工程、选定场景与组位恢复计划、选定参数只读恢复预览已支持。DT6和其他固件未实灯验收。具体证据和未完成项见 [验收记录](docs/VALIDATION.md)。

`dali_read_lamp_parameters` 每次1～16地址，按字段保留新鲜读回和时间戳：类型、状态原始值、渐变代码、亮度上下限、上电/故障参数、曲线、配置色温上下限、组成员。失败返回具体待处理字段及部分结果，未知不填0。它不测试掉电/故障行为，不包含物理CT极限、扩展渐变或版本，不刷新独立能力缓存；实时值与场景仍由各自工具读取。

本包不含厂家可执行文件、对象文件、安装包、用户网关配置和现场日志。本包用于私有仓库交付，未附开源许可证；不包含厂家程序的再分发授权。

## 组成员与名称流程

组成员变更先prepare，changes每项含address/add/remove；组号0～15。核对差异后按已授权范围启用任务配置enable_experimental_group_writes，allowed_group_write_operations逐项包含channel/address/group/member。member是布尔值，false移出。用计划确认串apply，一次性声明阻止重复执行；get可在执行中读取持久化阶段，但阶段不是进程存活证明。失败后不重发；restore先新读并生成独立计划，只恢复有原值和已尝试记录支持的差异。详情见MCP Skill。

原生缓存导入只接受已观察的ConfigMaster1.2.4.1单网关JSON，核对服务配置IP/MAC/固件，不连接文件指定地址。名称草稿按channel/address或channel/group/scene定位，最多30UTF8字节，超长拒绝而不截断。rebase接收调用者提供的当前JSON，保留无关更新、拒绝同一名称的并发变化；draft_ready不是已应用，也不证明源文件仍未变化。完整JSON导出可能含项目信息，按用户指定范围分享。

## 原亮状态找灯补充验证

0.12原亮状态找灯现场补验：通道B地址0临时设置raw229后，独立读取64地址确认仅该灯亮度改变、全部色温保持。找灯工具依次发送raw0、254、229，13.969秒完成，回执、各阶段读回和原亮度/色温恢复均通过；随后再读取64地址，全部与找灯前点亮状态一致。最后仅将地址0关闭，再读64地址，全部亮度/色温与最初全关基线一致。没有场景、参数、分组或色温写入；异常分支仍为模拟验证，不表示现场故障注入通过。
