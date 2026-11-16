"""Independent Tasks interaction/privacy review; run from the checkout's cwd."""
import base64
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.parse
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path.cwd()/'src'))
sys.path.insert(0, str(Path.cwd()/'tests'))
from conftest import Env
from msgboard.client import BoardError

out=Path(__file__).parent
results={}
with tempfile.TemporaryDirectory() as directory:
    env=Env(Path(directory)/'board')
    try:
        oid,token,owner=env.human('review-owner')
        aid,_,agent=env.agent('review-agent',owner=owner)
        channel=owner.create_channel('review-quotes')
        target=agent.post(channel['id'],'Nested message target')
        storage=agent.storage_put('files/quote-note.txt',b'Nested storage file contents',content_type='text/plain')
        image=agent.storage_put('files/quote-pixel.png',base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII='),content_type='image/png')
        attachment=agent.upload(channel['id'],b'Nested channel attachment',filename='channel-note.txt',content_type='text/plain')
        quote=agent.post(channel['id'],f'Quote body. [Storage note]({storage["url"]})\n\n![Pixel]({image["url"]})\n\n[Another message](#msg/{target["id"]})',attachments=[attachment['id']])
        blocks=[{'type':'text','text':'Before quote'},{'type':'quote','message_id':quote['id']},{'type':'text','text':'After quote'}]
        task=agent.put_task('interactive','Interactive quote',blocks=blocks)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell')
            context=browser.new_context(viewport={'width':1280,'height':1000})
            context.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+');')
            lib=out.parent.parent/'msgboard-pr15/lib'
            def local_lib(route):
                file=lib/urllib.parse.urlsplit(route.request.url).path.rsplit('/',1)[-1]
                if file.exists(): route.fulfill(content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'},body=file.read_bytes())
                else: route.abort()
            context.route('https://cdnjs.cloudflare.com/**',local_lib)
            page=context.new_page(); errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            # Each legacy URL starts in a fresh browser context/document. A
            # same-document hash transition exercises a different startup path.
            results['cold_legacy_bookmarks']=[]
            for legacy in ('queue', 'queue/'+str(task['id'])):
                cold_context=browser.new_context(viewport={'width':1280,'height':1000})
                cold_context.add_init_script('localStorage.setItem("msgboard:token",'+json.dumps(token)+');')
                cold_context.route('https://cdnjs.cloudflare.com/**',local_lib)
                cold_page=cold_context.new_page()
                cold_page.goto(env.url+'/#'+legacy)
                expect(cold_page.locator('#tasks')).to_be_visible()
                if '/' in legacy: expect(cold_page.locator('#tasks-dialog')).to_be_visible()
                assert urllib.parse.urlsplit(cold_page.url).fragment==legacy.replace('queue','tasks',1)
                results['cold_legacy_bookmarks'].append({'input':legacy,'hash':urllib.parse.urlsplit(cold_page.url).fragment,'view':cold_page.evaluate('state.view'),'dialog_open':cold_page.locator('#tasks-dialog').evaluate('(e)=>e.open')})
                cold_context.close()
            page.goto(env.url+'/?independent-review=1#tasks/'+str(task['id']))
            expect(page.locator('#tasks-dialog')).to_be_visible()
            expect(page.locator('.tasks-source a[data-board-file]')).to_have_count(1)
            page.locator('.tasks-source a[data-board-file]').click()
            expect(page.locator('#tasks-dialog')).not_to_be_visible()
            expect(page.locator('#viewer')).to_be_visible()
            expect(page.locator('#v-body')).to_contain_text('Nested storage file contents')
            results['nested_storage_link']='passed'
            page.locator('#v-close').click()
            def reopen():
                page.evaluate('openTasks('+str(task['id'])+')')
                expect(page.locator('#tasks-dialog')).to_be_visible()
            reopen()
            page.locator('.tasks-source [data-att]').click()
            expect(page.locator('#tasks-dialog')).not_to_be_visible()
            expect(page.locator('#v-body')).to_contain_text('Nested channel attachment')
            results['nested_attachment']='passed'
            page.locator('#v-close').click()
            reopen()
            expect(page.locator('.tasks-source .board-img img')).to_be_visible()
            page.locator('.tasks-source .board-img img').click()
            expect(page.locator('#tasks-dialog')).not_to_be_visible()
            expect(page.locator('#viewer')).to_be_visible()
            expect(page.locator('#v-name')).to_have_text('quote-pixel.png')
            results['nested_image']='passed'
            page.locator('#v-close').click()
            reopen()
            page.locator('.tasks-source a[data-internal]').click()
            page.wait_for_function('state.view === "chat"')
            expect(page.locator('#tasks-dialog')).not_to_be_visible()
            expect(page.locator('#messages .msg.flash')).to_have_attribute('data-mid',str(target['id']))
            results['nested_message_link']='passed'
            reopen()
            page.locator('.tasks-source').focus();page.keyboard.press(' ')
            page.wait_for_function('state.view === "chat"')
            expect(page.locator('#messages .msg.flash')).to_have_attribute('data-mid',str(quote['id']))
            results['whole_quote_space_navigation']='passed'
            results['page_errors']=errors
            assert not errors,errors
            browser.close()

        # Change protected text blocks and their quote sources in one upsert,
        # while a narrow sibling reads the list and individual task repeatedly.
        private=owner.create_channel('review-private-quotes',private=True)
        owner.add_member(private['id'],aid['id'])
        _,_,reader=env.agent('review-narrow',owner=owner,role='guest',channels=['review-quotes'])
        secret=agent.post(private['id'],'SECRET BLOCK SNAPSHOT MARKER')
        marker='SECRET BLOCK SNAPSHOT MARKER'
        item=agent.put_task('changing-quote','Protected task',blocks=[{'type':'text','text':marker},{'type':'quote','message_id':secret['id']}])
        barrier=threading.Barrier(2)
        def change():
            current=item
            barrier.wait(timeout=5)
            for i in range(80):
                hidden=i%2==0
                current=agent.put_task('changing-quote','Protected task' if hidden else 'Public task',blocks=[{'type':'text','text':marker if hidden else 'Public block text'},{'type':'quote','message_id':secret['id'] if hidden else target['id']}],base_version=current['version'])
        reads=0
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            worker=pool.submit(change);barrier.wait(timeout=5)
            while not worker.done() or reads<20:
                assert marker not in json.dumps(reader.tasks())
                try: visible=reader.task(item['id'])
                except BoardError as error: assert error.status==404
                else: assert marker not in json.dumps(visible)
                reads+=1
            worker.result(timeout=10)
        results['concurrent_block_quote_visibility']={'writes':80,'read_pairs':reads,'leaks':0}
        print(json.dumps(results,indent=2))
        (out/'independent-interactions.json').write_text(json.dumps(results,indent=2))
    finally:env.close()
