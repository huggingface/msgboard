import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, '/home/node/local/git/msgboard-cx')
from tests.conftest import Env
OUT = Path(__file__).parent
LIB = OUT.parent/'msgboard-pr15/lib'
CHROME = '/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'
errors=[];checks=[]
with tempfile.TemporaryDirectory(prefix='guest-files-', dir='/home/node/local') as tmp:
 env=Env(Path(tmp)/'board')
 try:
  channel=env.op.create_channel('files')['id']
  _,_,author=env.human('person-a', display_name='Person A')
  guest_id,token,guest=env.agent('guest-agent',role='guest')
  env.op.add_member(channel,guest_id['id'])
  with sync_playwright() as pw:
   browser=pw.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
   sample=browser.new_page(viewport={'width':520,'height':220})
   sample.set_content('<body style="margin:0;background:white;font:16px monospace"><div style="margin:24px;border:1px solid black;padding:22px"><strong>TEST SCREENSHOT</strong><hr><p>Shared in #files by Person A.</p><p>A guest can read this file.</p></div></body>')
   image=sample.screenshot();sample.close()
   author.storage_put('files/sample screenshot.png',image,content_type='image/png')
   author.storage_put('files/private.txt',b'not shared')
   message=author.post(channel,'Here is the screenshot: ![sample](/v1/storage/person-a/files/sample%20screenshot.png)')
   for cache in (env.data_dir/'storage-cache').iterdir():cache.unlink()
   context=browser.new_context(viewport={'width':1100,'height':800},accept_downloads=True)
   context.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+');')
   def lib(route):
    f=LIB/urlsplit(route.request.url).path.rsplit('/',1)[-1]
    if f.exists():route.fulfill(content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'},body=f.read_bytes())
    else:route.abort()
   context.route('https://cdnjs.cloudflare.com/**',lib)
   p=context.new_page();p.on('pageerror',lambda e:errors.append(str(e)))
   p.goto(env.url+'/#files');expect(p.locator('#composer textarea')).to_be_visible();p.wait_for_function('md!==null')
   expect(p.locator('.board-img img')).to_be_visible()
   p.locator('.board-img img').first.click()
   expect(p.locator('#v-body img')).to_be_visible();p.wait_for_function('document.querySelector("#v-body img").naturalWidth === 520')
   expect(p.locator('#v-name')).to_have_text('sample screenshot.png')
   p.screenshot(path=str(OUT/'guest-image-viewer.png'))
   checks.append('guest opens shared image in real viewer from a cold cache, including metadata and pixels')
   with p.expect_download() as download_info:p.locator('#v-download').click()
   downloaded=download_info.value
   assert Path(downloaded.path()).read_bytes()==image
   checks.append('native streamed download has exact original image bytes')
   # Use a fresh browser load after permission revocation: old pixels already downloaded cannot be retracted.
   author.edit(channel,message['id'],'Screenshot removed.')
   p.goto(env.url+'/#files');p.reload()
   expect(p.locator('#composer textarea')).to_be_visible()
   p.evaluate('(id) => openViewer([id], 0)', 'storage:person-a/files/sample screenshot.png')
   expect(p.locator('#v-body img')).to_have_count(0)
   expect(p.locator('#v-body')).to_contain_text('not available')
   checks.append('fresh viewer cannot open image after sharing link is removed')
   assert not errors,errors
   browser.close()
 finally:env.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':checks,'page_errors':errors},indent=2));print(json.dumps({'checks':checks,'page_errors':errors},indent=2))
