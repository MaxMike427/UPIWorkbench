const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
function createApp() {
  const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
  const context = {localStorage:{getItem:()=>'',setItem(){},removeItem(){}},
    setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},
    URLSearchParams,URL,Blob,console,confirm:()=>true,fetch:async()=>{throw Error('unexpected HTTP')},
    window:{},document:{},Date};
  vm.createContext(context);
  vm.runInContext(js,context);
  return context.app();
}

test('workspace has exactly the seven requested modules', () => {
  const app = createApp();
  assert.deepEqual(Array.from(app.navs,n=>n.label), ['仪表盘','账号库','代理库','任务管理','成功库','集成','设置']);
  const sections = [...html.matchAll(/<section[^>]*x-show="tab==='([^']+)'"/g)].map(m=>m[1]);
  assert.deepEqual(sections.sort(),['batches','credentials','dashboard','settings','successes','proxies','integration'].sort());
});

test('dashboard entry loads both real stats and terminal logs', async () => {
  const app = createApp();
  const calls = [];
  app.loadStats=async()=>calls.push('stats');
  app.loadRuntimeLogs=async()=>calls.push('logs');
  await app.go('dashboard');
  assert.ok(calls.includes('stats') && calls.includes('logs'));
});

test('creation wizard keeps selection on return and creates only one task if start fails', async () => {
  const app = createApp();
  app.selCred=[2,5];
  app.beginTaskCreation('selected');
  app.nextTaskStep();
  assert.equal(app.taskStep,2);
  app.previousTaskStep();
  assert.equal(app.taskStep,1);
  assert.deepEqual(Array.from(app.taskSelection),[2,5]);
  app.nextTaskStep();
  let creates=0, starts=0;
  app.api=async(url)=> {
    if(url==='/api/batches') { creates++;return {id:17}; }
    if(url==='/api/batches/17/start') { starts++;if(starts===1)throw Error('兑换码未配置');return {ok:true}; }
    return {items:[],total:0};
  };
  app.loadStats=async()=>{};app.openBatch=async()=>{};app.loadBatches=async()=>{};
  await app.createSelectedTask();
  assert.equal(app.createdTaskId,17);
  assert.equal(app.taskStep,2);
  await app.createSelectedTask();
  assert.equal(creates,1);assert.equal(starts,2);
});

test('downloads send authorization instead of opening a cookie-only URL', async () => {
  const app=createApp();
  app.selectedSuccess=[1,3];
  const downloads=[];
  app.downloadTxt=async(url,name)=>downloads.push({url,name});
  await app.exportSuccesses();
  assert.equal(downloads.length,1);
  assert.ok(downloads[0].url.includes('ids=1,3'));
  assert.ok(downloads[0].name.endsWith('.txt'));
});

test('settings load one supplier summary without exposing separate credential forms', async () => {
  const app=createApp();
  app.api=async url=>url==='/api/configs/supplier'?{summary:'ytc 内置代理 · 提链+支付 · Handy 接码',credential_items:[],credential_status:{ytc:true}}:[
    {key:'sms_provider.cdk',value:'****',masked:true},
    {key:'extract_provider.cdk',value:'****',masked:true},
    {key:'payment_provider.cdk',value:'****',masked:true},
    {key:'app.concurrency',value:'7',masked:false},
    {key:'qualification.api_url',value:'https://hidden.example',masked:false}];
  await app.loadConfigs();
  assert.equal(app.supplierConfig.summary,'ytc 内置代理 · 提链+支付 · Handy 接码');
  assert.equal(app.configs,undefined);
  assert.equal(app.concurrencyValue,'7');
  const settings=html.match(/<section x-show="tab==='settings'"[\s\S]*?<\/section>/)[0];
  assert.match(settings,/供应商配置/);
  assert.doesNotMatch(settings,/saveYtcProxySource\(\)|x-for="c in configs"/);
  assert.doesNotMatch(html,/async saveSmsEnabled|async saveYtcProxySource|async saveConfig\(/);
});

test('supplier wizard skips integrated payment, confirms before saving, and cancels without writes', async () => {
  const app=createApp();
  app.providers=[{id:'ytc',name:'ytc',category:'extract_pay',combined:true},{id:'handy',name:'Handy',category:'sms'}];
  app.supplierConfig={credential_status:{ytc:true,handy:true}};
  const calls=[];
  app.api=async (url,opt)=>{calls.push([url,opt]);return {summary:'saved',credential_items:[]};};
  app.beginSupplierConfig();
  app.chooseSupplierFlow('extract_pay');
  app.chooseSupplierExtract('ytc');
  app.chooseSupplierProxy('provider');
  assert.equal(app.supplierStep,5);
  app.chooseSupplierSms('yes');
  assert.equal(app.supplierStep,6);
  app.chooseSupplierSmsProvider('handy');
  assert.equal(app.supplierStep,7);
  app.reviewSupplierConfig();
  assert.equal(app.supplierStep,8);
  assert.equal(calls.length,0);
  app.previousSupplierStep();
  assert.equal(app.supplierStep,7);
  app.reviewSupplierConfig();
  await app.saveSupplierConfig();
  const body=JSON.parse(calls[0][1].body);
  assert.deepEqual(body,{flow_type:'extract_pay',extract_provider:'ytc',payment_provider:'ytc',proxy_source:'provider',sms_enabled:true,sms_provider:'handy',credentials:{}});
  assert.equal(app.supplierStep,0);
  app.beginSupplierConfig();app.supplierDraft.credentials.ytc='ohk_discard';
  app.cancelSupplierConfig();
  assert.equal(calls.length,1);
  assert.equal(app.supplierStep,0);
  assert.ok(!JSON.stringify(app.supplierDraft).includes('ohk_discard'));
});

test('supplier options do not append placeholder suffixes to provider names', () => {
  const settings=html.match(/<section x-show="tab==='settings'"[\s\S]*?<\/section>/)[0];
  assert.doesNotMatch(settings,/\+\(p\.id==='ytc'\?'':'（占位）'\)/);
  assert.doesNotMatch(settings,/\+\(p\.id==='handy'\?'':'（占位）'\)/);
  assert.doesNotMatch(settings,/\+\(p\.id==='xxsyun_upi'\?'（使用 ytc API Key）':'（占位）'\)/);
  assert.match(settings,/未接入的组合会在确认或启动时提示/);
});

test('single and multiple JSON downloads select CPA, sub2api or both', async () => {
  const app=createApp();
  const downloads=[];
  app.downloadTxt=async(url,name)=>downloads.push({url,name});
  app.selectedSuccess=[1,3];
  app.archiveFormat='both';
  await app.downloadSuccessJson();
  assert.ok(downloads[0].url.includes('ids=1,3'));
  assert.ok(downloads[0].url.includes('format=both'));
  await app.downloadSuccessJson(3,'cpa');
  assert.ok(downloads[1].url.includes('ids=3'));
  assert.ok(downloads[1].url.includes('format=cpa'));
  assert.ok(downloads.every(d=>d.name.endsWith('.zip')));
});

test('delete actions use correct APIs and password form has no purchase button', async () => {
  const app=createApp();const calls=[];
  app.api=async(url,opt)=>{calls.push([url,opt]);return {deleted:2};};
  app.loadCreds=async()=>{};app.loadStats=async()=>{};app.loadBatches=async()=>{};
  app.selCred=[1,2];
  await app.deleteSelectedAccounts();
  assert.equal(calls[0][0],'/api/credentials/batch-delete');
  assert.deepEqual(JSON.parse(calls[0][1].body),{ids:[1,2]});
  await app.deleteTask(3);
  assert.equal(calls[1][0],'/api/batches/3');
  assert.equal(calls[1][1].method,'DELETE');
  const passwordForm=html.match(/<form[^>]*@submit\.prevent="changePassword\(\)"[\s\S]*?<\/form>/)[0];
  assert.ok(!passwordForm.includes('去购买'));
});

test('account page size uses 10,20,50,100 and does not lose selection', async () => {
  const app=createApp();
  app.selCred=[1,3];
  const calls=[];
  app.api=async(url)=>{calls.push(url);return {items:[],total:200};};
  app.credPage=4;
  app.credPageSize=50;
  await app.changeCredPageSize();
  assert.equal(app.credPage,1);
  assert.ok(calls[0].includes('page_size=50'));
  assert.deepEqual(Array.from(app.selCred),[1,3]);
  for (const size of [10,20,50,100])assert.ok(html.includes('<option value="'+size+'">'+size));
});

test('account library polling refreshes statuses without resetting selection', async () => {
  const app=createApp();
  app.token='test';app.tab='credentials';app.selCred=[1];
  let reads=0;
  app.loadCreds=async()=>reads++;
  await app.tick();
  assert.equal(reads,1);
  assert.deepEqual(Array.from(app.selCred),[1]);
});

test('concurrency control only saves nonzero positive integers', async () => {
  const app=createApp();
  const calls=[];
  app.api=async(url,opt)=>calls.push({url,opt});
  for(const value of ['','0','-1','1.2','1e2','abc']){
    app.concurrencyValue=value;
    await app.saveConcurrency();
  }
  assert.equal(calls.length,0);
  app.concurrencyValue='6';
  await app.saveConcurrency();
  assert.equal(calls[0].url,'/api/configs/app.concurrency');
  assert.deepEqual(JSON.parse(calls[0].opt.body),{value:'6'});
});

test('account purchase uses one supplier dropdown and source filter keeps cross-page selection', async () => {
  const app=createApp();
  const accounts=html.match(/<section x-show="tab==='credentials'"[\s\S]*?<\/section>/)[0];
  assert.equal([...accounts.matchAll(/>去购买<\/a>/g)].length,1);
  assert.doesNotMatch(accounts,/去购买 · 0217|去购买 · AI 批发商/);
  assert.match(accounts,/选择账号服务商/);
  assert.match(accounts,/按来源文件筛选/);
  app.selCred=[1,5];app.credPage=3;app.credSource='imports & 1.txt';
  const calls=[];
  app.api=async url=>{calls.push(url);return {items:[],total:0};};
  await app.changeCredSource();
  assert.equal(app.credPage,1);
  assert.equal(new URLSearchParams(calls[0].split('?')[1]).get('source_file'),'imports & 1.txt');
  assert.deepEqual(Array.from(app.selCred),[1,5]);
});

test('selected-account entry only shows selected rows and deselect removes candidates', async () => {
  const app=createApp();app.selCred=[2,5];app.selectedAccounts={2:'two@example.com',5:'five@example.com'};
  app.api=async()=>({items:[{id:1,account:'one'},{id:2,account:'two'},{id:5,account:'five'}],total:3});
  app.beginTaskCreation('selected');
  await app.loadWizardAccounts();
  assert.deepEqual(Array.from(app.wizardAccounts,c=>c.id),[2,5]);
  app.removeTaskAccount(2);
  assert.deepEqual(Array.from(app.taskSelection),[5]);
  assert.deepEqual(Array.from(app.wizardAccounts,c=>c.id),[5]);
  app.cancelTaskCreation();
  app.beginTaskCreation('all');
  assert.deepEqual(Array.from(app.taskSelection),[]);
  assert.deepEqual(Array.from(app.selCred),[2,5]);
});

test('cancel of created but unstarted draft prompts then removes only that draft', async () => {
  const app=createApp();
  app.taskStep=2;app.createdTaskId=19;app.taskSelection=[2];
  const calls=[];app.api=async(url,opt)=>calls.push([url,opt.method]);
  await app.cancelTaskCreation();
  assert.deepEqual(calls,[['/api/batches/19','DELETE']]);
  assert.equal(app.taskStep,0);assert.equal(app.createdTaskId,null);
});

test('manual push reports rejected selected accounts instead of unconditional success', async () => {
  const app=createApp();app.selectedSuccess=[1,2];
  app.api=async()=>({count:1,failed:1,results:[{credential_id:1,status:'queued'},{credential_id:2,status:'rejected',error:'未接码账号不能推送'}]});
  await app.enqueueSelectedPushes();
  assert.ok(app.toasts.some(t=>t.msg.includes('未接码账号不能推送') && !t.ok));
});

test('success library exposes one manual sub2api push action for selected accounts', () => {
  const section=html.match(/<section x-show="tab==='successes'"[\s\S]*?<\/section>/)[0];
  const exportIndex=section.indexOf('导出选中 TXT');
  const pushIndex=section.indexOf('推送 sub2api');
  assert.ok(exportIndex>=0 && pushIndex>exportIndex,'推送按钮应紧邻导出 TXT 按钮');
  assert.match(section,/enqueueSelectedPushes\(\)/);
  assert.doesNotMatch(section,/接码成功账号会按任务设置自动推送/);
  assert.doesNotMatch(section,/已成功未接码账号仅可导出 TXT，不生成 JSON、不推送/);
});

test('proxy library exposes add import free-import batch-delete and row test controls', () => {
  const section=html.match(/<section x-show="tab==='proxies'"[\s\S]*?<\/section>/)[0];
  for (const label of ['添加代理','导入代理','批量测试','批量删除','测试连接']) assert.match(section,new RegExp(label));
  assert.doesNotMatch(section,/免费代理导入|proxy\.scdn\.io|import-free/);
  assert.match(section,/动态 IP（网关代理/);
  assert.match(section,/动态标记/);
  assert.match(section,/711proxy 动态代理/);
  assert.match(section,/保存动态代理配置/);
  assert.match(section,/拉取并添加/);
  assert.match(html,/async toggleProxyDynamic\(proxy\)/);
  assert.match(html,/exits\.slice\(0,2\)\.join/);
  assert.match(section,/选择全部代理/);
  assert.match(section,/本机经代理实测/);
  assert.doesNotMatch(section,/test_proxy\.php/);
  assert.match(html,/proxyTestText\(result\)/);
  assert.match(html,/tested_protocol/);
  assert.match(html,/出口 '\+result\.exit_ip/);
});

test('dynamic proxy save and fetch are separate and secrets are cleared after save', async () => {
  const app=createApp();const calls=[];
  app.dynamicProxyUrl='https://api.711proxy.com/gen?token=secret';app.dynamicProxyEnabled=true;app.dynamicProxyProtocol='socks5';
  app.api=async(url,opt)=>{calls.push([url,opt]);return url.endsWith('/fetch')?{items:[],fetched:2,enabled:false}:{url_saved:true,enabled:true,protocol:'socks5',host:'api.711proxy.com'};};
  await app.saveDynamicProxyConfig();
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],'/api/proxies/dynamic');
  assert.equal(app.dynamicProxyUrl,'');
  await app.fetchDynamicProxies();
  assert.equal(calls[1][0],'/api/proxies/dynamic/fetch');
  assert.ok(!calls[1][1].body);
});

test('supplier extract-only flow asks for payment and no SMS skips supplier question', () => {
  const app=createApp();app.supplierConfig={credential_status:{lumen:true,masi:true}};
  app.beginSupplierConfig();app.chooseSupplierFlow('extract');app.chooseSupplierExtract('lumen');app.chooseSupplierProxy('system');
  assert.equal(app.supplierStep,4);
  app.chooseSupplierPayment('masi');app.chooseSupplierSms('no');
  assert.equal(app.supplierStep,7);
  assert.deepEqual(Array.from(app.supplierCredentialIds()),['lumen','masi']);
  app.reviewSupplierConfig();assert.equal(app.supplierStep,8);assert.equal(app.supplierDraftExecutable(),false);
  app.previousSupplierStep();app.previousSupplierStep();assert.equal(app.supplierStep,5);
  app.chooseSupplierSms('yes');assert.equal(app.supplierStep,6);
  app.chooseSupplierFlow('extract_pay');assert.equal(app.supplierStep,2);assert.deepEqual(Array.from(app.supplierCredentialIds()),[]);
});

test('logout clears unsaved supplier and dynamic proxy secrets even while busy', () => {
  const app=createApp();app.busy=true;
  app.supplierStep=7;app.supplierDraft.credentials={ytc:'ohk_unsaved'};app.dynamicProxyUrl='https://api.711proxy.com/gen?token=unsaved';
  app.logout();
  assert.equal(app.supplierStep,0);
  assert.equal(app.dynamicProxyUrl,'');
  assert.ok(!JSON.stringify(app.supplierDraft).includes('ohk_unsaved'));
});
