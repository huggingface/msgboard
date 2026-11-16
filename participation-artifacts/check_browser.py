import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, '/home/node/local/git/msgboard-cx')
from tests.conftest import Env

OUT = Path(__file__).parent
LIB = OUT.parent / 'msgboard-pr15/lib'
CHROME = '/opt/pw-browsers/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell'
errors = []
checks = []
with tempfile.TemporaryDirectory(prefix='participation-', dir='/home/node/local') as tmp:
    env = Env(Path(tmp) / 'board')
    try:
        channel = env.op.create_channel('planning')['id']
        other = env.op.create_channel('other')['id']
        env.op.post(channel, 'Please review the release notes before tomorrow.')
        _, token, sender = env.human('person-b', display_name='Person B')
        person, _, recipient = env.human('person-a', display_name='Person A')
        _, _, helper = env.agent('person-helper', display_name='Person Helper', channels=['planning'])
        _, _, _ = env.agent('person-outsider')
        unrelated, _, _ = env.human('unrelated')
        env.op.add_member(channel, unrelated['id'])
        private = env.op.create_channel('restricted', private=True)['id']
        env.op.add_member(private, sender.identity['id'])
        dm = sender.open_dm(person['id'])['id']
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
            def context(**kwargs):
                ctx = browser.new_context(**kwargs)
                ctx.add_init_script('localStorage.setItem("msgboard:token", ' + json.dumps(token) + ');')
                def lib(route):
                    f = LIB / urlsplit(route.request.url).path.rsplit('/', 1)[-1]
                    if f.exists():
                        route.fulfill(content_type='application/javascript', headers={'Access-Control-Allow-Origin':'*'}, body=f.read_bytes())
                    else:
                        route.abort()
                ctx.route('https://cdnjs.cloudflare.com/**', lib)
                return ctx
            ctx = context(viewport={'width':1280,'height':850})
            p = ctx.new_page(); p.on('pageerror', lambda e: errors.append(str(e)))
            p.goto(env.url + '/#planning')
            box = p.locator('#composer textarea')
            expect(box).to_be_visible(); p.wait_for_function('md !== null')
            expect(p.get_by_text('Please review the release notes before tomorrow.', exact=True)).to_be_visible()
            expect(p.locator('#ch-join')).to_be_visible()
            box.fill('@person')
            expect(p.locator('.mention-pop .opt')).to_have_count(2)
            rows = p.locator('.mention-pop .opt').all_text_contents()
            assert 'Person Helper' in rows[0] and 'Person A' in rows[1] and 'not joined' in rows[1], rows
            p.screenshot(path=str(OUT/'mention-desktop.png'))
            checks.append('member first; readable nonmember person offered; outsider agents and unmatched people absent')
            box.fill('@')
            expect(p.locator('.mention-pop .opt').filter(has_text='Person A')).to_have_count(0)
            checks.append('bare @ keeps group tags and channel members')
            box.fill('@person'); expect(p.locator('.mention-pop .opt')).to_have_count(2)
            p.locator('.mention-pop .opt').filter(has_text='Person A').click()
            expect(box).to_have_value('@Person A ')
            box.fill('@Person A please review.'); p.locator('#composer .btn').click()
            expect(box).to_have_value(''); expect(p.locator('#ch-join')).not_to_be_visible()
            assert next(c for c in sender.list_channels() if c['id']==channel)['member']
            posted = env.op.read(channel)['messages'][-1]
            assert [m['id'] for m in posted['mentions']] == [person['id']]
            assert any(m['id']==posted['id'] and m['reason']=='mention' for m in recipient.activity_page(0)['messages'])
            assert not next(c for c in recipient.list_channels() if c['id']==channel)['member']
            checks.append('selected outsider receives mention activity; successful send joins only author and hides Join immediately')
            # Different composers use the same suggestions.
            root = env.op.read(channel)['messages'][0]
            p.locator('.msg').filter(has=p.locator('[data-thread="' + str(root['id']) + '"]')).first.hover()
            p.locator('[data-thread="' + str(root['id']) + '"]').first.click()
            expect(p.locator('#panel-composer textarea')).to_be_visible()
            p.locator('#panel-composer textarea').fill('@person')
            expect(p.locator('#panel-composer .mention-pop .opt').filter(has_text='Person A')).to_have_count(1)
            checks.append('thread composer includes readable nonmember people')
            p.goto(env.url + '/#restricted'); expect(box).to_be_visible(); box.fill('@person')
            expect(p.locator('.mention-pop')).to_have_count(0)
            checks.append('private channel does not suggest inaccessible person')
            p.goto(env.url + '/#' + 'dm:' + ':'.join(map(str, sorted([sender.identity['id'], person['id']]))))
            expect(box).to_be_visible(); box.fill('@person')
            expect(p.locator('.mention-pop .opt')).to_have_count(1)
            expect(p.locator('.mention-pop .opt')).to_contain_text('Person A')
            checks.append('DM offers only peer')
            # A failed load is retried on the next keystroke and never falls back to everyone.
            p.goto(env.url + '/#other'); expect(box).to_be_visible()
            pattern = '**/v1/channels/' + str(other) + '/mention-candidates'
            p.route(pattern, lambda route: route.fulfill(status=503, json={'error': {'code':'busy', 'message':'busy'}}), times=1)
            box.fill('@person'); p.wait_for_timeout(300)
            expect(p.locator('.mention-pop')).to_have_count(0)
            box.fill('@person-'); expect(p.locator('.mention-pop .opt').filter(has_text='Person A')).to_have_count(1)
            checks.append('503 closes candidates, next keystroke retries')
            # Delayed response cannot resurrect a list after Escape.
            p.reload(); expect(box).to_be_visible()
            pending=[]
            p.route(pattern, lambda route: pending.append(route), times=1)
            box.fill('@person'); p.wait_for_timeout(150)
            assert pending
            box.fill('changed my mind'); pending[0].continue_(); p.wait_for_timeout(250)
            expect(p.locator('.mention-pop')).to_have_count(0)
            checks.append('late candidate response cannot resurrect cleared suggestions')
            mobile = context(viewport={'width':375,'height':750}, is_mobile=True, has_touch=True)
            phone = mobile.new_page(); phone.on('pageerror', lambda e: errors.append(str(e)))
            phone.goto(env.url + '/#planning'); expect(phone.locator('#composer textarea')).to_be_visible()
            phone.locator('#composer textarea').fill('@person')
            expect(phone.locator('.mention-pop .opt').filter(has_text='Person A')).to_have_count(1)
            phone.screenshot(path=str(OUT/'mention-phone.png'))
            assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth')
            checks.append('phone suggestions fit viewport')
            assert not errors, errors
            browser.close()
    finally:
        env.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':checks,'page_errors':errors}, indent=2))
print(json.dumps({'checks':checks,'page_errors':errors}, indent=2))
