# 工程快照、差异与分阶段恢复

原生Excel来源另读 [工作簿](native-workbooks.md)；多灯参数细节见 [参数](parameters.md)，平面图附件见 [名称与平面图](native-files.md)。

版本号表示接口引入及当时验证范围；先确认当前服务实际提供该接口。某接口未覆盖的类别需另走对应流程，不能从历史验收推断全部范围已完成。

## 0.37：设备与本地文件的分阶段工程恢复

仅在服务实际列出新接口时使用。`dali_prepare_project_delivery(project_id,addresses,fields,group_numbers,scenes,name_selections,mark_scope)` 从同一不可变工程准备两个阶段：先设备参数/场景/组成员，再本地名称和/或平面图。至少选择一项设备类别和一项本地类别；name_selections=[]保持名称，mark_scope省略保持平面图，其他选择语义沿用原接口。先验证并备份本地文件，再采集设备原值；名称与平面图合为一次JSON替换，避免两份旧文件计划互相冲突。准备不写设备或上位机文件。

用`dali_get_project_delivery`检查计划与children中的原值、范围、差异和子计划。按现有授权启用`enable_project_delivery=true`和准确的`allowed_project_delivery_digest`，同时保留设备子计划的`enable_project_restore`、准确`allowed_project_restore_digest`、参数/场景/组的明确允许范围；名称和平面图仍分别需要各自文件写开关。协调任务开关不能替代任何子项权限，也不能通过改摘要、改范围或刷新计划绕过冲突。

0.45起，expires_at约束任务开始，不是已启动任务的强制终止时间。长时间设备采集后，服务在核对最初文件及底图未变的前提下生成新的本地子计划；以最终返回的计划、子计划和摘要配置权限。按时进入执行的同一个活跃任务可在期限后继续后续阶段，每阶段仍核对权限、身份、原值和日志。保持原服务并查询原任务；过期但未启动的计划需要重新准备，进程退出或已领取的任务不能据此重放，独立子任务也不继承延期资格。本改动有延时模型及跨三次MCP连接的下发/撤回验证，不代表全64灯工程的实体验收。

查询结果含worker_error_in_this_process、worker-error、execution_unavailable或执行记录journal_error时，不能仅凭旧的completed_readback判断成功。保存完整错误结果并核对原任务及设备状态，不重新启动任务。worker_error_in_this_process只保存在当前服务内存中，退出前留存；Windows短暂占用的限次日志发布重试由服务处理，不重发设备指令。

`dali_start_project_delivery`启动后台任务；保持服务存活，查询completed_phases、total_phases、pending及children中的逐项进度，直到active_in_this_process=false。`dali_cancel_project_delivery`只请求在设备步骤/本地阶段之间停止，已发命令先读回，进行中的文件替换完成后再返回；取消不撤回已完成阶段。回执不明、本地写失败或末尾任一领域复核失败都保留partial状态，不把完成了一阶段称为整工程成功。被领取的任务不得重放，另一个新ID也不是重复命令的许可。

授权测试恢复或用户要求撤回时，用`dali_prepare_project_delivery_undo`，按实际尝试范围先撤回本地字段，再恢复设备。准备无写入；执行前重新核对两侧原值。新任务和设备子计划都有新摘要，需按其明确范围配置。第三方名称/底图/设备值冲突时停止；未尝试阶段不编造恢复操作。源工程被移除仍可使用计划备份；新PNG保留为证据。没有跨总线/文件原子性、自动回滚、寻址/网关修改、工具包网格激活或实时调灯。

0.37已完成本板B0最小联合往返：渐变时间4→5→4、场景15从MASK改为raw200/4000K后恢复、组15加入后移出；同时恢复并撤回三个本地名称和单一底图标记。两个阶段各自读回通过，完整重启上位机确认新名称/底图，再撤回并第二次完整重启确认默认名称/空画布。最终独立核对B0全部已支持参数和16场景、64灯地址/成员/输出与基线一致，全关4000K。保留正常界面缓存刷新字段和原图片；新增PNG保留为未引用证据。大范围及异常/取消仍以模拟测试为证据，不外推为全板实体工程验收或真实灯位核对。文件读回后仍需Computer Use重启上位机核对名称、底图和所选标记；completed_readback不代表UI或物理灯位已核对。正式包与已安装Skill以最近完成交付记录为准。

## 0.29：工程联合预检

仅在当前服务工具列表提供 `dali_preview_project_restore` 时使用。输入完整工具包工程的project_id、明确addresses、fields、group_numbers、scenes；后三个列表可以为空以省略对应类别，但至少选择一类。地址0～63、组和场景0～15，不自动扩大范围。一次串行连接读取全部所选灯的已支持参数、成员、实时输出和所选场景后，再统一比较；最多64灯×16场景。

返回rows列参数、组位、场景差异与rejections。场景同时显示fits_current_limits和fits_projected_limits；后者依据所选参数恢复后的限值，参数投影被拒绝时为null，不能当作场景通过。未选组位保持；未选场景不检查。reviewed只表示只读预检完成，reviewed_with_rejections不能挑出通过子集直接执行。该报告没有confirmation或plan_id，不能传给现有写入入口，也不证明原生完整工程恢复可用。源工程digest与网关绑定会在读取前后核对；stopped_partial保留pending和已读原值，不自动重试。

`dali_get_project_restore_review(review_id)`读取已存阶段，不等待总线锁；reading不证明进程还在运行。B0的9字段/16组位/16场景联合预检已现场只读通过，64灯全场景范围是模拟测试证据；联合执行与条件恢复的B0现场范围见下一节。名称、布局、寻址、网关设置和实时灯光不在本预检恢复范围。先核对当前服务版本；源码包和Skill更新不会自动升级旧常驻服务。

## 0.29：联合执行与条件恢复（实验功能）

`dali_prepare_project_restore(project_id,addresses,fields,group_numbers,scenes)`重新读取原值，生成明确参数/场景/组位计划；不要把只读review_id直接当成plan_id。参数先于场景、成员最后；逐步安排亮度/色温范围和配对上电/故障色温，不能盲目反转恢复顺序。已取得用户相应范围授权后，在任务配置中设置 `enable_project_restore=true`、`allowed_project_restore_digest`为该计划完整digest，同时按计划实际步骤配置现有参数批量、场景、分组各自的开关和精确允许范围；未涉及的类别不需开启。不要默认允许所有地址或所有场景。

`dali_start_project_restore(plan_id,confirmation)`启动后台执行；保持同一个服务器存活，用 `dali_get_project_restore` 查询 `active_in_this_process`、execution.state、completed_steps、total_steps及pending。一次性CLI已经会保持连接并轮询至worker退出。`dali_cancel_project_restore`请求在步骤间停止，已发命令先完成读回；返回取消请求不等于已停止。缺失执行记录或worker_error不算成功；进程退出后不自动重放，保留旧plan_id查询。

第一条写入之前检查全部目标原值与所有类别权限。每个类别/地址边界检查该灯完整所选范围；每条命令前后检查相关目标和实时输出，末尾再次检查全部所选范围。场景阶段只逐项重读相关场景，避免每次重复读取全16场景。其他客户端或上位机不参与此状态目录的锁，不能当作全网络原子事务。

回执不明只重连读取，不重发；配置读回不符或实时输出意外改变立即停止。取消/失败不会自动回滚。用户要求撤回或本次测试已包括恢复时，用 `dali_prepare_project_restore_undo(original_plan_id)`从原计划、claim和实际尝试前缀生成条件恢复计划：仅回退已尝试且当前仍为原值/本次目标的项目；其他配置、所选场景或实时输出出现外部变化时拒绝。原始参数范围随证据保存，恢复范围可回到该原始范围；外部工程文件被改动或移走不妨碍使用已绑定原始证据撤回。新恢复计划仍需自己的精确digest和各类别范围，之后用同一start入口执行。

联合执行与条件恢复已完成B0现场最小试验：渐变4→5→4、场景15原值→raw200/4000K→原值、组15加入→移出；B0全部支持参数/16场景以及全64成员/输出均恢复，全关4000K。仅B0三个步骤，取消/丢回执/其他异常及大范围仍为模拟测试证据；不得称为已经验证的完整原生工程恢复。普通单项场景预览/保存仍沿用已验收的快速入口，不绕到工程恢复流程。试验结束关闭任务写开关；旧常驻服务不会随本Skill自动重启或升级。

## 工程快照、差异与场景恢复

### 上位机本地名称和缓存

0.11增加 `dali_import_configmaster_cache(document)`，接收已明确选择的 ConfigMaster 1.2.4.1 单网关 JSON 缓存。先核对来源和网关身份；当前实现要求文件IP、DALI MAC、固件与服务配置一致，导入不会连接文件中的IP。已观察到 Windows 存储位置为 `%APPDATA%/ConfigMaster/ConfigMaster/gateways/<DALI-MAC>.json`，实际位置先查证，MAC文件名使用连字符。只读取对应网关，不批量收集其他工程。

导入保留完整原始JSON，并提取每通道灯名、组名及按组保存的场景名。`dali_get_configmaster_cache(cache_id)` 查名称和来源；指定ID及 `include_document=true` 返回保留的原生JSON，供用户明确要求的文件导出。省略ID列最近10份记录。名称不能跨通道或跨组混用；场景8仍是8，空名称与缺少名称原样保留，不写入默认显示名。

本地缓存里的地址键不证明灯具在线；参数/场景可能是旧值或占位零。`sceneKnown`仅作为原始缓存标记保留，不能把缓存零当作已验证OFF、把缓存灯具当作扫描结果，或从该接口直接生成设备恢复/场景写入目标。实时调试仍用现有总线读接口。导入不刷新能力缓存、不覆盖上位机文件、不恢复设备；名称回写使用下述独立文件事务；此JSON缓存接口不接收Excel或全网关汇总导出文件；Excel使用 [原生工作簿流程](native-workbooks.md)，完整原生工程恢复仍未完成。名称可能包含项目信息，仅在用户指定的范围分享。

### 名称草稿与冲突检查

名称改动使用 `dali_prepare_configmaster_names(cache_id,changes)` 生成本地计划。每项为 `kind=lamp/group/scene`、`channel`、`name`，再附 `address` 或 `group` 或 `group+scene`；名称最多30个UTF8字节，超长拒绝不截断，空串清除标签。只改已有缓存中的灯行，不新建灯具；组/场景名称键可按明确编号增加。`dali_get_configmaster_name_plan(plan_id,include_document=true)` 返回草稿JSON，其他配置字段原样保留。

准备应用文件之前，另行读取当前上位机缓存，传给 `dali_rebase_configmaster_names(plan_id,current_document)`。它保留当前文件中无关字段的变化；若目标名称被改成第三个值或灯行消失，则返回conflict、不提供合并草稿。已是目标名时不重复修改。此工具只比较调用者提供的JSON，不能证明文件刚读过；返回draft_ready也不表示已覆盖文件、界面已更新或数据已写入网关。草稿接口本身不写文件。需要保存名称时使用本文件“名称文件写回与恢复”中的独立文件事务，不能把草稿生成当作改名完成。

### 工具包总线快照

`dali_capture_project(name,channel,addresses,scenes)` 串行新读指定1～64地址的已支持参数（含分组）和指定0～15场景，最多64×16项场景。它不搜索、不修改地址，不点灯。采集不是原子快照，记录各字段时间；当前未覆盖物理CT极限、扩展渐变、设备版本、PC备注/工程文件、网关设置、布局或实时输出。全通道全场景可能数分钟，保持stdio进程及连接；CLI可用 `--timeout-seconds 1800`。超时后先查记录，不自动重采。

`dali_get_project()` 列最近记录及pending；传project_id返回完整JSON，可保存为工程导出。读取记录不等待总线锁，但reading只表示最后保存的阶段，不能证明原进程存活。stopped_partial保留已采集字段和失败位置；不能用于导入、差异或恢复。没有记录不等于空工程。

`dali_import_project(document)` 只把完整工具包JSON导入为一个新的本地记录，不连接文件内的IP、不恢复设备。校验和用于发现意外损坏，不验证来源真实性；导入标为来源未验证。它不兼容上位机原生工程格式。导出包含网关和现场配置，只随用户指定的交付范围分享。

`dali_diff_projects(before_id,after_id)` 比较同网关IP/MAC/固件和同通道的精确参数与场景值；忽略读取时间、回执封装以及DT6无定义的色温字节。范围不同会给出not_captured_before/after，不能把缺项当成0、离线或删除要求。差异本身不授权改设备。

用户要求恢复时，用 `dali_preview_project_scene_restore(project_id,addresses,scenes)` 明确选择已采集的1～64个地址/场景组合。它新读能力限制与当前原值，生成现有的10分钟场景计划；仅支持同配置网关的DT8场景存储。原工程中超出现灯具限制的值会拒绝，不截断。取得用户对该范围的恢复授权后，使用已有dali_apply_scene_batch及任务专用写入范围；保持原值备份、写前冲突检查和写后逐灯读回。不会恢复参数、组成员、地址、标签、网关设置或实时灯光。用户已明确要求恢复的范围无需再次重复询问。

### 从工程快照恢复选定分组

`dali_preview_project_group_restore(project_id,addresses,group_numbers)` 从完整工具包总线快照中取分组目标，重新读取选定地址的完整成员、类型和实时值。必须明确1～64个已采集地址以及1～16个组号，不能将未采集地址当作应删除成员。仅将选中的组位恢复为快照值，其他当前组位与其他地址保持；只有明确要求全部分组时才传0～15。它不是从上位机原生缓存恢复，也不恢复灯光、场景或其他参数。

网关身份、固件和灯类型需匹配；返回`already_matches`表示选定组位已经一致，没有生成执行计划，也不代表检查了未选范围。有差异则返回10分钟组计划，包含工程ID/摘要、范围、完整当前原值与实际步骤。用已有`dali_get_group_membership_plan`复核；按明确恢复授权及精确操作开关，通过`dali_apply_group_membership`应用。恢复到旧工程可能移出现有组，不能只理解成补加成员。

应用前仍核对完整成员及实时状态；即使未选组位在准备后改变，也会停住，不用旧原值覆盖。源工程文件改变会使计划失效。需要撤回本次工程恢复时，用`dali_prepare_group_restore`恢复本次操作前的成员，而不是再套另一个工程。快照摘要不验证来源真实性；本功能的多地址写入/撤回和DT6路径目前为模拟测试，现场只读检查不等同于恢复写入验收。

### 名称文件写回与恢复

用户要求直接保存上位机本地名称时，用0.22的文件事务，不能把草稿生成说成已改名。将经核对的**单网关名称缓存文件**绝对路径配置为 `configmaster_cache_file`，不用参数缓存或任意JSON；`dali_read_configmaster_file()` 从实际文件新读并返回文件SHA256、缓存ID和名称来源。该工具不读取设备或上位机内存，磁盘内容也不是灯具实时状态。

准备/写入/恢复要求Windows上所有ConfigMaster进程已退出。先处理用户原有未保存编辑，不擅自丢弃；服务只检查进程，不会自行强杀或启动上位机。关闭后 `dali_prepare_configmaster_file_names(changes)` 按既有kind/channel/address/group/scene/name格式生成10分钟计划，保留原始字节备份与精确名称差异。仅修改明确要求的标签，地址/场景号仍0起始。

在已获授权的任务配置中单独设置 `enable_native_cache_writes=true`，调用 `dali_apply_configmaster_file_names(plan_id,confirmation)`。它只操作绑定文件，不发网关命令；写前再次检查文件SHA256和进程，原生替换后逐字节读回。`written_verified`仅证明文件符合目标，重开上位机后还须核对显示。工具间的路径锁不阻止上位机或其他编辑器启动，执行期间保持它们关闭。不要通过改计划或删claim重试；每份计划只尝试一次。

`dali_get_configmaster_file_plan(plan_id)` 查备份摘要、claim和执行记录；saved phase不代表进程仍活着。`write_uncertain`、日志不完整或替换异常时先读文件和原记录，不重放。已授权撤销或测试包含恢复时，关闭上位机后调用 `dali_prepare_configmaster_file_restore(original_plan_id)`，再应用返回的新计划。它仅撤销实际改变的名称，目标已改成第三个值则整份拒绝；保留其他新数据和其他新名称，缺失与空标签不混为一谈。文件未发生其他变化时恢复原始字节；有无关变化则重建仅名称逆变更。`already_restored`不需要写入。任务完成关闭专用写开关。

PC1.2.4.1实际缓存已验证B0、组15及组0场景5三种名称：写入后完整重启，上位机树和批量页显示新名称；关闭、恢复后再次完整重启，空灯名/无组备注/原场景备注均正确。写入与恢复期间其他文件字段及独立参数缓存未变；上位机自行刷新的缓存字段保留。此能力不修改DALI场景、地址、分组成员或网关设置，不代表原生完整工程恢复。


