"""Post-move labels/placement: scoped file transaction and immutable grid revision.

Parameters/group caches and historical projects are not reinterpreted or moved.
Actual bus mapping must still match the verified address transaction. No device
write command is issued by this module. File and bus observations are not atomic.
"""
import copy
import json
import math
import time
import uuid
from .address_moves import AddressMoveController, scope, signature
from .native_cache import canonical, inspect_configmaster_cache
from .native_file import NativeFileController, digest, parse, serialize, restore_names
from .native_names import prepare_name_document
from .native_floorplan import inspect_native_floorplan
from .projects import identity


def components_check(components):
    if (not isinstance(components,list) or not components
            or any(type(x) is not str or x not in ('lamp_name','floorplan_mark') for x in components)
            or len(set(components))!=len(components)):
        raise ValueError('select unique lamp_name and/or floorplan_mark components')


def migrate_document(document,channel,src,dst,components):
    scope(channel,src,dst);components_check(components);inspect_configmaster_cache(document)
    result=copy.deepcopy(document);review=dict(names=[],marker=None,skipped=[])
    if 'lamp_name' in components:
        rows=document[f'zCh{channel}']['addresses']
        if str(src) not in rows or str(dst) not in rows:raise ValueError('both lamp cache rows must exist; refresh application cache first')
        if rows[str(dst)]['name']!='':raise ValueError('destination already has a lamp name; refusing overwrite')
        changes=[dict(kind='lamp',channel=channel,address=src,name=''),
                 dict(kind='lamp',channel=channel,address=dst,name=rows[str(src)]['name'])]
        review['names'],result=prepare_name_document(result,changes)
        if rows[str(src)]['name']=='':review['skipped'].append('source_name_empty')
    if 'floorplan_mark' in components:
        floor=inspect_native_floorplan(document)
        if not floor['present']:review['skipped'].append('floorplan_absent')
        else:
            marks=result['floorPlan']['marks']
            source=[m for m in marks if (m['ch'],m['addr'])==(channel,src)]
            target=[m for m in marks if (m['ch'],m['addr'])==(channel,dst)]
            if not source:review['skipped'].append('source_marker_absent')
            elif target:raise ValueError('destination already has a floorplan marker; refusing overwrite')
            else:
                before=copy.deepcopy(source[0]);source[0]['addr']=dst
                review['marker']=dict(before=before,after=copy.deepcopy(source[0]),image=document['floorPlan']['image'])
            inspect_native_floorplan(result)
    return result,review


def restore_document(current,original,review):
    result=restore_names(current,original,review['names'])
    entry=review['marker']
    if entry is not None:
        inspect_native_floorplan(current)
        if 'floorPlan' not in current or current['floorPlan']['image']!=entry['image']:
            raise ValueError('floorplan background changed; marker restoration refused')
        marks=result['floorPlan']['marks'];old,new=entry['before'],entry['after']
        src=[m for m in marks if (m['ch'],m['addr'])==(old['ch'],old['addr'])]
        dst=[m for m in marks if (m['ch'],m['addr'])==(new['ch'],new['addr'])]
        if src==[old] and not dst:pass
        elif not src and dst==[new]:
            index=marks.index(dst[0]);marks[index]=copy.deepcopy(old)
        else:raise ValueError('moved marker changed or address conflicted; restoration refused')
        inspect_native_floorplan(result)
    inspect_configmaster_cache(result)
    return result


class AddressMetadataController(NativeFileController):
    write_gate='enable_native_address_metadata_writes'

    def __init__(self,service):
        super().__init__(service)
        self.moves=AddressMoveController(service,process_guard=lambda:self.guard())

    def _record_path(self,kind,ident,suffix='json'):
        self.moves._id(ident)
        return self.service.state_dir/f'native-address-{kind}-{ident}.{suffix}'

    @staticmethod
    def _confirmation(plan):return f"MIGRATE ADDRESS METADATA {plan['plan_id']} {plan['digest']}"

    def _journal(self,ident,report):
        report.pop('device_commands_sent',None)
        report.update(network_traffic=True,device_write_commands_sent=0)
        super()._journal(ident,report)

    def _move(self,ident,expected_digest=None):
        plan=self.moves.get(ident);run=plan.get('execution')
        if (not run or run.get('state') not in ('completed_readback','verified_receipt_unknown') or
                run.get('write_intent') is not True or
                run.get('attempt')!=scope(plan['channel'],plan['source_address'],plan['destination_address'])):
            raise ValueError('verified address move execution required')
        if expected_digest is not None and plan['digest']!=expected_digest:raise ValueError('source address plan changed')
        return plan

    def _verify_bus(self,move,original=False):
        self.guard()
        ch=move['channel'];which='before_addresses' if original else 'after_addresses'
        address=move['source_address'] if original else move['destination_address']
        _,scan=self.moves._scan(ch,move['gateway'])
        if scan['addresses']!=move[which]:raise ValueError('bus addresses no longer match selected metadata mapping')
        duplicates=self.moves._duplicates(ch,move['gateway'])
        lamp=self.moves._lamp(ch,address,move['gateway'])
        if signature(lamp)!=signature(move['original']):raise ValueError('lamp differs from address move backup')
        return dict(scan=scan,duplicates=duplicates,lamp=lamp,captured_at=time.time(),device_write_commands_sent=0)

    def _save_plan(self,path,before,after,operation,**details):
        ident=uuid.uuid4().hex;now=time.time()
        plan=dict(plan_id=ident,format='native-address-metadata-v1',operation=operation,file_path=str(path),
            before_sha256=digest(before),after_sha256=digest(after),created_at=now,expires_at=now+600,
            network_traffic=True,device_write_commands_sent=0,parameter_and_group_caches_preserved=True,**details)
        plan['digest']=self._plan_digest(plan)
        self._write_new(self._record_path('before',ident,'bin'),before)
        self._write_new(self._record_path('after',ident,'bin'),after)
        self._write_new(self._record_path('plan',ident),canonical(plan))
        return dict(plan,confirmation=self._confirmation(plan),application_file_written=False)

    def prepare(self,address_plan_id,components):
        self.moves._id(address_plan_id);components_check(components)
        path=self._target()
        with self.moves._locked(),self._locked(path):
            move=self._move(address_plan_id)
            verified=self._verify_bus(move)
            self.guard();before,document=self._read(path)
            if identity(inspect_configmaster_cache(document)['gateway'])!=identity(move['gateway']):raise ValueError('cache and move gateway differ')
            draft,review=migrate_document(document,move['channel'],move['source_address'],move['destination_address'],components)
            after=before if draft==document else serialize(draft,before)
            return self._save_plan(path,before,after,'migrate',address_plan_id=address_plan_id,address_plan_digest=move['digest'],
                components=components,review=review,bus_verification=verified)

    def _load(self,ident,seen=None):
        seen=set() if seen is None else set(seen)
        if ident in seen or len(seen)>=16:raise ValueError('invalid metadata restoration ancestry')
        seen.add(ident)
        plan=json.loads(self._record_path('plan',ident).read_text(encoding='utf-8'))
        if plan.get('format')!='native-address-metadata-v1' or plan.get('plan_id')!=ident or plan.get('digest')!=self._plan_digest(plan):
            raise ValueError('address metadata plan checksum mismatch')
        if any(type(plan[k]) not in (int,float) or not math.isfinite(plan[k]) for k in ('created_at','expires_at')):
            raise ValueError('invalid metadata plan time')
        if plan['expires_at']!=plan['created_at']+600:raise ValueError('invalid metadata lifetime')
        if plan['file_path']!=str(self._target()):raise ValueError('configured metadata file changed')
        before=self._record_path('before',ident,'bin').read_bytes();after=self._record_path('after',ident,'bin').read_bytes()
        if digest(before)!=plan['before_sha256'] or digest(after)!=plan['after_sha256']:raise ValueError('metadata backup checksum mismatch')
        first,last=parse(before),parse(after)
        for document in (first,last):self.service._check_native_identity(inspect_configmaster_cache(document))
        move=self._move(plan['address_plan_id'],plan['address_plan_digest'])
        if plan['operation']=='migrate':
            desired,review=migrate_document(first,move['channel'],move['source_address'],move['destination_address'],plan['components'])
            if review!=plan['review']:raise ValueError('metadata review changed')
            expected=before if desired==first else serialize(desired,before)
        elif plan['operation']=='restore':
            parent,old,new=self._load(plan['source_plan_id'],seen)
            if parent['operation']!='migrate' or parent['address_plan_id']!=plan['address_plan_id'] or parent['digest']!=plan['source_digest']:
                raise ValueError('invalid metadata restoration source')
            self._require_intent(parent)
            desired=restore_document(first,parse(old),parent['review'])
            expected=old if before==new else (before if desired==first else serialize(desired,before))
        else:raise ValueError('invalid metadata operation')
        if expected!=after:raise ValueError('metadata draft changes unselected fields')
        return plan,before,after

    def _require_intent(self,plan):
        intent=json.loads(self._record_path('intent',plan['plan_id']).read_text(encoding='utf-8'))
        if intent!={'plan_id':plan['plan_id'],'digest':plan['digest']}:raise ValueError('matching file replacement intent required')

    def apply(self,ident,confirmation):
        plan,_,_=self._load(ident)
        if self.service.config.get(self.write_gate) is not True:raise ValueError('separate metadata write gate disabled')
        if confirmation!=self._confirmation(plan):raise ValueError('metadata confirmation mismatch')
        with self.moves._locked():
            if self._record_path('claim',ident).exists():return self.get(ident)
            if time.time()>plan['expires_at']:raise ValueError('metadata plan expired')
            move=self._move(plan['address_plan_id'],plan['address_plan_digest'])
            verification=self._verify_bus(move,original=plan['operation']=='restore')
            result=super().apply(ident,confirmation)
            result['bus_verification']=verification
            result['device_write_commands_sent']=0
            # Parent file controller's counter concerns its own filesystem work.
            result['network_traffic']=True
            try:self._journal(ident,result)
            except OSError as exc:result['journal_error']=str(exc)
            return result

    def prepare_restore(self,ident):
        source,old,new=self._load(ident)
        if source['operation']!='migrate':raise ValueError('restore only an original metadata migration')
        if old==new:return dict(state='already_restored',source_plan_id=ident,application_file_written=False)
        self._require_intent(source);path=self._target()
        with self.moves._locked(),self._locked(path):
            move=self._move(source['address_plan_id'],source['address_plan_digest'])
            verified=self._verify_bus(move,original=True)
            self.guard();before,document=self._read(path)
            desired=restore_document(document,parse(old),source['review'])
            if desired==document:return dict(state='already_restored',source_plan_id=ident,application_file_written=False)
            after=old if before==new else serialize(desired,before)
            return self._save_plan(path,before,after,'restore',source_plan_id=ident,source_digest=source['digest'],
                address_plan_id=source['address_plan_id'],address_plan_digest=source['address_plan_digest'],
                exact_original_bytes=before==new,unrelated_current_fields_preserved=True,bus_verification=verified)

    def migrate_layout(self,address_plan_id,layout_id):
        """New local revision only; original remains available for restoration."""
        self.moves._id(address_plan_id)
        with self.moves._locked():
            move=self._move(address_plan_id);layout=self.service._load_layout(layout_id)
            ch,src,dst=move['channel'],move['source_address'],move['destination_address']
            if layout['channel']!=ch or src not in layout['addresses']:raise ValueError('source address not in selected same-channel layout')
            if dst in layout['addresses']:raise ValueError('destination already occupies a layout cell')
            verified=self._verify_bus(move)
            grid=[[dst if a==src else a for a in row] for row in layout['address_grid']]
            result=copy.deepcopy(layout)
            result.update(layout_id=uuid.uuid4().hex,address_grid=grid,addresses=sorted((set(layout['addresses'])-{src})|{dst}),
                created_at=time.time(),physical_mapping_verified=False,
                previous_revision=dict(layout_id=layout['layout_id'],digest=layout['digest']),
                address_move_source=dict(plan_id=address_plan_id,digest=move['digest'],source_address=src,destination_address=dst),
                mapping_check=dict(source='address_move_readback',automatically_verified=False,
                                   previous_physical_claim=layout.get('physical_mapping_verified',False)))
            result['digest']=self.service._layout_digest(result)
            self.service._validate_layout_document(result,result['layout_id'])
            self.service._save('layout',result,result['layout_id'])
            return dict(state='layout_revision_created',layout=result,previous_layout_id=layout_id,bus_verification=verified,
                        device_write_commands_sent=0,application_file_written=False,
                        note='Use the returned layout ID explicitly; old layouts/projects remain unchanged. Recheck physical positions before recording verification.')
