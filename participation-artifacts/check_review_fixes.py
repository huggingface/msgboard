import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, '/home/node/local/git/msgboard-cx')
from tests.conftest import Env

OUT=Path(__file__).parent
LIB=OUT.parent/'msgboard-pr15/lib'
CHROME='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'
checks=[];errors=[]
with tempfile.TemporaryDirectory(prefix='participation-review-',dir='/home/node/local') as tmp:
 env=Env(Path(tmp)/'board')
 try:
  channel=env.op.create_channel('planning')['id']
  identity,token,sender=env.human('person-b',role='admin',display_name='Person B')
  bot,_,helper=env.agent('probe-bot',display_name='ProbeOld',channels=['planning'])
  helper.post(channel,'Review fixture')
  race=env.op.create_channel('race')['id'];env.op.add_member(race,bot['id']);helper.post(race,'Race fixture')
  channels={name:env.op.create_channel(name)['id'] for name in ['retry-left','retry-joined','retry-read-error']}
  with sync_playwright() as pw:
   b=pw.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
   ctx=b.new_context(viewport={'width':1280,'height':850})
   ctx.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+');')
   def lib(route):
    f=LIB/urlsplit(route.request.url).path.rsplit('/',1)[-1]
    if f.exists():route.fulfill(content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'},body=f.read_bytes())
    else:route.abort()
   ctx.route('https://cdnjs.cloudflare.com/**',lib)
   p=ctx.new_page();p.on('pageerror',lambda e:errors.append(str(e)))
   p.on('dialog',lambda d:d.accept('test suspension'))
   p.goto(env.url+'/#planning');box=p.locator('#composer textarea');expect(box).to_be_visible();p.wait_for_function('md!==null')
   def suggestions(query='@probe'):
    box.fill('');box.fill(query)
   def profile():
    p.evaluate('(id)=>openProfile(id)',bot['id']);expect(p.locator('#name-edit')).to_be_visible()
   def rename(name):
    p.locator('#name-edit').click();p.locator('#name-input').fill(name);p.locator('#name-form [type=submit]').click()
    expect(p.locator('#name-row h3')).to_have_text(name)
   suggestions();expect(p.locator('.mention-pop')).to_contain_text('ProbeOld')
   profile();rename('ProbeNew');p.locator('#panel-close').click()
   suggestions();expect(p.locator('.mention-pop')).to_contain_text('ProbeNew');expect(p.locator('.mention-pop')).not_to_contain_text('ProbeOld')
   suggestions('@ProbeOld');expect(p.locator('.mention-pop')).to_have_count(0)
   checks.append('profile rename invalidates cached candidates and old name no longer appears')
   suggestions();expect(p.locator('.mention-pop')).to_contain_text('ProbeNew')
   profile();p.locator('#pic-menu-btn').click();p.locator('#pic-regen').click();p.locator('#sug-use').click()
   expect(p.locator('#suggest')).not_to_be_visible();p.locator('#panel-close').click()
   with p.expect_response(lambda r:'/mention-candidates' in r.url) as refreshed:suggestions()
   seed=next(c for c in refreshed.value.json()['candidates'] if c['id']==bot['id'])['pattern']
   assert isinstance(seed,int)
   checks.append('changing generated picture forces a fresh candidate response with the new pattern')
   profile();p.locator('#prof-suspend').click();expect(p.locator('#prof-unsuspend')).to_be_visible();p.locator('#panel-close').click()
   suggestions();expect(p.locator('.mention-pop')).to_have_count(0)
   checks.append('locally suspended account disappears from already-used channel suggestions')
   profile();p.locator('#prof-unsuspend').click();expect(p.locator('#prof-suspend')).to_be_visible();p.locator('#panel-close').click()
   suggestions();expect(p.locator('.mention-pop')).to_contain_text('ProbeNew')
   checks.append('unsuspending account restores it to suggestions without reopening channel')
   p.goto(env.url+'/#race');expect(box).to_be_visible();pending=[]
   pattern='**/v1/channels/'+str(race)+'/mention-candidates'
   def held(route):pending.append((route,route.fetch()))
   p.route(pattern,held,times=1);suggestions();p.wait_for_timeout(200);assert pending
   profile();rename('ProbeLatest');p.locator('#panel-close').click()
   pending[0][0].fulfill(response=pending[0][1]);p.wait_for_timeout(200)
   suggestions();expect(p.locator('.mention-pop')).to_contain_text('ProbeLatest');expect(p.locator('.mention-pop')).not_to_contain_text('ProbeNew')
   checks.append('candidate fetch held across a profile rename cannot repopulate stale cache')
   for name,cid in channels.items():
    p.goto(env.url+'/#'+name);expect(box).to_be_visible();expect(p.locator('#ch-join')).to_be_visible()
    seen=[];pattern='**/v1/channels/'+str(cid)+'/messages'
    def lost(route):
     if route.request.method!='POST':return route.continue_()
     seen.append(route.request.headers.get('idempotency-key'))
     if len(seen)==1:
      reply=route.fetch();assert reply.status==201
      if name!='retry-joined':sender.remove_member(cid,identity['id'])
      route.abort('failed')
     else:route.continue_()
    p.route(pattern,lost)
    fail_pattern='**/v1/channels/'+str(cid)+'/members'
    if name=='retry-read-error':p.route(fail_pattern,lambda r:r.fulfill(status=503,json={'error':{'code':'busy','message':'busy'}}),times=1)
    box.fill('A single message despite the lost response.');p.locator('#composer .btn').click();expect(box).to_have_value('')
    assert len(seen)==2 and len(set(seen))==1
    assert len(env.op.read(cid)['messages'])==1
    actual=next(c for c in sender.list_channels() if c['id']==cid)['member']
    assert actual==(name=='retry-joined')
    if actual:expect(p.locator('#ch-join')).not_to_be_visible()
    else:expect(p.locator('#ch-join')).to_be_visible()
    if name=='retry-left':p.screenshot(path=str(OUT/'replay-left.png'))
    checks.append(name+': one message, two same-key POST attempts; UI matches authoritative membership')
    p.unroute(pattern)
    if name=='retry-read-error':p.unroute(fail_pattern)
   assert not errors,errors
   b.close()
 finally:env.close()
result={'checks':checks,'page_errors':errors}
(OUT/'review-fixes-results.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
