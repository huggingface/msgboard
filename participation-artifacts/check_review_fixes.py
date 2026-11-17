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
  channels={name:env.op.create_channel(name)['id'] for name in ['retry-left','retry-joined','retry-read-error','retry-known-joined-read-error','retry-known-left-read-error']}
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
    known_joined=name.startswith('retry-known-')
    keep_joined=name in ('retry-joined','retry-known-joined-read-error')
    if known_joined:
     env.op.add_member(cid,identity['id']);p.evaluate('refreshChannels()')
    p.goto(env.url+'/#'+name);expect(box).to_be_visible()
    if known_joined:expect(p.locator('#ch-join')).not_to_be_visible()
    else:expect(p.locator('#ch-join')).to_be_visible()
    # Hold periodic membership reconciliation until the retained-state assertion.
    if known_joined:p.route('**/v1/channels',lambda r:r.fulfill(status=503,json={'error':{'code':'busy','message':'busy'}}))
    seen=[];pattern='**/v1/channels/'+str(cid)+'/messages'
    def lost(route):
     if route.request.method!='POST':return route.continue_()
     seen.append(route.request.headers.get('idempotency-key'))
     if len(seen)==1:
      reply=route.fetch();assert reply.status==201
      if not keep_joined:sender.remove_member(cid,identity['id'])
      route.abort('failed')
     else:route.continue_()
    p.route(pattern,lost)
    fail_pattern='**/v1/channels/'+str(cid)+'/members'
    if name.endswith('read-error'):p.route(fail_pattern,lambda r:r.fulfill(status=503,json={'error':{'code':'busy','message':'busy'}}),times=1)
    box.fill('A single message despite the lost response.');p.locator('#composer .btn').click();expect(box).to_have_value('')
    assert len(seen)==2 and len(set(seen))==1
    assert len(env.op.read(cid)['messages'])==1
    actual=next(c for c in sender.list_channels() if c['id']==cid)['member']
    assert actual==keep_joined
    immediate=known_joined if name.endswith('read-error') else actual
    if immediate:expect(p.locator('#ch-join')).not_to_be_visible()
    else:expect(p.locator('#ch-join')).to_be_visible()
    if known_joined:
     p.unroute('**/v1/channels');p.evaluate('refreshChannels()')
     if actual:expect(p.locator('#ch-join')).not_to_be_visible()
     else:expect(p.locator('#ch-join')).to_be_visible()
    if name=='retry-left':p.screenshot(path=str(OUT/'replay-left.png'))
    checks.append(name+': one message, two same-key POST attempts; UI matches authoritative membership')
    p.unroute(pattern)
    if name.endswith('read-error'):p.unroute(fail_pattern)
   # An older channels response cannot overwrite a newly acknowledged local join.
   stale=env.op.create_channel('stale-channel-refresh')['id']
   p.reload();expect(box).to_be_visible();p.goto(env.url+'/#stale-channel-refresh');expect(box).to_be_visible()
   pending_channels=[]
   def hold_channels(route):pending_channels.append((route,route.fetch()))
   p.route('**/v1/channels',hold_channels,times=1);p.evaluate('void refreshChannels()');p.wait_for_timeout(150);assert pending_channels
   box.fill('New post joins despite an older pending refresh.');p.locator('#composer .btn').click();expect(box).to_have_value('')
   expect(p.locator('#ch-join')).not_to_be_visible()
   pending_channels[0][0].fulfill(response=pending_channels[0][1]);p.wait_for_timeout(150)
   expect(p.locator('#ch-join')).not_to_be_visible()
   assert next(c for c in sender.list_channels() if c['id']==stale)['member']
   checks.append('old channel refresh cannot overwrite a more recent successful local join')
   # Also protect a new join when the last known value was already true (same-value local update).
   stale_known=env.op.create_channel('stale-known-refresh')['id'];env.op.add_member(stale_known,identity['id'])
   p.reload();expect(box).to_be_visible();p.goto(env.url+'/#stale-known-refresh');expect(box).to_be_visible()
   expect(p.locator('#ch-join')).not_to_be_visible();sender.remove_member(stale_known,identity['id'])
   pending_channels=[]
   p.route('**/v1/channels',hold_channels,times=1);p.evaluate('void refreshChannels()');p.wait_for_timeout(150);assert pending_channels
   box.fill('A newer successful join keeps its revision even when already known joined.');p.locator('#composer .btn').click();expect(box).to_have_value('')
   expect(p.locator('#ch-join')).not_to_be_visible();pending_channels[0][0].fulfill(response=pending_channels[0][1]);p.wait_for_timeout(150)
   expect(p.locator('#ch-join')).not_to_be_visible()
   assert next(c for c in sender.list_channels() if c['id']==stale_known)['member']
   checks.append('same-value local membership update also rejects an older channel response')
   assert not errors,errors
   b.close()
 finally:env.close()
result={'checks':checks,'page_errors':errors}
(OUT/'review-fixes-results.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
