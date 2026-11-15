"""SDK startup check. Default is isolated and offline; --live needs explicit targets."""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true")
    parser.add_argument('--config',type=Path)
    parser.add_argument('--channel',type=int,choices=[1,2])
    parser.add_argument('--addresses',type=int,nargs='+')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.live and (not args.config or args.channel is None or not args.addresses):
        parser.error('--live requires --config, --channel and --addresses')
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root))
    from dali_mcp import __version__
    temp=tempfile.TemporaryDirectory(prefix='dali-smoke-')
    config=Path(temp.name)/'config.json'
    data=(json.loads(args.config.read_text(encoding='utf-8-sig')) if args.live else
          {'gateway_ip':'127.0.0.1','local_ip':'127.0.0.1','listen_port':19502,
           'expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9'})
    data['enable_experimental_writes']=False
    data['enable_native_cache_writes']=False
    data['enable_native_floorplan_writes']=False
    data.pop('configmaster_cache_file',None)
    config.write_text(json.dumps(data),encoding='utf-8')
    params=StdioServerParameters(command=sys.executable,args=[str(root/"run_server.py"),"--config",str(config)])
    report={}
    async with Client(params) as client:
        listing=await client.list_tools()
        report["tools"]=[t.name for t in listing.tools]
        for name,arguments in [
            ('dali_start_address_allocation_review',dict(channel=True,expected_new_count=1)),
            ('dali_start_address_allocation_review',dict(channel=2,expected_new_count=0)),
            ('dali_start_address_allocation',dict(operation_id='../bad',confirmation='yes')),
            ('dali_get_address_allocation',dict(operation_id='../bad')),
            ('dali_cancel_address_allocation',dict(operation_id='../bad'))]:
            assert (await client.call_tool(name,arguments)).is_error
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint==(name=='dali_get_address_allocation')
            assert hints.destructive_hint==(name=='dali_start_address_allocation')
        report['address_allocation_invalid_inputs_and_annotations']=True
        assert len(report["tools"])==135
        for name,arguments in [
            ('dali_diagnose_lamps',dict(channel=True,addresses=[0])),
            ('dali_diagnose_lamps',dict(channel=2,addresses=[64])),
            ('dali_get_diagnostic',dict(operation_id='../bad'))]:
            assert (await client.call_tool(name,arguments)).is_error
            assert next(t for t in listing.tools if t.name==name).annotations.read_only_hint
        report['diagnostics_invalid_inputs_and_annotations']=True
        for name,arguments in [
            ('dali_recall_lamp_scene',dict(channel=True,address=0,scene=8)),
            ('dali_recall_lamp_scene',dict(channel=2,address=64,scene=8)),
            ('dali_recall_lamp_scene',dict(channel=2,address=0,scene=16)),
            ('dali_get_lamp_recall',dict(operation_id='../bad'))]:
            assert (await client.call_tool(name,arguments)).is_error
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint==(name=='dali_get_lamp_recall')
        report['lamp_recall_invalid_inputs_and_annotations']=True
        for name,arguments in [
            ('dali_prepare_address_removal',dict(channel=True,address=0)),
            ('dali_prepare_address_removal',dict(channel=2,address=255)),
            ('dali_apply_address_removal',dict(plan_id='../bad',confirmation='yes')),
            ('dali_get_address_removal',dict(plan_id='../bad'))]:
            assert (await client.call_tool(name,arguments)).is_error
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint==(name=='dali_get_address_removal')
            assert hints.destructive_hint==(name=='dali_apply_address_removal')
        report['address_removal_invalid_inputs_and_annotations']=True
        for name,arguments in [
            ('dali_prepare_project_delivery',dict(project_id='../bad',addresses=[0],fields=['fade_time'],group_numbers=[],scenes=[],name_selections=[dict(kind='lamp',address=0)])),
            ('dali_start_project_delivery',dict(plan_id='../bad',confirmation='no')),
            ('dali_get_project_delivery',dict(plan_id='../bad')),
            ('dali_cancel_project_delivery',dict(plan_id='../bad')),
            ('dali_prepare_project_delivery_undo',dict(plan_id='../bad'))]:
            assert (await client.call_tool(name,arguments)).is_error
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint==(name=='dali_get_project_delivery')
            assert hints.destructive_hint==(name=='dali_start_project_delivery')
        report['project_delivery_invalid_inputs_and_annotations']=True
        for name,arguments in [
            ('dali_attach_project_names',dict(project_id='../bad',cache_id='a'*32,selections=[])),
            ('dali_get_project_names',dict(project_id='../bad')),
            ('dali_prepare_project_name_restore',dict(project_id='../bad',selections=[]))]:
            assert (await client.call_tool(name,arguments)).is_error
        report['project_names_invalid_inputs_rejected']=3
        for name in ('dali_attach_project_names','dali_get_project_names','dali_prepare_project_name_restore'):
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint==(name=='dali_get_project_names') and hints.destructive_hint is False
        for name,arguments in [
            ('dali_prepare_address_metadata',dict(address_plan_id='../bad',components=['lamp_name'])),
            ('dali_prepare_address_metadata',dict(address_plan_id='a'*32,components=['parameters'])),
            ('dali_apply_address_metadata',dict(plan_id='../bad',confirmation='yes')),
            ('dali_get_address_metadata',dict(plan_id='../bad')),
            ('dali_prepare_address_metadata_restore',dict(plan_id='../bad')),
            ('dali_migrate_address_layout',dict(address_plan_id='../bad',layout_id='a'*32))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        assert next(t for t in listing.tools if t.name=='dali_apply_address_metadata').annotations.destructive_hint is True
        assert next(t for t in listing.tools if t.name=='dali_get_address_metadata').annotations.read_only_hint is True
        report['address_metadata_invalid_inputs_rejected']=6
        for name,arguments in [
            ('dali_prepare_address_move',dict(channel=True,source_address=0,destination_address=63)),
            ('dali_prepare_address_move',dict(channel=2,source_address=0,destination_address=255)),
            ('dali_prepare_address_move',dict(channel=2,source_address=0,destination_address=0)),
            ('dali_apply_address_move',dict(plan_id='../bad',confirmation='yes')),
            ('dali_get_address_move',dict(plan_id='../bad')),
            ('dali_prepare_address_restore',dict(plan_id='../bad'))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        assert next(t for t in listing.tools if t.name=='dali_apply_address_move').annotations.destructive_hint is True
        assert next(t for t in listing.tools if t.name=='dali_get_address_move').annotations.read_only_hint is True
        report['address_move_invalid_inputs_rejected']=6
        for channel in (0,3,True):
            bad=await client.call_tool('dali_read_duplicate_addresses',dict(channel=channel))
            assert bad.is_error
        assert next(t for t in listing.tools if t.name=='dali_read_duplicate_addresses').annotations.read_only_hint is True
        report['duplicate_invalid_channels_rejected']=3
        for name,arguments in [
            ('dali_prepare_configmaster_floorplan',dict(project_id='0'*32,mark_scope='captured_marks')),
            ('dali_apply_configmaster_floorplan',dict(plan_id='../bad',confirmation='yes')),
            ('dali_get_configmaster_floorplan_plan',dict(plan_id='../bad')),
            ('dali_prepare_configmaster_floorplan_restore',dict(plan_id='../bad'))]:
            result=await client.call_tool(name,arguments)
            assert result.is_error
        assert next(t for t in listing.tools if t.name=='dali_apply_configmaster_floorplan').annotations.destructive_hint is True
        assert next(t for t in listing.tools if t.name=='dali_get_configmaster_floorplan_plan').annotations.read_only_hint is True
        report['native_floorplan_file_invalid_or_unconfigured_rejected']=4
        for name,arguments in [
            ('dali_inspect_configmaster_floorplan',dict(cache_id='../invalid')),
            ('dali_attach_project_floorplan',dict(project_id='0'*32,cache_id='0'*32,image_path='relative.png')),
            ('dali_get_project_floorplan',dict(project_id='0'*32,include_image=1))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        for name in ('dali_inspect_configmaster_floorplan','dali_get_project_floorplan'):
            assert next(t for t in listing.tools if t.name==name).annotations.read_only_hint is True
        report['native_floorplan_invalid_inputs_rejected']=3
        for name,arguments in [
            ('dali_preview_project_restore',dict(project_id='a'*32,addresses=[True],fields=[],group_numbers=[],scenes=[0])),
            ('dali_preview_project_restore',dict(project_id='../bad',addresses=[0],fields=[],group_numbers=[],scenes=[0])),
            ('dali_get_project_restore_review',dict(review_id='../bad')),
        ]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        review_tool=next(t for t in listing.tools if t.name=='dali_preview_project_restore')
        assert review_tool.annotations.read_only_hint is True
        assert set(review_tool.input_schema['required'])=={'project_id','addresses','fields','group_numbers','scenes'}
        report['project_restore_invalid_inputs_rejected']=3
        for name,arguments in [
            ('dali_prepare_project_restore',dict(project_id='../bad',addresses=[0],fields=[],group_numbers=[],scenes=[0])),
            ('dali_start_project_restore',dict(plan_id='../bad',confirmation='no')),
            ('dali_get_project_restore',dict(plan_id='../bad')),
            ('dali_cancel_project_restore',dict(plan_id='a'*32)),
            ('dali_prepare_project_restore_undo',dict(plan_id='../bad'))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        report['project_execution_invalid_inputs_rejected']=5
        native_combined=next(t for t in listing.tools if t.name=='dali_prepare_workbook_restore')
        assert native_combined.annotations.destructive_hint is False
        for override in (dict(channel=True),dict(workbook_id='../bad'),dict(scene_source=None),dict(group=True),dict(addresses=[True])):
            args0=dict(workbook_id='a'*32,channel=2,addresses=[0],fields=['fade_time'],group_numbers=[15],scenes=[8],scene_source='ecg')
            args0.update(override)
            rejected=await client.call_tool('dali_prepare_workbook_restore',args0)
            assert rejected.is_error
        report['native_combined_invalid_inputs_rejected']=5


        for name,arguments in [
            ('dali_prepare_parameter_batch',dict(channel=2,targets=[])),
            ('dali_prepare_parameter_batch',dict(channel=2,targets=[dict(address=True,changes=[dict(field='fade_time',values={'code':5})])])),
            ('dali_prepare_parameter_batch',dict(channel=2,targets=[dict(address=0,changes=[dict(field='fade_time',values={'code':0})])])),
            ('dali_prepare_project_parameter_batch',dict(project_id='a'*32,channel=2,addresses=[],fields=['fade_time'])),
            ('dali_prepare_workbook_parameter_batch',dict(workbook_id='a'*32,channel=2,addresses=[],fields=['fade_time'])),
            ('dali_start_parameter_batch',dict(plan_id='../bad',confirmation='no')),
            ('dali_get_parameter_batch',dict(plan_id='../bad')),
            ('dali_cancel_parameter_batch',dict(plan_id='a'*32)),
            ('dali_prepare_parameter_batch_restore',dict(plan_id='../bad'))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        report['parameter_batch_invalid_inputs_rejected']=9
        assert {'dali_import_configmaster_workbook','dali_get_configmaster_workbook'} <= set(report['tools'])
        from test_native_workbook import document as workbook_document, package as workbook_package
        workbook_path=Path(temp.name)/'synthetic-native.xlsx'
        workbook_source=workbook_document()
        workbook_source['网关信息']['B3']=data['expected_dali_mac']
        workbook_path.write_bytes(workbook_package(workbook_source))
        imported=await client.call_tool('dali_import_configmaster_workbook',dict(xlsx_path=str(workbook_path)))
        assert not imported.is_error
        workbook_id=imported.structured_content['workbook_id']
        native=await client.call_tool('dali_get_configmaster_workbook',dict(workbook_id=workbook_id,channel=2))
        assert not native.is_error
        assert [f['address'] for f in native.structured_content['channel_details']['fixtures']]==[0,63]
        assert native.structured_content['summary']['executable'] is False
        assert native.structured_content['summary']['device_values_verified'] is False
        for name,arguments in [('dali_import_configmaster_workbook',dict(xlsx_path='relative.xlsx')),
                ('dali_get_configmaster_workbook',dict(workbook_id='../bad')),
                ('dali_get_configmaster_workbook',dict(workbook_id=workbook_id,channel=True))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        assert next(t for t in listing.tools if t.name=='dali_get_configmaster_workbook').annotations.read_only_hint is True
        report['native_workbook_sdk']='synthetic source import and channel2 retrieval passed;3 invalid inputs rejected; no native application import/device restore'
        assert 'dali_prepare_workbook_scene_restore' in report['tools']
        for arguments in [dict(workbook_id=workbook_id,channel=2,addresses=[True],scenes=[8],scene_source='ecg'),
                          dict(workbook_id=workbook_id,channel=2,addresses=[0],scenes=[8],scene_source='group'),
                          dict(workbook_id=workbook_id,channel=2,addresses=[0],scenes=[16],scene_source='ecg'),
                          dict(workbook_id=workbook_id,channel=2,addresses=[1],scenes=[8],scene_source='ecg')]:
            rejected=await client.call_tool('dali_prepare_workbook_scene_restore',arguments)
            assert rejected.is_error
        report['native_scene_restore_invalid_inputs_rejected']=4
        assert {'dali_prepare_workbook_group_restore','dali_prepare_workbook_parameter_restore'} <= set(report['tools'])
        for name,arguments in [('dali_prepare_workbook_group_restore',dict(workbook_id=workbook_id,channel=2,addresses=[True],group_numbers=[15])),
                ('dali_prepare_workbook_group_restore',dict(workbook_id=workbook_id,channel=2,addresses=[1],group_numbers=[15])),
                ('dali_prepare_workbook_group_restore',dict(workbook_id=workbook_id,channel=2,addresses=[0],group_numbers=[16])),
                ('dali_prepare_workbook_parameter_restore',dict(workbook_id=workbook_id,channel=2,address=0,fields=['min_level'])),
                ('dali_prepare_workbook_parameter_restore',dict(workbook_id=workbook_id,channel=2,address=True,fields=['fade_time'])),
                ('dali_prepare_workbook_parameter_restore',dict(workbook_id=workbook_id,channel=2,address=0,fields=['curve']))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        report['native_group_parameter_invalid_inputs_rejected']=6
        for arguments in [dict(workbook_id=workbook_id,channel=2,selections=[]),
                dict(workbook_id=workbook_id,channel=2,selections=[dict(kind='lamp',address=True)]),
                dict(workbook_id=workbook_id,channel=2,selections=[dict(kind='scene',group=0,scene=16)]),
                dict(workbook_id=workbook_id,channel=2,selections=[dict(kind='group',group=15,name='injected')])]:
            rejected=await client.call_tool('dali_prepare_workbook_name_restore',arguments)
            assert rejected.is_error
        report['native_workbook_name_invalid_inputs_rejected']=4
        assert 'dali_prepare_project_parameter_restore' in report['tools']
        for arguments in [dict(project_id='../bad',address=0,fields=['fade_time']),
                          dict(project_id='a'*32,address=True,fields=['fade_time']),
                          dict(project_id='a'*32,address=0,fields=[]),
                          dict(project_id='a'*32,address=0,fields=['groups'])]:
            rejected=await client.call_tool('dali_prepare_project_parameter_restore',arguments)
            assert rejected.is_error
        report['project_parameter_invalid_inputs_rejected']=4
        native_file_tools={'dali_read_configmaster_file','dali_prepare_configmaster_file_names',
                           'dali_apply_configmaster_file_names','dali_get_configmaster_file_plan',
                           'dali_prepare_configmaster_file_restore'}
        assert native_file_tools <= set(report['tools'])
        for name,arguments in [('dali_read_configmaster_file',{}),
                ('dali_prepare_configmaster_file_names',dict(changes=[])),
                ('dali_apply_configmaster_file_names',dict(plan_id='../invalid',confirmation='no')),
                ('dali_get_configmaster_file_plan',dict(plan_id='../invalid')),
                ('dali_prepare_configmaster_file_restore',dict(plan_id='../invalid'))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        report['native_file_unconfigured_or_invalid_rejected']=5
        assert {'dali_blink_lamp','dali_get_blink','dali_cancel_blink'} <= set(report['tools'])
        invalid_blinks=[dict(channel=True,address=0),dict(channel=2,address=64),
            dict(channel=2,address=0,cycles=True),dict(channel=2,address=0,period_ms=450),
            dict(channel=2,address=0,cycles=2,period_ms=60000)]
        for arguments in invalid_blinks:
            rejected=await client.call_tool('dali_blink_lamp',arguments)
            assert rejected.is_error
        for name in ('dali_get_blink','dali_cancel_blink'):
            rejected=await client.call_tool(name,dict(operation_id='../bad'))
            assert rejected.is_error
        blink=await client.call_tool('dali_get_blink',{})
        assert not blink.is_error and blink.structured_content['operations']==[]
        report['blink_invalid_inputs_rejected']=len(invalid_blinks)+2
        assert {'dali_relative_dim','dali_get_relative_dim'} <= set(report['tools'])
        for arguments in [dict(channel=True,address=0,direction='up'),
                          dict(channel=2,address=64,direction='up'),
                          dict(channel=2,address=0,direction='UP'),
                          dict(channel=2,address=0,direction=1)]:
            rejected=await client.call_tool('dali_relative_dim',arguments)
            assert rejected.is_error
        relative=await client.call_tool('dali_get_relative_dim',{})
        assert not relative.is_error and relative.structured_content['operations']==[]
        for name in ('dali_relative_dim','dali_get_relative_dim'):
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint == (name=='dali_get_relative_dim')
            if name=='dali_relative_dim': assert hints.idempotent_hint is False
        report['relative_dim_invalid_inputs_rejected']=4
        assert {'dali_set_lamp_level_extreme','dali_get_level_extreme'}<=set(report['tools'])
        for arguments in (dict(channel=True,address=0,extreme='maximum'),dict(channel=2,address=64,extreme='minimum'),dict(channel=2,address=0,extreme='off')):
            assert (await client.call_tool('dali_set_lamp_level_extreme',arguments)).is_error
        assert (await client.call_tool('dali_get_level_extreme',{})).structured_content['operations']==[]
        for name in ('dali_set_lamp_level_extreme','dali_get_level_extreme'):
            hint=next(t for t in listing.tools if t.name==name).annotations
            assert hint.read_only_hint==(name=='dali_get_level_extreme')
            if name=='dali_set_lamp_level_extreme':assert hint.idempotent_hint is False
        assert {'dali_attach_project_layouts','dali_import_project_layout'} <= set(report['tools'])
        for name,arguments in [('dali_attach_project_layouts',dict(project_id='../bad',layout_ids=[])),
                              ('dali_attach_project_layouts',dict(project_id='0'*32,layout_ids=[True])),
                              ('dali_import_project_layout',dict(project_id='../bad',source_layout_id='0'*32)),
                              ('dali_import_project_layout',dict(project_id='0'*32,source_layout_id='../bad'))]:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        report['project_layout_invalid_inputs_rejected']=4
        for name in ('dali_attach_project_layouts','dali_import_project_layout'):
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint is False and hints.destructive_hint is False
        lamp_names={'dali_prepare_lamp_parameter','dali_prepare_power_parameter','dali_apply_lamp_parameter',
                    'dali_get_lamp_parameter_plan','dali_prepare_lamp_parameter_restore'}
        assert lamp_names <= set(report['tools'])
        curve_schema=next(t for t in listing.tools if t.name=='dali_prepare_lamp_parameter').input_schema
        assert 'dimming_curve' in curve_schema['properties']['field']['enum']
        report['curve_schema_registered']=True
        lamp_plans=await client.call_tool('dali_get_lamp_parameter_plan',{})
        assert not lamp_plans.is_error and lamp_plans.structured_content['plans']==[]
        invalid_lamp=[('dali_prepare_lamp_parameter',dict(channel=2,address=0,field=f,value=v))
            for f,v in [('fade_rate',True),('fade_rate',0),('fade_rate',16),('min_level','3'),
                        ('max_level',255),('configured_min_ct',999),('power_on',255),
                        ('configured_max_ct',10001),('system_failure',-1)]]
        invalid_lamp += [(name,dict(plan_id='../bad',**({'confirmation':'bad'} if 'apply' in name else {})))
            for name in ('dali_get_lamp_parameter_plan','dali_apply_lamp_parameter','dali_prepare_lamp_parameter_restore')]
        invalid_lamp += [('dali_prepare_lamp_parameter',dict(channel=2,address=0,field='dimming_curve',value=v))
            for v in (-1,2,255,True,'1')]
        invalid_lamp += [('dali_prepare_power_parameter',dict(channel=2,address=0,field='power_on',**values))
            for values in ({},{'level_raw':255},{'level_raw':True},{'kelvin_raw':True},
                           {'kelvin_raw':999},{'kelvin_raw':65534},{'kelvin_raw':'4000'})]
        for name,arguments in invalid_lamp:
            rejected=await client.call_tool(name,arguments)
            assert rejected.is_error
        report['lamp_parameter_invalid_inputs_rejected']=len(invalid_lamp)
        for name in lamp_names:
            hints=next(t for t in listing.tools if t.name==name).annotations
            assert hints.read_only_hint == (name=='dali_get_lamp_parameter_plan')
            assert hints.destructive_hint == (name=='dali_apply_lamp_parameter')
        fade_plans=await client.call_tool('dali_get_fade_time_plan',{})
        assert not fade_plans.is_error and fade_plans.structured_content['plans']==[]
        for tool,arguments in [('dali_prepare_fade_time',{'channel':2,'address':0,'fade_time_code':True}),
            ('dali_prepare_fade_time',{'channel':2,'address':0,'fade_time_code':0}),
            ('dali_prepare_fade_time',{'channel':2,'address':0,'fade_time_code':16}),
            ('dali_get_fade_time_plan',{'plan_id':'../bad'}),
            ('dali_apply_fade_time',{'plan_id':'../bad','confirmation':'bad'}),
            ('dali_prepare_fade_time_restore',{'plan_id':'../bad'})]:
            rejected=await client.call_tool(tool,arguments)
            assert rejected.is_error
        report['fade_tools_invalid_inputs_rejected']=6
        created=await client.call_tool('dali_create_layout',{'name':'SDK handover','channel':2,'address_grid':[[0,63]]})
        assert not created.is_error
        layout=created.structured_content
        checked=await client.call_tool('dali_record_layout_check',{'layout_id':layout['layout_id'],'expected_digest':layout['digest'],
            'checked_addresses':[0],'note':'Synthetic offline test only','checked_by':'test fixture'})
        assert not checked.is_error and not checked.structured_content['physical_mapping_verified']
        exported=await client.call_tool('dali_export_layout',{'layout_id':checked.structured_content['layout_id']})
        imported=await client.call_tool('dali_import_layout',{'document':exported.structured_content})
        assert not imported.is_error and imported.structured_content['address_grid']==[[0,63]]
        assert not imported.structured_content['physical_mapping_verified']
        for tool,arguments in [('dali_import_layout',{'document':{}}),
            ('dali_record_layout_check',{'layout_id':layout['layout_id'],'expected_digest':'stale','checked_addresses':[0],'note':'test','checked_by':'fixture'}),
            ('dali_get_live_operation',{'kind':'live-output','operation_id':'../bad'}),
            ('dali_preview_scene_restore',{'plan_id':'../bad'})]:
            rejected=await client.call_tool(tool,arguments)
            assert rejected.is_error
        live=await client.call_tool('dali_get_live_operation',{'kind':'live-channel'})
        assert not live.is_error and live.structured_content['operations']==[]
        report['layout_handover_sdk_roundtrip']=True
        report['new_workflow_invalid_inputs_rejected']=4
        recent_parameters=await client.call_tool('dali_get_parameter_review',{})
        assert not recent_parameters.is_error and recent_parameters.structured_content['reviews']==[]
        for tool,arguments in [
            ('dali_preview_parameter_changes',{'channel':True,'targets':[]}),
            ('dali_preview_parameter_changes',{'channel':2,'targets':[{'address':True,'changes':[{'field':'fade_time','values':{'code':5}}]}]}),
            ('dali_preview_parameter_changes',{'channel':2,'targets':[{'address':0,'changes':[{'field':'fade_time','values':{'code':True}}]}]}),
            ('dali_preview_parameter_changes',{'channel':2,'targets':[{'address':0,'changes':[{'field':'fade_time','values':{'code':5}}],'force':True}]}),
            ('dali_preview_project_parameter_restore',{'project_id':'../outside','addresses':[0],'fields':['fade_time']}),
            ('dali_get_parameter_review',{'review_id':'../outside'})]:
            rejected=await client.call_tool(tool,arguments)
            assert rejected.is_error
        report['parameter_review_invalid_inputs_rejected']=6
        for name in ('dali_preview_parameter_changes','dali_preview_project_parameter_restore'):
            tool=next(t for t in listing.tools if t.name==name)
            assert tool.annotations.destructive_hint is False and tool.annotations.read_only_hint is False
        assert not any(t.name.startswith('dali_apply_parameter') for t in listing.tools)
        report['broad_parameter_write_tools_absent']=True
        native_document=dict(version='1.2.4.1',ip=data['gateway_ip'],dalimac=data['expected_dali_mac'],
            gwVersion=data['expected_firmware'],devName='SDK fixture',
            zCh1=dict(addresses={'0':{'name':'Lamp zero','sceneKnown':0}},groupNames={},sceneNamesByGroup={'0':{'15':'Scene15'}}),
            zCh2=dict(addresses={},groupNames={},sceneNamesByGroup={}),preserved_extension={'value':42})
        native=await client.call_tool('dali_import_configmaster_cache',{'document':native_document})
        assert not native.is_error
        native_id=native.structured_content['cache_id']
        exported=await client.call_tool('dali_get_configmaster_cache',{'cache_id':native_id,'include_document':True})
        assert not exported.is_error and exported.structured_content['document']==native_document
        assert exported.structured_content['summary']['device_values_verified'] is False
        for tool,arguments in [('dali_import_configmaster_cache',{'document':{}}),
                               ('dali_get_configmaster_cache',{'include_document':True}),
                               ('dali_get_configmaster_cache',{'cache_id':'../outside'}),
                               ('dali_get_configmaster_cache',{'include_document':1})]:
            rejected=await client.call_tool(tool,arguments)
            assert rejected.is_error
        report['native_cache_roundtrip']=True
        report['native_cache_invalid_inputs_rejected']=4
        name_plan=await client.call_tool('dali_prepare_configmaster_names',{'cache_id':native_id,
            'changes':[dict(kind='scene',channel=1,group=0,scene=15,name='SDK draft')]})
        assert not name_plan.is_error
        name_id=name_plan.structured_content['plan_id']
        draft=await client.call_tool('dali_get_configmaster_name_plan',{'plan_id':name_id,'include_document':True})
        assert not draft.is_error and draft.structured_content['draft_document']['zCh1']['sceneNamesByGroup']['0']['15']=='SDK draft'
        merged=await client.call_tool('dali_rebase_configmaster_names',{'plan_id':name_id,'current_document':native_document})
        assert not merged.is_error and merged.structured_content['state']=='draft_ready'
        report['native_name_draft_roundtrip']=True
        recent_groups=await client.call_tool('dali_get_group_membership_plan',{})
        assert not recent_groups.is_error and recent_groups.structured_content['plans']==[]
        for tool,arguments in [('dali_prepare_group_membership',{'channel':True,'changes':[]}),
            ('dali_prepare_group_membership',{'channel':2,'changes':[{'address':0,'add':[15],'remove':[15]}]}),
            ('dali_get_group_membership_plan',{'plan_id':'../outside'}),
            ('dali_prepare_group_restore',{'plan_id':'../outside'}),
            ('dali_apply_group_membership',{'plan_id':'../outside','confirmation':'invalid'})]:
            rejected=await client.call_tool(tool,arguments)
            assert rejected.is_error
        report['group_membership_invalid_inputs_rejected']=5
        for arguments in [dict(project_id='../outside', addresses=[0], group_numbers=[15]),
                          dict(project_id='0'*32, addresses=[True], group_numbers=[15]),
                          dict(project_id='0'*32, addresses=[0], group_numbers=['15'])]:
            rejected=await client.call_tool('dali_preview_project_group_restore',arguments)
            assert rejected.is_error
        report['project_group_restore_invalid_inputs_rejected']=3
        project_group_tool=next(t for t in listing.tools if t.name=='dali_preview_project_group_restore')
        assert project_group_tool.annotations.destructive_hint is False
        assert project_group_tool.annotations.read_only_hint is False  # Saves a local plan, no device writes.
        for invalid in ({'group':True},{'scene':16},{'expected_addresses':[]},{'verify_addresses':[1]}):
            arguments=dict(channel=2,group=1,scene=8,expected_addresses=[0],verify_addresses=[0])
            arguments.update(invalid)
            rejected=await client.call_tool('dali_recall_group_scene',arguments)
            assert rejected.is_error,'Invalid group recall scope accepted'
        recent=await client.call_tool('dali_get_group_recall',{})
        assert not recent.is_error and recent.structured_content['operations']==[]
        report['group_recall_invalid_scopes_rejected']=4
        hints={t.name:t.annotations for t in listing.tools}
        assert hints['dali_capture_project'].read_only_hint is False and hints['dali_capture_project'].destructive_hint is False
        assert hints['dali_import_project'].read_only_hint is False and hints['dali_diff_projects'].read_only_hint is True
        for invalid in ({'channel':True,'addresses':[0],'scenes':[0]},
                        {'channel':2,'addresses':['0'],'scenes':[0]},
                        {'channel':2,'addresses':[0],'scenes':[16]},
                        {'channel':2,'addresses':[0],'scenes':[]}):
            rejected=await client.call_tool('dali_capture_project',dict(name='invalid',**invalid))
            assert rejected.is_error,'Invalid project scope accepted'
        invalid_import=await client.call_tool('dali_import_project',{'document':{}})
        assert invalid_import.is_error
        projects=await client.call_tool('dali_get_project',{})
        assert not projects.is_error and projects.structured_content['projects']==[]
        report['project_invalid_inputs_rejected']=5
        from test_project_floorplan import cache_fixture, png
        from test_project_layouts import project_fixture
        if not args.live:
            fp_cache=await client.call_tool('dali_import_configmaster_cache',dict(document=cache_fixture()))
            assert not fp_cache.is_error
            fp_id=fp_cache.structured_content['cache_id']
            inspected=await client.call_tool('dali_inspect_configmaster_floorplan',dict(cache_id=fp_id))
            assert not inspected.is_error and inspected.structured_content['mark_count']==3
            fp_project=await client.call_tool('dali_import_project',dict(document=project_fixture()))
            assert not fp_project.is_error
            fp_image=Path(temp.name)/'floorplan.png';fp_image.write_bytes(png())
            attached=await client.call_tool('dali_attach_project_floorplan',dict(project_id=fp_project.structured_content['project_id'],cache_id=fp_id,image_path=str(fp_image)))
            assert not attached.is_error
            fp_result=await client.call_tool('dali_get_project_floorplan',dict(project_id=attached.structured_content['project_id'],include_image=True))
            assert not fp_result.is_error and fp_result.structured_content['outside_capture_count']==1
            assert fp_result.structured_content['image']['base64']
            report['native_floorplan_roundtrip']=True
            names=await client.call_tool('dali_attach_project_names',dict(project_id=attached.structured_content['project_id'],cache_id=fp_id,selections=[dict(kind='lamp',address=0),dict(kind='group',group=14),dict(kind='scene',group=0,scene=15)]))
            assert not names.is_error
            named=await client.call_tool('dali_get_project_names',dict(project_id=names.structured_content['project_id']))
            assert not named.is_error and [e['present'] for e in named.structured_content['entries']]==[True,False,True]
            portable=await client.call_tool('dali_get_project',dict(project_id=names.structured_content['project_id']))
            assert not portable.is_error and portable.structured_content['schema_version']==4
            assert portable.structured_content['native_floorplan']['image']['base64']==fp_result.structured_content['image']['base64']
            report['project_names_roundtrip']=True
        layout=await client.call_tool('dali_create_layout',{'name':'offline SDK test','channel':2,'address_grid':[[0,63]]})
        assert not layout.is_error
        ident=layout.structured_content['layout_id']
        fetched=await client.call_tool('dali_get_layout',{'layout_id':ident})
        assert not fetched.is_error and fetched.structured_content['addresses']==[0,63]
        report['local_layout_roundtrip']=True
        for invalid in ({'channel':True,'changes':[{'address':0,'percent':50}]},
                        {'channel':2,'changes':[{'address':0,'percent':True}]},
                        {'channel':2,'changes':[{'address':0,'percent':'50'}]},
                        {'channel':2,'changes':[{'address':0,'percent':.01}]},
                        {'channel':2,'changes':[{'address':0,'percent':50}],'scene':16}):
            rejected=await client.call_tool('dali_prepare_live_output',invalid)
            assert rejected.is_error,'Invalid percentage preparation accepted'
        for override in ({'foreground_percent':True},{'background':'automatic'}, {'pattern_rows':['1x']},
                         {'glyph':'X','pattern_rows':None}):
            arguments={'layout_id':ident,'foreground_percent':50,'background':'off','pattern_rows':['10']}
            arguments.update(override)
            rejected=await client.call_tool('dali_compose_layout_output',arguments)
            assert rejected.is_error,'Invalid composition accepted'
        report['authoring_invalid_inputs_rejected']=9
        assert {'dali_start_scene_save','dali_get_job','dali_cancel_job','dali_scan_existing','dali_read_groups','dali_read_lamp_parameters'} <= set(report['tools'])
        # Strict schema must reject coercible booleans/strings before any bus I/O.
        for invalid in ({'channel':True,'addresses':[0]}, {'channel':2,'addresses':['0']},
                        {'channel':2,'addresses':[False]}, {'channel':2,'addresses':list(range(17))}):
            rejected=await client.call_tool('dali_read_lamp_parameters',invalid)
            assert rejected.is_error, 'Invalid parameter scope was accepted'
        report['parameter_invalid_scopes_rejected']=4
        for invalid in ({'channel':True,'address':0},{'channel':2,'address':'0'},
                        {'channel':2,'address':0,'hold_seconds':True},{'channel':2,'address':0,'hold_seconds':6}):
            rejected=await client.call_tool('dali_identify_lamp',invalid)
            assert rejected.is_error, 'Invalid identification scope was accepted'
        report['identification_invalid_scopes_rejected']=4
        base={'channel':2,'group':0,'expected_addresses':[0],'verify_addresses':[0],'level_raw':0}
        for override in ({'group':True},{'expected_addresses':['0']},{'level_raw':False},{'group':16},{'verify_addresses':[63]}):
            rejected=await client.call_tool('dali_set_group_live_output',dict(base,**override))
            assert rejected.is_error, 'Invalid group scope was accepted'
        report['group_invalid_scopes_rejected']=5
        listing_result=await client.call_tool('dali_get_identification',{})
        assert not listing_result.is_error and listing_result.structured_content['operations']==[]
        calls=[("dali_status",{})]
        if args.live:
            calls += [("dali_discover",{}),
                      ("dali_read_actual",{"channel":args.channel,"addresses":args.addresses})]
        for name, arguments in calls:
            result=await client.call_tool(name,arguments,read_timeout_seconds=180)
            data=result.model_dump(mode="json", by_alias=True)
            report.setdefault("calls",[]).append({"tool":name,"result":data})
            print(json.dumps({"tool":name,"isError":result.is_error,"structuredContent":result.structured_content},ensure_ascii=True),flush=True)
            assert not result.is_error,data
            if name=='dali_status':
                status=result.structured_content
                assert status['version']==__version__
                if not args.live:assert status['project_restore_writes_enabled'] is False
                assert status['parameter_write_fields']==['fade_time','fade_rate','min_level','max_level','configured_min_ct','configured_max_ct','power_on','system_failure','dimming_curve']
                if not args.live:
                    assert status['parameter_writes_enabled'] is False
                    assert status['fade_time_writes_enabled'] is False
    if args.output:
        with args.output.open('x',encoding='utf-8') as output:
            json.dump(report,output,ensure_ascii=False,indent=2)
    temp.cleanup()
    print(f'Stdio OK: {len(report["tools"])} tools')

if __name__=="__main__":
    asyncio.run(main())
