# 百分比、图案、多场景、布局及保存任务

## 单灯调用已存场景（0.42）

用户指定某盏灯调用已有场景时，用`dali_recall_lamp_scene(channel,address,scene)`，通道1=A/2=B、地址0～63、场景0～15，均按用户原号传入。它只向该短地址调用一次，不扩成组或整通道，不写场景存储。当前支持DT8且已知渐变上限不超过7.75秒；先读灯类型、渐变、场景和原输出，保存原值再下发。普通设计场景仍先实时预览，不用调用替代场景设计。

分别检查receipt_status和verification_status；回执丢失时重新建立连接并读回，仍保留stopped_receipt_unknown，不重发。亮度255/色温65535的MASK分量按调用前原值核对；全MASK即使输出保持也不能证明调用生效。日志失败为outcome_unknown，不能当作干净成功。`dali_get_lamp_recall(operation_id?)`只查本地记录，省略ID取最近10项，不重播、不恢复；原值和待处理阶段都保留，记录不证明进程仍活着。

0.42在当前固件1.6.9测试板B0调用现有场景8，4.469秒内完成预检、回执和到位读回，输出raw254/6024K；独立读64灯确认其他63灯未变。另用实时输出恢复B0原值，全部64灯输出、组成员以及B0已支持参数和16场景与本轮基线相同，全关4000K。恢复属于测试脚本，本工具不会自动恢复。其他地址/通道及超时、MASK、日志失败为模拟测试；单次工具自身只读目标灯，不证明背景灯未变或物理地址唯一。

实时预览与保存顺序仍按主入口；工程采集及选定恢复见 [工程恢复](project-delivery.md)。

版本号表示接口引入及当时验证范围；先确认当前服务实际提供该接口。某接口未覆盖的类别需另走对应流程，不能从历史验收推断全部范围已完成。

## 百分比与图案准备

优先用 `dali_prepare_live_output(channel,changes,scene?)` 把每灯 `percent` 与可选 `kelvin_raw` 转为原始目标。它只读设备能力，不点灯、不保存；返回 `live_arguments` 可交给 `dali_set_live_output`。非零百分比只支持已确认的标准对数曲线0，并核对配置亮度上下限；0是OFF，低于0.1%的非零值、其他曲线及越界值会拒绝，不静默截断。量化百分比不是实测照度。换灯或改参数后刷新 profiles，包括亮度限制。

编排图案先 `dali_get_layout` 查项目布局；无合适记录时，用用户已明确的地址矩阵调用 `dali_create_layout`。矩阵每行从左到右、各行从上到下，null为空位，地址不可重复；记录绑定当前配置网关与固件。它不会自动辨认实物排列，换项目不能硬套旧布局。

`dali_compose_layout_output` 接受布局ID、foreground_percent、可选色温、8×8内置glyph X/M/N/B，或自定义同尺寸0/1字符串行；glyph与pattern_rows二选一。background必须明确：off生成图案外已映射灯具的OFF目标，preserve省略背景。根据用户意图选择，不扩大到未映射地址；输出列出前景、背景及不操作地址。内置字形是当前模板，不保证与历史手工保存图案完全相同。

准备后按用户授权执行 live_arguments、检查预览效果，再按保存请求存储。可选scene直接传0～15；返回scene_changes只是目标清单，不是捕获当前输出，不会保存。省略的分量在场景中成为MASK（亮度255、色温65535），表示调用时保持。若用户要求保存实际当前效果，应核对当前值或最近已验证预览结果，不能把未知实时值当成这些目标。准备结果中的样本地址不等于全板验证，也不自动切换成广播。

## 多场景、布局交接与恢复

0.14的保存、保存作业及差异预览最多接收1024个明确的通道/地址/场景组合，例如64灯×6场景=384项。使用同一个`dali_start_scene_save`提交完整授权目标，避免为每个场景反复启动进程；所有目标先备份，再串行写入和读回。`progress.scenes`逐场景统计总数、备份数、完成数和已尝试写入数。大批次的实际现场速度尚未验收，不能将模拟测试时长当作硬件速度。

`allowed_write_targets`兼容旧单对象，也支持多个`{channel,scene,addresses}`对象组成的列表，每项独立匹配，不把不同项的通道、场景、地址交叉组合。空列表拒绝所有写入；已有明确用户授权时只设置相应范围，不把所有16场景默认开放。场景读取和工程恢复预览原有64对限制仍适用，不混淆不同接口。

任务部分结束时先查旧job和plan，不换ID重做。`dali_preview_scene_restore(plan_id)`可为completed/stopped_partial/cancelled_partial记录中实际尝试写过的目标新读存储值并生成恢复预览，排除unchanged及尚未发送的目标。当前值既不是原值也不是尝试目标时返回restore_conflict，不提供可执行确认串。恢复不自动执行；只有用户要求恢复对应范围时才应用新计划，仍受写入范围及新读冲突检查约束。旧进程仍在运行或遗留applying状态时，不能用此接口假定已停止；先处理原作业并核对现场。

0.43.1起，`dali_apply_scene_batch`的执行结果将此前只读说明保存在`preview_metadata`。执行层`device_writes=true`表示已有写入意图记录，命令可能已发出，不证明收到回执或读回正确；`write_intent_count`统计这些意图。全部unchanged时为false/0。仍以state和逐项results判断，不把历史预览的false解释成恢复过程未写入。0.43旧记录有沿用预览只读标记的问题，不修改旧记录来掩盖它。

布局用`dali_export_layout`导出JSON、`dali_import_layout`导入新ID；同网关IP/MAC/固件绑定，校验和只防数据损坏，不证明来源或现场排列。导入不会覆盖旧记录，也不会继承现场已核对标记；布局可单独交接，也可按下节显式附到工程新修订；不是上位机原生平面图。

仅在用户或操作员确实核对过实物后，才用`dali_record_layout_check`提交当前digest、实际核对地址、说明和核对人。返回新修订ID，部分核对不会标全板通过；全部地址覆盖才标physical_mapping_verified，来源始终为human_attestation，automatically_verified=false。不能把地址清单、截图推测或一次导入自动记成人工验收。

实时操作等待期间，可在保持原MCP连接存活的同时，用`dali_get_live_operation`查询记录；指定地址用kind=live-output，广播用live-channel，组用live-group，省略ID列最近10项。单灯/批量和广播预检显示profiles/fade_bounds阶段、已完成数、待查询地址及cache/bus来源；组接口仍以pending记录阶段。读进度不等待总线锁、不重发控制；缓存命中阶段合并日志。保存的中间状态不证明进程仍活着，dispatched也仍须看samples_verified。一次性CLI另开进程不能查询正在使用同状态目录的服务器，应在原长连接查询或只读原记录。

0.15.1的三个实时输出工具支持标准MCP进度回调。通知message为JSON，含kind、operation_id、state、阶段内progress与已记录命令/验证数；外层progress是递增通知序号，不是百分比。通知来自已保存记录，可能合并或丢失，不能以通知条数、100或最后阶段代替最终返回及读回验证。它不新增总线查询；客户端超时、通知中断后保留原operation_id查询证据，不新建操作重试。没有订阅通知的客户端仍正常返回最终结果。

## 工程随附布局（0.17）

需要把地址排列随工程交付时，先有完整工具包工程和明确布局，再用`dali_attach_project_layouts(project_id,layout_ids)`选择1～16个本地布局。它不读总线、不重新采集参数；创建新的schema2工程修订，保持原采集参数、场景和时间，嵌入完整布局快照。网关IP/MAC/固件和通道须一致，布局地址必须全部在工程已采集范围内。不能为容纳布局自动扩大采集或恢复范围。

layout_ids是新修订包含的完整列表，会替代该新修订中的旧随附列表；源工程和本地布局均不覆盖。`dali_get_project`导出完整JSON，`dali_import_project`能在另一目录导入schema1/2工程。原来的人工核对声明保留为来源证据，`layout_context`明确不是当前现场已核对；导入工程不会自动安装可用于控制的布局。校验和只证明数据一致性，不认证来源。

确认要使用其中某个排列时，以工程内的source_layout_id调用`dali_import_project_layout(project_id,source_layout_id)`。返回layout中的新layout_id才是本地可用映射，physical_mapping_verified=false、不继承mapping_check；原项目核对记录仍保留在工程内。当前配置网关不匹配时拒绝提取，不能直接把旧现场排列套给新现场。需要实际核对时沿用dali_record_layout_check，仅记录用户或操作员确实完成的检查。

`dali_diff_projects`的layout_comparison另列来源名称/几何排列差异；缺少布局是not_captured，不等于空布局或删除要求。参数/场景differences仍按采集值比较。离线project_report.py会输出地址矩阵、空位和来源声明。新格式仍不是上位机原生工程/平面图，也不会恢复地址、成员、场景或实时灯光；旧版仅支持schema1的客户端会拒绝schema2，不能直接删字段冒充兼容。

## 长任务进度

需要保存进度或停止能力时，用 `dali_start_scene_save(request_id, changes)` 启动，再用 `dali_get_job` 查询。相同任务重连重查沿用同一个 request_id；相同 ID 不重复执行，换一个 ID 会成为新的写入任务，不能用来掩盖不确定结果。

stdio 客户端必须保持服务器进程存活，直到任务终态。不要启动任务后马上关闭一次性 Client。进程退出后未完成任务被标记 interrupted，不会自动恢复；通过 plan_id 查看原值及部分执行记录并读设备状态。

`dali_cancel_job` 仅请求在灯具操作之间停止。当前已发送写入会继续完成读回；cancel_requested 不等于已停止，仍需查询终态。completed、stopped_partial、failed、interrupted、cancelled_partial、cancelled_before_write 是终态。取消不会回滚已完成修改。

0.18现场取消验收：通道B地址0～15、场景15，目标raw254/4000K。观察到3项已写并验证后只请求取消一次，终态cancelled_partial，实际完成4项（地址0,1,2,3）；已发送项均完成读回，剩余项未写。再次提交相同请求ID和相同目标返回原任务，变更内容但沿用该ID被拒绝；独立读取64项确认没有额外变化。基于原计划生成仅已尝试项的恢复计划，应用一次后，全64项场景和实时输出与基线逐值相同，全部关闭/4000K，任务写开关已关闭。没有调用场景或发送实时调光命令。此结果不代替掉电、丢回执、强杀进程或其他设备的现场验收。

