import json,sys,tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.conftest import Env
out=Path(__file__).parent;label=sys.argv[1];records={}
with tempfile.TemporaryDirectory(prefix='emoji-review-fix-') as temp:
 env=Env(Path(temp)/'board')
 try:
  _,token,owner=env.human('person-a');channel=owner.create_channel('channel-a')
  with sync_playwright() as pw:
   browser=pw.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell')
   context=browser.new_context();context.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+')');context.route('https://cdnjs.cloudflare.com/**',lambda route:route.abort())
   page=context.new_page();page.goto(env.url+'/#channel-a');field=page.locator('#composer textarea');expect(field).to_be_visible()
   field.fill('Send this literal :coffee')
   with page.expect_response(lambda r:r.request.method=='POST' and '/messages' in r.url):page.locator('#composer .btn').click()
   expect(field).to_have_value('');opened=page.get_by_role('listbox',name='Emoji suggestions').count();field.press('Tab')
   records['send']={'popup_after_send':opened,'draft_after_tab':field.input_value(),'posted_body':owner.read(channel['id'])['messages'][-1]['body']}
   body='First line\nSecond line\nchoose :rocket'
   for key in ['PageUp','Control+ArrowUp']:
    field.fill(body);field.press(key);caret=field.evaluate('el=>el.selectionStart');opened=page.get_by_role('listbox',name='Emoji suggestions').count();field.press('Tab')
    records[key]={'caret_after_navigation':caret,'old_end':len(body),'popup_after_navigation':opened,'unchanged_after_tab':field.input_value()==body,'draft_after_tab':field.input_value()}
   url='https://example.com/(:rocket';field.fill(url);opened=page.get_by_role('listbox',name='Emoji suggestions').count();field.press('Tab')
   records['url']={'popup':opened,'unchanged_after_tab':field.input_value()==url,'draft_after_tab':field.input_value()}
   context.close();browser.close()
 finally:env.close()
(out/(label+'.json')).write_text(json.dumps(records,ensure_ascii=False,indent=2));print(json.dumps(records,ensure_ascii=False,indent=2))
