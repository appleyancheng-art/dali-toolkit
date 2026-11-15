"""Loopback-only SDK fixture; never opens a physical gateway."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_adapter import Emulator
from dali_mcp import service
from dali_mcp.protocol import Gateway

config=json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
assert config['gateway_ip']==config['local_ip']=='127.0.0.1'
em=Emulator();em.recall_receipt=config.get('model_receipt','captured')
em.parameter_drop=config.get('model_parameter_drop')
em.drop_extreme=config.get('model_drop_extreme',False)
for ch in (1,2):
    for address in range(64):em.live[ch,address]=(0,4000)
em.values[2,0,8]=(180,5000)
service.Gateway=lambda *args,**kwargs:Gateway('127.0.0.1','127.0.0.1',0,.15,em.port)
try:
    from dali_mcp.__main__ import main
    main()
finally:
    em.stop()
    audit=dict(
        recalls=[x.hex() for x in em.recalls],writes=len(em.writes),
        live_commands=len(em.live_commands),scans=len(em.scans))
    if config.get('model_include_extremes'):
        audit.update(extremes=[x.hex() for x in em.extreme_commands],
            live={f'{ch}:{address}':list(v) for (ch,address),v in em.live.items()})
    Path(config['model_audit']).write_text(json.dumps(audit),encoding='utf-8')
