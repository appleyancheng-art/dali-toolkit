"""Render a validated toolkit project JSON as a readable, offline text report."""
import argparse
import json
from datetime import datetime,timezone
from pathlib import Path
from dali_mcp.projects import validate_document


def render(document):
    d=validate_document(document);scope=d['scope'];g=d['gateway']
    stamp=lambda value:datetime.fromtimestamp(value,timezone.utc).isoformat(timespec='seconds')
    lines=[f"DALI 工程快照：{d['name']}",f"记录 ID：{d['project_id']}",
           f"网关：{g['ip']} / {g['dali_mac']} / 固件 {g['dali_firmware']}",
           f"通道：{scope['channel']}；地址范围：{','.join(map(str,scope['addresses']))}",
           f"场景范围：{','.join(map(str,scope['scenes']))}",
           f"读取开始：{stamp(d['started_at'])}；完成：{stamp(d['completed_at'])}",
           f"来源标记：{d.get('origin','unknown')}；校验和：{d['digest']}",
           '说明：这是指定范围的串行快照，不是同一瞬间的状态，也不是上位机原生工程。',
           '亮度显示为 DALI 原始值：0=关闭，1～254=非零输出，255=MASK（调用时保持）。',
           '色温 65535=MASK；DT6 的色温字节不作有效色温解释。场景存储不代表当前灯光。',
           '状态字段保留原始字节，不据此擅自判断故障类型。校验和不验证文件来源。','',
           '灯具参数（原始值）',
           '地址 | 类型 | 组号 | 亮度最小/最大 | 渐变时间码/速率码 | 配置色温范围 | 上电亮度/色温 | 故障亮度/色温 | 状态字节']
    def value(parameters,field,key):return parameters[field].get('value',{}).get(key,'不适用')
    for row in sorted(d['rows'],key=lambda r:r['address']):
        p=row['parameters'];v=lambda f,k:value(p,f,k)
        group=','.join(map(str,v('groups','groups'))) or '无'
        lines.append(f"{row['address']} | {v('light_type','name')} | {group} | "
            f"{v('min_level','level_raw')}/{v('max_level','level_raw')} | "
            f"{v('fade_time_rate','fade_time_code')}/{v('fade_time_rate','fade_rate_code')} | "
            f"{v('configured_min_ct','kelvin_raw')}～{v('configured_max_ct','kelvin_raw')} | "
            f"{v('power_on','level_raw')}/{v('power_on','kelvin_raw')} | "
            f"{v('system_failure','level_raw')}/{v('system_failure','kelvin_raw')} | {v('status','raw')}")
    lines+=['','场景汇总（仅包含本次采集地址）','场景号 | 非零亮度 | 关闭 | 亮度MASK | DT8色温MASK | 非零亮度地址']
    for scene in sorted(scope['scenes']):
        rows=[s for r in d['rows'] for s in r['scenes'] if s['scene']==scene]
        lit=sorted(r['address'] for r in rows if 1<=r['level_raw']<=254)
        off=sum(r['level_raw']==0 for r in rows);mask=sum(r['level_raw']==255 for r in rows)
        ctmask=sum(r['light_type_code']==1 and r['kelvin_raw']==65535 for r in rows)
        lines.append(f"{scene} | {len(lit)} | {off} | {mask} | {ctmask} | {','.join(map(str,lit)) or '无'}")
    if d.get('layouts'):
        lines+=['','工程布局（来源快照；不是当前现场已核对）',
                '行从上到下，列从左到右；点号表示空位。导入为本地布局后必须重新核对实物。']
        for layout in d['layouts']:
            lines.append(f"{layout['name']} | 通道{layout['channel']} | 来源ID {layout['layout_id']}")
            lines.append('来源核对声明：'+('完整' if layout['physical_mapping_verified'] else '未完整核对')+'；不继承为当前现场结论。')
            lines.extend(' '.join('.' if a is None else str(a) for a in row) for row in layout['address_grid'])
    if d.get('native_floorplan'):
        fp=d['native_floorplan'];im=fp['image']
        lines+=['','原生平面图附件（本地来源，未核对实物）',
                f"底图：{fp['summary']['image_reference']}；{im['width']}×{im['height']}；SHA256 {im['sha256']}",
                '底图原始PNG字节已嵌入工程。坐标为底图宽高比例，不能直接当作屏幕像素或网格行列。',
                f"工程内标记 {len(fp['selected_marks'])} 个；另有 {fp['outside_capture_count']} 个仅保留在来源证据中。",
                '通道 | 地址 | x比例 | y比例 | 是否位于底图范围']
        lines.extend(f"{m['channel']} | {m['address']} | {m['x']} | {m['y']} | {m['within_image_bounds']}"
                     for m in fp['selected_marks'])
    if d.get('native_names'):
        lines+=['','原生名称附件（本地来源；不是设备参数或在线证明）','范围 | 原文；缺失与空名称分别保留']
        for entry in d['native_names']['entries']:
            label=json.dumps(entry['selection'],ensure_ascii=False,sort_keys=True)
            value=json.dumps(entry['name'],ensure_ascii=False) if entry['present'] else '（来源未设置，恢复时清空选定标签）'
            lines.append(label+' | '+value)
    lines+=['','总线采集未包含的内容（本地附件另列）：'+', '.join(d.get('not_included',[])),
            '本报告只读导出文件，不连接网关，也不执行任何恢复。参数、组成员和场景须按明确授权范围分别或联合建立恢复计划；本地名称、平面图使用独立文件事务。']
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True,help='Raw project document returned by dali_get_project')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    document=json.loads(args.input.read_text(encoding='utf-8-sig'))
    text=render(document)
    with args.output.open('x',encoding='utf-8') as f:f.write(text)
    print(str(args.output))


if __name__=='__main__':main()
