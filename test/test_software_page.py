"""执行真实软件管理脚本，验证独立分区、保守归组和逐包操作边界。"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

PAGE = Path(__file__).resolve().parents[1] / 'web/software.html'
HARNESS = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
let currentDocument;
class Element {
 constructor(tag='div') { this.tag=tag;this.children=[];this.events={};this.value='';this.disabled=false;this.attributes={};this.ownText='';this.className='';this.classes=new Set();this.open=false;this.classList={toggle:(n,v)=>v?this.classes.add(n):this.classes.delete(n)}; }
 set innerHTML(_) { throw new Error('禁止动态 innerHTML'); }
 set textContent(v) {this.replaceChildren();this.ownText=String(v);}
 get textContent() {return this.ownText+this.children.map(n=>n.textContent).join('');}
 append(...nodes) {const wasEmpty=this.children.length===0;for(const n of nodes){n.parentNode=this;this.children.push(n);}if(this.tag==='select'&&wasEmpty&&nodes.length)this.value=nodes[0].value;}
 replaceChildren(...nodes) {if(this.children.some(n=>n.contains(currentDocument?.activeElement)))currentDocument.activeElement=null;this.children=[];this.ownText='';this.append(...nodes);}
 contains(n){return this===n||this.children.some(c=>c.contains(n));}
 addEventListener(n,f){this.events[n]=f;}
 setAttribute(n,v){this.attributes[n]=String(v);}
 getAttribute(n){return this.attributes[n]??null;}
 focus(){currentDocument.activeElement=this;}
}
const flatten=nodes=>nodes.flatMap(n=>[n,...flatten(n.children)]);
function record(fields={}){const {metadata={},...rest}=fields;return {id:'upper-new',metadata:{product:'mskdsp-upper',version:'1.2.3',platform:'windows-x86_64-nsis',file_name:'installer.exe',size:2048,sha256:'a'.repeat(64),notes:'Release notes',...metadata},channels:[],ever_published:false,created_at:'2026-10-15T00:00:00Z',...rest};}
const lower=f=>record({id:'lower',...f,metadata:{product:'mskdsp-lower',version:'0.4.0',platform:'linux-arm64',file_name:'lower.tar.gz',...f?.metadata}});
const platform=f=>record({id:'platform',...f,metadata:{product:'mskdsp-platform',version:'0.0.1-ci.20261018t094236.6.98+sha.aaaaaaa',platform:'linux-amd64',file_name:'platform.tar.gz',channel:'ci',source_ref:'main',source_sha:'a'.repeat(40),build_time:'2026-10-18T09:42:36Z',...f?.metadata}});
const response=(status,body)=>({ok:status>=200&&status<300,status,json:async()=>body});
async function open({records=[record(),lower(),platform()],user={username:'operator',role:'admin'},rejectAt='',failList=false,statusCode=200,statusBody={enabled:true,state:'available',current_version:'0.1.0',latest_version:'0.2.0',message:'等待管理员确认'},cleanupDays=null,cleanupCandidates=[]}={}){
 const ids={};for(const m of HTML.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)){const n=new Element(m[1]);n.id=m[2];ids[n.id]=n;}if(ids.view)ids.view.value='list';
 const calls=[],redirects=[],confirms=[],state={records,failList,rejectAt,statusCode,statusBody,allowConfirm:true,publishFailure:false,hold:false,finish:null,cleanupDays,cleanupCandidates,cleanupError:null,cleanupConflict:false,pendingCleanup:0};
 const all=()=>flatten(Object.values(ids));const document={activeElement:null,getElementById:id=>all().find(n=>n.id===id)||null,createElement:t=>new Element(t),querySelectorAll:()=>all().filter(n=>n.getAttribute('data-package-control')!==null)};currentDocument=document;
 const context=vm.createContext({AbortController,setTimeout,clearTimeout,document,window:{confirm:v=>{confirms.push(v);return state.allowConfirm;}},location:{assign:v=>redirects.push(v)},fetch:async(path,options)=>{
  calls.push({path,options});if(path===state.rejectAt)return response(401,{error:'请重新登录'});
  if(path==='/api/v1/auth/me')return response(200,user);if(path==='/api/v1/auth/logout')return response(204,null);
  if(path==='/api/update/status'){if(state.statusCode===0)throw new Error('维护接口离线');return response(state.statusCode,state.statusBody);}
  if(path.startsWith('/api/v1/software/cleanup')) {
   if(options.method!=='POST')return response(200,{days:state.cleanupDays});
   const {days,package_ids}=JSON.parse(options.body);
   const sum=items=>items.reduce((total,pkg)=>total+pkg.size,0);
   const remove=items=>{state.records=state.records.filter(pkg=>!items.some(item=>item.id===pkg.id));return {deleted:items.length,freed_bytes:sum(items),pending:state.pendingCleanup};};
   if(path.endsWith('/preview'))return response(200,{packages:state.cleanupCandidates,total_bytes:sum(state.cleanupCandidates)});
   if(path.endsWith('/settings')){
    state.cleanupDays=days;
    return response(200,{days,result:days===null||state.cleanupError?null:remove(state.cleanupCandidates),cleanup_error:state.cleanupError});
   }
   if(state.cleanupConflict)return response(409,{error:'清理候选包已变化，请重新预览'});
   return response(200,remove(state.cleanupCandidates.filter(pkg=>package_ids.includes(pkg.id))));
  }
  if(options.method==='POST'){
   if(state.publishFailure)return response(412,{error:'尚未配置公开地址'});if(state.hold)await new Promise(r=>{state.finish=r;});
   const channel=JSON.parse(options.body).channel,target=decodeURIComponent(path.split('/').at(-2)),chosen=state.records.find(p=>p.id===target),publish=path.endsWith('/publish');
   state.records=state.records.map(p=>{if(p.id===target)return {...p,channels:publish?[...new Set([...p.channels,channel])]:p.channels.filter(n=>n!==channel),ever_published:true};
    const replaced=chosen.metadata.product==='mskdsp-upper'?p.metadata.version!==chosen.metadata.version:p.metadata.platform===chosen.metadata.platform;
    return publish&&p.metadata.product===chosen.metadata.product&&replaced?{...p,channels:p.channels.filter(n=>n!==channel)}:p;});return response(200,state.records.find(p=>p.id===target));}
  if(state.failList)throw new Error('网络离线');return response(200,state.records);
 }});
 await vm.runInContext('(async()=>{'+SCRIPT+'\nthis.api={refresh,mutate,sections:typeof sections===\'undefined\'?null:sections,groupPackages:typeof groupPackages===\'undefined\'?null:groupPackages,snapshot:()=>({busy,authenticated,packages})};})()',context);
 const section=p=>context.api.sections.get('mskdsp-'+p),expand=async p=>{const s=section(p);s.history.open=true;await s.history.events.toggle();return s;};
 return {ids,calls,redirects,confirms,state,api:context.api,document,section,expand,flatten};
}
'''

class SoftwarePageTests(unittest.TestCase):
    @classmethod

    def setUpClass(cls):
        cls.node=shutil.which('node')
        if not cls.node: raise AssertionError('软件页面行为测试需要 Node')
        cls.page=PAGE.read_text(encoding='utf-8-sig')
        cls.script=re.search(r'<script type="module">([\s\S]*?)</script>',cls.page).group(1)
        syntax=subprocess.run([cls.node,'--check','--input-type=module'],input=cls.script,capture_output=True,text=True,encoding='utf-8',timeout=15)
        if syntax.returncode: raise AssertionError(syntax.stderr)

    def run_scenario(self,scenario):
        source='const SCRIPT='+json.dumps(self.script)+';const HTML='+json.dumps(self.page)+';\n'+HARNESS+'\n(async()=>{\n'+scenario+'\n})().catch(e=>{console.error(e);process.exitCode=1});'
        result=subprocess.run([self.node],input=source,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_fixed_sections_and_read_only_platform_status(self):
        self.run_scenario(r'''
const products=['upper','lower','testbench','testbench-cases','platform'];
const p=await open();assert(p.api.sections);assert.equal(p.api.sections.size,products.length);assert.equal(p.document.getElementById('product'),null);
for(const product of products)assert.equal(p.section(product).history.open,false);
assert(p.section('upper').summary.textContent.includes('暂无推荐版本'));assert(p.section('platform').summary.textContent.includes('0.1.0'));assert(p.section('platform').summary.textContent.includes('0.2.0'));
assert.equal(p.calls[0].path,'/api/v1/auth/me');assert.equal(p.calls.filter(c=>c.path==='/api/update/status').length,1);assert(p.calls.every(c=>!c.options.method||c.options.method==='GET'));
const empty=await open({records:[]});for(const product of products)assert((await empty.expand(product)).empty.textContent.includes('暂无安装包'));
        ''')

    def test_testbench_actions_keep_ci_and_stable_separate(self):
        self.run_scenario(r'''
for(const [name,stableChannel,platform] of [['testbench','','windows-x86_64-nsis'],['testbench-cases','stable','python-zip']]) {
const product=`mskdsp-${name}`;
const pkg=record({id:name,metadata:{product,channel:stableChannel,platform}});
const ci=record({id:`${name}-ci`,metadata:{product,channel:'ci',version:'1.2.3-ci.10.2',platform}});
const p=await open({records:[pkg,ci,record()]}),section=await p.expand(name);
assert.deepEqual(section.channel.children.map(n=>n.value),['','unpublished','ci','stable']);
assert(section.summary.textContent.includes('STABLE'));assert(section.summary.textContent.includes('CI'));
const selects=flatten(section.list.children).filter(n=>n.tag==='select');
assert.deepEqual(selects.map(select=>select.children.map(n=>n.value).join(',')).sort(),['ci','stable']);
await p.api.mutate(pkg,'ci','publish');assert.equal(p.confirms.length,0);
await p.api.mutate(ci,'stable','publish');assert.equal(p.confirms.length,0);
await p.api.mutate(ci,'ci','publish');assert(section.summary.textContent.includes('1.2.3-ci.10.2'));
await p.api.mutate(pkg,'stable','publish');assert(section.summary.textContent.includes('1.2.3'));
section.channel.value='stable';section.channel.events.change();assert.equal(section.count.textContent,'1 个安装包');
assert(!section.list.textContent.includes('1.2.3-ci.10.2'));
assert(!p.section('upper').summary.textContent.includes('1.2.3'));
assert(!p.section(name==='testbench'?'testbench-cases':'testbench').summary.textContent.includes('1.2.3'));
}
        ''')

    def test_local_filters_summary_and_focus_survive_refresh(self):
        self.run_scenario(r'''
const old=record({id:'old',metadata:{version:'1.1.0'},channels:['stable'],ever_published:true});const p=await open({records:[record(),old,lower(),platform()]});const u=await p.expand('upper'),l=await p.expand('lower'),summary=u.summary.textContent;
u.search.value='missing';u.search.focus();u.search.events.input();assert.equal(u.count.textContent,'0 个安装包');assert(u.empty.textContent.includes('没有匹配'));assert.equal(u.summary.textContent,summary);assert(summary.includes('1.1.0'));assert.equal(l.count.textContent,'1 个安装包');assert.equal(p.document.activeElement,u.search);
l.search.value='0.4';l.search.events.input();l.channel.value='unpublished';l.channel.events.change();const search=l.search;search.focus();await p.api.refresh();assert.equal(p.document.activeElement,search);assert.equal(p.section('lower').search,search);assert.equal(l.search.value,'0.4');assert.equal(l.channel.value,'unpublished');assert(u.history.open&&l.history.open);
u.clear.events.click();assert.equal(u.count.textContent,'2 个安装包');assert.equal(l.search.value,'0.4');u.architecture.value='linux-arm64';u.architecture.events.change();assert.equal(u.count.textContent,'0 个安装包');u.clear.events.click();u.view.value='cards';u.view.events.change();assert(!flatten(u.list.children).some(n=>n.tag==='table'));assert.equal(l.channel.value,'unpublished');
        ''')

    def test_conservative_build_grouping(self):
        self.run_scenario(r'''
const src={source_repository:'repo',source_ref:'main',source_sha:'a'.repeat(40)};const a=lower({id:'arm',metadata:{...src,version:'0.4.0-ci-p18-b289-j292-ab39c1a',platform:'linux-arm64'}}),b=lower({id:'x64',metadata:{...src,version:'0.4.0-ci-p18-b290-j293-ab39c1a',platform:'linux-x64'}});const p=await open({records:[a,b]}),group=p.api.groupPackages;
assert.equal(group([a,b]).length,1);assert.equal(group([a,b])[0].packages.length,2);
for(const changed of [{...b,metadata:{...b.metadata,version:'0.4.0-ci-p19-b290-j293-ab39c1a'}},{...b,metadata:{...b.metadata,source_ref:'release'}},{...b,metadata:{...b.metadata,source_sha:'b'.repeat(40)}},{...b,metadata:{...b.metadata,source_repository:''}},{...b,metadata:{...b.metadata,platform:'linux-arm64'}},{...b,metadata:{...b.metadata,version:'0.5.0-ci-p18-b290-j293-ab39c1a'}}])assert.equal(group([a,changed]).length,2);
assert.equal(group([a,b,{...b,id:'duplicate'}]).length,3);assert.equal(group([a,b],[a,b,{...b,id:'duplicate'}]).length,2);assert.equal(group([{...a,metadata:{...a.metadata,version:'0.4.0'}},{...b,metadata:{...b.metadata,version:'0.4.0'}}]).length,1);
const nodes=flatten((await p.expand('lower')).list.children);assert(nodes.some(n=>n.textContent===a.metadata.version));assert(nodes.some(n=>n.textContent===b.metadata.version));assert.equal(nodes.filter(n=>n.tag==='button'&&n.textContent==='发布').length,2);assert(!nodes.some(n=>n.tag==='th'&&n.textContent==='产品'));
const part=p.section('lower');part.search.value='ab39c1a';part.search.events.input();assert(flatten(part.list.children).some(n=>n.className==='build-group'));
p.state.records.push({...b,id:'hidden-duplicate',metadata:{...b.metadata,version:'0.4.0-ci-p18-b999-j999-bbbbbbb'}});await p.api.refresh();assert.equal(part.count.textContent,'2 个安装包');assert(!flatten(part.list.children).some(n=>n.className==='build-group'));
        ''')

    def test_platform_failure_is_local_and_401_stops_actions(self):
        self.run_scenario(r'''
for(const options of [{statusCode:500,statusBody:{error:'维护不可用'}},{statusCode:404},{statusCode:503},{statusCode:0},{statusBody:{}},{statusBody:null}]){const p=await open(options);assert.equal((await p.expand('upper')).count.textContent,'1 个安装包');assert.equal(p.ids.refresh.disabled,false);assert(p.section('platform').summary.textContent.includes('暂不可用'));assert(!p.section('platform').summary.textContent.includes('正在运行'));assert(!p.calls.some(c=>c.options.method==='POST'));}
const p=await open({rejectAt:'/api/update/status'});assert.deepEqual(p.redirects,['/login']);assert.equal(p.api.snapshot().authenticated,false);const count=p.calls.length;await p.api.mutate(record(),'stable','publish');await p.api.refresh();assert.equal(p.calls.length,count);
        ''')

    def test_actions_confirmations_and_other_section_preserved(self):
        self.run_scenario(r'''
const old=record({id:'old',metadata:{version:'1.1.0'},channels:['stable'],ever_published:true}),other=record({id:'other',metadata:{version:'1.1.0',platform:'windows-x86_64'},channels:['stable'],ever_published:true});const p=await open({records:[record(),old,other,lower()]}),u=await p.expand('upper'),l=await p.expand('lower'),nodes=flatten(u.list.children),selects=nodes.filter(n=>n.tag==='select'),withdraws=nodes.filter(n=>n.tag==='button'&&n.textContent==='撤回');assert.equal(selects.at(-1).value,'stable');assert.equal(withdraws[0].disabled,true);assert.equal(withdraws[1].disabled,false);selects.at(-1).value='beta';selects.at(-1).events.change();assert.equal(withdraws.at(-1).disabled,true);
const calls=p.calls.length;p.state.allowConfirm=false;await p.api.mutate(p.state.records[0],'stable','publish');assert.equal(p.calls.length,calls);assert(p.confirms.at(-1).includes('1.1.0 → 1.2.3'));assert(p.confirms.at(-1).includes('windows-x86_64'));assert(p.confirms.at(-1).includes('稳定通道'));await p.api.mutate(p.state.records[0],'beta','withdraw');assert.equal(p.confirms.length,1);await p.api.mutate(old,'stable','withdraw');assert(p.confirms.at(-1).includes('仍可下载'));
l.search.value='0.4';l.search.events.input();l.search.focus();p.state.allowConfirm=true;p.state.hold=true;const pending=p.api.mutate(p.state.records[0],'stable','publish');assert(p.api.snapshot().busy);assert(p.ids.refresh.disabled);const held=p.calls.length;await p.api.mutate(p.state.records[0],'stable','publish');assert.equal(p.calls.length,held);p.state.finish();await pending;p.state.hold=false;assert(l.history.open);assert.equal(l.search.value,'0.4');assert.equal(p.document.activeElement,l.search);assert(u.summary.textContent.includes('1.2.3'));assert(!u.summary.textContent.includes('1.1.0'));
const post=p.calls.find(c=>c.options.method==='POST');assert.equal(post.options.headers['X-MskDSP-Action'],'software');assert.equal(post.options.credentials,'same-origin');assert.deepEqual(JSON.parse(post.options.body),{channel:'stable'});await p.api.mutate(p.state.records[0],'stable','withdraw');assert.equal(p.state.records[0].channels.length,0);assert(u.summary.textContent.includes('暂无推荐版本'));
        ''')

    def test_candidate_boundary_safe_text_and_dates(self):
        self.run_scenario(r'''
const candidate=platform(),malicious=record({id:'id/<script>',metadata:{version:'<img src=x onerror=alert(1)>',notes:'<script>alert(1)</script>'},channels:['ci'],ever_published:true}),p=await open({records:[candidate,malicious,lower({metadata:{platform:'linux-x64'}})]});const nodes=flatten((await p.expand('platform')).list.children);assert(nodes.some(n=>n.textContent==='CI 候选包 · 待手动升级'));assert(nodes.some(n=>n.tag==='time'&&n.getAttribute('datetime')===candidate.metadata.build_time&&n.title.includes('时区')));assert(!nodes.some(n=>n.tag==='button'||n.tag==='select'||n.href?.startsWith('/software/packages/')));const count=p.calls.length;await p.api.mutate(candidate,'ci','publish');assert.equal(p.calls.length,count);assert.equal(p.confirms.length,0);
const upperNodes=flatten((await p.expand('upper')).list.children);assert(upperNodes.some(n=>n.textContent===malicious.metadata.version));assert(upperNodes.some(n=>n.tag==='p'&&n.textContent===malicious.metadata.notes));assert(upperNodes.some(n=>n.href==='/software/packages/id%2F%3Cscript%3E/installer.exe'));const time=upperNodes.find(n=>n.tag==='time');assert.equal(time.getAttribute('datetime'),malicious.created_at);assert.notEqual(time.textContent,malicious.created_at);const select=flatten((await p.expand('lower')).list.children).find(n=>n.tag==='select');assert.equal(select.children.length,1);assert.equal(select.value,'ci');const invalid=await open({records:[record({created_at:'invalid'})]});assert(flatten((await invalid.expand('upper')).list.children).some(n=>n.tag==='time'&&n.textContent==='—'));
        ''')

    def test_beta_is_history_only_and_can_be_withdrawn(self):
        self.run_scenario(r'''
const legacy=record({id:'legacy',metadata:{version:'1.1.0'},channels:['beta'],ever_published:true}),p=await open({records:[record(),legacy,lower()]});
for(const name of ['upper','lower','platform'])assert(!p.section(name).channel.children.some(n=>n.value==='beta'));
for(const name of ['upper','lower'])assert(!p.section(name).summary.textContent.includes('BETA'));
const nodes=flatten((await p.expand('upper')).list.children);for(const select of nodes.filter(n=>n.tag==='select'))assert(!select.children.some(n=>n.value==='beta'));
assert(nodes.some(n=>n.textContent==='BETA'));assert(nodes.some(n=>n.href==='/software/packages/legacy/installer.exe'));
const calls=p.calls.length;await p.api.mutate(legacy,'beta','publish');assert.equal(p.calls.length,calls);assert.equal(p.confirms.length,0);
const withdraw=nodes.find(n=>n.tag==='button'&&n.textContent==='撤回历史 Beta');assert(withdraw);await withdraw.events.click();
const post=p.calls.find(c=>c.options.method==='POST');assert(post.path.endsWith('/legacy/withdraw'));assert.deepEqual(JSON.parse(post.options.body),{channel:'beta'});assert(p.confirms.at(-1).includes('仍可下载'));assert(p.state.records.find(n=>n.id==='legacy').ever_published);assert.equal(p.state.records.find(n=>n.id==='legacy').channels.length,0);
assert(!flatten(p.section('upper').list.children).some(n=>n.tag==='button'&&n.textContent==='撤回历史 Beta'));
        ''')

    def test_errors_recovery_and_authentication(self):
        self.run_scenario(r'''
const p=await open();await p.expand('upper');p.state.publishFailure=true;await p.api.mutate(p.state.records[0],'stable','publish');assert.equal(p.ids.message.textContent,'尚未配置公开地址');assert(p.ids.message.classes.has('error'));p.state.publishFailure=false;p.state.failList=true;await p.api.refresh();assert.equal(p.ids.message.textContent,'网络离线');assert.equal(p.ids.refresh.disabled,false);assert.equal(p.section('upper').count.textContent,'1 个安装包');const posts=p.calls.filter(c=>c.options.method==='POST').length;p.state.failList=false;await p.api.refresh();assert.equal(p.calls.filter(c=>c.options.method==='POST').length,posts);
const denied=await open({user:{username:'viewer',role:'viewer'}});assert.equal(denied.calls.length,1);assert(denied.ids.refresh.disabled);for(const rejectAt of ['/api/v1/auth/me','/api/v1/software/packages']){const expired=await open({rejectAt});assert.deepEqual(expired.redirects,['/login']);assert(expired.ids.refresh.disabled);}await p.ids.logout.events.click();assert.equal(p.calls.at(-1).path,'/api/v1/auth/logout');assert.equal(p.calls.at(-1).options.headers['X-MskDSP-Action'],'auth');assert.deepEqual(p.redirects,['/login']);
const initialFailure=await open({failList:true});assert(initialFailure.section('platform').summary.textContent.includes('0.1.0'));assert((await initialFailure.expand('upper')).empty.textContent.includes('暂不可用'));
        ''')

    def test_cleanup_setting_is_saved_explicitly_and_reports_immediate_result(self):
        self.run_scenario(r'''
const pkg=record(), candidate={id:pkg.id,size:2048};
const p=await open({records:[pkg],cleanupDays:45,cleanupCandidates:[candidate]});
const {ids,calls,state}=p, auto=ids['cleanup-auto-days'], manual=ids['cleanup-manual-days'];
assert.deepEqual(auto.children.map(option=>option.value),['','7','30','45','60','90']);
assert.deepEqual(manual.children.map(option=>option.value),['7','30','45','60','90']);
assert.equal(auto.value,'45');assert.equal(manual.value,'30');
auto.value='7';assert(calls.every(call=>call.options.method!=='POST'));
await ids['cleanup-save'].events.click();
const save=calls.find(call=>call.options.method==='POST');
assert.equal(save.path,'/api/v1/software/cleanup/settings');assert.equal(save.options.headers['X-MskDSP-Action'],'software');
assert.deepEqual(JSON.parse(save.options.body),{days:7});assert.equal(state.cleanupDays,7);
assert.equal(calls.at(-1).path,'/api/v1/software/packages');assert.equal(p.section('upper').total.textContent,'0 个安装包');
assert.match(ids['cleanup-settings-status'].textContent,/已清理 1 个安装包，释放 2.0 KiB/);
state.cleanupError='仓库离线';auto.value='90';await ids['cleanup-save'].events.click();
assert.equal(auto.value,'90');assert.match(ids['cleanup-settings-status'].textContent,/已保存 90 天保留期限。.*仓库离线/);assert(ids['cleanup-settings-status'].classes.has('error'));
auto.value='';await ids['cleanup-save'].events.click();
assert.deepEqual(JSON.parse(calls.at(-1).options.body),{days:null});assert.equal(state.cleanupDays,null);
assert.equal(ids['cleanup-preview'].disabled,false);assert.equal(manual.value,'30');
        ''')

    def test_manual_cleanup_uses_confirmed_preview_without_changing_setting(self):
        self.run_scenario(r'''
const pkg=record({metadata:{version:'<img src=x onerror=alert(1)>'}}), other=lower();
const candidate={id:pkg.id,product:pkg.metadata.product,version:pkg.metadata.version,platform:pkg.metadata.platform,size:2048,created_at:pkg.created_at};
const p=await open({records:[pkg,other],cleanupCandidates:[candidate]}), {ids,calls,state}=p;
const select=ids['cleanup-manual-days'], preview=ids['cleanup-preview'], execute=ids['cleanup-execute'];
assert.equal(ids['cleanup-auto-days'].value,'');assert.equal(execute.disabled,true);
await preview.events.click();assert.equal(execute.disabled,false);assert.deepEqual(JSON.parse(calls.at(-1).options.body),{days:30});
assert.match(ids['cleanup-manual-status'].textContent,/预计释放 2.0 KiB/);
const nodes=flatten(ids['cleanup-preview-packages'].children);
assert(nodes.some(node=>node.tag==='td'&&node.textContent===pkg.metadata.version));
assert(nodes.some(node=>node.tag==='time'&&node.getAttribute('datetime')===pkg.created_at));
select.value='7';select.events.change();assert.equal(execute.disabled,true);
await execute.events.click();assert.equal(calls.at(-1).path,'/api/v1/software/cleanup/preview');
await preview.events.click();const count=calls.length;state.allowConfirm=false;
await execute.events.click();assert.equal(calls.length,count);assert.match(p.confirms.at(-1),/预览中的 1 个安装包/);
state.allowConfirm=true;state.cleanupConflict=true;await execute.events.click();
assert.equal(calls.at(-1).path,'/api/v1/software/packages');assert.equal(execute.disabled,true);
assert.match(ids['cleanup-manual-status'].textContent,/请重新预览/);assert(ids['cleanup-manual-status'].classes.has('error'));
state.cleanupConflict=false;state.pendingCleanup=1;await preview.events.click();await execute.events.click();assert.equal(execute.disabled,true);
const removal=calls.at(-2);assert.equal(removal.path,'/api/v1/software/cleanup');
assert.deepEqual(JSON.parse(removal.options.body),{days:7,package_ids:[pkg.id]});
assert.equal(calls.at(-1).path,'/api/v1/software/packages');assert.equal(state.cleanupDays,null);
assert.deepEqual(state.records.map(pkg=>pkg.id),[other.id]);assert.match(ids['cleanup-manual-status'].textContent,/已清理 1 个安装包.*1 个文件等待重试/);
        ''')

if __name__=='__main__': unittest.main()
