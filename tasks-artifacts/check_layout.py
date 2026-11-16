import json,pathlib,sys,tempfile,urllib.parse
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,'/home/node/local/git/msgboard-cx/tests')
from conftest import Env
out=pathlib.Path(__file__).parent;lib=out.parent.parent/'msgboard-pr15/lib';results={};errors=[]
with tempfile.TemporaryDirectory() as d:
 env=Env(pathlib.Path(d)/'board')
 try:
  oid,token,owner=env.human('owner',role='admin');aid,_,agent=env.agent('agent-a',owner=owner)
  ch=owner.create_channel('planning');one=agent.post(ch['id'],'FIRST QUOTED MESSAGE');two=agent.post(ch['id'],'SECOND QUOTED MESSAGE')
  item=agent.put_task('blocks','Interleaved quote example',description='Introduction to the question.',anchor_id=one['id'],blocks=[{'type':'text','text':'First context:'},{'type':'quote','message_id':one['id']},{'type':'text','text':'More text between the quotes.'},{'type':'quote','message_id':two['id']},{'type':'text','text':'Final question after both quotes.'}],options=[{'id':'yes','label':'Use this plan'},{'id':'no','label':'Try another plan'}])
  plain=agent.put_task('plain','A second task',description='A normal description.',sources=[one['id']])
  for kind in ['done','dismiss']:
   i=agent.put_task(kind,'Archived '+kind);owner.task_action(i['id'],kind,i['version'])
  with sync_playwright() as pw:
   b=pw.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',args=['--no-sandbox'])
   ctx=b.new_context(viewport={'width':1440,'height':1050});ctx.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+');')
   def pinned(r):
    f=lib/urllib.parse.urlsplit(r.request.url).path.rsplit('/',1)[-1]
    if f.exists():r.fulfill(content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'},body=f.read_bytes())
    else:r.abort()
   ctx.add_init_script("localStorage.setItem("+json.dumps("msgboard:queue-choices:"+str(oid["id"])+":"+str(item["id"]))+","+json.dumps(json.dumps(["yes"]))+");")
   ctx.route('https://cdnjs.cloudflare.com/**',pinned);p=ctx.new_page();p.on('pageerror',lambda e:errors.append(str(e)))
   p.goto(env.url+'/#tasks');expect(p.locator('#tasks-list .tasks-row')).to_have_count(2);expect(p.locator('#tasks-dm-composer textarea')).to_be_visible()
   bounds=lambda sel:p.locator(sel).evaluate('e=>{let r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}}')
   nav={name:bounds(sel) for name,sel in {'search':'#search-input','tasks':'#tasks-open','activity':'#activity-open'}.items()}
   assert len({x['left'] for x in nav.values()})==len({x['right'] for x in nav.values()})==1,nav
   assert abs(nav['activity']['top']-nav['tasks']['bottom'])<=1,nav
   assert p.locator('#tasks-open svg rect').count()==3
   listing=bounds('#tasks-list');chat=bounds('#tasks-dm-composer .cbox')
   assert abs(listing['left']-chat['left'])<=1 and abs(listing['right']-chat['right'])<=1,(listing,chat)
   assert p.locator('.tasks-summary').evaluate('e=>getComputedStyle(e).borderTopWidth')=='0px'
   results['sidebar']=nav;results['list_chat_alignment']={'list':listing,'chat':chat}
   handle=p.locator('#tasks-dm-resize');height=lambda:p.locator('#tasks-dm-content').bounding_box()['height']
   initial=height();r=handle.bounding_box();p.mouse.move(r['x']+100,r['y']+6);p.mouse.down();p.mouse.move(r['x']+100,r['y']-144,steps=12);p.mouse.up();grown=height();assert grown>initial+100,(initial,grown)
   handle.focus();p.keyboard.press('ArrowDown');assert abs(height()-(grown-24))<2
   saved=height();p.reload();expect(p.locator('#tasks-dm-composer textarea')).to_be_visible();assert abs(height()-saved)<2
   draft=p.locator('#tasks-dm-composer textarea');draft.fill('Long draft line\n'*18)
   p.wait_for_timeout(150);send=p.locator('#tasks-dm-composer button.btn');assert send.bounding_box()['y']+send.bounding_box()['height']<=p.viewport_size['height']
   results['agent_resize_long_draft']={'height':height(),'send_bottom':send.bounding_box()['y']+send.bounding_box()['height']}
   draft.fill('');p.wait_for_timeout(100);assert abs(height()-saved)<2

   p.locator('#tasks-dm-hide').click();expect(handle).not_to_be_visible();p.locator('#tasks-agent-tab').click();assert abs(height()-saved)<2
   p.set_viewport_size({'width':1440,'height':520});assert height()<=p.locator('#tasks').bounding_box()['height']-100
   assert p.locator('#tasks-scroll').bounding_box()['height']>=100
   p.set_viewport_size({'width':1440,'height':1050});handle.focus();p.keyboard.press('Home');assert abs(height()-initial)<2
   results['agent_resize']={'initial':initial,'dragged':grown,'persisted':saved,'keyboard_reset':height()}
   p.screenshot(path=str(out/'tasks-list.png'))
   p.locator('#tasks-show-archived').check();expect(p.locator('#tasks-list .closed')).to_have_count(2)
   assert p.locator('.tasks-row-mark[aria-label=Completed] svg path').get_attribute('d')==p.locator('#tasks-list [data-tasks-action=done] svg path').first.get_attribute('d')
   assert p.locator('.tasks-row-mark[aria-label=Dismissed] svg path').get_attribute('d')==p.locator('#tasks-list [data-tasks-action=dismiss] svg path').first.get_attribute('d')
   p.screenshot(path=str(out/'tasks-archived.png'))
   p.locator(f'[data-tasks-open="{item["id"]}"]').click();expect(p.locator('#tasks-dialog')).to_be_visible()
   expect(p.locator('#tasks-item-options input[value=yes]')).to_be_checked()
   assert p.evaluate('(key)=>localStorage.getItem(key)', 'msgboard:queue-choices:'+str(oid['id'])+':'+str(item['id'])) is None
   body=p.locator('.tasks-body');expect(body.locator('.tasks-source')).to_have_count(2)
   order=body.locator(':scope > *').all_inner_texts();assert len(order)==6,order
   assert [order[i] for i in (0,1,3,5)]==['Introduction to the question.','First context:','More text between the quotes.','Final question after both quotes.'],order
   assert 'FIRST QUOTED MESSAGE' in order[2] and 'SECOND QUOTED MESSAGE' in order[4] and order[3]=='More text between the quotes.',order
   rects=body.locator('.tasks-source').evaluate_all('xs=>xs.map(e=>{let r=e.getBoundingClientRect();return {left:r.left,right:r.right}})');br=bounds('.tasks-body')
   assert all(abs(r['left']-br['left'])<=1 and abs(r['right']-br['right'])<=1 for r in rects),(rects,br)
   assert p.locator('.tasks-source-link').count()==0
   p.locator('#tasks-item-title').hover();assert p.locator('.tasks-proposal').evaluate('e=>getComputedStyle(e).backgroundColor') in ['rgba(0, 0, 0, 0)','transparent']
   p.screenshot(path=str(out/'tasks-interleaved.png'))
   results['block_order']=order;results['quote_bounds']=rects
   p.locator('.tasks-source').first.click(position={'x':4,'y':4});p.wait_for_function('state.view === "chat"');expect(p.locator('#tasks-dialog')).not_to_be_visible()
   p.goto(env.url+'/#tasks/'+str(item['id']));expect(p.locator('#tasks-dialog')).to_be_visible();p.locator('.tasks-source').nth(1).focus();p.keyboard.press('Enter');p.wait_for_function('state.view === "chat"')
   p.goto(env.url+'/#queue/'+str(item['id']));expect(p.locator('#tasks-dialog')).to_be_visible();assert '#tasks/' in p.url
   p.keyboard.press('Escape');p.locator('#tasks-show-archived').uncheck()
   for suffix in ['#queue','#queue/'+str(item['id'])]:
    cold=ctx.new_page();cold.goto(env.url+'/'+suffix);expect(cold.locator('#tasks-list .tasks-row')).to_have_count(2)
    if '/' in suffix:expect(cold.locator('#tasks-dialog')).to_be_visible()
    assert '#tasks' in cold.url;cold.close()
   p.set_viewport_size({'width':390,'height':844});p.screenshot(path=str(out/'tasks-phone.png'));p.locator(f'[data-tasks-open="{item["id"]}"]').click();expect(p.locator('#tasks-dialog')).to_be_visible()
   p.screenshot(path=str(out/'tasks-phone-item.png'));assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
   p.keyboard.press('Escape');r=handle.bounding_box();initial_phone=height();cdp=ctx.new_cdp_session(p)
   x=r['x']+r['width']/2;y=r['y']+6
   cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
   for step in range(1,11):cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-step*12}]})
   cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});assert height()>initial_phone+90,(initial_phone,height())
   results['agent_resize']['touch']={'before':initial_phone,'after':height()}
   handle.dblclick();assert abs(height()-initial_phone)<2

   assert not errors,errors;results['page_errors']=errors;(out/'layout-checks.json').write_text(json.dumps(results,indent=2));print(json.dumps(results));b.close()
 finally:env.close()
