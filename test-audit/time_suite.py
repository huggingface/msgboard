import json,re,subprocess,sys,time
from pathlib import Path
out=Path('/data/workspaces/msg-board/engineer-cx/test-audit')
label=sys.argv[1]
start=time.perf_counter()
with (out/(label+'.log')).open('w') as f:
 p=subprocess.run(['/home/node/local/envs/msgboard-cx/bin/python','-m','pytest','-q','--durations=0','--durations-min=0','--junitxml='+str(out/(label+'.xml'))],stdout=f,stderr=subprocess.STDOUT,cwd='/home/node/local/git/msgboard-cx')
wall=time.perf_counter()-start
rows=[]
for line in (out/(label+'.log')).read_text().splitlines():
 m=re.match(r'([\d.]+)s (setup|call|teardown)\s+(.*)',line)
 if m:rows.append({'seconds':float(m[1]),'phase':m[2],'test':m[3]})
result={'wall_s':wall,'exit_code':p.returncode,'phase_s':{k:sum(x['seconds'] for x in rows if x['phase']==k) for k in ['setup','call','teardown']},'timings':rows}
(out/(label+'.json')).write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='timings'}));sys.exit(p.returncode)
