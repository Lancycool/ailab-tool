"""Browser regressions for page-owned notices and late async responses."""
import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from campus_runner.api import create_app
from conftest import batch

pytestmark = pytest.mark.skipif(os.environ.get('CAMPUS_BROWSER_TESTS') != '1', reason='Requires Edge')


def test_notices_clear_when_navigating_between_modules(service):
    from playwright.sync_api import sync_playwright
    client = TestClient(create_app(service.store.home, service, background=False), base_url='http://127.0.0.1:8765')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.add_init_script("localStorage.setItem('campus-resource-interval','0')")
        def route_handler(route):
            request = route.request
            response = client.request(request.method, request.url, content=request.post_data_buffer,
                                      headers={k:v for k,v in request.headers.items() if k in ('authorization','content-type','origin')})
            route.fulfill(status=response.status_code, body=response.content,
                          headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')})
        page.route('http://127.0.0.1:8765/**',route_handler)
        page.goto('http://127.0.0.1:8765/#token='+service.store.token('panel')+'&page=approvals')
        assert page.url.endswith('/#approvals')
        assert page.locator('#heading').inner_text() == '确认这一批实验'
        page.get_by_role('link',name='实例管理',exact=True).click()
        page.locator('article.instance').first.wait_for()
        for destination in ('实例管理','实验批准','任务详情'):
            page.get_by_role('link',name='结果对比',exact=True).click()
            page.get_by_role('button',name='汇总结果',exact=True).click()
            page.locator('#notice').filter(has_text='报告已保存在本地').wait_for()
            page.get_by_role('link',name=destination,exact=True).click()
            page.locator('#notice').wait_for(state='hidden')
            assert page.locator('#notice').inner_text()==''
        page.get_by_role('link',name='实例管理',exact=True).click()
        page.locator('article.instance').first.get_by_role('button',name='编辑连接',exact=True).click()
        page.get_by_role('button',name='保存连接信息',exact=True).click()
        page.locator('#notice').filter(has_text='实例已保存').wait_for()
        page.get_by_role('link',name='实验批准',exact=True).click()
        page.locator('#notice').wait_for(state='hidden')
        page.get_by_role('button',name='手动准备实验',exact=True).click()
        page.locator('#batch-form textarea').fill(json.dumps(batch(service)))
        page.get_by_role('button',name='保存草案',exact=True).click()
        page.locator('#notice').filter(has_text='实验草案已创建').wait_for()
        page.get_by_role('link',name='任务详情',exact=True).click()
        page.locator('#notice').wait_for(state='hidden')
        browser.close()


@pytest.mark.parametrize('kind', ['report-success','report-error','resource-error','portal-error'])
def test_late_responses_do_not_show_in_another_page_or_after_return(service,kind):
    from playwright.async_api import async_playwright
    client=TestClient(create_app(service.store.home,service,background=False),base_url='http://127.0.0.1:8765')
    async def check():
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(channel='msedge',headless=True)
            page=await browser.new_page()
            await page.add_init_script("localStorage.setItem('campus-resource-interval','0')")
            requested,release,completed=asyncio.Event(),asyncio.Event(),asyncio.Event()
            async def route_handler(route):
                request=route.request
                target = '/api/tools/get_metrics' if kind.startswith('report') else '/api/panel/resources' if kind=='resource-error' else '/api/panel/portal/start'
                if request.url.endswith(target):
                    requested.set()
                    await release.wait()
                    if kind.endswith('error'):
                        await route.fulfill(status=400,json={'message':'delayed-page-error'})
                    else:
                        response=client.post('/api/tools/get_metrics',json={},headers={'Authorization':'Bearer '+service.store.token('panel')})
                        await route.fulfill(status=response.status_code,body=response.content,content_type='application/json')
                    completed.set()
                    return
                response=client.request(request.method,request.url,content=request.post_data_buffer,
                    headers={k:v for k,v in request.headers.items() if k in ('authorization','content-type','origin')})
                await route.fulfill(status=response.status_code,body=response.content,
                    headers={k:v for k,v in response.headers.items() if k not in ('content-length','content-encoding')})
            await page.route('http://127.0.0.1:8765/**',route_handler)
            await page.goto('http://127.0.0.1:8765/#token='+service.store.token('panel'))
            await page.locator('article.instance').first.wait_for()
            if kind.startswith('report'):
                await page.get_by_role('link',name='结果对比',exact=True).click()
                await page.get_by_role('button',name='汇总结果',exact=True).click()
            elif kind=='resource-error':
                await page.get_by_role('button',name='刷新资源',exact=True).click()
            else:
                await page.get_by_role('button',name='从学校平台导入 / 刷新',exact=True).click()
            await asyncio.wait_for(requested.wait(),2)
            await page.get_by_role('link',name='任务详情',exact=True).click()
            release.set()
            await asyncio.wait_for(completed.wait(),2)
            await page.wait_for_timeout(100)
            assert await page.locator('#notice').is_hidden()
            # Old requests are also invalidated if the user leaves and returns before completion.
            requested.clear();release.clear();completed.clear()
            origin='结果对比' if kind.startswith('report') else '实例管理'
            await page.get_by_role('link',name=origin,exact=True).click()
            button='汇总结果' if kind.startswith('report') else '刷新资源' if kind=='resource-error' else '从学校平台导入 / 刷新'
            await page.get_by_role('button',name=button,exact=True).click()
            await asyncio.wait_for(requested.wait(),2)
            await page.get_by_role('link',name='任务详情',exact=True).click()
            await page.get_by_role('link',name=origin,exact=True).click()
            release.set()
            await asyncio.wait_for(completed.wait(),2)
            await page.wait_for_timeout(100)
            assert await page.locator('#notice').is_hidden()
            await browser.close()
    asyncio.run(check())
