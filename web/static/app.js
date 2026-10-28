/**
 * 拼多多一键上架 - 前端逻辑
 */

// 全局状态
const state = {
    migratedProducts: []
};

function readStoredJson(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback; }
    catch (_error) { return fallback; }
}

let currentDraftPackage = readStoredJson('currentProductPackage', null);

function cleanProductPackage(value) {
    const privateFields = new Set(['api_key','image_api_key','prompt_api_key','authorization','provider_overrides','traceback']);
    if(Array.isArray(value)) return value.map(cleanProductPackage);
    if(value && typeof value==='object') return Object.fromEntries(Object.entries(value).filter(([key])=>!privateFields.has(key.toLowerCase())).map(([key,item])=>[key,cleanProductPackage(item)]));
    return value;
}

let _syncingListingPackage = false;

// A synchronous SHA-256 keeps SKU identities identical to the Python package
// model.  Identity is derived from sorted dimensions, never row order/name.
function productSkuId(values) {
    const canonical = JSON.stringify(Object.fromEntries(Object.keys(values || {}).sort().map(key => [key, String(values[key])])));
    const bytes = new TextEncoder().encode(canonical);
    const bitLength = bytes.length * 8;
    const total = Math.ceil((bytes.length + 9) / 64) * 64;
    const padded = new Uint8Array(total); padded.set(bytes); padded[bytes.length] = 128;
    const view = new DataView(padded.buffer);
    view.setUint32(total - 8, Math.floor(bitLength / 4294967296)); view.setUint32(total - 4, bitLength >>> 0);
    const k = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    const h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    const rr = (x,n) => (x >>> n) | (x << (32-n));
    for (let offset = 0; offset < total; offset += 64) {
        const w = new Uint32Array(64);
        for (let i=0;i<16;i++) w[i]=view.getUint32(offset+i*4);
        for (let i=16;i<64;i++) { const x=w[i-15], y=w[i-2]; w[i]=(w[i-16]+(rr(x,7)^rr(x,18)^(x>>>3))+w[i-7]+(rr(y,17)^rr(y,19)^(y>>>10)))>>>0; }
        let [a,b,c,d,e,f,g,j]=h;
        for(let i=0;i<64;i++){const t1=(j+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^((~e)&g))+k[i]+w[i])>>>0;const t2=((rr(a,2)^rr(a,13)^rr(a,22))+((a&b)^(a&c)^(b&c)))>>>0;j=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}
        [a,b,c,d,e,f,g,j].forEach((value,i)=>{h[i]=(h[i]+value)>>>0;});
    }
    return 'sku-' + h.map(value=>value.toString(16).padStart(8,'0')).join('').slice(0,20);
}

function mergeSkusForSpecs(specs, existing = []) {
    const dimensions = Object.entries(specs || {}).filter(([,values]) => Array.isArray(values) && values.length)
        .map(([name,values]) => [name, Array.from(new Set(values.map(String).map(value=>value.trim()).filter(Boolean)))]);
    const expected = dimensions.reduce((total,[,values])=>total*values.length,1);
    if (expected > 200) throw new Error(`规格组合共${expected}项，最多200项，请减少规格值`);
    let combinations=[{}];
    dimensions.forEach(([name,values]) => { combinations=combinations.flatMap(combo=>values.map(value=>({...combo,[name]:value}))); });
    const names=combinations.map(combo=>dimensions.map(([name])=>combo[name]).join(' ') || '默认');
    const byId=new Map(), byName=new Map();
    (existing || []).filter(row=>row && typeof row==='object').forEach(row=>{
        const values=row.spec_values || row.specs;
        const identity=values && typeof values==='object' && !Array.isArray(values) ? productSkuId(values) : (row.id || row.sku_id);
        if(identity) byId.set(identity,row);
        const name=String(row.name || '').trim(); byName.set(name,(byName.get(name)||[]).concat(row));
    });
    const used=new Set((existing||[]).map(row=>String(row.spec_code||'')));
    let code=Math.max(0,...Array.from(used).filter(value=>/^\d+$/.test(value)).map(Number))+1;
    return combinations.map((combo,index)=>{
        const id=productSkuId(combo), name=names[index];
        let previous=byId.get(id);
        if(!previous && names.filter(value=>value===name).length===1 && (byName.get(name)||[]).length===1) previous=byName.get(name)[0];
        const row={...(previous||{})};
        if(!row.spec_code){while(used.has(String(code)))code++;row.spec_code=String(code++);used.add(row.spec_code);}
        return {...row,id,sku_id:id,name,spec_values:combo,price:row.price??0,single_price:row.single_price??0,stock:row.stock??0,enabled:row.enabled!==false};
    });
}
window.mergeSkusForSpecs = mergeSkusForSpecs;

function productMeta(packageData) {
    const source=packageData || {}, metadata=source.metadata || {};
    return {brand:source.brand ?? metadata.brand ?? '', attrs:source.attrs ?? metadata.attrs ?? {},
        description:source.description ?? metadata.description ?? '', category:source.category ?? metadata.category ?? ''};
}

function syncListingPackage() {
    const titleInput=document.getElementById('productTitle'), sourceInput=document.getElementById('migrateUrl');
    currentDraftPackage={...(currentDraftPackage || {}),schema_version:2,id:currentMigratedId || (currentDraftPackage||{}).id || '',
        title:titleInput ? titleInput.value.trim() : (currentDraftPackage||{}).title || '',
        source_url:sourceInput ? sourceInput.value.trim() : (currentDraftPackage||{}).source_url || '',
        specs:JSON.parse(JSON.stringify(scrapedSpecs||{})),spec_images:{...(specImages||{})},
        sku:JSON.parse(JSON.stringify(skuData||[])),skus:JSON.parse(JSON.stringify(skuData||[])),
        main_images:[...(localMainImages.length ? localMainImages : mainImageUrls)],
        detail_images:[...(localDetailImages.length ? localDetailImages : detailImageUrls)],
        ...productMeta(window.aiListingMeta || currentDraftPackage)};
    currentDraftPackage=cleanProductPackage(currentDraftPackage);
    localStorage.setItem('currentProductPackage',JSON.stringify(currentDraftPackage));
    if(!_syncingListingPackage && typeof window.onListingProductPackageChanged==='function') {
        _syncingListingPackage=true;
        try { window.onListingProductPackageChanged(JSON.parse(JSON.stringify(currentDraftPackage))); }
        finally { _syncingListingPackage=false; }
    }
    return currentDraftPackage;
}
window.syncListingPackage=syncListingPackage;
window.getListingProductPackage=()=>JSON.parse(JSON.stringify(syncListingPackage()));

function applyProductPackageToListing(packageData) {
    const incoming=cleanProductPackage(JSON.parse(JSON.stringify(packageData || {})));
    const sameProduct=!!(currentDraftPackage && ((incoming.job_id && incoming.job_id===currentDraftPackage.job_id) || (incoming.id && incoming.id===currentDraftPackage.id)));
    const previous=sameProduct ? skuData : [];
    currentDraftPackage=incoming;
    currentMigratedId=incoming.id || null;
    scrapedSpecs=JSON.parse(JSON.stringify(incoming.specs ?? incoming.specs_confirmed ?? {}));
    const incomingSkus=incoming.skus || incoming.sku || [];
    const preserved=Object.keys(scrapedSpecs).length && previous.length ? mergeSkusForSpecs(scrapedSpecs,previous) : previous;
    skuData=Object.keys(scrapedSpecs).length ? mergeSkusForSpecs(scrapedSpecs,incomingSkus.concat(preserved)) : (sameProduct && previous.length ? previous : incomingSkus);
    specImages={...(incoming.spec_images || {})};
    localMainImages=[...(incoming.main_images || [])];localDetailImages=[...(incoming.detail_images || [])];
    mainImageUrls=[];detailImageUrls=[];
    window.aiListingMeta=productMeta(incoming);
    const titleInput=document.getElementById('productTitle');if(titleInput)titleInput.value=incoming.title || '';
    const sourceInput=document.getElementById('migrateUrl');if(sourceInput)sourceInput.value=incoming.source_url || (incoming.job_id ? `ai://${incoming.job_id}` : '');
    for(const [key,value] of Object.entries({scrapedSpecs,specImages,skuData,localMainImages,localDetailImages,mainImageUrls,detailImageUrls})) localStorage.setItem(key,JSON.stringify(value));
    syncListingPackage();
    renderSpecInfo();renderMainImages();renderDetailImages();renderSkuTable();
    const info=document.getElementById('productInfo');if(info)info.style.display='block';
    return true;
}
window.applyProductPackageToListing=applyProductPackageToListing;

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    loadMigratedProducts();
    checkLicense();
    initAdminMode();
    startUsageClock();
    setInterval(updateTestCountdown, 1000);
    if (currentDraftPackage && currentDraftPackage.title) applyProductPackageToListing(currentDraftPackage);
    const titleInput=document.getElementById('productTitle');
    if(titleInput) titleInput.addEventListener('input',()=>syncListingPackage());
    if(readStoredJson('currentListingTask',null)) resumeListingTask();
});

// 导航切换
function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    // Normalize the initial state: older builds left more than one panel with
    // `.active`, so the first screen showed the AI flow and the legacy editor
    // together. Keep the active nav item as the single source of truth without
    // touching any form values or saved listing state.
    const initial = document.querySelector('.nav-item.active') || navItems[0];
    const initialTab = initial && initial.dataset ? initial.dataset.tab : null;
    if (initialTab) {
        document.querySelectorAll('.tab-panel').forEach(panel => {
            panel.classList.toggle('active', panel.id === `tab-${initialTab}`);
        });
    }
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            // 切换active状态
            navItems.forEach(n => n.classList.remove('active'));
            item.classList.add('active');

            // 切换tab
            const tabId = item.dataset.tab;
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.getElementById(`tab-${tabId}`).classList.add('active');
        });
    });
}

// 全局SKU数据
let skuData = readStoredJson('skuData', []);
// 兜底：给没有spec_code的SKU补上顺序编号
if (skuData.length > 0 && !skuData[0].spec_code) {
    skuData.forEach((sku, i) => { sku.spec_code = String(i + 1); });
}
// 主图/详情图也必须落盘。原来这四个只放在内存里：规格、SKU、规格预览图都在 localStorage，
// 唯独图片列表没有 —— 页面一刷新（重开工具、浏览器重启、手动 F5）主图和详情图就全空了，
// 「执行上架」发过去的 main_images/detail_images 是空数组，页面上什么都不传，
// 而且日志里一个字都不提（2026-09-15 就是这么白跑了一次上架，用户看到的是「主图详情图都没上传」）。
let detailImageUrls = readStoredJson('detailImageUrls', []);
let mainImageUrls = readStoredJson('mainImageUrls', []);
let localMainImages = readStoredJson('localMainImages', []);
let localDetailImages = readStoredJson('localDetailImages', []);
// 规格预览图也要落盘（和 skuData / scrapedSpecs 一样）：
// 抓完不刷新还好，一刷新内存里的 specImages 就没了 → 这次上架就没有规格预览图，还没任何提示。
let specImages = readStoredJson('specImages', {});  // {spec_value_name: local_path}
let scrapedSpecs = readStoredJson('scrapedSpecs', {});  // {规格类型名: [规格值列表]}
let currentMigratedId = null;  // 当前加载的已保存商品id，重存时用于更新而非新建
let licenseExpireAt = null;  // 分钟级测试码的到期时间（ISO 字符串）

// 打开内置浏览器里的淘宝 / 拼多多界面
async function openTaobao() {
    await openBrowserPage('taobao');
}

async function openPdd() {
    await openBrowserPage('pdd');
}

// 打开新店铺：拉起一个独立浏览器窗口（独立 profile）到商家后台登录页，用于登录另一家店铺
async function openPddLogin() {
    await openBrowserPage('pdd-login');
}

// 同一时刻只放一个「打开页面」的请求过去。
// 「打开新店铺」特别怕连点：后端每收到一次就判一次「这个店铺槽位的窗口关了没有」，
// 而浏览器冷启动要好几秒才绑上调试端口 —— 这几秒里连点，每次都判成「关了」，
// 于是连着拉好几遍，同一个 profile 的第二遍不会开新窗口，只会往那个窗口里再塞一个
// 一模一样的登录页（2026-09-27 那个店铺窗口里堆了 5 个同款标签页）。
// 后端也加了锁和等端口，这里再挡一道：请求在飞的时候按钮点不动。
let _openingPage = false;

async function openBrowserPage(target) {
    if (_openingPage) {
        setStatus('上一个打开请求还在处理中，等它一下…');
        return;
    }
    _openingPage = true;
    document.querySelectorAll('button[onclick^="open"]')
        .forEach(b => { b.disabled = true; b.style.opacity = '0.6'; });
    try {
        const response = await fetch('/api/open-page', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target })
        });
        const data = await response.json();
        if (!response.ok) {
            alert(data.detail || '打开失败');
        } else if (data.message) {
            setStatus(data.message);
        }
    } catch (error) {
        alert('打开失败：' + (error.message || '内置浏览器未启动'));
    } finally {
        _openingPage = false;
        document.querySelectorAll('button[onclick^="open"]')
            .forEach(b => { b.disabled = false; b.style.opacity = ''; });
    }
}

// 清空搬家表单残留数据
function resetMigrateForm() {
    currentMigratedId = null;
    currentDraftPackage = null;
    window.aiListingMeta = null;
    localStorage.removeItem('currentProductPackage');
    mainImageUrls = [];
    detailImageUrls = [];
    localMainImages = [];
    localDetailImages = [];
    specImages = {};
    localStorage.removeItem('specImages');
    localStorage.removeItem('localMainImages');
    localStorage.removeItem('mainImageUrls');
    localStorage.removeItem('localDetailImages');
    localStorage.removeItem('detailImageUrls');
    skuData = [];
    scrapedSpecs = {};
    localStorage.removeItem('skuData');
    localStorage.removeItem('scrapedSpecs');
    document.getElementById('mainImagesPreview').innerHTML = '';
    document.getElementById('detailImagesPreview').innerHTML = '';
    document.getElementById('specInfo').innerHTML = '';
    document.getElementById('productTitle').value = '';
    const migrateUrlInput = document.getElementById('migrateUrl');
    if (migrateUrlInput) migrateUrlInput.value = '';
    document.getElementById('skuTableBody').innerHTML = '';
    document.getElementById('mainImagesSection').style.display = 'none';
    document.getElementById('detailImagesSection').style.display = 'none';
    document.getElementById('skuSection').style.display = 'none';
    document.getElementById('executeSection').style.display = 'none';
}

// 把外部文本放进 innerHTML 之前必须转义：商品标题是从淘宝页面抓来的，卖家想写什么写什么，
// `<img src=x onerror="...">` 这种会在这台机器上真执行（同源页面，能调本机所有接口）
function escHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 轮询器管理：同一类任务（抓取 / 上架）同一时间只允许一个轮询器。
// 以前每次点都新建一个 setInterval，句柄只存在函数局部 —— 连着点两次「抓取」就是两个
// 轮询同时在跑，旧任务后回来的 done 会把旧商品的标题/图/SKU 写回新表单，把用户刚填的价格清掉。
const _pollCtl = {};

function _beginPoll(key) {
    const prev = _pollCtl[key];
    if (prev) clearInterval(prev.id);          // 掐掉上一次的轮询
    const ctl = { id: null, gen: prev ? prev.gen + 1 : 1 };
    // fresh()：我还代表「最新的那次」吗（旧的轮询就算请求已经发出去了，回来也不许再写界面）
    ctl.fresh = () => _pollCtl[key] === ctl;
    ctl.stop = () => { clearInterval(ctl.id); };
    _pollCtl[key] = ctl;
    return ctl;
}

// 抓取淘宝商品信息
async function scrapeTaobao() {
    // 权利确认：不勾就不让抓（2026-09-28 加）。
    // 这一勾是使用者对「素材我有权用」的书面确认 —— 出事时它是「操作者是商家、
    // 且商家自述有权使用」的凭证。**每次都要勾**，不记住上次的状态：
    // 记住状态等于让一次勾选覆盖后面所有商品，那这一勾就没有意义了。
    const rights = document.getElementById('rightsConfirm');
    const rightsHint = document.getElementById('rightsHint');
    if (rights && !rights.checked) {
        if (rightsHint) rightsHint.style.display = 'block';
        rights.focus();
        return;
    }
    if (rightsHint) rightsHint.style.display = 'none';
    // 勾选当场作废：下一个商品要重新勾一次（见上面的说明，不记住状态）
    if (rights) rights.checked = false;

    const url = document.getElementById('migrateUrl').value;
    if (!url) {
        alert('请输入淘宝商品链接');
        return;
    }

    // 清除上一个商品的残留数据
    resetMigrateForm();
    document.getElementById('migrateUrl').value=url;

    const progressDiv = document.getElementById('scrapeProgress');
    const progressContent = document.getElementById('scrapeProgressContent');
    progressDiv.style.display = 'block';
    progressContent.innerHTML = '<p>⏳ 正在抓取商品信息...</p>';
    setStatus('抓取中...');

    try {
        const response = await fetch('/api/scrape-taobao', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await response.json();
        if (!response.ok) {
            const reasonText = LICENSE_REASON_TEXT[data.detail] || data.detail || '请求失败';
            progressContent.innerHTML = `<p>❌ ${reasonText}</p>`;
            setStatus('抓取失败');
            return;
        }
        const taskId = data.task_id;

        // 轮询抓取状态
        const ctl = _beginPoll('scrape');
        let pollCount = 0;
        let failCount = 0;
        const poll = setInterval(async () => {
            pollCount++;
            if (!ctl.fresh()) { ctl.stop(); return; }   // 这是上一次抓取的轮询，丢掉
            try {
                const statusRes = await fetch(`/api/one-click/${taskId}`);
                if (!ctl.fresh()) { ctl.stop(); return; }
                if (!statusRes.ok) {
                    // 以前这里不看状态码：404（工具重启过、任务没了）和网络失败都会被当成
                    // 「还在跑」，界面永远停在「已等待 N 秒」，用户以为还在抓
                    failCount++;
                    if (statusRes.status === 404 || failCount >= 5) {
                        ctl.stop();
                        progressContent.innerHTML = `<p>❌ ${statusRes.status === 404
                            ? '抓取任务已经没了（工具重启过），请重新点「抓取商品信息」'
                            : '连不上工具（连续失败 5 次），已停止等待'}</p>`;
                        setStatus('抓取失败');
                        return;
                    }
                    progressContent.innerHTML = `<p>⏳ 暂时取不到进度（第 ${failCount} 次），重试中...</p>`;
                    return;
                }
                const status = await statusRes.json();
                progressContent.innerHTML = `<p>⏳ ${status.progress || '处理中...'} (已等待${pollCount * 2}秒)</p>`;

                if (status.status === 'done') {
                    ctl.stop();
                    progressContent.innerHTML = '<p>✅ 抓取完成</p>';

                    // 显示商品信息
                    document.getElementById('productTitle').value = status.title || '';
                    document.getElementById('productInfo').style.display = 'block';

                    // 显示规格信息（名称可修改，下载表格同步）
                    const specs = status.specs || {};
                    scrapedSpecs = specs;  // 保存规格类型→规格值映射，用于后续生成Excel
                    localStorage.setItem('scrapedSpecs', JSON.stringify(scrapedSpecs));
                    renderSpecInfo();

                    // 生成SKU矩阵
                    generateSkuMatrix(specs, status.title || '');

                    // 显示主图（带删除按钮）
                    mainImageUrls = status.main_image_urls || [];
                    localMainImages = status.main_images || [];
                    renderMainImages();

                    // 显示详情图（带删除按钮）
                    detailImageUrls = status.detail_image_urls || [];
                    localDetailImages = status.detail_images || [];
                    renderDetailImages();

                    // 存储规格预览图（必须在generateSkuMatrix之前赋值）
                    specImages = status.spec_images || {};
                    localStorage.setItem('specImages', JSON.stringify(specImages));
                    console.log('规格预览图:', Object.keys(specImages).length, '个');
                    // 重新渲染SKU表格以显示预览图
                    if (skuData.length > 0) renderSkuTable();
                    syncListingPackage();

                    setStatus('抓取完成');
                } else if (status.status === 'error') {
                    ctl.stop();
                    progressContent.innerHTML = `<p>❌ 失败: ${escHtml(status.message) || '未知错误'}</p>`;
                    setStatus('抓取失败');
                } else if (pollCount >= 300) {
                    // 10 分钟还没终态：不再无限转圈（以前会一直转，定时器也一直挂着）
                    ctl.stop();
                    progressContent.innerHTML = '<p>❌ 等了 10 分钟还是没结果，已停止等待（可以刷新页面重新抓一次）</p>';
                    setStatus('抓取超时');
                }
            } catch (e) {
                // 取不到就下一轮再试（连不上时会走 failCount 那条路，不会无限转）
                console.error('抓取进度查询失败:', e);
            }
        }, 2000);
        ctl.id = poll;

    } catch (error) {
        progressContent.innerHTML = `<p>❌ 请求失败: ${escHtml(error.message)}</p>`;
        setStatus('抓取失败');
        console.error(error);
    }
}

// 渲染可编辑的规格类型名（商品搬家页，名称可改，下载表格同步新名）
function renderSpecInfo() {
    const specInfo = document.getElementById('specInfo');
    const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    const specNames = Object.keys(scrapedSpecs || {});
    if (specNames.length === 0) {
        specInfo.innerHTML = '<em>未检测到规格选项，将使用默认规格</em>';
        return;
    }
    const rows = specNames.map((n, i) => {
        const values = (scrapedSpecs[n] || []).join('、');
        return `<div class="spec-info-row">` +
            `<input type="text" class="spec-name-input" value="${esc(n)}" data-index="${i}" placeholder="规格名称" onchange="renameSpec(${i}, this.value)">` +
            `<span class="spec-info-values">：${esc(values)}</span>` +
            `</div>`;
    }).join('');
    specInfo.innerHTML = '<strong>检测到的规格（名称可修改，下载表格会同步）：</strong>' + rows;
}

// 修改规格类型名（key），同步到 scrapedSpecs 与 localStorage
function renameSpec(index, newName) {
    const names = Object.keys(scrapedSpecs || {});
    const oldName = names[index];
    newName = (newName || '').trim();
    if (!oldName || !newName || newName === oldName) {
        // 名称没变或为空，还原显示
        renderSpecInfo();
        return;
    }
    if(names.includes(newName)){alert('规格名称已存在');renderSpecInfo();return;}
    const rows=mergeSkusForSpecs(scrapedSpecs,skuData);
    // 重建对象保持顺序，仅替换对应 key
    const newSpecs = {};
    names.forEach((n, i) => { newSpecs[i === index ? newName : n] = scrapedSpecs[n]; });
    scrapedSpecs = newSpecs;
    skuData=rows.map(row=>{
        const values=Object.fromEntries(Object.entries(row.spec_values).map(([key,value])=>[key===oldName?newName:key,value]));
        const id=productSkuId(values);return {...row,spec_values:values,id,sku_id:id};
    });
    localStorage.setItem('scrapedSpecs', JSON.stringify(scrapedSpecs));
    localStorage.setItem('skuData',JSON.stringify(skuData));syncListingPackage();renderSkuTable();
}

// 生成SKU矩阵
function generateSkuMatrix(specs, title) {
    try { skuData = mergeSkusForSpecs(specs, skuData); }
    catch(error) { alert(error.message); return; }
    scrapedSpecs=JSON.parse(JSON.stringify(specs || {}));
    localStorage.setItem('skuData', JSON.stringify(skuData));
    localStorage.setItem('scrapedSpecs',JSON.stringify(scrapedSpecs));
    syncListingPackage();
    renderSkuTable();
}

// 渲染SKU表格
function renderSkuTable() {
    const tbody = document.getElementById('skuTableBody');
    tbody.innerHTML = skuData.map((sku, i) => {
        // 查找规格对应的预览图（优先用SKU上记录的 spec_img，其次按颜色前缀匹配）
        let specImg = sku.spec_img || specImages[sku.name] || '';
        if (!specImg) {
            const values=sku.spec_values || {};
            const dimension=Object.keys(values).find(name=>/色|款式|花色|图案|配色|color|style/i.test(name));
            if(dimension) specImg=specImages[String(values[dimension])] || '';
        }
        const imgHtml = specImg ?
            `<span class="spec-img-wrap">
                <img src="${escHtml(toImgUrl(specImg))}" class="spec-preview-img" onerror="this.style.display='none'" onclick="openLightbox(this.src)">
                <button class="img-download-btn" data-image="${escHtml(specImg)}" onclick="downloadSingleImage(this.dataset.image)" title="下载预览图">⬇</button>
            </span>` :
            '<span class="no-spec-img">无图片</span>';
        return `
        <tr>
            <td><input type="checkbox" class="sku-check" data-index="${i}" ${sku.enabled ? 'checked' : ''} onchange="updateSku(${i}, 'enabled', this.checked)"></td>
            <td><input type="text" value="${escHtml(sku.name)}" style="width:140px" onchange="updateSkuName(${i}, this.value)"></td>
            <td class="spec-img-cell">${imgHtml}</td>
            <td><input type="number" value="${escHtml(sku.price)}" min="0.01" step="0.01" onchange="updateSku(${i}, 'price', this.value)"></td>
            <td><input type="number" value="${escHtml(sku.single_price)}" min="0.01" step="0.01" onchange="updateSku(${i}, 'single_price', this.value)"></td>
            <td><input type="number" value="${escHtml(sku.stock)}" min="0" step="1" onchange="updateSku(${i}, 'stock', this.value)"></td>
            <td><input type="text" value="${escHtml(sku.spec_code || '')}" style="width:60px" onchange="updateSku(${i}, 'spec_code', this.value)"></td>
        </tr>`;
    }).join('');

    // 显示汇总
    document.getElementById('skuSummary').textContent = `共 ${skuData.length} 个规格`;
    document.getElementById('skuSection').style.display = 'block';
    document.getElementById('executeSection').style.display = 'block';
    refreshShopHint();
}

// 更新单个SKU
function updateSku(index, field, value) {
    if (field === 'enabled' || field === 'spec_code') {
        skuData[index][field] = value;
    } else {
        skuData[index][field] = String(value).trim() === '' ? '' : Number(value);
    }
    localStorage.setItem('skuData', JSON.stringify(skuData));
    syncListingPackage();
}

// 修改SKU规格名称（同步到 scrapedSpecs，下载表格自动带新名）
function updateSkuName(index, newName) {
    newName = (newName || '').trim();
    if (!newName) { renderSkuTable(); return; }
    const oldName = skuData[index].name;
    if (newName === oldName) return;
    const specNames = Object.keys(scrapedSpecs || {});
    // A combined display name cannot be split safely: a colour itself may
    // contain spaces. Edit multi-dimensional values in the specification bar.
    if(specNames.length>1){alert('请在规格选择栏修改颜色或尺码，组合名称不能按空格拆分');renderSkuTable();return;}
    if(skuData.some((sku,i)=>i!==index && sku.name===newName)){alert('规格名称已存在');renderSkuTable();return;}
    skuData[index].name = newName;
    if (specNames.length === 1) {
        const vals = scrapedSpecs[specNames[0]] || [];
        const vi = vals.indexOf((skuData[index].spec_values || {})[specNames[0]] || oldName);
        if (vi >= 0) vals[vi] = newName;
        skuData[index].spec_values={[specNames[0]]:newName};
        skuData[index].id=productSkuId(skuData[index].spec_values);
        skuData[index].sku_id=skuData[index].id;
        if(specImages[oldName]){specImages[newName]=specImages[oldName];delete specImages[oldName];}
    }
    localStorage.setItem('skuData', JSON.stringify(skuData));
    localStorage.setItem('scrapedSpecs', JSON.stringify(scrapedSpecs));
    localStorage.setItem('specImages',JSON.stringify(specImages));
    syncListingPackage();
    renderSpecInfo();renderSkuTable();
}

// 批量设置价格
function batchSetSkuPrices() {
    const price = Number(document.getElementById('batchPrice').value);
    const singlePrice = Number(document.getElementById('batchSinglePrice').value);
    const stockRaw = document.getElementById('batchStock').value.trim();
    const stock = stockRaw === '' ? NaN : Number(stockRaw);
    if(!Number.isFinite(price)||price<=0||!Number.isFinite(singlePrice)||singlePrice<price||!Number.isInteger(stock)||stock<0){
        alert('请填写有效价格（单买价不低于拼单价）和大于或等于0的整数库存');return;
    }

    skuData.forEach(sku => {
        sku.price = price;
        sku.single_price = singlePrice;
        sku.stock = stock;
    });

    localStorage.setItem('skuData', JSON.stringify(skuData));
    syncListingPackage();
    renderSkuTable();
    setStatus(`已应用：拼单价 ${price}，单买价 ${singlePrice}，库存 ${stock}`);
}

// 批量设置规格编码（顺序填充）
function batchSetSpecCode() {
    const start = parseInt(document.getElementById('batchSpecCodeStart').value) || 1;
    skuData.forEach((sku, i) => {
        sku.spec_code = String(start + i);
    });
    localStorage.setItem('skuData', JSON.stringify(skuData));
    syncListingPackage();
    renderSkuTable();
    alert(`已按顺序填充 ${skuData.length} 个规格编码（${start} ~ ${start + skuData.length - 1}）`);
}

// 添加新尺码（为每个颜色各生成一行，自动复用颜色预览图）
function addSize() {
    const input = document.getElementById('newSizeValue');
    const size = (input.value || '').trim();
    if (!size) { alert('请先输入要添加的尺码值'); return; }

    const sizeDim=Object.keys(scrapedSpecs||{}).find(name=>/尺码|尺寸|规格|size/i.test(name)) || '尺码';
    const nextSpecs=JSON.parse(JSON.stringify(scrapedSpecs||{}));
    if((nextSpecs[sizeDim]||[]).includes(size)){alert('这个尺码已经存在，未重复添加');return;}
    nextSpecs[sizeDim]=[...(nextSpecs[sizeDim]||[]),size];
    try {
        const merged=mergeSkusForSpecs(nextSpecs,skuData);
        scrapedSpecs=nextSpecs;skuData=merged;
    } catch(error){alert(error.message);return;}
    localStorage.setItem('skuData',JSON.stringify(skuData));
    localStorage.setItem('scrapedSpecs',JSON.stringify(scrapedSpecs));
    syncListingPackage();input.value='';renderSpecInfo();renderSkuTable();
}

// 全选/取消全选
function toggleSelectAll() {
    const checked = document.getElementById('selectAll').checked;
    document.querySelectorAll('.sku-check').forEach(cb => {
        cb.checked = checked;
        const i = parseInt(cb.dataset.index);
        skuData[i].enabled = checked;
    });
    localStorage.setItem('skuData', JSON.stringify(skuData));
    syncListingPackage();
}

// 图片放大预览
function openLightbox(src) {
    document.getElementById('lightboxImg').src = src;
    document.getElementById('lightbox').style.display = 'flex';
}

function closeLightbox() {
    document.getElementById('lightbox').style.display = 'none';
    document.getElementById('lightboxImg').src = '';
}

// 本地路径/相对路径 → 可直接展示的 URL（data/ 目录已挂载为 /data）
function toImgUrl(path) {
    if (!path) return '';
    let s = String(path).replace(/\\/g, '/');
    const idx = s.indexOf('/data/');
    if (idx !== -1) return s.slice(idx);
    if (s.startsWith('data/')) return '/' + s;
    if (s.startsWith('http')) return s;
    return '/data/' + s.split('/').pop();
}

// 单张图片下载（主图/详情图/规格预览图通用）
async function downloadSingleImage(path) {
    if (!path) return;
    try { path = decodeURIComponent(path); } catch (e) {}
    try {
        const res = await fetch('/api/download-single-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(data.detail || '下载失败');
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = String(path).replace(/\\/g, '/').split('?')[0].split('/').pop() || 'image.jpg';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('下载失败：' + (e.message || ''));
    }
}

// 渲染主图（带删除按钮）
function renderMainImages() {
    localStorage.setItem('localMainImages', JSON.stringify(localMainImages));
    localStorage.setItem('mainImageUrls', JSON.stringify(mainImageUrls));
    const div = document.getElementById('mainImagesPreview');
    const images = localMainImages.length > 0 ? localMainImages : mainImageUrls;
    if (images.length === 0) {
        div.innerHTML = '<em>未找到主图</em>';
        document.getElementById('mainImagesSection').style.display = 'none';
        return;
    }
    div.innerHTML = images.map((p, i) => {
        const src = localMainImages.length > 0 ? toImgUrl(p) : p;
        return `<div class="img-item">
            <img src="${escHtml(src)}" onerror="this.style.display='none'" onclick="openLightbox(this.src)">
            <button class="img-download-btn" data-image="${escHtml(p)}" onclick="downloadSingleImage(this.dataset.image)" title="下载单张">⬇</button>
            <button class="img-del-btn" onclick="deleteMainImage(${i})" title="删除">✕</button>
        </div>`;
    }).join('');
    document.getElementById('mainImagesSection').style.display = 'block';
}

// 渲染详情图（带删除按钮）
function renderDetailImages() {
    localStorage.setItem('localDetailImages', JSON.stringify(localDetailImages));
    localStorage.setItem('detailImageUrls', JSON.stringify(detailImageUrls));
    const div = document.getElementById('detailImagesPreview');
    const images = localDetailImages.length > 0 ? localDetailImages : detailImageUrls;
    if (images.length === 0) {
        div.innerHTML = '<em>未找到详情图</em>';
        document.getElementById('detailImagesSection').style.display = 'none';
        return;
    }
    div.innerHTML = images.map((p, i) => {
        const src = localDetailImages.length > 0 ? toImgUrl(p) : p;
        return `<div class="img-item" draggable="true"
                ondragstart="dragDetailImage(event, ${i})"
                ondragover="event.preventDefault()"
                ondrop="dropDetailImage(event, ${i})">
            <span class="img-order">${i + 1}</span>
            <img src="${escHtml(src)}" onerror="this.style.display='none'" onclick="openLightbox(this.src)">
            <button class="img-download-btn" data-image="${escHtml(p)}" onclick="downloadSingleImage(this.dataset.image)" title="下载单张">⬇</button>
            <button class="img-del-btn" onclick="deleteDetailImage(${i})" title="删除">✕</button>
        </div>`;
    }).join('');
    document.getElementById('detailImagesSection').style.display = 'block';
}

// 删除主图
function deleteMainImage(index) {
    if (localMainImages.length > 0) {
        localMainImages.splice(index, 1);
    } else {
        mainImageUrls.splice(index, 1);
    }
    renderMainImages();
    syncListingPackage();
}

// 删除详情图
function deleteDetailImage(index) {
    if (localDetailImages.length > 0) {
        localDetailImages.splice(index, 1);
    } else {
        detailImageUrls.splice(index, 1);
    }
    renderDetailImages();
    syncListingPackage();
}

// 详情图拖拽排序
let dragDetailIndex = null;

function dragDetailImage(event, index) {
    dragDetailIndex = index;
    event.dataTransfer.effectAllowed = 'move';
    try { event.dataTransfer.setData('text/plain', String(index)); } catch (e) {}
}

function dropDetailImage(event, index) {
    event.preventDefault();
    if (dragDetailIndex === null || dragDetailIndex === index) {
        dragDetailIndex = null;
        return;
    }
    const arr = localDetailImages.length > 0 ? localDetailImages : detailImageUrls;
    const [moved] = arr.splice(dragDetailIndex, 1);
    arr.splice(index, 0, moved);
    dragDetailIndex = null;
    renderDetailImages();
    syncListingPackage();
}

// 下载全部抓取到的图片（主图 + 详情图打包成 zip）
async function downloadAllImages() {
    const images = [...localMainImages, ...localDetailImages];
    if (images.length === 0) {
        alert('还没有抓取到图片');
        return;
    }
    try {
        const res = await fetch('/api/download-images', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ images })
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(data.detail || '下载失败');
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = '商品图片.zip';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('下载失败：' + (e.message || ''));
    }
}

// 下载SKU Excel表格
async function downloadSkuExcel() {
    console.log('downloadSkuExcel called, scrapedSpecs:', scrapedSpecs, 'skuData:', skuData.length);
    if (skuData.length === 0) {
        alert('没有SKU数据可下载');
        return;
    }
    if (Object.keys(scrapedSpecs).length === 0) {
        alert('没有规格数据，请先抓取商品');
        return;
    }

    try {
        const response = await fetch('/api/generate-sku-excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ specs: scrapedSpecs, skus: skuData })
        });
        const result = await response.json();

        if (result.success) {
            // 触发下载
            const a = document.createElement('a');
            a.href = result.url;
            a.download = result.filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            alert('Excel已生成，请在浏览器下载栏查看');
        }
    } catch (error) {
        alert('生成失败: ' + error.message);
        console.error(error);
    }
}

// 从拼多多下载表格
async function downloadPddExcel() {
    try {
        setStatus('从拼多多下载表格...');
        const response = await fetch('/api/download-pdd-excel', {
            method: 'POST'
        });
        const result = await response.json();

        if (result.success) {
            // 触发浏览器下载
            const a = document.createElement('a');
            a.href = result.url;
            a.download = result.filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);

            // 同时解析并填充到表格
            const uploadResponse = await fetch(result.url);
            const blob = await uploadResponse.blob();
            const formData = new FormData();
            formData.append('file', blob, result.filename);
            const parseRes = await fetch('/api/upload-sku-excel', {
                method: 'POST',
                body: formData
            });
            const parseResult = await parseRes.json();
            if (parseResult.success) {
                skuData = parseResult.skus;
                // 必须落盘：skuData 是页面状态的唯一真源，只放在内存里的话一刷新就回退成
                // 上一次存过的价格/库存，用户会看到「刚填好的价格自己变回去了」
                localStorage.setItem('skuData', JSON.stringify(skuData));
                syncListingPackage();
                renderSkuTable();
            }

            alert('表格已下载，请在浏览器下载栏查看');
            setStatus('下载完成');
        } else {
            // 以前这里没有 else：接口报错（比如页面没开）时什么都不发生，
            // 状态栏永远停在「从拼多多下载表格...」，看起来就像点了没反应
            alert('下载表格失败：' + (result.detail || result.message || '拼多多页面没有响应'));
            setStatus('下载表格失败');
        }
    } catch (error) {
        alert('下载失败: ' + error.message);
        setStatus('下载失败');
        console.error(error);
    }
}

// 上传填好的Excel
async function uploadSkuExcel(event) {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);

    try {
        setStatus('解析Excel中...');
        const response = await fetch('/api/upload-sku-excel', {
            method: 'POST',
            body: formData
        });
        const result = await response.json();

        if (result.success) {
            // 用上传的数据替换当前SKU
            skuData = result.skus;
            localStorage.setItem('skuData', JSON.stringify(skuData));   // 同上：不落盘刷新就回退
            syncListingPackage();
            renderSkuTable();
            alert(`已导入 ${result.count} 个规格`);
            setStatus('导入完成');
        } else {
            alert('导入失败：' + (result.detail || result.message || '表格格式不对'));
            setStatus('导入失败');
        }
    } catch (error) {
        alert('上传失败: ' + error.message);
        setStatus('上传失败');
        console.error(error);
    }

    // 清空input以便重复上传
    event.target.value = '';
}

// ==================== 多店铺上架 ====================

let _shopNames = {};          // 端口 → 店名（弹窗里拿到的，进度区显示用）
let _shopIdentities = {};
let _pickedShops = null;

function _shopLabel(port) {
    return _shopNames[port] || `端口 ${port}`;
}

// 这家店的窗口到底开没开。
//
// 后端会给 alive 字段。但**不能**直接写 `!s.alive`：接口要是没给这个字段，
// undefined 取反是 true，于是每一家都被判成「窗口没开」—— 勾选框整列变灰、
// 一个都勾不动（2026-09-28 用户就是这么被卡住的）。所以这里带一层兜底：
// 没有 alive（老接口）就退回按 reason 判。
function _shopAlive(s) {
    if (typeof s.alive === 'boolean') return s.alive;
    return s.reason !== '窗口没开';
}

// 打开店铺勾选弹窗。shops 是 /api/pdd-shops 的原样返回。
function openShopPicker(shops) {
    const list = document.getElementById('shopPickerList');
    const hint = document.getElementById('shopPickerHint');
    const readyN = shops.filter(s => s.ready).length;
    hint.textContent = `请核对目标店铺。仅在填表回读验证通过后结算额度。`
                     + `当前 ${readyN} 家已停在发布商品页。`;

    list.innerHTML = shops.map(s => {
        const name = escHtml(s.name || s.label || _shopLabel(s.port));
        const account = s.account ? ` <span class="hint">（${escHtml(s.account)}）</span>` : '';
        // 窗口没开的店：勾了也一定失败（连接都不通，而且后端**不会**回退到别的店），
        // 所以这里直接禁用并告诉用户去哪儿把它开起来，别让他白勾。
        const known=!!(String(s.name||'').trim() || s.mallId);
        const dead = !_shopAlive(s) || !Number.isInteger(s.port) || s.port<1024 || s.port>65535 || s.identity_verified===false || !known;
        const state = escHtml(s.identity_verified===false || !known ? '店铺身份未确认，请先登录并重新检测' : readyStateText(s));
        const cats = s.cats || [];
        // 分类下拉：只在这家店正停在「分类选择」页、而且真读到了最近分类时才给。
        // **必须放在 <label> 外面** —— 放在里面点一下下拉会连带把勾选框给切了。
        const catSelect = cats.length ? `
            <div style="margin:6px 0 0 26px;">
                <span class="hint">用哪个分类：</span>
                <select class="shop-cat" data-port="${escHtml(s.port)}">
                    ${cats.map((c, i) => `<option value="${i}">${escHtml(c)}</option>`).join('')}
                </select>
            </div>` : '';
        return `
        <div style="padding:10px 12px;border:1px solid ${dead ? '#f0f0f0' : '#e5e7eb'};
                    border-radius:8px;margin-bottom:8px;${dead ? 'opacity:.55;' : ''}">
            <label style="display:flex;gap:10px;align-items:flex-start;
                          cursor:${dead ? 'not-allowed' : 'pointer'};">
                <input type="checkbox" class="shop-pick" value="${escHtml(s.port)}"
                       ${dead ? 'disabled' : (s.ready ? 'checked' : '')} style="margin-top:3px;">
                <span>
                    <b>${name}</b>${account}<br>
                    <span class="hint">连接 ${escHtml(s.port)} · ${state}</span>
                </span>
            </label>
            ${catSelect}
        </div>`;
    }).join('');

    _shopNames = {};
    _shopIdentities = {};
    shops.forEach(s => { _shopNames[s.port] = s.name || s.label; });
    shops.forEach(s => { _shopIdentities[s.port]={name:s.name||'',id:s.mallId??s.id??null,mallId:s.mallId??null,account:s.account||''}; });
    document.getElementById('shopPickerModal').style.display = 'block';
}

function readyStateText(s) {
    if (!_shopAlive(s)) return '窗口没开 —— 请先点「打开新店铺窗口」把它开起来并登录';
    if (s.ready) return '已就绪，正在发布商品页';
    // 「停在分类页」和「没停在发布页」要分开说：前者工具会替你选分类（没得选就停下来
    // 等你手选），后者会另开一个发布页。说错方向的话用户会白等 / 白去开页。
    if (s.on_cat) {
        return (s.cats || []).length
            ? `停在「选择分类」页 —— 会用下面选的分类自动发布`
            : '停在「选择分类」页，但这家的「最近使用的分类」是空的 —— 上架时会停下来等你手选';
    }
    return '没停在发布商品页 —— 会在这个店自己的窗口里自动打开一个';
}

function closeShopPicker() {
    document.getElementById('shopPickerModal').style.display = 'none';
}

function confirmShopPicker() {
    const picked = Array.from(document.querySelectorAll('.shop-pick:checked'))
                        .map(el => parseInt(el.value, 10))
                        .filter(p => !isNaN(p));
    if (picked.length === 0) {
        alert('至少要勾选一家店铺');
        return;
    }
    // 下拉里选的分类下标，按端口收。没下拉（没停在分类页）的店不收 —— 用不上，
    // 后端拿不到就是 0（第一个），跟以前完全一样。
    const shopCats = {};
    document.querySelectorAll('.shop-cat').forEach(el => {
        const p = parseInt(el.getAttribute('data-port'), 10);
        const i = parseInt(el.value, 10);
        if (!isNaN(p) && !isNaN(i)) shopCats[p] = i;
    });
    closeShopPicker();
    _pickedShops = picked;
    _doStartListing(picked, shopCats);
}

function listingValidationErrors(body, requireShops=true) {
    const errors=[];
    const add=(field,message)=>errors.push({field,message});
    if(!String(body.title||'').trim()) add('title','请填写商品标题');
    else if(Array.from(body.title).length>60) add('title','商品标题最多60个字符');
    if(!Array.isArray(body.main_images)||!body.main_images.length) add('main_images','至少选择一张主图');
    else if(body.main_images.length>10) add('main_images','上架主图最多10张');
    if((body.detail_images||[]).length>20) add('detail_images','上架详情图最多20张');
    for(const field of ['main_images','detail_images']) (body[field]||[]).forEach((value,index)=>{if(typeof value!=='string'||!value.trim())add(`${field}.${index}`,'存在无效图片');});
    const rows=(body.skus||body.sku||[]).filter(row=>row && row.enabled!==false);
    if(!rows.length)add('skus','至少启用一个SKU');
    const names=new Set(),codes=new Set();
    rows.forEach((row,index)=>{
        const field=`skus.${index}`;
        if(!String(row.name||'').trim()||names.has(row.name))add(`${field}.name`,'SKU名称不能为空或重复');names.add(row.name);
        for(const key of ['price','single_price']){
            const val=row[key],num=Number(val);
            if(val===null||val===undefined||val===''||typeof val==='boolean'||!Number.isFinite(num)||num<=0||Math.abs(Math.round(num*100)-num*100)>0.000001)add(`${field}.${key}`,`${row.name||'SKU'}的价格必须大于0且最多两位小数`);
        }
        if(Number(row.single_price)<Number(row.price))add(`${field}.single_price`,`${row.name||'SKU'}的单买价不能低于拼单价`);
        if(row.stock===null||row.stock===undefined||row.stock===''||typeof row.stock==='boolean'||!Number.isInteger(Number(row.stock))||Number(row.stock)<0)add(`${field}.stock`,`${row.name||'SKU'}的库存必须是大于或等于0的整数`);
        const code=String(row.spec_code||'').trim();if(code&&codes.has(code))add(`${field}.spec_code`,'规格编码重复');if(code)codes.add(code);
    });
    if(requireShops&&(!Array.isArray(body.shops)||!body.shops.length||body.shops.some(port=>!Number.isInteger(port)||port<1024||port>65535)))add('shops','请明确选择目标店铺');
    return errors;
}
window.listingValidationErrors=listingValidationErrors;

function listingErrorText(value) {
    if(typeof value==='string')return value;
    if(Array.isArray(value))return value.map(item=>item.message||item.msg||String(item)).join('；');
    return value && (value.message||listingErrorText(value.errors||value.detail)) || '请求失败';
}

let _listingBusy=false;
let _listingStarting=false;

// 执行一键上架：检测失败或未指定店铺时保持待处理。
async function startListing() {
    if(_listingBusy||_listingStarting){setStatus('已有填表任务正在处理');return;}
    if(readStoredJson('currentListingTask',null)){resumeListingTask();return;}
    const errors=listingValidationErrors(syncListingPackage(),false);
    if(errors.length){alert(errors.map(item=>item.message).join('\n'));return;}
    _listingStarting=true;
    try {
        const shopRes = await fetch('/api/pdd-shops');
        if(!shopRes.ok)throw new Error('无法检测店铺，请确认本地助手和店铺窗口已开启');
        const shops=(await shopRes.json()).shops||[];
        if(!shops.some(shop=>_shopAlive(shop)&&Number.isInteger(shop.port)&&shop.identity_verified!==false&&(shop.name||shop.mallId)))throw new Error('没有可确认的目标店铺，请先打开店铺窗口并登录');
        _pickedShops=null;openShopPicker(shops);
    } catch (e) {
        setStatus('店铺待确认');alert(e.message||'店铺检测失败，请重新检测');
    } finally {
        _listingStarting=false;
    }
}

// 始终提交明确的店铺连接，浏览器填表与商家最终发布分别记录。
async function _doStartListing(shops, shopCats) {
    if(_listingBusy){setStatus('已有填表任务正在处理');return;}
    const draft=syncListingPackage();
    const body={...draft,url:draft.source_url,taobao_url:draft.source_url,skus:draft.sku,shops:[...new Set(shops||[])],shop_cats:shopCats||{},shop_identities:Object.fromEntries((shops||[]).map(port=>[port,_shopIdentities[port]||{}]))};
    const submissionBody=JSON.stringify(body);
    let pendingSubmission;
    try{pendingSubmission=JSON.parse(sessionStorage.getItem('pendingListingSubmission')||'null');}catch(_){pendingSubmission=null;}
    if(!pendingSubmission||pendingSubmission.body!==submissionBody){pendingSubmission={body:submissionBody,id:typeof crypto!=='undefined'&&typeof crypto.randomUUID==='function'?crypto.randomUUID():Date.now()+'-'+Math.random()};try{sessionStorage.setItem('pendingListingSubmission',JSON.stringify(pendingSubmission));}catch(_){}}
    body.client_request_id=pendingSubmission.id;
    const errors=listingValidationErrors(body);
    if(errors.length){alert(errors.map(item=>item.message).join('\n'));return;}
    _listingBusy=true;
    const title=body.title;
    const selectedSkus=body.skus.filter(sku=>sku.enabled!==false);

    const progressDiv = document.getElementById('listingProgress');
    const progressContent = document.getElementById('listingProgressContent');
    const resultDiv = document.getElementById('listingResult');
    const resultContent = document.getElementById('listingResultContent');

    progressDiv.style.display = 'block';
    resultDiv.style.display = 'none';
    progressContent.innerHTML = '<p>⏳ 正在提交任务...</p>';
    setStatus('执行中...');

    try {
        const preflight=await fetch('/api/listing/preflight',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const checked=await preflight.json();
        if(!preflight.ok || !checked.ok)throw new Error(listingErrorText(checked.errors||checked.detail||checked));
        const response = await fetch('/api/start-listing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(listingErrorText(data.detail||data));
        }
        const taskId = data.task_id;
        try{sessionStorage.removeItem('pendingListingSubmission');}catch(_){}

        localStorage.setItem('currentListingTask',JSON.stringify({id:taskId,title,sku_count:selectedSkus.length}));
        // 同一个任务仅保留一个轮询器，连续失败时保留任务编号供恢复。
        const ctl=_beginPoll('listing');
        let pollCount = 0;
        let failedPolls=0,polling=false;
        ctl.id = setInterval(async () => {
            if(polling||!ctl.fresh())return;
            polling=true;
            pollCount++;
            try {
                const statusRes = await fetch(`/api/one-click/${taskId}`);
                if(!statusRes.ok)throw new Error('任务状态暂不可用');
                const status = await statusRes.json();
                if(!ctl.fresh())return;
                failedPolls=0;
                progressContent.innerHTML = _renderListingProgress(status, pollCount);

                if (['done','partial','error','filled_verified','needs_review','failed','cancelled'].includes(status.status)) {
                    ctl.stop();_listingBusy=false;localStorage.removeItem('currentListingTask');
                    const done = status.status === 'filled_verified' || (status.status==='done' && status.verified===true);
                    const partial = status.status === 'partial';
                    const needsReview=['needs_review','done'].includes(status.status)&&!done;
                    progressContent.innerHTML = done ? '<p>✅ 已验证填入，请商家核对后发布</p>'
                        : (partial ? '<p>⚠️ 部分店铺需要处理</p>'
                            : (needsReview ? '<p>⚠️ 已结束填表，需要人工核对</p>' : `<p>❌ ${escHtml(status.message || '填表未完成')}</p>`));
                    // 刷新上架计数
                    if (status.used !== undefined && status.used !== null) {
                        setUsageCount(status.used);
                    }
                    resultContent.innerHTML = `
                        <div class="field">
                            <span class="field-label">商品标题:</span>
                            <span class="field-value">${escHtml(title || '-')}</span>
                        </div>
                        <div class="field">
                            <span class="field-label">SKU数量:</span>
                            <span class="field-value">${status.sku_count || selectedSkus.length} 个</span>
                        </div>
                        <div class="field">
                            <span class="field-label">结果:</span>
                            <span class="field-value">${escHtml(status.message || (done ? '已验证填入' : '需要核对'))}</span>
                        </div>
                    ` + _renderShopResults(status);
                    resultDiv.style.display = 'block';
                    setStatus(done ? '已验证填入，待商家发布' : (partial||needsReview ? '需要人工核对' : '执行失败'));
                }
            } catch (e) {
                failedPolls++;
                if(failedPolls>=5){ctl.stop();_listingBusy=false;progressContent.textContent='暂时无法读取任务状态。已保留任务编号，请恢复连接后查询，避免重复填表。';setStatus('任务状态待恢复');}
            } finally {
                polling=false;
            }
        }, 2000);

    } catch (error) {
        _listingBusy=false;
        progressContent.innerHTML = `<p>❌ 请求失败: ${escHtml(error.message)}</p>`;
        setStatus('执行失败');
        console.error(error);
    }
}

// 进度区：多店时按店分行。status.shops 是 {端口: {port, name, status, progress}}。
function _renderListingProgress(status, pollCount) {
    const head = `<p>⏳ ${escHtml(status.progress || '处理中...')} (已等待${pollCount * 2}秒)</p>`;
    const shops = status.shops || {};
    const keys = Object.keys(shops);
    if (keys.length === 0) return head;      // 单店老路径：还是那行字
    const icon = s => s === 'filled_verified' ? '✅' : (['error','failed'].includes(s) ? '❌'
                     : (s === 'pending' ? '⏳'
                     : (s === 'waiting' ? '✋' : '🔄')));
    const rows = keys.map(k => {
        const r = shops[k] || {};
        // waiting = 停在「等你手选分类」这一步了。它跟别的状态不一样：不是在跑，是在**等人**，
        // 得让用户一眼看见该去点哪个窗口，所以加底色 + 描边，不能跟「正在跑」长一个样。
        const wait = r.status === 'waiting'
            ? 'background:#fff7e6;border:1px solid #ffd591;border-radius:6px;padding:4px 6px;'
            : '';
        return `<div style="margin:2px 0 2px 8px;${wait}">${icon(r.status)} `
             + `<b>${escHtml(r.name || _shopLabel(r.port || k))}</b>：${escHtml(r.progress || '处理中...')}</div>`;
    }).join('');
    return head + rows;
}

// 第三步里那行「当前检测到几家店」的提示。检测不到就什么都不显示，不打扰。
async function refreshShopHint() {
    const el = document.getElementById('shopSummaryHint');
    if (!el) return;
    try {
        const r = await fetch('/api/pdd-shops');
        if (!r.ok) { el.textContent = ''; return; }
        const shops = (await r.json()).shops || [];
        const alive = shops.filter(_shopAlive);
        if (alive.length === 0) {
            el.textContent = '⚠ 没检测到开着的店铺窗口 —— 请先点「打开新店铺窗口」并登录拼多多。';
            return;
        }
        el.textContent = `检测到 ${alive.length} 家店铺窗口：`
            + alive.map(s => `${s.name || s.label}${s.ready ? '' : '（未就绪）'}`).join('、')
            + (alive.length > 1 ? '。点「执行一键上架」会让你勾选要上哪几家。' : '。');
    } catch (e) {
        el.textContent = '';
    }
}

// 结果区：按店列出来，哪家成了、哪家没成一眼看清
function _renderShopResults(status) {
    const shops = status.shops || {};
    const keys = Object.keys(shops);
    if (keys.length === 0) return '';
    const rows = keys.map(k => {
        const r = shops[k] || {};
        const ok = r.status === 'filled_verified' || (r.status==='done' && r.verified===true);
        const needsReview=['done','needs_review'].includes(r.status)&&!ok;
        return `<div class="field">
                    <span class="field-label">${escHtml(r.name || _shopLabel(r.port || k))}:</span>
                    <span class="field-value">${ok ? '✅ 已验证填入' : `${needsReview ? '⚠️ 需要核对' : '❌ 未完成'}：${escHtml(r.progress || r.message || '请检查店铺窗口')}`}</span>
                </div>`;
    }).join('');
    return rows;
}

async function resumeListingTask() {
    const saved=readStoredJson('currentListingTask',null);
    if(!saved || _listingBusy)return;
    _listingBusy=true;
    const ctl=_beginPoll('listing');
    const progress=document.getElementById('listingProgressContent');
    document.getElementById('listingProgress').style.display='block';
    let polls=0,failures=0,polling=false;
    const read=async()=>{
        if(polling||!ctl.fresh())return;
        polling=true;
        try {
            const response=await fetch('/api/one-click/'+encodeURIComponent(saved.id));
            if(!response.ok)throw new Error('状态不可用');
            const status=await response.json();if(!ctl.fresh())return;
            failures=0;progress.innerHTML=_renderListingProgress(status,++polls);
            if(['done','partial','error','filled_verified','needs_review','failed','cancelled'].includes(status.status)){
                ctl.stop();_listingBusy=false;localStorage.removeItem('currentListingTask');
                const verified=status.status==='filled_verified'||(status.status==='done'&&status.verified===true);
                progress.textContent=verified ? '已验证填入，请商家核对后发布' : (status.message||'任务已结束，需要人工核对');
                const result=document.getElementById('listingResultContent');result.innerHTML=_renderShopResults(status);
                document.getElementById('listingResult').style.display='block';
                if(status.used!==undefined&&status.used!==null)setUsageCount(status.used);
                setStatus(verified?'已验证填入，待商家发布':'任务已恢复，请核对结果');
            }
        } catch(error) {
            if(++failures>=5){ctl.stop();_listingBusy=false;progress.textContent='尚未恢复任务状态，请检查本地助手后再次点击查询。已保留任务，避免重复填表。';setStatus('任务状态待恢复');}
        } finally {polling=false;}
    };
    ctl.id=setInterval(read,2000);await read();
}

// 显示搬家结果
// 加载搬家商品列表（抓取的商品）
async function loadMigratedProducts() {
    // 加载不到就把原因写到列表里。以前只在 console 里记一笔，界面照样显示
    // 「暂无抓取的商品」—— 用户以为自己的商品没了，其实是接口报错了
    // （比如激活码过期后这个接口会返回 403）。
    const fail = (why) => {
        state.migratedProducts = [];
        renderProducts();
        const list = document.getElementById('productList');
        if (list) {
            list.innerHTML = '<h3 style="margin:0 0 12px;color:#555;">🔄 抓取的商品</h3>'
                + `<p class="empty-state" style="color:#c00;">商品列表没加载出来：${escHtml(why)}</p>`;
        }
    };
    try {
        const response = await fetch('/api/migrated-products');
        if (!response.ok) {
            let why = 'HTTP ' + response.status;
            try {
                const d = await response.json();
                if (d && d.detail) why = d.detail;
            } catch (e) { /* 不是 JSON，用状态码 */ }
            console.error('加载搬家商品失败:', response.status, why);
            fail(why);
            return;
        }
        const data = await response.json();
        state.migratedProducts = Array.isArray(data) ? data : [];
        renderProducts();
    } catch (error) {
        console.error('加载搬家商品失败:', error);
        fail('工具没响应（可能已经关掉了）');
    }
}

// 渲染商品列表
function renderProducts() {
    const list = document.getElementById('productList');
    const migrated = state.migratedProducts || [];

    let html = '<h3 style="margin:0 0 12px;color:#555;">🔄 抓取的商品</h3>';
    if (migrated.length === 0) {
        html += '<p class="empty-state">暂无抓取的商品，去「抓取上架」抓取后点「保存到商品列表」</p>';
    } else {
        html += migrated.map(p => {
            const thumb = (p.main_images && p.main_images[0]) ? toImgUrl(p.main_images[0]) : '';
            const specCount = p.specs ? Object.keys(p.specs).length : 0;
            const mainCount = (p.main_images || []).length;
            const detailCount = (p.detail_images || []).length;
            const skuCount = (p.sku || []).length;
            return `<div class="product-item">
                ${thumb ? `<img class="product-image" src="${escHtml(thumb)}" onerror="this.style.display='none'">` : ''}
                <div class="product-info">
                    <div class="product-title">${escHtml(p.title || '未命名商品')}</div>
                    <div style="color:#888;font-size:12px;">规格 ${specCount} 组 · 主图 ${mainCount} · 详情图 ${detailCount} · SKU ${skuCount} 条 · 更新于 ${escHtml(p.updated_at || '-')}</div>
                </div>
                <div class="product-actions">
                    <button class="btn btn-secondary" data-product-id="${escHtml(p.id)}" onclick="loadMigratedProduct(this.dataset.productId)">加载 / 修改</button>
                    <button class="btn btn-primary" data-product-id="${escHtml(p.id)}" onclick="publishMigratedProduct(this.dataset.productId)">一键上架</button>
                    <button class="btn btn-danger" data-product-id="${escHtml(p.id)}" onclick="deleteMigratedProduct(this.dataset.productId)">删除</button>
                </div>
            </div>`;
        }).join('');
    }

    list.innerHTML = html;
}

// 保存当前抓取的商品到商品列表
async function saveMigratedProduct() {
    const title = document.getElementById('productTitle').value.trim();
    if (!title && skuData.length === 0 && localMainImages.length === 0 && localDetailImages.length === 0) {
        alert('请先生成或填写商品资料');
        return;
    }
    // 防连点：这个接口每次调用都会新建一条商品（id 由后端生成），
    // 按钮点两下就会在商品列表里多出一条重复的
    if (saveMigratedProduct._busy) return;
    saveMigratedProduct._busy = true;
    try {
        await _saveMigratedProductRequest(title);
    } finally {
        saveMigratedProduct._busy = false;
    }
}

async function _saveMigratedProductRequest(title) {
    const payload = {...syncListingPackage(),id:currentMigratedId||'',title};
    try {
        const res = await fetch('/api/migrated-products', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok && data.success) {
            currentMigratedId = data.id;
            currentDraftPackage={...payload,...(data.product||{}),id:data.id};
            syncListingPackage();
            alert('已保存到商品列表');
            await loadMigratedProducts();
        } else {
            alert('保存失败：' + (data.detail || '未知错误'));
        }
    } catch (e) {
        alert('保存失败：' + e.message);
        console.error(e);
    }
}

// 把已保存的商品加载回搬家页（可修改后再次上架）
function loadMigratedProduct(id) {
    const p = (state.migratedProducts || []).find(x => x.id === id);
    if (!p) { alert('商品不存在'); return; }

    // 切换到商品搬家 tab
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const migrateNav = document.querySelector('.nav-item[data-tab="migrate"]');
    if (migrateNav) migrateNav.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-migrate').classList.add('active');

    resetMigrateForm();

    applyProductPackageToListing(p);
    if(typeof window.aiSetProductPackage==='function')window.aiSetProductPackage(p);
    document.getElementById('executeSection').style.display = 'block';
    refreshShopHint();
    setStatus('已加载商品，可修改价格后上架');
}

// 直接用已保存商品一键上架
function publishMigratedProduct(id) {
    loadMigratedProduct(id);
    startListing();
}

// 删除已保存的商品
async function deleteMigratedProduct(id) {
    if (!confirm('确定删除这个商品草稿吗？')) return;
    try {
        const res = await fetch('/api/migrated-products/' + encodeURIComponent(id), { method: 'DELETE' });
        const data = await res.json();
        if (res.ok && data.success) {
            if(currentMigratedId===id)resetMigrateForm();
            await loadMigratedProducts();
        } else {
            alert('删除失败：' + (data.detail || '未知错误'));
        }
    } catch (e) {
        alert('删除失败：' + e.message);
    }
}

// 设置状态
function setStatus(text) {
    document.getElementById('status').textContent = text;
}

// 页面加载时恢复SKU表格
if (skuData.length > 0) {
    renderSkuTable();
}

// ============ 授权 ============
const LICENSE_REASON_TEXT = {
    invalid: '激活码无效，请核对后重试',
    expired: '该激活码已过期，请联系出租方续费',
    machine_mismatch: '该激活码绑定的是其他电脑，请确认验证码无误',
    clock_rollback: '检测到系统时间被回拨，请将系统时间改回正确时间后重试'
};

async function checkLicense() {
    try {
        const res = await fetch('/api/license/status');
        const data = await res.json();
        document.getElementById('machineId').textContent = data.machine_id || '未知';
        applyLicenseBar(data);
        if (data.ok) {
            document.getElementById('licenseGate').classList.add('hidden');
            showLicenseBanner(data);
        } else {
            document.getElementById('licenseGate').classList.remove('hidden');
            if (data.state === 'clock_rollback') {
                const m = document.getElementById('licenseMsg');
                m.className = 'msg err';
                m.textContent = LICENSE_REASON_TEXT.clock_rollback;
            }
        }
    } catch (e) {
        document.getElementById('licenseGate').classList.remove('hidden');
        document.getElementById('machineId').textContent = '未知';
    }
}

// 商品搬家界面：显示验证码 + 已上架数量
function updateUsageBar(data) {
    const um = document.getElementById('usageMachineId');
    const uc = document.getElementById('usageCode');
    const ue = document.getElementById('usageExpiry');
    const uq = document.getElementById('usageQuota');
    if (um) um.textContent = data.machine_id || '未知';
    if (uc) uc.textContent = data.admin_mode ? '管理端（免激活）' : (data.code || '（未激活）');
    if (ue) ue.textContent = data.admin_mode ? '不限（管理端）' : (data.expire_at ? String(data.expire_at).replace('T', ' ') : (data.expiry || '未知'));
    if (uq) uq.textContent = data.admin_mode ? '不限' : (data.quota > 0 ? `${data.quota} 款` : '不限');
    setUsageCount(data.used ?? 0);
}

// 应用授权状态到使用栏 + 测试码倒计时
function applyLicenseBar(data) {
    updateUsageBar(data);
    licenseExpireAt = data.expire_at || null;
    updateTestCountdown();
}

// 测试码倒计时显示
function updateTestCountdown() {
    const el = document.getElementById('testCountdown');
    if (!el) return;
    if (!licenseExpireAt) { el.style.display = 'none'; return; }
    const remainMs = new Date(licenseExpireAt.replace(' ', 'T')) - new Date();
    el.style.display = 'block';
    if (remainMs <= 0) {
        el.textContent = '⛔ 当前测试码已到期（无法再抓取/上架）';
    } else {
        const s = Math.floor(remainMs / 1000);
        const mm = Math.floor(s / 60), ss = s % 60;
        el.textContent = `⏳ 测试码剩余 ${mm}:${String(ss).padStart(2, '0')}`;
    }
}

// 刷新授权信息（不弹窗、不显示激活门，用于切换后刷新使用栏）
async function refreshLicenseInfo() {
    try {
        const res = await fetch('/api/license/status');
        const data = await res.json();
        applyLicenseBar(data);
        return data;
    } catch (e) {
        return null;
    }
}

// 切换激活码（测试用）
async function switchLicense() {
    const code = document.getElementById('switchCode').value.trim();
    const msg = document.getElementById('switchMsg');
    if (!code) { msg.style.color = '#c0392b'; msg.textContent = '请先粘贴激活码'; return; }
    msg.style.color = '#888';
    msg.textContent = '验证中...';
    try {
        const res = await fetch('/api/license/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        const data = await res.json();
        if (data.ok) {
            msg.style.color = '#1a9b4f';
            msg.textContent = '✅ 已切换到新激活码';
            document.getElementById('switchCode').value = '';
            await refreshLicenseInfo();
        } else {
            const reasonText = LICENSE_REASON_TEXT[data.reason] || data.reason || '切换失败';
            msg.style.color = '#c0392b';
            msg.textContent = '❌ ' + reasonText;
        }
    } catch (e) {
        msg.style.color = '#c0392b';
        msg.textContent = '切换失败: ' + e.message;
    }
}

// 清零已上架数（测试额度用）
async function resetUsage() {
    const msg = document.getElementById('switchMsg');
    try {
        const res = await fetch('/api/license/reset-usage', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.success) {
            msg.style.color = '#1a9b4f';
            msg.textContent = '✅ 已清零已上架数';
            await refreshLicenseInfo();
        } else {
            msg.style.color = '#c0392b';
            msg.textContent = '清零失败';
        }
    } catch (e) {
        msg.style.color = '#c0392b';
        msg.textContent = '清零失败: ' + e.message;
    }
}

// 上款数字每位用不同颜色，增加截图P图难度（颜色按位固定，方便核对）
const COUNT_COLORS = ['#e02e24', '#1a7f37', '#0969da', '#bf3989', '#9a6700', '#8250df', '#cf222e', '#0a3069'];

function setUsageCount(n) {
    const el = document.getElementById('usageCount');
    if (!el) return;
    el.innerHTML = String(n ?? 0).split('').map((ch, i) =>
        `<span style="color:${COUNT_COLORS[i % COUNT_COLORS.length]};">${ch}</span>`
    ).join('');
}

// 水印时间：租户截图时显示当前本机时间（每秒刷新）
function startUsageClock() {
    const el = document.getElementById('usageTime');
    if (!el) return;
    const pad = n => String(n).padStart(2, '0');
    const tick = () => {
        const d = new Date();
        el.textContent = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    };
    tick();
    setInterval(tick, 1000);
}

function showLicenseBanner(data) {
    const banner = document.getElementById('licenseBanner');
    const dl = data.days_left;
    // 激活了但资产包还没导入：上架功能真的用不了（干活的 JS 在包里），必须显眼地提示
    if (data.ok && !data.admin_mode && !data.pack_ready) {
        banner.textContent = '⚠️ 还差一步：把出租方发来的「资产包」（assets_…zip）在下面「📦 资产包」那里导入，上架功能才能用';
        banner.style.background = '#ffecec';
        banner.style.color = '#c0392b';
        banner.classList.remove('hidden');
        return;
    }
    banner.style.background = '';
    banner.style.color = '';
    if (data.state === 'expired') {
        // 已真正过期（含分钟级测试码到点）
        banner.textContent = '⚠️ 授权已到期，请联系续费';
        banner.classList.remove('hidden');
    } else if (dl !== null && dl !== undefined && dl <= 7) {
        const text = dl > 0
            ? `⚠️ 授权还剩 ${dl} 天，请及时续费`
            : '⚠️ 授权今天到期（今天仍可正常使用），请及时续费';
        banner.textContent = text;
        banner.classList.remove('hidden');
    } else {
        banner.classList.add('hidden');
    }
}

async function activateLicense() {
    const code = document.getElementById('licenseCode').value.trim();
    const msg = document.getElementById('licenseMsg');
    msg.className = 'msg';
    if (!code) {
        msg.className = 'msg err';
        msg.textContent = '请输入激活码';
        return;
    }
    msg.textContent = '激活中...';
    try {
        const res = await fetch('/api/license/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        const data = await res.json();
        if (data.ok) {
            msg.className = 'msg ok';
            msg.textContent = '✅ 激活成功！如果出租方还发了一个 assets_ 开头的 zip，请到上架页的「📦 资产包」那里导入，然后重启工具。';
        } else {
            msg.className = 'msg err';
            msg.textContent = LICENSE_REASON_TEXT[data.reason] || ('激活失败：' + (data.reason || '未知错误'));
        }
    } catch (e) {
        msg.className = 'msg err';
        msg.textContent = '激活失败，请核对激活码';
    }
}

// 导入出租方发来的资产包（assets_<验证码>.zip）
// 一码一份：每个人的密文是单独打的，只有自己那张激活码里的种子打得开（见 asset_store.py）。
async function importPack() {
    const inp = document.getElementById('packFile');
    const msg = document.getElementById('packMsg');
    if (!inp || !inp.files || !inp.files.length) {
        msg.style.color = '#c0392b';
        msg.textContent = '先点左边「选择文件」，挑出租方发来的那个 assets_ 开头的 zip';
        return;
    }
    const fd = new FormData();
    fd.append('file', inp.files[0]);
    msg.style.color = '#888';
    msg.textContent = '导入中...';
    try {
        const res = await fetch('/api/license/import-pack', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.success) {
            msg.style.color = '#1a9b4f';
            msg.textContent = '✅ ' + data.message + '（重启工具后生效）';
            inp.value = '';
            await checkLicense();
        } else {
            msg.style.color = '#c0392b';
            msg.textContent = '❌ ' + (data.message || '导入失败');
        }
    } catch (e) {
        msg.style.color = '#c0392b';
        msg.textContent = '导入失败：' + e.message;
    }
}

function copyMachineId() {
    const mid = document.getElementById('machineId').textContent;
    if (mid && navigator.clipboard) {
        navigator.clipboard.writeText(mid).then(() => {
            const msg = document.getElementById('licenseMsg');
            msg.className = 'msg ok';
            msg.textContent = '✅ 已复制验证码，请粘贴发给出租方';
        });
    }
}

// ============ 管理端（发码后台） ============
function copyText(t) {
    if (navigator.clipboard) {
        navigator.clipboard.writeText(t).then(() => alert('✅ 已复制'));
    } else {
        prompt('手动复制：', t);
    }
}

async function initAdminMode() {
    try {
        const res = await fetch('/api/config');
        const cfg = await res.json();
        if (cfg.admin_mode) {
            // 管理端：显示「管理」页签，隐藏授权门禁与到期提醒
            document.getElementById('navAdmin').classList.remove('hidden');
            document.getElementById('licenseGate').classList.add('hidden');
            document.getElementById('licenseBanner').classList.add('hidden');
            loadAdminStats();
            adminList();
        }
    } catch (e) {
        // 用户端无此页签，忽略
    }
}

async function loadAdminStats() {
    try {
        const res = await fetch('/api/admin/stats');
        const s = await res.json();
        const cards = [
            ['总发码', s.total], ['有效', s.active], ['7天内到期', s.expiring_soon],
            ['已过期', s.expired], ['作废', s.void],
            ['累计收入(元)', Number(s.total_revenue).toFixed(2)], ['本月收入(元)', Number(s.month_revenue).toFixed(2)]
        ];
        document.getElementById('adminStats').innerHTML = cards.map(([k, v]) =>
            `<div style="background:#fff;border:1px solid #eee;border-radius:8px;padding:10px 16px;text-align:center;min-width:90px;">
                <div style="color:#888;">${k}</div><div style="font-size:18px;font-weight:600;margin-top:4px;">${v}</div></div>`
        ).join('');
    } catch (e) { /* 忽略 */ }
}

/** 快捷时长：把「月,天」填进两个输入框。 */
function adminApplyPreset() {
    const preset = document.getElementById('adminPreset').value;
    if (!preset) return;
    const [m, d] = preset.split(',').map(Number);
    document.getElementById('adminMonths').value = m;
    document.getElementById('adminDays').value = d;
}

/** 时长显示：几天就显示「15天」，整月显示「1月」，带零头显示「1月15天」。 */
function fmtDuration(months, days) {
    const m = months || 0, d = days || 0;
    if (!m && !d) return '-';
    return (m ? m + '月' : '') + (d ? d + '天' : '');
}

async function adminGen() {
    const mid = document.getElementById('adminMid').value.trim();
    const months = parseInt(document.getElementById('adminMonths').value) || 0;
    const days = parseInt(document.getElementById('adminDays').value) || 0;
    const quota = parseInt(document.getElementById('adminQuota').value) || 1000;
    const amount = parseFloat(document.getElementById('adminAmount').value) || 0;
    const note = document.getElementById('adminNote').value.trim();
    const minutes = parseInt(document.getElementById('adminMinutes').value) || 0;
    const msg = document.getElementById('adminGenMsg');
    if (!mid) { msg.style.color = '#c0392b'; msg.textContent = '请填写验证码'; return; }
    if (minutes <= 0 && months <= 0 && days <= 0) {
        msg.style.color = '#c0392b';
        msg.textContent = '时长不能为 0：半个月填 0月15天，一个半月填 1月15天（测试码除外）';
        return;
    }
    msg.style.color = '#888'; msg.textContent = '生成中...';
    try {
        const res = await fetch('/api/admin/gen', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ machine_id: mid, months, days, quota, amount, note, minutes })
        });
        const data = await res.json();
        if (data.success) {
            const r = data.record;
            msg.style.color = '#1a9b4f'; msg.textContent = '✅ 已生成';
            const box = document.getElementById('adminGenResult');
            box.style.display = 'block';
            box.innerHTML =
                `<div style="font-size:14px;">激活码（发给租户）：</div>
                <div style="font-family:monospace;font-size:15px;word-break:break-all;background:#f6f8fa;padding:10px;border-radius:6px;margin:8px 0;">${r.code}</div>
                <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
                    <button type="button" class="btn btn-secondary" onclick="copyText('${r.code}')">📋 复制激活码</button>
                    <button type="button" class="btn btn-primary" onclick="adminPack(${r.id})">📦 下载资产包</button>
                    <span style="font-size:13px;color:#666;">到期：${r.expiry} ｜ 时长：${r.duration || fmtDuration(r.months, r.days)} ｜ 月限额：${r.quota} 款${r.expire_at ? ' ｜ 测试码到 ' + r.expire_at : ''}</span>
                </div>
                <div style="margin-top:8px;font-size:13px;color:${r.pack_error ? '#c0392b' : '#1a9b4f'};">
                    ${r.pack_error
                        ? '⚠️ 资产包没生成出来：' + r.pack_error + '<br>（没有它租户用不了上架功能；可以直接把激活码先发过去，资产包稍后从列表里补发）'
                        : '✅ 资产包已生成：' + (r.pack_name || '') + ' —— <b>要和激活码一起发给租户</b>'}
                </div>`;
            adminList();
            loadAdminStats();
        } else {
            msg.style.color = '#c0392b'; msg.textContent = '生成失败：' + (data.detail || '未知错误');
        }
    } catch (e) {
        msg.style.color = '#c0392b'; msg.textContent = '生成失败：' + e.message;
    }
}

async function adminDecode() {
    const code = document.getElementById('adminDecodeCode').value.trim();
    const box = document.getElementById('adminDecodeResult');
    if (!code) { box.innerHTML = ''; return; }
    box.textContent = '解析中...';
    try {
        const res = await fetch('/api/admin/decode', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        const data = await res.json();
        if (data.success) {
            const i = data.info;
            box.innerHTML = `验证码：<b>${i.machine_id}</b> ｜ 到期：<b>${i.expiry}</b> ｜ 月限额：<b>${i.quota}</b> ｜ 签名：${i.valid ? '✅ 有效' : '❌ 无效'}${i.expire_at ? ' ｜ 到点：' + i.expire_at : ''}`;
        } else {
            box.innerHTML = `<span style="color:#c0392b;">解析失败：${data.detail || '未知错误'}</span>`;
        }
    } catch (e) {
        box.innerHTML = `<span style="color:#c0392b;">解析失败：${e.message}</span>`;
    }
}

async function adminList() {
    const q = document.getElementById('adminSearch').value.trim();
    const status = document.getElementById('adminStatusFilter').value;
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (status) params.set('status', status);
    try {
        const res = await fetch('/api/admin/list?' + params.toString());
        const data = await res.json();
        const rows = data.items || [];
        const tb = document.getElementById('adminTableBody');
        if (!rows.length) {
            tb.innerHTML = '<tr><td colspan="12" style="text-align:center;color:#999;">暂无记录</td></tr>';
            return;
        }
        tb.innerHTML = rows.map(r => `
            <tr>
                <td>${r.id}</td>
                <td style="font-family:monospace;">${r.machine_id}</td>
                <td style="font-family:monospace;font-size:11px;max-width:180px;word-break:break-all;">${r.code}</td>
                <td>${r.expiry}</td>
                <td>${r.duration || fmtDuration(r.months, r.days)}</td>
                <td>${r.has_seed ? '🔑 有' : '⚠️ 旧版'}</td>
                <td>${r.quota}</td>
                <td>${r.amount}</td>
                <td>${r.note || ''}</td>
                <td>${r.status === 'active' ? '✅有效' : '🚫作废'}</td>
                <td style="font-size:11px;">${r.created_at}</td>
                <td style="white-space:nowrap;">
                    <button type="button" class="btn btn-secondary" style="padding:2px 8px;font-size:11px;" onclick="adminExtend(${r.id})">续期</button>
                    ${r.has_seed ? `<button type="button" class="btn btn-secondary" style="padding:2px 8px;font-size:11px;" onclick="adminPack(${r.id})">📦资产包</button>` : ''}
                    ${r.has_seed ? '' : `<button type="button" class="btn btn-primary" style="padding:2px 8px;font-size:11px;" onclick="adminReissue(${r.id})">换发</button>`}
                    <button type="button" class="btn btn-secondary" style="padding:2px 8px;font-size:11px;" onclick="adminToggleStatus(${r.id},'${r.status}')">${r.status === 'active' ? '作废' : '恢复'}</button>
                    <button type="button" class="btn btn-secondary" style="padding:2px 8px;font-size:11px;" onclick="adminDelete(${r.id})">删除</button>
                </td>
            </tr>`).join('');
    } catch (e) { /* 忽略 */ }
}

// 下载某条记录的资产包（就是连同激活码一起发给租户的那个 assets_<验证码>.zip）。
// 种子在账本里，所以随时能重出 —— 租户把包弄丢了、换了电脑重装，直接再发一份。
function adminPack(id) {
    const a = document.createElement('a');
    a.href = '/api/admin/pack/' + id;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

async function adminReissue(id) {
    if (!confirm('换发激活码？\n\n同一台机器、同一个到期日，换成带钥匙的新版码（不加时长）。\n换发的码会配一个新的资产包，两个要一起发给租户。\n老码会失效吗：不会，老码还能显示有效，但新版工具要用这张新码。')) return;
    try {
        const res = await fetch('/api/admin/reissue', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await res.json();
        if (data.success) {
            alert('✅ 换发成功，到期日不变：' + data.record.expiry + '\n\n新激活码（发给租户）：\n' + data.record.code);
            adminList();
        } else {
            alert('换发失败：' + (data.detail || '未知错误'));
        }
    } catch (e) {
        alert('换发失败：' + e.message);
    }
}

async function adminExtend(id) {
    const mText = prompt('续期几个月？（整数，可填 0）', '1');
    if (mText === null) return;
    const months = parseInt(mText) || 0;
    const dText = prompt('再加几天？（半个月填 15，不加就填 0）', '0');
    if (dText === null) return;
    const days = parseInt(dText) || 0;
    if (months <= 0 && days <= 0) { alert('时长不能为 0，已取消'); return; }
    const amount = parseFloat(prompt('本次收费（元，可留空填 0）', '0')) || 0;
    try {
        const res = await fetch('/api/admin/extend', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, months, days, amount })
        });
        const data = await res.json();
        if (data.success) {
            alert('✅ 续期成功，新到期：' + data.record.expiry
                + '（本次 +' + fmtDuration(data.record.months, data.record.days) + '）'
                + '\n\n新激活码（发给租户）：\n' + data.record.code);
            adminList();
            loadAdminStats();
        } else {
            alert('续期失败：' + (data.detail || '未知错误'));
        }
    } catch (e) {
        alert('续期失败：' + e.message);
    }
}

async function adminToggleStatus(id, cur) {
    const to = cur === 'active' ? 'void' : 'active';
    try {
        const res = await fetch('/api/admin/update', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, status: to })
        });
        const data = await res.json();
        if (data.success) { adminList(); loadAdminStats(); }
        else alert('操作失败：' + (data.detail || '未知错误'));
    } catch (e) {
        alert('操作失败：' + e.message);
    }
}

async function adminDelete(id) {
    if (!confirm('确定删除这条记录吗？（不可恢复）')) return;
    try {
        const res = await fetch('/api/admin/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await res.json();
        if (data.success) { adminList(); loadAdminStats(); }
        else alert('删除失败：' + (data.detail || '未知错误'));
    } catch (e) {
        alert('删除失败：' + e.message);
    }
}

function adminExport() {
    window.location.href = '/api/admin/export';
}
