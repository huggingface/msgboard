import json,sys,tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,str(Path.cwd()/'src'));sys.path.insert(0,str(Path.cwd()/'tests'))
from conftest import Env
with tempfile.TemporaryDirectory() as d:
 env=Env(Path(d)/'board')
 try:
  _,token,owner=env.human('review-owner');aid,_,agent=env.agent('review-agent',owner=owner)
  agent.put_task('one','Task');dm=owner.open_dm(aid['id']);agent.post(dm['id'],'One message')
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell')
   page=browser.new_page(viewport={'width':1280,'height':900});page.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+');')
   page.goto(env.url+'/#tasks');expect(page.locator('#tasks-dm-composer textarea')).to_be_visible()
   handle=page.locator('#tasks-dm-resize');handle.focus()
   for i in range(20):page.keyboard.press('ArrowDown')
   def bounds():return page.evaluate('''()=>Object.fromEntries(["tasks","tasks-dm-content","tasks-dm-messages","tasks-dm-composer"].map(id=>{const e=document.getElementById(id),r=e.getBoundingClientRect();return[id,{top:r.top,bottom:r.bottom,height:r.height,scrollHeight:e.scrollHeight}]}).concat([["send",(()=>{const r=document.querySelector("#tasks-dm-composer .btn").getBoundingClientRect();return{top:r.top,bottom:r.bottom}})()],["viewport",innerHeight]]))''')
   print('before_multiline',json.dumps(bounds()))
   page.locator('#tasks-dm-composer textarea').fill('\n'.join('line '+str(i) for i in range(18)))
   print('after_multiline',json.dumps(bounds()))
   print('send_visible',page.locator('#tasks-dm-composer .btn').is_visible())
   page.set_viewport_size({'width':1280,'height':400})
   print('short_viewport',json.dumps(bounds()))
   page.locator('#tasks-dm-composer .btn').scroll_into_view_if_needed()
   print('short_after_send_scroll',json.dumps(bounds()))
   page.set_viewport_size({'width':1280,'height':900})
   page.locator('#tasks-dm-composer textarea').fill('short draft')
   page.locator('#tasks-dm-composer .emoji-btn').click()
   popup=page.locator('#tasks-dm-composer .emoji-pop')
   print('popup',json.dumps(popup.evaluate('e=>{const r=e.getBoundingClientRect(),s=e.querySelector("input"),b=s.getBoundingClientRect();return {top:r.top,bottom:r.bottom,searchHit:document.elementFromPoint(b.x+20,b.y+b.height/2)?.outerHTML.slice(0,180)}}')))
   try:
    popup.locator('[data-e]').first.click(timeout=2000)
    print('emoji_pick','passed')
   except Exception as error: print('emoji_pick',type(error).__name__)
   page.locator('#tasks-dm-composer textarea').fill('@review')
   mention=page.locator('#tasks-dm-composer .mention-pop .opt')
   expect(mention.first).to_be_visible()
   mention.first.click(timeout=2000)
   print('mention_pick',page.locator('#tasks-dm-composer textarea').input_value())
   r=handle.bounding_box();initial=page.locator('#tasks-dm-content').bounding_box()['height']
   cdp=page.context.new_cdp_session(page);x=r['x']+r['width']/2;y=r['y']+6
   cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
   for n in range(1,11):cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-n*12}]})
   cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
   after_touch=page.locator('#tasks-dm-content').bounding_box()['height']
   print('touch_resize',json.dumps({'before':initial,'after':after_touch}))
   assert after_touch>initial+100
   page.screenshot(path='/tmp/agent-resize-multiline.png')
   browser.close()
 finally:env.close()
