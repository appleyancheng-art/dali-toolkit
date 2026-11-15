"""Offline two-gateway model. No sockets, no physical devices."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dali_mcp import gateway_discovery as discovery, service

COUNT=0
ROWS=[dict(ip='192.168.77.99',name='Site-A',dali_mac='02:00:00:00:00:11',dali_firmware='1.6.9',route='0115f0',
           local_ip='192.168.77.10',interface_index=8,identity_conflict=False),
      dict(ip='192.168.77.22',name='Site-B',dali_mac='02:00:00:00:00:12',dali_firmware='1.6.9',route='0215f0',
           local_ip='192.168.77.10',interface_index=8,identity_conflict=False)]

class ModelGateway:
    def __init__(self,host,local_ip,*args,**kwargs):
        self.info=next(dict(x) for x in ROWS if x['ip']==host)
    def discover(self):return dict(self.info)
    def connect(self):return dict(channels=2,route=self.info['route']+'abcd00')
    def close(self):pass
    def light_type(self,channel,address):return 1
    def read_actual(self,channel,address,*args):
        if address==63:time.sleep(1)
        return dict(channel=channel,address=address,light_type_code=1,level_raw=0,kelvin_raw=4000,model_site=self.info['name'])

def search(interfaces,timeout_seconds):
    global COUNT
    COUNT+=1
    if COUNT==2:ROWS[0]['ip']='192.168.77.88'
    return dict(state='completed',gateways=[*[dict(x) for x in ROWS],
        dict(ROWS[1],ip='192.168.77.23',dali_mac='02:00:00:00:00:13',dali_firmware='9.9.9')],lighting_commands_sent=False)

original_verify=discovery.verify_connection
discovery.verify_connection=lambda candidate:original_verify(candidate,gateway_factory=ModelGateway)
discovery.windows_interfaces=lambda:[dict(local_ip='192.168.77.10',prefix_length=24,interface_index=8,name='Model')]
discovery.search_gateways=search
service.Gateway=ModelGateway
from dali_mcp.__main__ import main
main()
