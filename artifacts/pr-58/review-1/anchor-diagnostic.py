from pathlib import Path
import io,json,sys,tempfile,re,struct,zlib,subprocess
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,'/home/node/local/git/msgboard-cx')
from tests.conftest import Env
e=Env(Path(tempfile.mkdtemp(prefix='reading-repro-'))/'board')
try:
 _,token,reader=e.human('reader');ch=e.op.create_channel('reading');chunk=lambda t,b:struct.pack('>I',len(b))+t+b+struct.pack('>I',zlib.crc32(t+b))
 png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1200,900,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+bytes([230,230,230])*1200)*900))+chunk(b'IEND',b'');buf=io.BytesIO(png);messages=[]
 for i in range(45):
  at=[]
  if i in (4,12):at=[e.op.upload(ch['id'],buf.getvalue(),filename=f'preview-{i}.png',content_type='image/png')['id']]
  messages.append(e.op.post(ch['id'],f'Message {i}: '+('A paragraph that wraps when the thread opens. '*(250 if i==30 else 12)),attachments=at))
 e.op.post(ch['id'],'A reply',reply_to=messages[22]['id'])
 e.op.post(ch['id'],'A second discussion',reply_to=messages[30]['id'])
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell');ctx=browser.new_context(viewport={'width':1440,'height':850});ctx.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+')');ctx.route('https://cdnjs.cloudflare.com/**',lambda r:r.abort())
  for url,filename in json.loads(Path('/home/node/local/tmp/reading-position/renderer/manifest.json').read_text()).items():
   ctx.route(url,lambda route,request,filename=filename:route.fulfill(body=Path(filename).read_bytes(),content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'}))
  if '--baseline' in sys.argv:
   old=subprocess.check_output(['git','show','f4a33aeae6965e7826d6c4b1410d05f312dfb5b2:src/msgboard/dashboard.html'],cwd='/home/node/local/git/msgboard-cx')
   ctx.route(e.url+'/',lambda route:route.fulfill(body=old,content_type='text/html'))
  page=ctx.new_page();held=[];blocking=[True]
  def hold(route):
   if route.request.url.endswith('/info') or not blocking[0]:route.continue_()
   else:held.append(route)
  page.route('**/v1/attachments/**',hold);page.goto(e.url+'/#reading');page.wait_for_function('window.marked && window.DOMPurify && window.katex');page.locator('#channels').get_by_text('reading',exact=True).click();expect(page.locator('#messages .body.md')).to_have_count(45);expect(page.locator('#messages .fchip .fname').filter(has_text=re.compile(r'\.png$'))).to_have_count(2)
  gap=lambda:page.locator('#scroll').evaluate('e=>e.scrollHeight-e.scrollTop-e.clientHeight')
  initial=gap();blocking[0]=False
  for route in held:route.continue_()
  page.wait_for_function("[...document.querySelectorAll('#messages .fchip img')].filter(i=>i.complete&&i.naturalWidth>0).length===2")
  after_images=gap();target=page.locator(f'#messages .msg[data-mid="{messages[22]["id"]}"]');target.evaluate("e=>{const s=document.getElementById('scroll');s.scrollTop+=e.getBoundingClientRect().top-s.getBoundingClientRect().top-40}");before=target.bounding_box()['y']
  candidates=page.evaluate("""() => {
 const sc=document.getElementById('scroll'), b=sc.getBoundingClientRect(), list=[];
 for(const m of sc.querySelectorAll('.msg[data-mid]')) { const r=m.getBoundingClientRect();if(r.bottom<=b.top+1 || r.top>=b.bottom)continue;
 const body=m.querySelector('.body'),parts=[[m,''],[body,'.body'],...[...(body?.children||[])].map((n,i)=>[n,'.body > :nth-child('+(i+1)+')']),[m.querySelector('.files'),'.files'],[m.querySelector('.replies'),'.replies']];
 for(const [n,selector] of parts){if(!n)continue;const r=n.getBoundingClientRect();if(r.bottom>b.top+1&&r.top<b.bottom) list.push({id:m.dataset.mid,selector,top:r.top-b.top,bottom:r.bottom-b.top});}}
 return list.sort((a,b)=>Math.abs(a.top)-Math.abs(b.top)).slice(0,4);
 }""");print('ANCHORS_BEFORE',json.dumps(candidates),flush=True)
  target.locator('.replies').click();expect(page.locator('#panel-composer textarea')).to_be_visible();expect(target).to_have_class(re.compile('open-thread'))
  page.wait_for_function("[...document.querySelectorAll('#messages .fchip img')].filter(i=>i.complete&&i.naturalWidth>0).length===2")
  page.wait_for_timeout(650)
  print('ANCHOR_AFTER', page.evaluate("a=>{const sc=document.getElementById('scroll'), m=sc.querySelector('.msg[data-mid=\"'+a.id+'\"]'),n=a.selector?m.querySelector(a.selector):m;return n.getBoundingClientRect().top-sc.getBoundingClientRect().top-a.top}",candidates[0]),flush=True)
  after=target.bounding_box()['y'];page.locator('#panel-close').click();page.wait_for_timeout(650);after_close=target.bounding_box()['y']
  pre_join=target.bounding_box()['y'];expect(page.locator('#ch-join')).to_be_visible();page.locator('#ch-join').click();expect(page.locator('#ch-join')).not_to_be_visible();joined=target.bounding_box()['y']
  long=page.locator(f'#messages .msg[data-mid="{messages[30]["id"]}"]');badge=long.locator('.replies');badge.scroll_into_view_if_needed();long_before=badge.bounding_box()['y'];badge.click();expect(long).to_have_class(re.compile('open-thread'));page.wait_for_timeout(650);long_after=badge.bounding_box()['y']
  # page.screenshot(path='/data/workspaces/msg-board/engineer-cx/reading-position/'+('before.png' if '--baseline' in sys.argv else 'after.png'))
  version_check=None
  if '--baseline' not in sys.argv:
   page.locator('#panel-close').click()
   uploaded=reader.storage_put('refresh.png',buf.getvalue(),content_type='image/png');sid='storage:'+reader.identity['external_id']+'/refresh.png'
   e.op.post(ch['id'],'An inline storage image: ![picture]('+uploaded['url']+')')
   page.wait_for_function("id => document.querySelector('#messages [data-file=\"'+id+'\"] img')?.naturalHeight===900",arg=sid)
   pic=page.locator('#messages [data-file="'+sid+'"] img');pic.click();page.wait_for_function("document.querySelector('#v-body img')?.naturalHeight===900");page.locator('#v-close').click()
   badge.scroll_into_view_if_needed();storage_before=badge.bounding_box()['y'];badge.click();expect(long).to_have_class(re.compile('open-thread'));page.wait_for_timeout(650)
   version_check={'inline_image_decoded':True,'viewer_fresh_info_loaded':True,'thread_anchor_delta':badge.bounding_box()['y']-storage_before}
   assert abs(version_check['thread_anchor_delta'])<3
  print(json.dumps({'actual_markdown_renderer':True,'storage_inline_check':version_check,'long_message_reply_button_delta':long_after-long_before,'initial_bottom_gap':initial,'after_attachment_load_gap':after_images,'thread_open_anchor_delta':after-before,'thread_close_anchor_delta':after_close-before,'membership_join_anchor_delta':joined-pre_join,'held_images':len(held)}),flush=True);browser.close()
finally:e.close()
