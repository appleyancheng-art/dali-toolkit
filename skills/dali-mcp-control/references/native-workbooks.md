# 原生 Excel 证据导入和选定恢复

联合执行、查询、取消和条件撤回见 [工程恢复](project-delivery.md)；参数执行范围见 [参数](parameters.md)。

版本号表示接口引入及当时验证范围；先确认当前服务实际提供该接口。某接口未覆盖的类别需另走对应流程，不能从历史验收推断全部范围已完成。

## 导入来源与精度

原生Excel工程使用`dali_import_configmaster_workbook(xlsx_path)`读取一个明确的本机绝对路径，返回workbook_id；用`dali_get_configmaster_workbook(workbook_id,channel)`查看选定通道的参数、分组、名称和场景，省略ID只列最近10份摘要。工具会保留原文件字节及摘要，并要求文件DALI MAC匹配配置。这是工具包证据导入，不会向上位机导入或向灯具下发；原生文件不含IP/固件，也不能证明来自总线实时导出。通道空表中的地址占位行不代表在线灯。不要将返回记录伪造成实时采集工程来调用恢复接口。

原生Excel亮度保留厂家显示百分比，不能直接套通用数学转换当作原始值。`summary.percent_review`列出静态导入候选和所有精确显示候选，例如0.1%可能来自raw1～15，原值不唯一；该候选尚未经原生程序运行时往返验证。普通参数空白表示保持，场景分量空白表示MASK；单灯ECG和组场景内容分别保留，先审查scene_conflicts，不擅自选后组覆盖。0.24已验证两份实际上位机导出样本经标准MCP读取；导入本身不恢复设备。选定场景恢复见本文件“从原生Excel恢复选定场景（0.25）”，完整原生工程恢复仍未完成。

## 0.30：原生 Excel 联合恢复

当前服务提供 `dali_prepare_workbook_restore` 时，可用已导入的 workbook_id、明确 channel/addresses/fields/group_numbers/scenes，从原生 Excel 同时准备选定参数、场景和组成员。后三个列表允许为空以保持该类别，至少一类非空。选场景必须明确 scene_source=ecg 或 group；group 来源需额外指定单一 group0～15，不选场景则省略 scene_source 和 group。成员始终取分组表，不由组场景表推断；保留冲突清单，不自动合并两种场景来源。

普通参数空白保持，配对参数的空白分量使用设备新读原值；场景空白为 MASK，数字0为关灯。默认 brightness_policy=require_unique 拒绝亮度百分比丢失原始精度造成的歧义；只有任务明确接受厂家最近显示值量化时使用 native_nearest。只有参数且所有选定分量均为空时返回 no_targets_in_source，不连接网关，也不证明设备已匹配。

计划保存原始 XLSX 字节、来源选择和换算说明，并单独保存新读设备原值。它不是实时采集的工具包工程。执行前核对已导入记录的字节/摘要、所选来源、网关身份和当前原值；不会自动重读原路径中后来替换的文件。需要使用新文件时重新导入、准备新计划。源记录改变或丢失后禁止正向执行；已有执行的条件恢复使用随计划保存的原值和证据，不依赖外部 Excel 记录仍在。

执行、查询、取消和条件恢复按 [工程恢复的联合执行流程](project-delivery.md) 使用 `dali_start_project_restore` / `dali_get_project_restore` / `dali_cancel_project_restore` / `dali_prepare_project_restore_undo`，精确计划 digest 和各类别写范围仍必需。不是原子整工程导入；名称、本地界面缓存、布局、地址、网关设置和实时输出均不包含在该计划内。普通场景设计继续先实时预览，再按保存指令写场景。现场验证范围查看对应版本验收记录，不把模拟64灯×16场景测试当作全板现场写入。

原生来源联合恢复已完成B0最小实板往返：渐变4→5→4、场景15原值→raw254/4000K→原值、组15加入→移出；B0参数及16场景、64灯分组/输出均恢复，全关4000K。首次只读准备超时未写入，保留记录后以新连接/新计划完成。其他地址、全量联合写入和取消异常仍以模拟证据为限。

## 从原生Excel恢复选定名称（0.28）

调光曲线及当前九字段参数范围见 [参数](parameters.md)。

`dali_prepare_workbook_name_restore(workbook_id,channel,selections,group_name_source='grouping')`准备恢复指定本地名称。selections用{kind:'lamp',address}、{kind:'group',group}、{kind:'scene',group,scene}，同通道最多336项；不附加name覆盖源文件。灯名来自参数表、场景备注来自对应组场景块；组名默认来自分组表，选择group_scene需明确使用该组场景块名称，冲突会报告。空名称清除选中标签，不创建缺失灯行。后续apply/get/条件restore沿用原生文件工具，必须绑定实际缓存文件、关闭上位机、保存字节备份及检查源摘要和当前文件。新名称来源已完成B0灯名、组15名、组0场景5备注的实际写入，上位机重启后显示三个新名称，并通过MCP恢复原名。恢复后的实际文件核对通过，仅保留上位机正常lastSeen更新；第二次重启接口当时超时；0.30 doc1已通过Computer Use在资源管理器启动程序，重新连接并搜索64地址，确认A0/G15默认名称和组0场景5原备注，补齐恢复后UI显示证据。336标签及异常路径为模拟证据，不扩大为任意工程全部标签现场验收。

原生UI已经对一份实际总线导出文件完成仅本地导入：64参数、1024单灯场景及组成员与源文件目标一致，B0显示已核对，并精确恢复两份原文件。0.1%导入成raw1得到运行时确认，不能保证还原原始raw2。该样本未测试名称变更/清空、普通参数空白、场景表冲突、线性百分比和原生设备下发。旧章节的“运行时待验”按此具体范围更新，不能推断所有原生恢复能力均完成。

## 从原生Excel恢复分组和参数（0.26）

`dali_prepare_workbook_group_restore(workbook_id,channel,addresses,group_numbers)`从原生分组表选择1～64地址、1～16组位，保留未选地址和未选组位。不能用组场景表中的行或名称推断成员关系。准备新读身份、类型、完整成员和实时输出；已匹配时返回already_matches且无执行计划，否则得到现有分组计划。按既有授权用`dali_apply_group_membership`及精确操作开关执行，查询/条件撤回沿用分组工具。源记录、摘要、选定组位和目标受校验，读回冲突或丢回执停止，不自动重发。源文件仍不证明实时状态或来源真实性。

`dali_prepare_workbook_parameter_restore(workbook_id,channel,address,fields,brightness_policy?)`为一盏DT8灯恢复1～9个字段：fade_time、fade_rate、min_level、max_level、configured_min_ct、configured_max_ct、power_on、system_failure、dimming_curve。只选择用户要恢复的字段；原生普通参数空白保持当前值，配对上电/故障参数可只恢复非空的亮度或色温分量。所选分量全空时返回no_targets_in_source，无执行计划、无总线读取，不表示当前参数已匹配。默认require_unique拒绝百分比歧义；native_nearest需明确接受厂家静态量化目标，不能承诺精确原值或原生UI运行时一致。亮度255、渐变0和超出当前色温范围仍按现有事务限制拒绝，旧Excel不是扩大能力范围的依据。

参数准备读取全部支持参数及实时输出，保存源绑定和原值；用`dali_apply_lamp_parameter`及精确完整目标开关执行。配对参数的允许目标须包含随包保留分量，按proposal.steps[].after核对。获取记录/条件撤回沿用参数工具，撤回仍受原计划、已尝试字段和当前状态约束。这是单灯选定参数计划，不是多灯批量参数任务；分组和参数两个计划也不是原子整工程事务，不混入名称、寻址、网关设置或实时控制。

现场验证限B0：组0/1→临时加入15、渐变4/7→5/6，再分别通过上述两个原生Excel来源接口恢复组0/1及4/7。每步均读回，完整阶段核对64灯组成员、B0所有已支持参数/16场景及64灯输出，最终全部恢复、全关4000K。64×16组位、八字段组合、空白配对保留、百分比歧义和异常撤回为模拟证据；原生UI导入、全板批量参数和完整工程恢复未完成。

## 从原生Excel恢复选定场景（0.25）

先用原生Excel导入工具取得workbook_id，再用`dali_prepare_workbook_scene_restore(workbook_id,channel,addresses,scenes,scene_source,group?,brightness_policy?)`指定一个通道和地址/场景集合，支持64×16完整范围。scene_source必须明确选ecg或group；group方式还需指定单一组块0～15，且目标地址必须存在于该块，不能为凑范围更改组成员。各表内容有冲突时按用户明确选择的表生成目标，返回冲突清单，不自动合并或后组覆盖。地址和场景号均从0起始。

默认brightness_policy=require_unique：百分比只有唯一原始显示候选才接受；0.1%等歧义值及非原生显示百分比拒绝。用户意图包含接受厂家最近显示值量化时，可明确使用native_nearest，先查看native_workbook_review.percent_review；这不是精确原始值恢复承诺，也不是原生程序运行时导入验证。MASK和百分比0分别保留MASK/OFF。源文件仍是来源未验证的目标数据，不伪造实时工程采集。

准备时新读身份、DT8类型、限值和全部目标的当前场景原值，返回10分钟计划和确认串；全板全场景可能需数分钟，保持服务会话及足够超时。按现有保存授权和精确场景写范围调用`dali_apply_scene_batch`；执行前重新核对源记录/摘要/目标、当前身份和限值，再逐项检查存储原值，所有检查通过才写入并读回。该恢复不调用场景，不改变实时效果；正常场景设计仍先实时预览再保存。用`dali_get_scene_plan`读结果；需撤回本次写入时用`dali_preview_scene_restore`生成仅已尝试项的条件恢复计划，不删除意图记录后重发。不恢复参数、成员、名称、寻址或网关设置，不等同完整原生工程恢复。

现场证据限通道B地址0场景15：原MASK/MASK临时写195/4000K，再从实际原生总线导出XLSX的ECG表恢复MASK/MASK；新接口完成源绑定、写入和读回，全64灯场景15、B0已支持参数/16场景及全板输出均恢复，最后全关4000K。1024项、组表选择、百分比量化及异常撤回为模拟证据；未完成原生UI本地导入往返。

