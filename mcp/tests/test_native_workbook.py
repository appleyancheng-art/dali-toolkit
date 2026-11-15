"""Synthetic OOXML protocol fixtures, not user spreadsheets or device evidence."""
import copy
import io
import unittest
import xml.etree.ElementTree as ET
import zipfile
from dali_mcp.native_workbook import inspect_workbook, column, PREFIXES, SUFFIXES, PARAM_HEADERS


S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'


def document(addresses=(0,63)):
    sheets = {}
    def row(cells, number, values):
        cells.update({f'{column(i)}{number}':v for i,v in enumerate(values,1)})
    for prefix in PREFIXES:
        params, groups, scenes, blocks = {}, {}, {}, {}
        row(params,1,PARAM_HEADERS)
        row(groups,1,[None,None,None]+[f'G{g}' for g in range(16)])
        row(groups,2,['短地址','地址类型','名称']+['']*16)
        row(scenes,1,[None,None,None]+[v for s in range(16) for v in (f'场景{s}',None)])
        row(scenes,2,['短地址','地址类型','名称']+['亮度','色温']*16)
        for index,a in enumerate(addresses,2):
            row(params,index,[f'A{a}','DT8',f'灯{a}',4,7,0.1,100,0,'MASK',51,'MASK','对数',6535,2702])
            row(groups,index+1,[f'A{a}','DT8',f'灯{a}','√','√']+[None]*14)
            row(scenes,index+1,[f'A{a}','DT8',f'灯{a}']+[v for s in range(16) for v in ((100 if s==8 else 0),('MASK' if s==15 else 4000))])
        start=1
        for group in range(16):
            row(blocks,start,[f'G{group}',None,None]+[v for s in range(16) for v in (f'场景{s}'+('-夜间' if s==5 else ''),None)])
            row(blocks,start+1,['Group name '+str(group)])
            row(blocks,start+2,['短地址','地址类型','名称']+['亮度','色温']*16)
            members = addresses if group<2 else ()
            for offset,a in enumerate(members,3):
                row(blocks,start+offset,[f'A{a}','DT8',f'灯{a}']+[v for s in range(16) for v in ((100 if s==8 else 0),('MASK' if s==15 else 4000))])
            start += 4+len(members)
        for suffix,table in zip(SUFFIXES,(params,groups,scenes,blocks)):
            sheets[prefix+suffix]=table
    sheets['网关信息']={'A1':'字段','B1':'值','A2':'网关名称','B2':'Synthetic gateway',
                       'A3':'DALI MAC','B3':'02:00:00:00:00:01','A4':'KNX MAC','B4':'02:00:00:00:00:02'}
    return sheets


def package(sheets, transform=None):
    book=ET.Element('{'+S+'}workbook'); listing=ET.SubElement(book,'{'+S+'}sheets')
    rels=ET.Element('{'+P+'}Relationships'); parts={}
    for i,(name,cells) in enumerate(sheets.items(),1):
        ET.SubElement(listing,'{'+S+'}sheet',{'name':name,'sheetId':str(i),'{'+R+'}id':f'r{i}'})
        ET.SubElement(rels,'{'+P+'}Relationship',{'Id':f'r{i}','Type':R+'/worksheet','Target':f'worksheets/sheet{i}.xml'})
        sheet=ET.Element('{'+S+'}worksheet'); data=ET.SubElement(sheet,'{'+S+'}sheetData'); rows={}
        for ref,value in cells.items():
            rownum=''.join(c for c in ref if c.isdigit())
            if rownum not in rows:rows[rownum]=ET.SubElement(data,'{'+S+'}row',{'r':rownum})
            c=ET.SubElement(rows[rownum],'{'+S+'}c',{'r':ref})
            if isinstance(value,dict):
                ET.SubElement(c,'{'+S+'}f').text=value['formula'];ET.SubElement(c,'{'+S+'}v').text='100'
            elif isinstance(value,str):
                c.set('t','inlineStr');inline=ET.SubElement(c,'{'+S+'}is');ET.SubElement(inline,'{'+S+'}t').text=value
            elif value is not None:
                ET.SubElement(c,'{'+S+'}v').text=str(value)
        parts[f'xl/worksheets/sheet{i}.xml']=ET.tostring(sheet)
    parts['xl/workbook.xml']=ET.tostring(book)
    parts['xl/_rels/workbook.xml.rels']=ET.tostring(rels)
    if transform:transform(parts)
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in parts.items():z.writestr(name,data)
    return out.getvalue()


class NativeWorkbookTests(unittest.TestCase):
    def decode(self,d=None,transform=None):
        return inspect_workbook(package(d if d is not None else document(),transform))

    def test_both_channels_zero_based_addresses_scenes_names_and_groups(self):
        result=self.decode()
        self.assertEqual([c['channel'] for c in result['channels']],[1,2])
        for ch in result['channels']:
            self.assertEqual([r['address'] for r in ch['fixtures']],[0,63])
            self.assertEqual(ch['fixtures'][1]['scenes'][8]['brightness'],{'kind':'value','percent':100})
            self.assertEqual(ch['fixtures'][0]['groups'],[0,1])
            self.assertEqual(ch['group_scene_blocks'][0]['name'],'Group name 0')
            self.assertEqual(ch['group_scene_blocks'][0]['scene_names']['5'],'夜间')
            self.assertEqual(ch['scene_conflicts'],[])
        self.assertFalse(result['executable'] or result['online_addresses_verified'] or result['device_values_verified'])

    def test_brightness_is_preserved_as_percent_not_guessed_raw_level(self):
        f=self.decode()['channels'][0]['fixtures'][0]
        self.assertEqual(f['parameters']['min_level'],{'kind':'value','percent':0.1})
        self.assertEqual(f['parameters']['system_failure_level'],{'kind':'value','percent':51})
        self.assertEqual(f['parameters']['power_on_ct'],{'kind':'mask'})

    def test_blank_parameter_keeps_but_blank_scene_masks_and_zero_stays_off(self):
        d=document();d['通道一参数']['D2']=None;d['通道一ECG场景']['D3']='';d['通道一ECG场景']['E3']=None
        f=self.decode(d)['channels'][0]['fixtures'][0]
        self.assertEqual(f['parameters']['fade_time'],{'kind':'keep'})
        self.assertEqual(f['scenes'][0]['brightness'],{'kind':'mask'})
        self.assertEqual(f['scenes'][0]['color'],{'kind':'mask'})
        self.assertEqual(f['scenes'][1]['brightness'],{'kind':'value','percent':0})

    def test_ecg_group_and_group_group_conflicts_retained_without_overwrite(self):
        d=document();d['通道一组场景']['D4']=22
        ch=self.decode(d)['channels'][0]
        self.assertEqual({x['kind'] for x in ch['scene_conflicts']},{'ecg_vs_group','group_vs_group'})
        self.assertEqual(ch['fixtures'][0]['scenes'][0]['brightness']['percent'],0)
        self.assertEqual(ch['group_scene_blocks'][0]['rows'][0]['scenes'][0]['brightness']['percent'],22)

    def test_empty_channels_have_no_fixtures_despite_placeholder_labels(self):
        d=document(())
        for prefix in PREFIXES:
            for suffix,start in (('参数',2),('分组',3),('ECG场景',3)):
                for a in range(64):d[prefix+suffix][f'A{start+a}']=f'A{a}'
        self.assertEqual([len(c['fixtures']) for c in self.decode(d)['channels']],[0,0])

    def test_full64_fixture_range(self):
        self.assertEqual(len(self.decode(document(tuple(range(64))))['channels'][1]['fixtures']),64)

    def test_wrong_sheet_header_mac_and_cross_sheet_type_rejected(self):
        for sheet,cell,value in [('通道一参数','D1','other'),('网关信息','B3','unknown'),
                                 ('通道二分组','B3','DT6'),('通道一ECG场景','T1','场景7')]:
            with self.subTest(sheet=sheet,cell=cell):
                d=document();d[sheet][cell]=value
                with self.assertRaises(ValueError):self.decode(d)

    def test_invalid_numbers_markers_and_types_rejected(self):
        for cell,value in [('D2',16),('E2',1.5),('F2',-1),('G2',101),('I2','NaN'),('N2','inf'),('L2','unknown'),('B2','DT9')]:
            with self.subTest(cell=cell,value=value):
                d=document();d['通道一参数'][cell]=value
                with self.assertRaises(ValueError):self.decode(d)
        d=document();d['通道一分组']['D3']='yes'
        with self.assertRaises(ValueError):self.decode(d)

    def test_duplicate_address_rejected_even_in_placeholder_rows(self):
        d=document(());d['通道一参数'].update(A2='A0',A3='A0')
        with self.assertRaisesRegex(ValueError,'duplicate address'):self.decode(d)

    def test_formula_with_cached_value_is_rejected(self):
        d=document();d['通道一参数']['G2']={'formula':'1+99'}
        with self.assertRaisesRegex(ValueError,'formula'):self.decode(d)

    def test_external_relationship_never_followed(self):
        def alter(parts):
            parts['xl/worksheets/_rels/sheet1.xml.rels']=f'<Relationships xmlns="{P}"><Relationship Id="x" Target="https://example.invalid/data" TargetMode="External"/></Relationships>'.encode()
        with self.assertRaisesRegex(ValueError,'external'):self.decode(transform=alter)

    def test_utf16_dtd_rejected(self):
        def alter(parts):
            parts['xl/workbook.xml']='<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "text">]><x/>'.encode('utf-16')
        with self.assertRaisesRegex(ValueError,'DTD'):self.decode(transform=alter)

    def test_utf16_without_bom_cannot_hide_entity_declarations(self):
        def alter(parts):
            parts['xl/workbook.xml']='<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "text">]><x>&a;</x>'.encode('utf-16-le')
        with self.assertRaisesRegex(ValueError,'DTD'):self.decode(transform=alter)

    def test_unsafe_zip_path_and_expansion_limit_rejected(self):
        for path,value in [('../escape',b'x'),('xl/large.xml',b' '*8_000_001)]:
            with self.assertRaises(ValueError):self.decode(transform=lambda p:p.update({path:value}))

    def test_shared_strings_with_rich_text_read_without_formatting_noise(self):
        def alter(parts):
            root=ET.fromstring(parts['xl/worksheets/sheet1.xml'])
            c=next(c for c in root.iter('{'+S+'}c') if c.get('r')=='C2');c.clear();c.set('r','C2');c.set('t','s')
            ET.SubElement(c,'{'+S+'}v').text='0'
            parts['xl/worksheets/sheet1.xml']=ET.tostring(root)
            parts['xl/sharedStrings.xml']=f'<sst xmlns="{S}"><si><r><rPr><b/></rPr><t>灯</t></r><r><t>零</t></r></si></sst>'.encode()
        self.assertEqual(self.decode(transform=alter)['channels'][0]['fixtures'][0]['name'],'灯零')


if __name__=='__main__':unittest.main()
