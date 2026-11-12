"""Real board behind a strict /board/ HTTP proxy; native download must keep the prefix."""
from pathlib import Path
import json,sys,tempfile,urllib.parse
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,'/home/node/local/git/msgboard-cx-download-review/tests')
from conftest import Env
source=Path(__file__).with_name('slow_proxy.py').read_text()
source=source.replace("body=self.rfile.read", "if not self.path.startswith('/board/'):\n                    self.send_error(404);return\n                body=self.rfile.read")
source=source.replace('backend+self.path,','backend+self.path.removeprefix("/board"),')
ns={};exec(compile(source,'prefix_proxy.py','exec'),ns)
with tempfile.TemporaryDirectory(prefix='blue-prefix-') as tmp:
    env=Env(Path(tmp)/'board');proxy=ns['SlowProxy'](env.url).start()
    try:
        channel=env.op.create_channel('downloads')
        payload=b'prefix download fixture'
        env.op.storage_put('video.mp4',payload,content_type='video/mp4')
        errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell',args=['--no-sandbox'])
            ctx=browser.new_context(accept_downloads=True)
            ctx.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(env.operator_token)+');')
            def libs(route):
                f=Path('/data/workspaces/msg-board/engineer-cx/msgboard-pr15/lib')/urllib.parse.urlsplit(route.request.url).path.rsplit('/',1)[-1]
                if f.exists():route.fulfill(content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'},body=f.read_bytes())
                else:route.abort()
            ctx.route('https://cdnjs.cloudflare.com/**',libs)
            page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(proxy.url+'/board/#downloads');page.wait_for_function('md!==null')
            page.evaluate('openViewer(["storage:operator/video.mp4"],0)')
            expect(page.locator('#v-name')).to_have_text('video.mp4')
            with page.expect_download() as d:page.locator('#v-download').click()
            downloaded=d.value
            assert downloaded.url.startswith(proxy.url+'/board/v1/downloads/')
            assert Path(downloaded.path()).read_bytes()==payload
            assert not errors,errors
            result={'prefix':'/board/','browser_download_keeps_prefix':True,'bytes_match':True,'page_errors':errors}
            Path(__file__).with_name('path-prefix-results.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));browser.close()
    finally:proxy.close();env.close()
