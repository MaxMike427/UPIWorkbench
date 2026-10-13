"""可选 Playwright 验收：单独测试库，不调用真实接码/支付服务。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).parents[1]
ARTIFACTS=Path(tempfile.mkdtemp(prefix='upi-browser-',dir='/tmp/opencode'))
BASE='http://127.0.0.1:8891'
env={**os.environ,'UPI_DB_PATH':str(ARTIFACTS/'test.sqlite'),'UPI_REPORT_DIR':str(ARTIFACTS/'reports'),
     'UPI_JWT_SECRET':'browser-test-secret-0123456789abcdefghijklmnopqrstuvwxyz','UPI_ADMIN_PASS':'browser-admin-123',
     'UPI_ADMIN_USER':'admin','UPI_PURCHASE_URL':'https://buy.example.test'}
env['UPI_DATA_DIR']=str(ARTIFACTS/'data')
log=(ARTIFACTS/'server.log').open('w')
server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','tests.browser_fixture'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE+'/api/health',timeout=1)
            break
        except Exception:time.sleep(0.2)
    else:raise RuntimeError('测试服务未启动')
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1440,'height':1000},ignore_https_errors=True)
        page=context.new_page()
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(BASE,wait_until='networkidle')
        page.get_by_label('管理员账号',exact=True).fill('admin')
        page.get_by_label('管理员密码',exact=True).fill('browser-admin-123')
        page.get_by_role('button',name='登录工作台',exact=True).click()
        page.get_by_role('navigation',name='工作区导航').wait_for()
        labels=page.get_by_role('navigation',name='工作区导航').get_by_role('button').all_text_contents()
        assert [text.strip()[1:].strip() for text in labels]==['仪表盘','账号库','代理库','任务管理','成功库','集成','设置'],labels
        page.get_by_role('button',name='设置',exact=True).click()
        def check_link(scope,label,url):
            link=scope.get_by_role('link',name=label,exact=True)
            assert link.is_visible()
            assert link.get_attribute('href')==url
            assert link.get_attribute('target')=='_blank'
            assert link.get_attribute('rel')=='noopener noreferrer'
        sms_form=page.locator('form').filter(has=page.get_by_label('接码 CDK',exact=True))
        extract_form=page.locator('form').filter(has=page.get_by_label('提炼 CDK',exact=True))
        payment_form=page.locator('form').filter(has=page.get_by_label('支付 CDK',exact=True))
        check_link(sms_form,'去购买','https://wzyp.cn/shop/8Q9RHH0W')
        check_link(sms_form,'CDK 融合','https://apihub.space/handy/')
        check_link(extract_form,'去购买','https://wzyp.cn/shop/Camellia')
        check_link(payment_form,'去购买','https://wzyp.cn/shop/Camellia')
        assert sms_form.get_by_role('link',name='提交 CDK',exact=True).count()==0
        assert extract_form.get_by_role('link',name='CDK 融合',exact=True).count()==0
        assert payment_form.get_by_role('link',name='CDK 融合',exact=True).count()==0
        page.get_by_label('接码 CDK',exact=True).fill('TEST-SMS-CDK')
        sms_form.get_by_role('button',name='保存',exact=True).click()
        page.wait_for_function('Alpine.$data(document.body).busy===false')
        page.get_by_label('提炼 CDK',exact=True).fill('extract-test-cdk')
        extract_form.get_by_role('button',name='保存',exact=True).click()
        page.wait_for_function('Alpine.$data(document.body).busy===false')
        page.get_by_label('支付 CDK',exact=True).fill('payment-test-cdk')
        payment_form.get_by_role('button',name='保存',exact=True).click()
        page.wait_for_function('Alpine.$data(document.body).busy===false')
        page.get_by_label('并发数',exact=True).fill('2')
        page.get_by_role('button',name='保存并发数',exact=True).click()
        page.get_by_text('并发数已保存，下次任务开始或继续时生效',exact=True).wait_for()
        page.get_by_role('button',name='仪表盘',exact=True).click()
        page.get_by_role('button',name='设置',exact=True).click()
        assert page.get_by_label('并发数',exact=True).input_value()=='2'
        page.get_by_label('实例 URL',exact=True).fill('https://sub2api.example.test/api/v1/')
        page.get_by_label('管理员 Admin Key',exact=True).fill('admin-browser-test')
        page.get_by_label('启用成功账号自动推送与手动推送',exact=True).check()
        page.get_by_role('button',name='保存集成设置',exact=True).click()
        page.get_by_text('集成设置已保存',exact=True).wait_for()
        assert page.get_by_label('管理员 Admin Key',exact=True).input_value()==''
        page.get_by_role('button',name='集成',exact=True).click()
        page.get_by_role('button',name='测试连接 / 读取分组',exact=True).click()
        page.get_by_text('连接正常，可选 OpenAI 分组 1 个',exact=True).wait_for()
        page.get_by_label('Test OpenAI',exact=True).check()
        page.get_by_label('远端账号并发',exact=True).fill('4')
        page.get_by_label('账号计费倍率',exact=True).fill('1.5')
        page.get_by_label('可用模型',exact=False).fill('gpt-5')
        page.get_by_role('button',name='保存推送模板',exact=True).click()
        page.get_by_text('推送模板已保存',exact=True).wait_for()
        page.get_by_role('button',name='代理库',exact=True).click()
        section=page.locator("section[x-show=\"tab==='proxies'\"]")
        assert not section.get_by_text('免费代理导入',exact=True).count()
        assert not section.get_by_text('API 拉取',exact=True).count()
        page.get_by_role('button',name='添加代理',exact=True).click()
        page.get_by_label('代理地址',exact=True).fill('134.209.144.56:80\nsocks5://test:secret@proxy.example.com:1080')
        page.get_by_role('button',name='添加到代理库',exact=True).click()
        page.get_by_text('http://134.209.144.56:80',exact=True).wait_for()
        page.get_by_label('切换代理 socks5://****:****@proxy.example.com:1080 动态标记',exact=True).click()
        page.get_by_text('已标记为动态 IP',exact=True).wait_for()
        page.get_by_role('button',name='测试连接',exact=True).first.click()
        page.get_by_text('可用 · ',exact=False).first.wait_for()
        page.get_by_text('出口 203.0.113.1',exact=False).first.wait_for()
        page.get_by_label('选择全部代理',exact=True).check()
        page.get_by_role('button',name='批量测试',exact=True).click()
        page.get_by_text('已测试 2 个代理',exact=True).wait_for()
        page.get_by_text('出口 3 个（',exact=False).first.wait_for()
        assert not section.get_by_text('test:secret',exact=False).count()
        page.once('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='删除',exact=True).nth(1).click()
        page.get_by_text('代理已删除',exact=True).wait_for()
        assert not section.get_by_text('socks5://',exact=False).count()
        page.get_by_text('http://134.209.144.56:80',exact=True).wait_for()
        page.get_by_role('button',name='添加代理',exact=True).click()
        page.get_by_label('代理地址',exact=True).fill('134.209.144.57:80\n134.209.144.58:80')
        page.get_by_role('button',name='添加到代理库',exact=True).click()
        page.get_by_text('http://134.209.144.58:80',exact=True).wait_for()
        page.get_by_label('选择全部代理',exact=True).check()
        page.once('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='批量删除',exact=True).click()
        page.get_by_text('已删除 3 个代理',exact=True).wait_for()
        page.get_by_role('button',name='添加代理',exact=True).click()
        page.get_by_label('代理地址',exact=True).fill('134.209.144.59:80')
        page.get_by_role('button',name='添加到代理库',exact=True).click()
        page.get_by_text('http://134.209.144.59:80',exact=True).wait_for()
        page.get_by_label('启用代理库',exact=True).check()
        page.get_by_text('代理库已启用',exact=True).wait_for()
        page.get_by_role('button',name='账号库',exact=True).click()
        check_link(page,'去购买 · 0217','https://wzyp.cn/shop/CPZF8PBK')
        check_link(page,'去购买 · AI 批发商','https://wzyp.cn/shop/8UOIIRW2')
        page.get_by_label('账号库每页数量',exact=True).select_option('50')
        page.wait_for_function('Alpine.$data(document.body).credPageSize===50')
        page.get_by_label('选择账号 TXT 文件',exact=True).set_input_files({'name':'test.txt','mimeType':'text/plain','buffer':b'alice@example.com---password---TOTP---eyJoriginal\nbob@example.com---password---TOTP---eyJoriginal2\nexcluded@example.com---password---TOTP---eyJoriginal3'})
        page.get_by_text('alice@example.com',exact=True).wait_for()
        page.get_by_label('选择账号 alice@example.com',exact=True).check()
        page.get_by_label('选择账号 bob@example.com',exact=True).check()
        page.get_by_role('button',name='用选中账号创建任务',exact=True).click()
        wizard=page.get_by_role('dialog',name='创建任务')
        assert not wizard.get_by_text('excluded@example.com',exact=True).count()
        assert wizard.locator('tbody tr').count()==2
        wizard.get_by_label('选择任务账号 bob@example.com',exact=True).click()
        wizard.get_by_text('bob@example.com',exact=True).wait_for(state='detached')
        assert wizard.locator('tbody tr').count()==1
        assert not wizard.get_by_text('bob@example.com',exact=True).count()
        wizard.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_role('button',name='用选中账号创建任务',exact=True).click()
        wizard.get_by_role('button',name='下一步',exact=True).click()
        wizard.get_by_role('button',name='开始任务',exact=True).click()
        page.locator('[aria-labelledby=detail-title]').wait_for()
        page.wait_for_function("Alpine.$data(document.body).batchDetails?.status==='completed'",timeout=15000)
        detail=page.locator('[aria-labelledby=detail-title]')
        assert detail.get_by_text('alice@example.com',exact=True).is_visible()
        with page.expect_download() as download:
            detail.get_by_role('button',name='成功导出 TXT',exact=True).click()
        result=download.value
        result.save_as(ARTIFACTS/'success.txt')
        assert (ARTIFACTS/'success.txt').read_text().splitlines()==['alice@example.com----password----TOTP','bob@example.com----password----TOTP']
        with page.expect_download() as download:
            detail.get_by_role('button',name='下载任务 MD',exact=True).click()
        download.value.save_as(ARTIFACTS/'task.md')
        assert '全流程成功' in (ARTIFACTS/'task.md').read_text()
        detail.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_role('button',name='成功库',exact=True).click()
        page.get_by_label('选择本页全部成功账号').check()
        page.get_by_role('button',name='推送 sub2api',exact=True).click()
        page.get_by_text('已加入 2 个推送记录',exact=False).wait_for()
        page.get_by_role('button',name='集成',exact=True).click()
        page.wait_for_function("Alpine.$data(document.body).integrationPushes.length===2 && Alpine.$data(document.body).integrationPushes.every(p=>p.status==='succeeded')",timeout=20000)
        assert page.locator("section[x-show=\"tab==='integration'\"]").get_by_text('已推送',exact=True).count()==2
        page.get_by_role('button',name='成功库',exact=True).click()
        page.get_by_label('选择本页全部成功账号').check()
        page.get_by_label('JSON 下载格式').select_option('both')
        with page.expect_download() as download:
            page.get_by_role('button',name='打包下载选中 JSON',exact=True).click()
        download.value.save_as(ARTIFACTS/'accounts.zip')
        import zipfile
        with zipfile.ZipFile(ARTIFACTS/'accounts.zip') as z:assert len(z.namelist())==4
        page.get_by_role('button',name='任务管理',exact=True).click()
        page.once('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='删除任务',exact=True).click()
        page.get_by_text('任务已删除',exact=True).wait_for()
        page.get_by_role('button',name='成功库',exact=True).click()
        page.locator("section[x-show=\"tab==='successes'\"]").get_by_text('alice@example.com',exact=True).wait_for()
        # 关闭接码，新任务无需调用短信接口，结果仍入成功库但不能下载 JSON。
        page.get_by_role('button',name='设置',exact=True).click()
        sms_form.get_by_label('启用接码',exact=False).uncheck()
        page.get_by_text('接码开关已保存，仅影响新任务',exact=True).wait_for()
        page.get_by_role('button',name='账号库',exact=True).click()
        page.get_by_label('选择账号 excluded@example.com',exact=True).check()
        page.get_by_role('button',name='用选中账号创建任务',exact=True).click()
        wizard.get_by_role('button',name='下一步',exact=True).click()
        wizard.get_by_role('button',name='开始任务',exact=True).click()
        page.wait_for_function("Alpine.$data(document.body).batchDetails?.members.some(m=>m.stage==='paid_without_sms')",timeout=15000)
        detail.get_by_text('已成功未接码，不推送',exact=True).wait_for()
        detail.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_role('button',name='成功库',exact=True).click()
        row=page.locator("section[x-show=\"tab==='successes'\"] tbody tr").filter(has_text='excluded@example.com')
        row.get_by_text('已成功未接码',exact=True).wait_for()
        assert row.get_by_text('未接码，无 JSON',exact=True).is_visible()
        page.get_by_role('button',name='任务管理',exact=True).click()
        page.once('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='删除任务',exact=True).click()
        page.get_by_text('任务已删除',exact=True).first.wait_for()
        page.get_by_role('button',name='账号库',exact=True).click()
        page.get_by_label('选择本页全部账号').check()
        page.once('dialog',lambda dialog:dialog.accept())
        page.get_by_role('button',name='删除选中',exact=True).click()
        page.get_by_text('已删除 3 个账号',exact=True).wait_for()
        page.get_by_role('button',name='仪表盘',exact=True).click()
        page.get_by_role('log').get_by_text('接码绑定成功，全流程完成',exact=False).first.wait_for()
        page.screenshot(path=str(ARTIFACTS/'desktop.png'),full_page=True)
        for width in (320,768,1024,1440):
            page.set_viewport_size({'width':width,'height':900})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),f'页面在 {width}px 溢出'
        assert not errors,errors
        browser.close()
        print(json.dumps({'result':'PASS','modules':7,'selection':'2 of 3 accounts; deselect removes candidate','integration':'template saved; 2 mock pushes succeeded','sms_off':'success library TXT only','screens':4,'external_links':6,'concurrency_saved':2,'account_page_size':50,'downloads':['TXT','MD','CPA+sub2api ZIP'],'artifacts':str(ARTIFACTS)},ensure_ascii=False))
finally:
    server.terminate()
    try:server.wait(timeout=10)
    except subprocess.TimeoutExpired:server.kill();server.wait()
    log.close()
