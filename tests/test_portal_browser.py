"""Opt-in real Edge checks: set CAMPUS_BROWSER_TESTS=1 before pytest."""
import os

import pytest
from fastapi.testclient import TestClient

from campus_runner.api import create_app
from campus_runner.portal import Observations, SchoolBrowser, URL
from test_portal import prepared

pytestmark = pytest.mark.skipif(os.environ.get('CAMPUS_BROWSER_TESTS') != '1', reason='Requires installed Edge and browser process access')


def test_real_dom_scan_and_connection_dialog():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        markup = '''<div data-cy="instanceView"><table><tr><td>运行中</td><td>
        <a href="/user/instance/123/node/456">test gpu</a></td><td>4天8小时19分</td><td>
        <svg onclick="document.querySelector('[role=dialog]').style.display='block'"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7"/></svg>
        </td></tr></table></div><div role="dialog" style="display:none">
        <button class="MuiChip-clickable" onclick="document.querySelector('#port').textContent='80'">web</button>
        <button class="MuiChip-clickable" onclick="document.querySelector('#port').textContent='22'">ssh</button>
        <p>类型 tcp</p><p>实例内部地址 *:<span id="port">80</span></p><p>服务外部地址 192.0.2.4:24060</p></div>'''
        page.route('**/*', lambda route: route.fulfill(body=markup, content_type='text/html; charset=utf-8'))
        page.goto(URL)
        adapter = SchoolBrowser()
        adapter.page = page
        adapter.observations = Observations()
        adapter.observations.apps['123'] = {'expStopAt': 1893456000}
        candidates = adapter.scan()
        assert candidates[0]['expiry_estimated'] is False
        assert candidates[0]['state'] == '运行中'
        assert adapter.connections(candidates[0]) == [{'host':'192.0.2.4','port':24060}]
        browser.close()


def test_panel_import_form_saves_through_real_api(service):
    from playwright.sync_api import sync_playwright
    portal, payload = prepared(service)
    portal.state['candidates'][0]['state'] = '运行中'
    app = create_app(service.store.home, service, background=False)
    # No actual school login is opened: the panel resumes a prepared import session.
    portal.start = portal.status
    with TestClient(app, base_url='http://127.0.0.1:8765') as client, sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def handle(route):
            request = route.request
            response = client.request(request.method, request.url, content=request.post_data_buffer,
                                      headers={k:v for k,v in request.headers.items() if k in ('authorization','content-type','origin')})
            route.fulfill(status=response.status_code, body=response.content,
                          headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-length','content-encoding')})
        page.route('http://127.0.0.1:8765/**', handle)
        page.goto('http://127.0.0.1:8765/#token=' + service.store.token('panel'))
        page.get_by_role('button', name='从学校平台导入 / 刷新').click()
        form = page.locator('#portal-form')
        form.wait_for(state='visible')
        form.locator('[name=target_instance_id]').select_option('a')
        assert form.locator('[name=username]').input_value() == 'user'
        assert form.locator('[name=password]').input_value() == ''
        form.get_by_role('button', name='确认并保存连接信息').click()
        page.get_by_text('实例 school gpu 已导入。', exact=False).wait_for()
        saved = service.store.get('instance','a')
        assert saved['portal']['platform_id'] == '123'
        assert saved['port'] == 24060
        assert saved['host_key'] is None
        assert service.credentials.get('a') == 'test-secret'
        assert not errors
        browser.close()
