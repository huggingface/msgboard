from pathlib import Path
import subprocess,time,json
p=Path('/home/node/local/git/msgboard-cx/src/msgboard/dashboard.html');original=p.read_text();out=Path('/home/node/local/tmp/reading-position/review1');results={}
variants={
 'body-child-replaced':('if (part && bodyText !== null && part.textContent !== bodyText) part = null;','if (false && part && bodyText !== null && part.textContent !== bodyText) part = null;'),
 'first-positive-part':('r.bottom > bounds.top + 1 && r.top < bounds.bottom && (!anchor || distance < anchor.distance)','r.top >= bounds.top && r.top < bounds.bottom && !anchor'),
}
try:
 for name,(old,new) in variants.items():
  assert original.count(old)==1;p.write_text(original.replace(old,new,1));start=time.monotonic()
  r=subprocess.run(['/home/node/local/envs/msgboard-cx/bin/python',str(out/'rich-driver.py')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=100)
  (out/(name+'.log')).write_text(r.stdout);results[name]={'exit_code':r.returncode,'seconds':round(time.monotonic()-start,2)};print(name,results[name],flush=True);assert (r.returncode!=0 if name == 'first-positive-part' else r.returncode==0)
finally:
 p.write_text(original);(out/'rich-mutations.json').write_text(json.dumps(results,indent=2)+'\n')
