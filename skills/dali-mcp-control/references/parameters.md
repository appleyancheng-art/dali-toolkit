# 单灯和批量参数、工程参数来源

与场景/组成员联合恢复时读 [工程恢复](project-delivery.md)；原生Excel来源见 [工作簿](native-workbooks.md)。

版本号表示接口引入及当时验证范围；先确认当前服务实际提供该接口。某接口未覆盖的类别需另走对应流程，不能从历史验收推断全部范围已完成。

## 只读诊断（0.43）

当前工具列表提供`dali_diagnose_lamps(channel,addresses)`时，可用它串行新读1～16个明确地址的已支持参数和实时输出，并汇总类型、范围或未知值不一致。新项目仍先搜索并选定网关；诊断不会搜索地址、不改灯、不自动修复，也不刷新能力缓存。用户问完整地址清单时仍用现有地址扫描，不能用少量诊断地址冒充整通道在线清单。

`state=completed`表示所选读取流程完成，不表示灯具没有故障。检查finding_count及每行assessment.findings；no_inconsistency_observed仅指本次数值比较，状态位保持raw_status且status_bits_interpreted=false。OFF低于配置最小亮度不算异常；逐项读取可能跨越渐变或其他操作，范围差异应再结合现场判断。物理色温极限、扩展渐变时长、设备版本、布线和地址唯一性均未验证。

connection_failed或stopped_partial保留pending和uncompleted_addresses；未读字段是未知，不是零，也不因查询超时判离线或自动重试。`dali_get_diagnostic(operation_id?)`只读本地记录，省略ID取最近10项，不证明当前在线或任务仍运行。已在固件1.6.9测试板B0/B63完成实际标准MCP读取及记录查询，输出保持关闭/4000K。状态位仍只保留原值；没有实际故障注入，不把正常返回当作故障诊断能力已全面验证。

## 当前支持字段及调光曲线

当前支持九个字段：fade_time、fade_rate、min_level、max_level、configured_min_ct、configured_max_ct、power_on、system_failure、dimming_curve。只选任务明确要求的字段；下文历史八字段实测不扩展为九字段全部组合的现场证明。

先以当前服务工具列表及dali_status核对版本；已安装旧服务不会因源码更新自动升级。第九个参数字段是dimming_curve：单灯准备传field="dimming_curve",value=0（对数）或1（线性）；批量或精确写范围使用values={raw:0/1}。参数写入后能力缓存失效；不能在切到线性后继续套对数百分比，当前非零百分比编排仍只支持已确认的对数曲线。未知原曲线不生成不可恢复的写计划。F0A4状态0表示曲线保存成功，仍需独立读回；不同于色温/上电字段的状态0=处理中。

外部上位机修改曲线不会自动使本服务缓存失效；先调用`dali_read_lamp_profiles`刷新明确目标，再准备百分比。本板UI曲线0→1→0后，独立MCP已验证刷新为1时非零百分比准备拒绝，恢复并刷新为0后50%准备为raw229；这两次只准备目标，没有点灯。

单灯、工程来源、原生Excel来源及批量参数现在支持最多9个字段；已有八字段历史验收范围不因此扩展。现场已验证B0曲线0→1→0，B0全部支持参数/16场景及64灯组成员/输出复核恢复。没有测量线性曲线的实际光输出函数，全板曲线写入也没有现场验收。

## 多灯参数批量执行（0.27）

多盏灯的参数存储修改用`dali_prepare_parameter_batch(channel,targets)`，明确1～64地址，每灯1～9个字段。targets为`[{address,changes:[{field,values}]}]`，字段沿用单灯九类参数；code、level_raw、kelvin_raw、曲线raw均为原始值，不是百分比或秒。普通批量修改无需虚构工程来源。配对上电/故障参数省略分量保持当前值。渐变0、亮度255、当前不可恢复原值和无能力证据的色温范围扩展仍拒绝。

工程来源批量恢复分别用`dali_prepare_project_parameter_batch(project_id,channel,addresses,fields)`和`dali_prepare_workbook_parameter_batch(workbook_id,channel,addresses,fields,brightness_policy?)`。保留原文件摘要/来源绑定；原生空白保持、全空地址明确跳过，默认require_unique拒绝亮度百分比歧义，native_nearest需明确接受厂家静态量化。工程内容不是当前状态或物理能力证明。准备阶段读取所有实际目标的完整支持参数与实时输出，生成10分钟计划，不写设备。

按现有授权调用`dali_start_parameter_batch(plan_id,confirmation)`。专用配置除`enable_experimental_parameter_writes=true`外，还需`allowed_parameter_batch_targets=[{channel,address,values:[{field,values:完整目标值}]}]`，逐地址匹配entries[].proposal.steps[].after；配对值包括随包保持分量。原有单地址allowed_parameter_write_targets不能开启批量写入，不在循环里改全局配置或扩大地址范围。结束关闭任务写开关。

start返回仅表示启动，应保持服务存活并轮询`dali_get_parameter_batch(plan_id)`。执行先核对全部目标并落盘，再逐灯串行写入、每字段完整读回；执行前及长预检后核对来源，每灯写前再次检查原参数与输出。进度见execution.pending、completed_steps、total_steps和steps；active_in_this_process只表示本进程worker存活，不能用保存的writing推断进程仍在运行。步骤仅一次发送，丢回执、输出意外变化或冲突即停，不自动重发或回滚。completed_readback只证明各项读回时状态，不是原子全板快照，也不证明实际渐变时长或断电/故障表现。

`dali_cancel_parameter_batch(plan_id)`请求本进程worker在已发命令读回后停止后续写入；请求不是已取消，也不自动恢复。等待active_in_this_process=false后检查结果。进程退出、超时、缺日志或claimed记录不能作为重新执行许可。恢复用`dali_prepare_parameter_batch_restore(plan_id)`：新读原选择的全部地址，仅恢复有发送意图且当前仍为目标值的字段；第三值、无关参数/实时输出变化拒绝。返回新计划或already_original，按精确原值范围另行start。未经尝试的字段不写；CT范围恢复只使用原执行证据。

现场已验证B0/B1：两灯渐变4/7→5/6，四次串行写入和逐项读回，再通过批量条件恢复回到4/7；两灯所有支持参数/32项场景、64灯组成员和实时输出前后核对，最终全关4000K。64×8全量往返、取消、丢回执、两类工程来源批量恢复及崩溃声明为模拟证据，未声称全板参数现场写入或上位机同速。完整原生工程/原生UI导入/寻址与网关设置仍未完成。

## 单灯参数读取

需要查看单灯配置时，用 `dali_read_lamp_parameters(channel,addresses)`，每次1～16个地址。它从总线新读类型、原始状态、渐变时间/速率代码、亮度上下限、上电/故障亮度及DT8色温、曲线、配置色温上下限和所属组。不同字段有各自时间戳，不是原子快照。中途失败时看 `pending.address/field` 和部分参数，缺失字段不可当作0。DT6不适用的色温字段不会查询；DT6仅有离线解析测试，未实灯验收。255/65535保持原始哨兵值，不换算成百分比或温度；参数读回不证明断电/故障条件下的实际表现。物理色温极限、扩展渐变和设备版本未包含；实时值、场景另用对应工具。此读取不刷新独立的能力缓存，需要刷新缓存仍用 `dali_read_lamp_profiles`。

## 参数修改与工程参数恢复的只读预览

0.13新增 `dali_preview_parameter_changes(channel,targets)`：targets为1～64项 `{address,changes:[{field,values}]}`，每灯明确1～9个字段。支持fade_time/fade_rate的原始code、min_level/max_level的level_raw、power_on/system_failure的level_raw与kelvin_raw，以及configured_min_ct/configured_max_ct的kelvin_raw、dimming_curve的raw。普通修改中，上电/故障只传一个分量会保留另一个当前分量；渐变代码不是秒数。工具新读原参数及实时值，列出原值、目标和恢复候选，不控制灯具、不生成可执行参数计划。

`dali_preview_project_parameter_restore(project_id,addresses,fields)` 从完整工具包工程选择已采集地址与上述字段。只比较选择范围；选择power_on/system_failure意味着恢复该字段的亮度和色温两个分量，fade_time/fade_rate各自独立。新读当前值后保留未选字段；旧工程的色温上限不能证明当前灯具的物理极限，扩大当前配置色温范围仍会拒绝。当前仅支持DT8；导入工程摘要不验证来源真实性。

用 `dali_get_parameter_review(review_id?)` 获取本地记录或最近10条摘要，不占总线锁。`reviewed`仅表示预览已完成，`reviewed_with_rejections`须查看各行错误，`stopped_partial`保留已读字段与pending；缺值不是零。保存的reading阶段不证明进程存活，不因超时自动重做。review_id不能交给写入工具；无确认串，候选载荷不能当作发包指令。参数review不等于可执行计划；独立写入接口及其历史验收范围见下一节，当前第九字段按本文件“当前支持字段及调光曲线”处理。

## 单灯参数写入与恢复（0.19）

`dali_prepare_lamp_parameter(channel,address,field,value)` 新读一盏DT8灯的全部已支持参数和实时输出，准备一个参数的10分钟计划，不写设备。field/value范围如下：fade_time、fade_rate用原始code1～15，min_level、max_level用原始亮度1～254，configured_min_ct、configured_max_ct用Kelvin1000～10000，power_on、system_failure用原始亮度0～254。不是百分比或秒数。上电/故障字段仅改亮度，原色温含65535均保持；本接口不能修改配对色温，原亮度255目前拒绝，以免生成不能恢复的计划。旧fade_time接口仍只接受渐变时间。

`dali_prepare_power_parameter(channel,address,field,level_raw?,kelvin_raw?)`准备一个上电或故障参数对，field只能是power_on或system_failure。至少提供一个分量：level_raw为0～254原始亮度，kelvin_raw为配置范围内1000～10000K或原始MASK值65535；省略/null的分量保持刚读出的原值，两个分量也可同时修改。不改变实时输出，不代表已实际断电或触发故障。原亮度255仍拒绝；原有限色温若落在当前配置范围外也拒绝，以免生成无法恢复的计划。使用同一套dali_apply_lamp_parameter/get/restore接口，执行范围必须包含合并后的完整亮度、色温对；只授权kelvin_raw而漏掉随包保留的亮度会拒绝。完整目标见proposal.steps[].after。

原值和目标都须可恢复，最小/最大值保持有序。普通色温限制计划只允许在当前配置范围内缩小；不能用配置限值冒充物理能力并向外扩展。恢复计划可以基于原执行计划的原范围证据恢复该次修改，移除/篡改恢复关联不能绕过检查。Kelvin请求值与量化读回值分别保留；不把原始2702K擅自取整成2700K写入。

按用户已授权范围，任务专用配置启用`enable_experimental_parameter_writes=true`，并将`allowed_parameter_write_targets={channel,address,values:[{field,values:完整目标值}]}`限定到计划`proposal.steps[].after`。例如上电亮度目标必须包含`{level_raw:2,kelvin_raw:65535}`这两个实际分量；只允许亮度而未声明随包保留的色温会拒绝。色温限制用`{kelvin_raw:3003}`，渐变用`{code:6}`。这些只是格式例子，应以本次计划为准。把plan_id和confirmation交给`dali_apply_lamp_parameter`；既有修改/恢复授权无需再问，结束关闭任务开关，不改变常驻默认值。

执行前核对身份、原参数和实时输出，落盘一次性声明及写意图；单次发送后完整读回并使目标地址能力缓存失效。色温、上电/故障的状态0表示保存进行中，服务等待末次回执再查询。completed_readback仅证明本次存储/输出读回，不证明实际渐变时长、断电或故障行为。丢回执只读协调，stopped_receipt_unknown即使读值符合也保留不确定，不重放。限制修改导致实时输出改变时停止，不自动补发点灯命令。

`dali_get_lamp_parameter_plan(plan_id?)`只读本地记录，不占总线锁；缺失执行记录或保存的中间状态不证明进程存活，也不是重试许可。`dali_prepare_lamp_parameter_restore(plan_id)`新读后准备仅原字段恢复；任一配对分量的第三值、无关参数改变或没有工程绑定的多字段祖先会拒绝，already_original无需写入。恢复使用新计划和精确原值范围，不自动恢复意外改变的实时输出。

0.19配对色温现场验收：通过标准MCP stdio在B0逐项执行上电、故障色温MASK→4000K→MASK；分别保持原始上电亮度0、故障亮度229。每次写入和恢复均等待末次回执并完整读回，两项均completed_readback。最终B0全部已支持参数、16场景和全64灯实时输出与本轮新读基线逐值一致，64灯全部关闭/4000K；会话已关闭、任务专用写开关已关闭。没有实际断电、故障注入或实时控制命令。现场证据限这两个指定值往返；其他值、原亮度255和物理触发效果不在本轮验证范围。

固件1.6.9的B0已完成八类指定值核心执行器试验并恢复：时间4→5→4、速率7→6→7、亮度下限2→3→2/上限254→253→254、色温下限2702→3003→2702/上限6535→6000（读6024）→6535、上电亮度0→2→0、故障亮度229→195→229。后四项的早期进行中回执失败记录保留，0.17.1修复后通过。上电/故障现场色温为MASK；非MASK分量保持及其他值/设备主要为离线覆盖，不能外推为实际断电/故障验收。正式接口八字段经合成网关和标准stdio验证；本版现场客户端验收见交付记录。

## 从工程快照执行参数恢复（0.23）

用户要求恢复工程中的灯具参数时，使用`dali_prepare_project_parameter_restore(project_id,address,fields)`。一次选择同一工程中已采集的一盏DT8灯和1～9个明确字段，通道由工程决定；不把未选字段或其他地址加入操作。准备阶段新读全部已支持参数与实时输出，保存当前原值、工程ID/摘要和10分钟计划，不写设备。选择上电/故障字段会恢复亮度和色温两个分量；渐变时间和速率分别选择。工程摘要不验证来源真实性，旧工程色温范围不证明当前物理能力，不能借此扩大当前配置范围。原始渐变0、亮度255仍不支持。

复核计划的选定字段、目标值和实际执行顺序，按已有授权启用该地址及`proposal.steps[].after`完整目标范围，再调用`dali_apply_lamp_parameter`。允许有工程绑定的多字段计划，普通单字段接口不因此开放任意多字段原始计划。上下限按有效中间范围排序，每项单次写入后完整读回；原配置或实时值变化在写前停止，写中异常或输出变化停止后续项，不自动重发、续跑或回滚。工程源文件变更使计划失效，应保留原工程记录。

用`dali_get_lamp_parameter_plan`查询计划和执行记录。需要撤回本次工程恢复时，`dali_prepare_lamp_parameter_restore(plan_id)`只为已尝试且读回可确认的字段生成新计划，目标是本次操作前的原值；第三值或无关配置变化会拒绝。恢复依赖原工程、计划和执行记录，不删除这些依据。参数恢复不恢复场景、分组、地址、标签或实时灯光，也不等同完整上位机原生工程恢复。

0.23现场证据限B0渐变双字段：采集工程4/7，临时改5/6后，经新入口恢复4/7；两项均逐项读回通过，其他已支持参数、16场景及64灯输出保持，最终全关4000K。第一次准备前F021只读查询超时，未发送写入；失败记录保留，重新完整读回后才开始试验。八字段组合、限值排序、丢回执后的部分撤回及冲突拒绝另有模拟测试，不能据此声称所有组合都已现场验收。

