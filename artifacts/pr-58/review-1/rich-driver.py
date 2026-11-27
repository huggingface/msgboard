from pathlib import Path
import os,sys,json,tempfile
sys.path.insert(0, '/home/node/local/git/msgboard-cx')
from tests.conftest import Env
os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH']='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'
source=Path('/home/node/local/git/msgboard-cx/tests/test_tasks_layout.py').read_text()
needle='        context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())'
replacement=needle+'''
        for url, filename in json.loads(Path('/home/node/local/tmp/reading-position/renderer/manifest.json').read_text()).items():
            context.route(url, lambda route, request, filename=filename: route.fulfill(body=Path(filename).read_bytes(), content_type='application/javascript', headers={'Access-Control-Allow-Origin': '*'}))
'''
source=source.replace(needle,replacement,1)
source=source.replace('        pw.expect(page.locator(\'#messages .msg\')).to_have_count(45)', '''        page.wait_for_function('window.marked && window.DOMPurify && window.katex')
        pw.expect(page.locator('#messages .body.md')).to_have_count(45)
        pw.expect(page.locator('#messages .msg')).to_have_count(45)''',1)
# Native actual-renderer fallback regression for a removed Markdown body block too.
needle="        # A partially visible message header is also a valid nearby anchor."
source=source.replace(needle,'''        # Remove the selected final body block through a real message edit.
        original_body = messages[22]['body']
        end = root.locator('.body > :has(pre), .body > pre')
        end.evaluate("el => {const sc=document.getElementById('scroll'); sc.scrollTop += el.getBoundingClientRect().top-sc.getBoundingClientRect().top-20;}")
        previous_top = root.bounding_box()['y']
        owner.edit(channel['id'], root_id, original_body.split('\\n```')[0] + '\\n\\nEnd of this post.')
        pw.expect(root.locator('pre')).to_have_count(0, timeout=12000)
        page.wait_for_function("({id,top}) => Math.abs(document.querySelector('#messages .msg[data-mid=\\\"'+id+'\\\"]').getBoundingClientRect().top-top)<3", arg={'id':root_id,'top':previous_top}, timeout=3000)
''' + needle,1)
ns={'Path':Path};exec(compile(source,'real-renderer-test_tasks_layout.py','exec'),ns)
env=Env(Path(tempfile.mkdtemp(prefix='reading-rich-review1-'))/'board')
try:
 ns['test_thread_panel_preserves_channel_reading_position'](env)
 print('PASS: actual pinned renderer; late image growth; synchronous preview reuse; shared decode; partial message; deleted body block; deleted reply; bottom/thread cases.',flush=True)
finally: env.close()
