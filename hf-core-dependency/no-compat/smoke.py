from pathlib import Path
import json,importlib.metadata,subprocess,sys,tempfile
from packaging.requirements import Requirement
import msgboard
from msgboard.client import Client
from msgboard.local import ThreadedServer
from huggingface_hub import HfApi, SpaceCard
out=Path(__file__).parent
assert '/msgboard-core-no-compat-smoke/' in msgboard.__file__,msgboard.__file__
dist=importlib.metadata.distribution('msgboard')
base=[Requirement(r) for r in dist.requires or [] if Requirement(r).marker is None]
assert any(r.name=='huggingface_hub' and str(r.specifier)=='>=1.31' for r in base)
assert 'hf' not in dist.metadata.get_all('Provides-Extra',[])
for args in [[],['deploy-space']]:
 r=subprocess.run([sys.executable,'-m','msgboard',*args,'--help'],capture_output=True,text=True,check=True)
 assert 'needs the hf extra' not in r.stdout
with tempfile.TemporaryDirectory(prefix='msgboard-core-smoke-') as temp:
 data=Path(temp)/'board'
 subprocess.run([sys.executable,'-m','msgboard','init','--data-dir',str(data)],capture_output=True,text=True,check=True)
 token=(data/'operator.token').read_text().strip()
 with ThreadedServer(data) as server,Client(server.url,token) as op:
  ch=op.create_channel('smoke');posted=op.post(ch['id'],'Standard install works')
  assert op.read(ch['id'])['messages'][0]['id']==posted['id']
result={'package':'msgboard','version':dist.version,'sdk':importlib.metadata.version('huggingface_hub'),'sdk_is_core_dependency':True,'legacy_hf_extra_declared':False,'cli_help_and_deploy_help':True,'real_local_board_round_trip':True,'source':'installed wheel, no extras'}
(out/'smoke.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
