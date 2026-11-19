import json,sys,tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.conftest import Env
out=Path(__file__).parent
with tempfile.TemporaryDirectory(prefix='subscription-review-') as temp:
 env=Env(Path(temp)/'board')
 try:
  _,token,reader=env.human('person-a');_,_,writer=env.human('person-b')
  ch=writer.create_channel('checks');root=writer.post(ch['id'],'A discussion');tid=root['id'];reader.subscribe_thread(ch['id'],tid)
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell')
   def context(**kw):
    c=browser.new_context(**kw);c.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+')');c.route('https://cdnjs.cloudflare.com/**',lambda r:r.abort());return c
   ctx=context(viewport={'width':1280,'height':900});page=ctx.new_page();page.goto(env.url+'/#checks')
   post=page.locator(f'#messages [data-mid="{tid}"] [data-thread-subscribe]');expect(post).to_have_attribute('aria-pressed','true')
   hidden=post.locator('..').evaluate('el=>({opacity:getComputedStyle(el).opacity,pointerEvents:getComputedStyle(el).pointerEvents})')
   other=env.client(token);other.subscribe_thread(ch['id'],tid,False);page.evaluate('window.dispatchEvent(new Event("focus"))')
   # A response/badge is observable; no fixed delay or direct calls into app functions.
   reply=writer.post(ch['id'],'An unmentioned reply',reply_to=tid)
   expect(page.locator(f'#messages [data-mid="{tid}"] .replies b')).to_have_text('1 reply')
   expect(page.locator('#channels')).to_contain_text('checks')
   desktop={'bell_after_remote_unsubscribe':post.get_attribute('aria-pressed'),'thread_badges':page.locator(f'#messages [data-mid="{tid}"] .rbadge').all_text_contents(),'server_for_me':next(x for x in reader.list_channels() if x['id']==ch['id'])['for_me'],'hidden_toolbar':hidden}
   phone=context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);mobile=phone.new_page();mobile.goto(env.url+'/#checks')
   bell=mobile.locator(f'#messages [data-mid="{tid}"] [data-thread-subscribe]');expect(bell).to_be_enabled();toolbar=bell.locator('..');opacity=toolbar.evaluate('el=>getComputedStyle(el).opacity');before=reader.thread_subscription(ch['id'],tid)['subscribed'];box=bell.bounding_box();mobile.touchscreen.tap(box['x']+box['width']/2,box['y']+box['height']/2)
   # The pressed state changes once a real PUT completes.
   expect(bell).to_have_attribute('aria-pressed',str(not before).lower())
   after=reader.thread_subscription(ch['id'],tid)['subscribed']
   result={'desktop':desktop,'touch':{'toolbar_opacity_before_tap':opacity,'before':before,'after':after,'invisible_tap_changed_preference':opacity=='0' and before!=after}}
   (out/(sys.argv[1]+'.json')).write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2));phone.close();ctx.close();browser.close()
 finally:env.close()
