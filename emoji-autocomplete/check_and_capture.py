import json,tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.conftest import Env
out=Path(__file__).parent;results={}
with tempfile.TemporaryDirectory(prefix='emoji-autocomplete-') as tmp:
 env=Env(Path(tmp)/'board')
 try:
  _,token,owner=env.human('person-a');channel=owner.create_channel('channel-a');owner.post(channel['id'],'Ready for launch?')
  with sync_playwright() as pw:
   for name,driver,kwargs in [('chromium',pw.chromium,{'executable_path':'/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'}),('firefox',pw.firefox,{})]:
    browser=driver.launch(**kwargs);context=browser.new_context(viewport={'width':1200,'height':800});context.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+')')
    context.route('https://cdnjs.cloudflare.com/**',lambda route:route.abort());page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(env.url+'/#channel-a')
    field=page.locator('#composer textarea');expect(field).to_be_visible();field.fill('');field.press_sequentially('Hello :rocket');field.press('Enter');expect(field).to_have_value('Hello 🚀 ');assert len(owner.read(channel['id'])['messages'])==1
    field.press('Control+z');expect(field).to_have_value('Hello :rocket');field.press('Control+Shift+z');expect(field).to_have_value('Hello 🚀 ')
    field.fill('Ready :ro');field.press('ArrowDown');field.press('ArrowDown');menu=page.get_by_role('listbox',name='Emoji suggestions');expect(menu).to_be_visible();assert '🚀' in menu.locator('[aria-selected="true"]').inner_text()
    box=menu.bounding_box();assert box['x']>=0 and box['y']>=0 and box['x']+box['width']<=1200 and box['y']+box['height']<=800
    if name=='chromium':
     page.screenshot(path=str(out/'implementation-desktop.png'))
     top=max(0,box['y']-16);page.screenshot(path=str(out/'implementation-detail.png'),clip={'x':240,'y':top,'width':960,'height':800-top})
     page.set_viewport_size({'width':390,'height':844});page.wait_for_function("document.querySelector('aside').getBoundingClientRect().right <= 0");field.fill(':ro');menu=page.get_by_role('listbox',name='Emoji suggestions');expect(menu).to_be_visible();box=menu.bounding_box();assert box['x']>=0 and box['x']+box['width']<=390
     page.screenshot(path=str(out/'implementation-phone.png'));page.set_viewport_size({'width':1200,'height':800})
    field.fill('');page.locator('#messages .msg').first.hover();page.locator('#messages [data-edit]').first.click();edit=page.get_by_role('textbox',name='Edit message');edit.fill('Edited :ro');menu=page.get_by_role('listbox',name='Emoji suggestions');expect(menu).to_be_visible();box=menu.bounding_box();assert box['x']>=0 and box['y']>=52 and box['y']+box['height']<=800
    results[name]={'native_selection_undo_redo':True,'selection_did_not_send':True,'menu_and_editor_in_view':True,'page_errors':errors};assert not errors,errors
    context.close();browser.close()
 finally:env.close()
(out/'manual-checks.json').write_text(json.dumps(results,indent=2));print(json.dumps(results,indent=2))
