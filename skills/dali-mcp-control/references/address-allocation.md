# 新增无地址灯具（0.39）

先检查服务实际列出四个 address_allocation 接口。已知64灯DT8测试板的B63删除后单次新增、独立全板原值核对已验证；DT6、多灯新增和故障路径仍主要是模拟证据。旧版本和其他客户端不会自动升级。

“新增地址”对明确通道的所有无短地址灯具执行一次 F001 mode2，保留既有地址。它不是重置、全新分配、按物理灯选择或删除后的自动撤回。expected_new_count 是结果校验数量，不能限制设备侧实际分配数量。不要根据空闲地址数推断无地址灯数；用户任务未明确通道或预计新增数量时，先查清再准备。

在已有授权范围内关闭 ConfigMaster，先处理未保存编辑；不强杀。调用 `dali_start_address_allocation_review(channel,expected_new_count)`，保存返回 operation_id，保持同一服务存活并用 `dali_get_address_allocation(operation_id)` 查询至 active_in_this_process=false。默认查询只含进度和数量；用 include_details=true 查看完整原参数、组、16 场景和输出备份。reviewed 只表示准备完成，不是已分配。计划自准备完成起 30 分钟内有效，快照不是原子采集。

核对范围、原值和任务授权后，在任务专用配置启用 `enable_experimental_address_allocation=true`、准确的 `allowed_address_allocation_digest` 和 `allowed_address_allocation_channels=[明确通道]`。以该 operation_id 和返回的 confirmation 调用 `dali_start_address_allocation` 一次。确认串是计划绑定机制，不要求用户重复批准已有授权。磁盘配置改变不代表旧常驻服务已更新。

只读准备到达 reviewed 且 worker 已退出后，可正常关闭准备会话，用更新后的任务配置启动新服务执行同一 operation_id；保留原状态目录和未过期计划。若客户端在只读准备阶段中断，先确认原进程已退出，再查原记录：只有无 claim、无 execution/写入意图，且明确只运行过 review 时，才能重新做只读准备。活动进程、未知执行结果或已有 claim 不能用新 ID 绕过；不能把一次观察超时当作进程退出。

执行前重读原值/地址；命令后独立扫描并核对原灯完整支持值和新增数量。保持服务存活直至 active_in_this_process=false，并读取完整证据。CLI 会自动等待后台任务结束，按原 operation_id 轮询，最后只取一次完整结果。仅 completed_readback 且没有 worker-error、log_error 或缺失执行记录才是成功；verified_receipt_unknown 保留丢回执事实，stopped_count_mismatch 不能靠再次寻址补数量。

`dali_cancel_address_allocation` 请求在发送前停止；已发出的原生寻址无法撤销或保证中断，工具继续必要读回并报告 cancel_effect。取消不删除新增地址。服务退出后的历史 phase 不证明仍在运行；已有 claim 永不重放，先查原记录和当前总线，不能换新 ID 绕过未知状态。

新增后重新核对地址与实际灯位，刷新相关能力缓存；旧项目、本地名称、平面图不会自动迁移。短地址和相同配置不是物理身份，不能据此保证删除后分配回原编号。任务结束关闭专用写门，保留原值、意图及读回证据。
