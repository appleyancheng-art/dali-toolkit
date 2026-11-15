# 上位机名称与平面图附件

名称缓存、名称草稿及文件提交/恢复见 [工程与文件事务](project-delivery.md)。文件读回后仍须按任务核对上位机显示。

版本号表示接口引入及当时验证范围；先确认当前服务实际提供该接口。某接口未覆盖的类别需另走对应流程，不能从历史验收推断全部范围已完成。

## 0.36：名称随工程移交与恢复

工程名称用 `dali_attach_project_names(project_id,cache_id,selections)` 附加为新schema4版本，旧工程不变；每项仅填kind和对应address/group/scene，通道由工程确定。灯具地址、场景号必须属于工程采集范围，组号明确选择；最多336项。附件保留名称来源缓存及其局部选择，缺失与空名称不同；不把缓存的参数占位值当作设备恢复目标。新版本保留原参数/场景、网格、底图。用 `dali_get_project_names` 查选定名称，用现有工程导入/导出移交，不依赖另一台服务存在源cache_id。

恢复选定名称用 `dali_prepare_project_name_restore(project_id,selections)`，只能选附件已有项。源缺失或空名称会清空所选当前标签，准备前要核对这符合任务；超长名称拒绝，不截断。关闭上位机后按现有 `dali_apply_configmaster_file_names`、查询及条件撤回流程执行，沿用独立名称文件写门。应用前复核工程摘要和附件，不能改用另一工程绕过冲突；撤回依赖计划原值，源工程移除后仍可核对恢复。文件回执不能替代UI重载核验，也不代表参数/场景/分组与本地标签已经原子联合恢复。0.36实证是两次独立SDK会话、真实归档工程/名称、工作区缓存副本的写入和精确撤回；实际AppData未写，实体板未操作。

## 0.32：工程平面图恢复到上位机

用 `dali_prepare_configmaster_floorplan(project_id,mark_scope)` 准备本地恢复；服务须显式绑定 gateways 目录中的 configmaster_cache_file，上位机须关闭。`captured_marks` 只取工程采集通道/地址，`all_source_marks` 明确包含原生来源里的范围外标记。两者都替换整个本地平面图，不与旧标记合并，不扩大任何灯具写范围。

准备保存原JSON、原底图（存在时）和工程来源。检查返回范围后，按本次已授权任务，用 `dali_apply_configmaster_floorplan` 提交准确计划确认串；独立开关 `enable_native_floorplan_writes=true` 不由名称或场景开关代替。执行先独占创建新PNG，再切换JSON引用，旧图片不覆盖；准备与提交之间源工程、缓存或底图改变会停止。相同已领取计划不重复执行，异常先查 `dali_get_configmaster_floorplan_plan`。

文件读回成功后，仍须重新打开上位机核对显示，需要时点适应窗口。撤回用 `dali_prepare_configmaster_floorplan_restore` 再提交：仅恢复 floorPlan，保留退出刷新产生的其他字段；平面图或图片被第三方改变时拒绝覆盖。只有当前JSON恰等于写入版本才恢复原字节。新建PNG在撤回或失败后保留作事务证据，不宣称文件目录完全复原；原图片丢失也不会自动重建。此流程不访问总线、不证明真实灯位、不等同完整工程恢复。

## 0.31：原生平面图与工程附件

先确认当前服务提供新入口。已有原生cache_id时，用`dali_inspect_configmaster_floorplan`查看底图文件名及通道/地址/x/y标记；坐标是底图宽高比例，不是像素或网格行列。缺少floorPlan与有底图但无标记不同；缓存地址和灯位仍不是现场核对结果。

用`dali_attach_project_floorplan(project_id,cache_id,image_path)`把底图和标记附到工程的新版本。image_path必须是用户明确选定的绝对本地PNG路径；不根据缓存里的文件名搜索或猜选图片。工具验证非隔行PNG、CRC、解压尺寸，限2MB、400万像素；文件名相同本身不能证明底图来源。返回的新project_id保持原参数/场景采集数据和已有网格布局，内嵌原生来源JSON及PNG原字节；只将工程采集通道/地址范围内的标记列为selected_marks，其余只作来源证据，不能扩大设备恢复范围。

用原有工程导入/导出在电脑间传递，或`dali_get_project_floorplan(project_id,include_image=true)`取出PNG base64。默认查询省略图片字节；导入工程不会把底图写入ConfigMaster，也不会生成已核对网格或发灯光命令。已有原生缓存记录的摘要格式保持不变。普通场景仍按已核对布局先预览后保存，不能把这个图像附件当成自动寻址结果。实际UI两灯保存样本已通过两个独立MCP会话传递，第二会话没有源缓存也能读回相同底图字节；这是文件交付验证，不是全原生工程恢复。

