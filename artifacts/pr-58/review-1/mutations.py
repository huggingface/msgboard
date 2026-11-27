from pathlib import Path
import subprocess,os,time,json,hashlib
root=Path('/home/node/local/git/msgboard-cx');out=Path('/home/node/local/tmp/reading-position/review1');p=root/'src/msgboard/dashboard.html';original=p.read_text();durable=Path('/data/workspaces/msg-board/engineer-cx/reading-position/review-1');durable.mkdir(exist_ok=True);(durable/'dashboard-before-mutations.html').write_text(original)
def change(old,new):
 assert original.count(old)==1,original.count(old)
 return original.replace(old,new,1)
variants={
 'no-late-restore':change('      if (bottom) sc.scrollTop = sc.scrollHeight; else if (restore) restore();','      if (bottom) sc.scrollTop = sc.scrollHeight;'),
 'no-synchronous-cache':change('if (cached?.image && cached.request === fileInfo.get(id))','if (false && cached?.image && cached.request === fileInfo.get(id))'),
 'wrong-fallback-offset':change('const node = part || message, offset = part ? off : messageOff;', 'const node = part || message, offset = off;'),
 'no-shared-decode':change('if (!entry || entry.request !== request) {', 'if (true) {'),
}
results={};env=os.environ.copy();env['PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH']='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'
try:
 for name,source in variants.items():
  p.write_text(source);start=time.monotonic()
  r=subprocess.run(['/home/node/local/envs/msgboard-cx/bin/python','-m','pytest','tests/test_tasks_layout.py::test_thread_panel_preserves_channel_reading_position','-q'],cwd=root,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=100)
  (out/(name+'.log')).write_text(r.stdout);results[name]={'exit_code':r.returncode,'seconds':round(time.monotonic()-start,2)};print(name,results[name],flush=True)
  assert r.returncode==1,(name,r.stdout[-2000:])
finally:
 p.write_text(original);assert hashlib.sha256(p.read_bytes()).digest()==hashlib.sha256(original.encode()).digest();(out/'mutations.json').write_text(json.dumps(results,indent=2)+'\n')
