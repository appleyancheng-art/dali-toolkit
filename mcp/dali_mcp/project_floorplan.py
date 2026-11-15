"""Portable native floor-plan evidence; never restores native files or devices."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import time
import uuid
import zlib

from .native_cache import canonical
from .native_floorplan import inspect_native_floorplan

IMAGE_LIMIT = 2_000_000
CONTEXT = 'native_local_evidence_not_verified_physical_mapping'


def inspect_png(data):
    """Check bounded noninterlaced PNG bytes, CRCs and inflated scanline sizes."""
    if not isinstance(data, bytes) or not 45 <= len(data) <= IMAGE_LIMIT or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('background must be a PNG of at most 2MB')
    pos=8; chunks=[]; compressed=[]; header=None; ended=False; idat_ended=False
    while pos < len(data):
        if pos+12 > len(data): raise ValueError('truncated PNG chunk')
        size=struct.unpack_from('>I',data,pos)[0];kind=data[pos+4:pos+8]
        end=pos+12+size
        if end>len(data): raise ValueError('truncated PNG payload')
        payload=data[pos+8:pos+8+size]
        if zlib.crc32(kind+payload)!=struct.unpack_from('>I',data,end-4)[0]:
            raise ValueError('PNG CRC mismatch')
        if not all(65<=b<=90 or 97<=b<=122 for b in kind):raise ValueError('invalid PNG chunk type')
        if not chunks and kind!=b'IHDR':raise ValueError('PNG IHDR must be first')
        if kind==b'IHDR':
            if chunks or len(payload)!=13:raise ValueError('invalid PNG IHDR')
            width,height,bits,color,compression,filtering,interlace=struct.unpack('>IIBBBBB',payload)
            depths={0:(1,2,4,8,16),2:(8,16),3:(1,2,4,8),4:(8,16),6:(8,16)}
            if (not 1<=width<=8192 or not 1<=height<=8192 or width*height>4_000_000
                    or color not in depths or bits not in depths[color]
                    or compression!=0 or filtering!=0 or interlace!=0):
                raise ValueError('unsupported PNG dimensions/encoding (noninterlaced, <=4M pixels)')
            channels={0:1,2:3,3:1,4:2,6:4}[color]
            stride=(width*channels*bits+7)//8+1
            header=dict(width=width,height=height,bit_depth=bits,color_type=color)
        elif kind==b'IDAT':
            if idat_ended:raise ValueError('nonconsecutive PNG IDAT')
            compressed.append(payload)
        elif kind==b'IEND':
            if size or not compressed or end!=len(data):raise ValueError('invalid PNG IEND')
            ended=True
        elif kind==b'PLTE':
            if compressed or b'PLTE' in chunks or not size or size%3 or size>768:
                raise ValueError('invalid PNG palette')
        elif kind[0]&32==0:
            raise ValueError('unsupported critical PNG chunk')
        if compressed and kind!=b'IDAT':idat_ended=True
        chunks.append(kind);pos=end
    if not ended or (header['color_type']==3 and b'PLTE' not in chunks):
        raise ValueError('incomplete PNG')
    expected=stride*height
    try:
        inflater=zlib.decompressobj()
        raw=inflater.decompress(b''.join(compressed),expected+1)
    except zlib.error as exc:raise ValueError('invalid PNG compressed pixels') from exc
    if (len(raw)!=expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail
            or any(raw[i]>4 for i in range(0,len(raw),stride))):
        raise ValueError('invalid PNG pixel stream size/filter')
    return dict(**header,byte_length=len(data),sha256=hashlib.sha256(data).hexdigest(),
                media_type='image/png')


def selected_marks(summary,scope):
    return [copy.deepcopy(m) for m in summary['marks']
            if m['channel']==scope['channel'] and m['address'] in scope['addresses']]


def validate_project_floorplan(document):
    from .projects import identity
    item=document.get('native_floorplan')
    if document['schema_version']==4 and 'native_floorplan' not in document:return
    if document['schema_version'] not in (3,4):
        if 'native_floorplan' in document:raise ValueError('native floorplan requires project schema3')
        return
    if not isinstance(item,dict) or item.get('context')!=CONTEXT:
        raise ValueError('schema3 requires native floorplan evidence')
    source=item['source_document']; summary=inspect_native_floorplan(source)
    if not summary['present']:raise ValueError('source has no floorplan')
    if item['source_document_sha256']!=hashlib.sha256(canonical(source)).hexdigest():
        raise ValueError('native floorplan source checksum mismatch')
    if canonical(item['summary'])!=canonical(summary) or identity(summary['gateway'])!=identity(document['gateway']):
        raise ValueError('native floorplan summary or gateway mismatch')
    asset=item['image'];encoded=asset['base64']
    if type(encoded) is not str or len(encoded)>((IMAGE_LIMIT+2)//3)*4:
        raise ValueError('PNG base64 exceeds limit')
    try:data=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError) as exc:raise ValueError('invalid image base64') from exc
    metadata=inspect_png(data)
    if canonical({k:v for k,v in asset.items() if k!='base64'})!=canonical(metadata):
        raise ValueError('PNG metadata/hash mismatch')
    chosen=selected_marks(summary,document['scope'])
    if not chosen or canonical(item['selected_marks'])!=canonical(chosen):
        raise ValueError('floorplan selected marks differ from captured project scope')
    if type(item['outside_capture_count']) is not int or item['outside_capture_count']!=summary['mark_count']-len(chosen):
        raise ValueError('floorplan omitted scope count mismatch')


class ProjectFloorplanMixin:
    def inspect_configmaster_floorplan(self,cache_id):
        record=self.get_configmaster_cache(cache_id,True)
        return dict(cache_id=cache_id,**inspect_native_floorplan(record['document']))

    def attach_project_floorplan(self,project_id,cache_id,image_path):
        from .projects import validate_document,digest,identity
        if type(image_path) is not str or image_path.startswith(('\\\\','//')):
            raise ValueError('provide an explicit absolute local PNG path')
        path=Path(image_path)
        if not path.is_absolute() or path.suffix.lower()!='.png':
            raise ValueError('provide an explicit absolute local PNG path')
        with self.lock:
            project=validate_document(self.get_project(project_id))
            record=self.get_configmaster_cache(cache_id,True)
            summary=inspect_native_floorplan(record['document'])
            if not summary['present']:raise ValueError('cache has no floorPlan')
            if identity(summary['gateway'])!=identity(project['gateway']):
                raise ValueError('floorplan/project gateway mismatch')
            chosen=selected_marks(summary,project['scope'])
            if not chosen:raise ValueError('no marked addresses in captured project scope')
            # Read only this explicit path; native image_reference is a label,
            # never an authority to resolve paths or fetch files automatically.
            with path.open('rb') as f:data=f.read(IMAGE_LIMIT+1)
            asset=inspect_png(data)
            asset['base64']=base64.b64encode(data).decode('ascii')
            result=copy.deepcopy(project)
            result.update(schema_version=4 if 'native_names' in project else 3,project_id=uuid.uuid4().hex,
                          origin='derived_project_with_native_floorplan',
                          parent_project=dict(project_id=project_id,digest=project['digest']),
                          floorplan_attached_at=time.time(),
                          native_floorplan=dict(context=CONTEXT,
                              source_document=record['document'],
                              source_document_sha256=record['document_sha256'],
                              summary=summary,image=asset,selected_marks=chosen,
                              outside_capture_count=summary['mark_count']-len(chosen)))
            result['digest']=digest(result)
            validate_document(result)
            self._save('project',result,result['project_id'])
            return dict(state='attached',project_id=result['project_id'],digest=result['digest'],
                        selected_marks=chosen,outside_capture_count=result['native_floorplan']['outside_capture_count'],
                        image={k:v for k,v in asset.items() if k!='base64'},
                        native_image_reference=summary['image_reference'],
                        image_binding='explicit caller-selected file; filename is not proof it matches the native reference',
                        device_writes=False,network_traffic=False,physical_mapping_verified=False)

    def get_project_floorplan(self,project_id,include_image=False):
        if type(include_image) is not bool:raise ValueError('include_image must be boolean')
        project=self.get_project(project_id)
        if project.get('state')!='completed':raise ValueError('project is incomplete')
        if 'native_floorplan' not in project:return dict(project_id=project_id,present=False)
        result=copy.deepcopy(project['native_floorplan'])
        result.pop('source_document')
        if not include_image:result['image'].pop('base64')
        return dict(project_id=project_id,present=True,**result)


def compare_floorplans(before,after):
    def projection(d):
        item=d.get('native_floorplan')
        if item is None:return None
        return dict(marks=sorted(item['selected_marks'],key=lambda m:(m['channel'],m['address'])),
                    image_sha256=item['image']['sha256'])
    left,right=projection(before),projection(after)
    state=('not_captured_both' if left is None and right is None else
           'not_captured_before' if left is None else 'not_captured_after' if right is None else
           'unchanged' if left==right else 'changed')
    return dict(state=state,before=left,after=right,
                comparison='Selected source marks and exact image bytes; not physical verification')
