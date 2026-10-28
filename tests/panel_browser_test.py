"""Browser fixtures only: isolated headless Chrome, no user profile or account."""
import threading
from pathlib import Path

import httpx
import pytest

from claude_rc import RemoteControlClient
from claude_rc.webui import _RCServer, _Handler


def test_headless_question_variants_and_first_load(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    release = threading.Event()
    seen = []
    def handler(req):
        if req.url.path == '/v1/code/sessions':
            release.wait(20)
            return httpx.Response(200, json={'data': []})
        if req.url.path.endswith('/model_selector/cc'):
            return httpx.Response(200,json={'model_selector_config':[{'models':[{'id':'toy-model','display_name':'Toy'}]}]})
        return httpx.Response(404,json={})
    rc=RemoteControlClient(token='toy',org_uuid='toy-org',transport=httpx.MockTransport(handler))
    server=_RCServer(('127.0.0.1',0),_Handler,rc,False)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',args=['--no-sandbox'],headless=True)
            page=browser.new_page(viewport={'width':1100,'height':900})
            try:
                page.goto(f'http://127.0.0.1:{server.server_port}',wait_until='domcontentloaded')
                page.wait_for_function("document.getElementById('session-list').textContent.includes('Loading sessions')")
                assert 'No sessions' not in page.locator('#session-list').inner_text()
                release.set()
                page.wait_for_function("document.getElementById('session-list').textContent.includes('No sessions')")
                page.wait_for_function("document.getElementById('sel-model').options.length===2")
                assert page.locator('#sel-model option').all_text_contents()==['— keep —','Toy']
                page.route('**/api/sessions/cse_toy/answer',lambda route: (seen.append(route.request.post_data_json),route.fulfill(json={'ok':True})))
                result=page.evaluate('''() => {
                  S.selectedId='cse_toy';
                  const ev={request_id:'toy-question',tool_input:{questions:[
                    {question:'Colour?',options:[{label:'Blue',preview:'toy preview'}]},
                    {question:'Toppings?',multiSelect:true,options:[{label:'Ham'},{label:'Egg'}]},
                    {question:'Name?',kind:'text',placeholder:'Name here',defaultValue:'Ada'},
                    {question:'Count?',kind:'number',min:1,max:5,step:1,defaultValue:2,unit:'items'}
                  ]}};
                  const form=questionForm(ev);form.id='toy-form';document.body.appendChild(form);
                  const permission=promptNode({request_id:'toy-permission',blocking_subtype:'can_use_tool',tool_name:'Bash',permission_suggestions:[]});
                  document.body.appendChild(permission);
                  return {radios:form.querySelectorAll('input[type=radio]').length,
                    choices:form.querySelectorAll('input[data-q][type=checkbox]').length,
                    skips:form.querySelectorAll('input[data-skip]').length,
                    notes:form.querySelectorAll('input[data-notes]').length,
                    previews:form.querySelectorAll('details').length,
                    min:form.querySelector('input[type=number]').min,
                    max:form.querySelector('input[type=number]').max,
                    defaultNumber:form.querySelector('input[type=number]').value,
                    alwaysDisabled:[...permission.querySelectorAll('button')].find(b=>b.textContent==='Always allow').disabled};
                }''')
                assert result=={'radios':1,'choices':2,'skips':4,'notes':4,'previews':1,'min':'1','max':'5','defaultNumber':'2','alwaysDisabled':True}
                page.locator('#toy-form input[data-q="0"]').check()
                page.locator('#toy-form input[data-q="1"]').first.check()
                page.locator('#toy-form input[data-other="1"]').fill('Basil')
                page.locator('#toy-form input[data-notes="0"]').fill('toy note')
                page.locator('#toy-form input[data-preview="0"]').fill('toy annotation')
                page.locator('#toy-form input[data-skip="3"]').check()
                page.locator('#toy-form button').filter(has_text='Submit').click()
                page.wait_for_timeout(100)
                assert seen[-1]=={'request_id':'toy-question','selections':{'0':'Blue','1':['Ham']},
                    'other':{'1':'Basil','2':'Ada'},'notes':{'0':'toy note'},
                    'previews':{'0':'toy annotation'},'skipped':[3]}
                for label, flag in [('Dismiss','dismissed'),('Replied in chat','replied_in_chat')]:
                    page.locator('#toy-form button').filter(has_text=label).click()
                    page.wait_for_timeout(100)
                    assert seen[-1]=={'request_id':'toy-question',flag:True}
                page.screenshot(path=str(tmp_path/'panel-fixtures.png'),full_page=True)
            finally:
                page.close()
                browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        rc.close()
