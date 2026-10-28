/* Merchant image workbench. Provider credentials stay in form memory only. */
(function () {
    'use strict';
    const AI_RESULT_STORAGE_KEY = 'aiWorkflowResult';
    const AI_PREVIOUS_STORAGE_KEY = 'aiWorkflowPrevious';
    const AI_TASK_STORAGE_KEY = 'aiWorkflowTask';
    const AI_RESULT_VERSION = 18;
    const AI_MAX_FILES = 12;
    const AI_MAX_BYTES = 12 * 1024 * 1024;
    const AI_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/bmp']);
    const KIND_LABELS = { hero: '主图', detail: '详情图', spec: '规格图', sku: '规格图' };
    const ROLE_LABELS = { hook:'首屏卖点', carousel_sell:'首屏卖点', pain_point:'痛点场景', angle:'多角度', scene:'生活场景', lifestyle:'生活场景', proof:'材质细节', feature_macro:'材质细节', benefit:'核心卖点', mechanism:'结构说明', usage:'使用步骤', steps:'使用步骤', comparison:'对比说明', packaging:'包装', sku_preview:'规格预览', trust_faq:'常见问题', closing_cta:'选购提示', size_chart:'尺码表', white_pack:'白底首图', flat_lay:'平铺展示' };
    const PROVIDER_FIELDS = [['image_api_url','aiImageApiUrl'],['image_api_key','aiImageApiKey'],['image_model','aiImageModel'],['image_reference_mode','aiReferenceMode'],['prompt_api_url','aiPromptApiUrl'],['prompt_api_key','aiPromptApiKey'],['prompt_model','aiPromptModel'],['vision_api_url','aiVisionApiUrl'],['vision_api_key','aiVisionApiKey'],['vision_model','aiVisionModel']];
    const $ = id => document.getElementById(id);
    const value = id => String(($ (id) || {}).value || '').trim();
    const checked = id => !!(($ (id) || {}).checked);
    const clone = object => JSON.parse(JSON.stringify(object));
    const esc = input => String(input == null ? '' : input).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    const splitValues = input => Array.from(new Set(String(input || '').split(/[，,、;；\r\n]+/).map(item => item.trim()).filter(Boolean))).slice(0,24);
    let aiResultData = null;
    let aiPreviousData = null;
    let aiSelectedFiles = [];
    let aiFileBindings = new Map();
    let aiSizeRows = new Map();
    let aiPlan = null;
    let aiPlanError = '';
    let aiDirty = false;
    let aiBusy = false;
    let aiStarting = false;
    let aiTaskId = null;
    let aiPollToken = 0;
    let aiPlanSequence = 0;
    let aiPlanTimer = null;
    let aiCreativeBriefOverride = null;
    let aiSourceChanged = false;
    let aiObjectUrls = [];
    let aiAttrRows = [];

    function notify(text) {
        if (typeof setStatus === 'function') setStatus(text);
    }
    function setState(text) {
        if ($('aiProgress')) $('aiProgress').style.display = 'block';
        if ($('aiProgressContent')) $('aiProgressContent').textContent = text;
    }
    function safeSnapshot(data) {
        const redact = item => {
            if (Array.isArray(item)) return item.map(redact);
            if (item && typeof item === 'object') {
                const output = {};
                Object.entries(item).forEach(([key, val]) => {
                    if (!/(?:api.?key|secret|authorization|password|credential|provider_overrides|traceback)/i.test(key)) output[key] = redact(val);
                });
                return output;
            }
            if(typeof item==='string') {
                let clean=item.replace(/([?&](?:api[_-]?key|token|secret|access[_-]?token)=)[^&\s]+/gi,'$1[已隐藏]');
                ['aiImageApiKey','aiPromptApiKey','aiVisionApiKey'].map(value).filter(key=>key.length>3).forEach(key=>{clean=clean.split(key).join('[已隐藏]');});
                return clean;
            }
            return item;
        };
        const snapshot = redact(clone(data));
        snapshot._workflow_version = AI_RESULT_VERSION;
        return snapshot;
    }
    function storeJson(key, data) {
        try { localStorage.setItem(key, JSON.stringify(safeSnapshot(data))); }
        catch (_error) { notify('浏览器存储空间不足，请保存商品草稿'); }
    }
    function persistAiResult() {
        if (aiResultData) storeJson(AI_RESULT_STORAGE_KEY, aiResultData);
        if (aiPreviousData) storeJson(AI_PREVIOUS_STORAGE_KEY, aiPreviousData);
    }
    function markDirty(reason, sourceChanged, changeType='content') {
        aiDirty = true;
        if (sourceChanged) {
            aiSourceChanged = true;
            aiCreativeBriefOverride = null;
            // Keep the reviewed version, but do not let it look like a new
            // product's upload result. The listing handoff checks this flag.
        }
        const box = $('aiDirtyNotice');
        if (box) {
            box.hidden = !aiResultData;
            box.textContent = reason || '配置已修改；下方仍是上一版成品，重新生成成功后才会替换。';
        }
        schedulePlan();
        if(typeof window.aiProposalSettingsChanged==='function')window.aiProposalSettingsChanged(reason,!!sourceChanged,changeType);
    }
    function imageUrl(path) {
        if (typeof toImgUrl === 'function') return toImgUrl(path);
        const relative = String(path || '').replace(/\\/g,'/').replace(/^data\//,'');
        return '/data/' + relative.split('/').map(encodeURIComponent).join('/');
    }
    function fileKey(file) { return [file.name,file.size,file.lastModified].join('|'); }
    function formatBytes(bytes) { return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
    function releaseFileUrls() { aiObjectUrls.forEach(url => URL.revokeObjectURL(url)); aiObjectUrls = []; }
    function renderFiles() {
        const box = $('aiFileList');
        if (!box) return;
        releaseFileUrls();
        box.style.display = aiSelectedFiles.length ? 'grid' : 'none';
        box.innerHTML = aiSelectedFiles.map((file,index) => {
            const url = URL.createObjectURL(file); aiObjectUrls.push(url);
            return `<article class="ai-file-card"><img src="${esc(url)}" alt="参考照片 ${index+1}"><div class="ai-file-card-body"><div class="ai-file-card-name" title="${esc(file.name)}">${index+1}. ${esc(file.name)} · ${formatBytes(file.size)}</div><label>对应颜色 / 款式<input data-reference-index="${index}" aria-label="照片${index+1}对应颜色或款式" value="${esc(aiFileBindings.get(fileKey(file)) || '')}" placeholder="例如：米白色；可留空"></label><button type="button" data-remove-file="${index}">移除照片</button></div></article>`;
        }).join('');
        box.querySelectorAll('[data-reference-index]').forEach(input => input.addEventListener('change', () => {
            const file = aiSelectedFiles[Number(input.dataset.referenceIndex)];
            if (file) aiFileBindings.set(fileKey(file), input.value.trim());
            markDirty('参考照片的规格绑定已修改，上一版仍保留。',false,'reference');
        }));
        box.querySelectorAll('[data-remove-file]').forEach(button => button.addEventListener('click', () => window.removeAiFile(Number(button.dataset.removeFile))));
        refreshMode();
    }
    function addFiles(files) {
        if (aiBusy) return;
        const incoming = Array.from(files || []);
        const existing = new Set(aiSelectedFiles.map(fileKey));
        let bytes = aiSelectedFiles.reduce((sum,file) => sum + file.size,0);
        const rejected = [];
        let added = 0;
        for (const file of incoming) {
            if (file.type && !AI_TYPES.has(file.type)) { rejected.push(`${file.name}：格式不支持`); continue; }
            if (existing.has(fileKey(file))) continue;
            if (aiSelectedFiles.length >= AI_MAX_FILES) { rejected.push('最多12张照片'); break; }
            if (bytes + file.size > AI_MAX_BYTES) { rejected.push(`${file.name}：超过12MB总上限`); continue; }
            aiSelectedFiles.push(file); existing.add(fileKey(file)); bytes += file.size; added += 1;
        }
        if (added) markDirty('参考照片已改变；下方上一版仅供比较，请重新生成当前商品。', true);
        renderFiles();
        if (rejected.length) notify(rejected.join('；'));
    }
    window.removeAiFile = index => {
        if (aiBusy || index < 0 || index >= aiSelectedFiles.length) return;
        const removed = aiSelectedFiles.splice(index,1)[0];
        aiFileBindings.delete(fileKey(removed));
        markDirty('已移除参考照片；上一版成品保留供比较。',true); renderFiles();
    };
    function initUploader() {
        const input = $('aiProductImage'), zone = $('aiDropZone'), choose = $('aiChooseImage');
        if (!input || !zone) return;
        choose.addEventListener('click', event => { event.stopPropagation(); if (!aiBusy) input.click(); });
        zone.addEventListener('click', () => { if (!aiBusy) input.click(); });
        zone.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (!aiBusy) input.click(); } });
        input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });
        zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('is-dragging'); });
        zone.addEventListener('dragleave', () => zone.classList.remove('is-dragging'));
        zone.addEventListener('drop', event => { event.preventDefault(); zone.classList.remove('is-dragging'); addFiles(event.dataTransfer && event.dataTransfer.files); });
    }
    function selectedSpecs() {
        const specs = {};
        const colors = splitValues(value('aiSelectedColors')), sizes = splitValues(value('aiSelectedSizes'));
        if (colors.length) specs['颜色'] = colors;
        if (sizes.length) specs['尺码'] = sizes;
        return specs;
    }
    function variantReferences() {
        const bindings = {};
        if(!aiSelectedFiles.length && !aiSourceChanged && aiResultData){
            (aiResultData.module_outputs||[]).filter(item=>['spec','sku'].includes(item.kind)&&item.spec_value&&Array.isArray(item.reference_indices)).forEach(item=>{bindings[item.spec_value]=item.reference_indices.slice();});
        }
        aiSelectedFiles.forEach((file,index) => {
            splitValues(aiFileBindings.get(fileKey(file)) || '').forEach(name => {
                if (!bindings[name]) bindings[name] = [];
                bindings[name].push(index+1);
            });
        });
        return bindings;
    }
    function parsePastedSizeRows() {
        const raw = value('aiSizeChartSpecs');
        if (!raw) return [];
        try {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) return parsed;
            if (parsed && Array.isArray(parsed.rows)) return parsed.rows;
            if (parsed && typeof parsed === 'object') return Object.entries(parsed).map(([size, measurements]) => ({size,measurements}));
        } catch (_error) { /* merchant may paste readable line format */ }
        return raw.split(/\r?\n/).map(line => {
            const parts = line.split('|').map(item => item.trim()).filter(Boolean), size = parts.shift();
            const measurements = {};
            parts.forEach(part => { const index = part.indexOf('='); if (index > 0) measurements[part.slice(0,index).trim()] = part.slice(index+1).trim(); });
            return {size,measurements};
        }).filter(row => row.size);
    }
    function collectSizeRows() {
        const pasted = parsePastedSizeRows();
        const rows = new Map();
        pasted.forEach(row => { if (row && row.size) rows.set(String(row.size), {size:String(row.size),measurements:row.measurements || {}}); });
        aiSizeRows.forEach((measurements,size) => rows.set(size,{size,measurements:{...(rows.get(size) || {}).measurements,...measurements}}));
        const sizes = splitValues(value('aiSelectedSizes'));
        if (checked('aiSizeChartOnlySelected') && sizes.length) return sizes.map(size => rows.get(size) || {size,measurements:{}});
        return Array.from(rows.values()).slice(0,24);
    }
    function renderSizeTable() {
        const box = $('aiSizeChartTable');
        if (!box) return;
        const sizes = splitValues(value('aiSelectedSizes'));
        const rows = sizes.length ? sizes : parsePastedSizeRows().map(row => String(row.size));
        const columns = splitValues(value('aiSizeColumns') || '衣长、胸围、肩宽、袖长');
        if (!rows.length) { box.innerHTML = '<p class="hint">先填写实际尺码，下面会自动出现实测数据表格。</p>'; return; }
        const pasted=new Map(parsePastedSizeRows().filter(row=>row && row.size).map(row=>[String(row.size),row.measurements||{}]));
        box.innerHTML = `<table class="ai-size-table"><thead><tr><th>尺码</th>${columns.map(column => `<th>${esc(column)}</th>`).join('')}</tr></thead><tbody>${rows.map((size,index) => `<tr><th>${esc(size)}</th>${columns.map((column,col) => `<td><input aria-label="${esc(size)} ${esc(column)}" data-size-row="${index}" data-size-col="${col}" value="${esc(({...(pasted.get(size)||{}),...(aiSizeRows.get(size)||{})})[column] ?? '')}" placeholder="待补充"></td>`).join('')}</tr>`).join('')}</tbody></table>`;
        box.querySelectorAll('input').forEach(input => input.addEventListener('input', () => {
            const size = rows[Number(input.dataset.sizeRow)], column = columns[Number(input.dataset.sizeCol)];
            aiSizeRows.set(size,{...(aiSizeRows.get(size) || {}),[column]:input.value.trim()}); markDirty('实测尺码已修改；重新生成尺码表后生效。');
        }));
    }
    function collectModules() {
        return Array.from(document.querySelectorAll('#aiDetailModules .ai-module-choice')).filter(input => input.checked).map(input => {
            const type = input.dataset.module;
            const countInput = document.querySelector(`[data-module-count="${type}"]`);
            return {type,count:Math.max(0,Math.min(20,Number.parseInt(countInput.value,10) || 0))};
        }).filter(module => module.count > 0);
    }
    function settings() {
        const grid = (value('aiGrid') || '2x3').split('x').map(Number);
        const configured = {
            product_hint:value('aiProductHint'), style:value('aiStyle') || 'minimal',
            rows:grid[0] || 2, cols:grid[1] || 3, strategy:value('aiStrategy') || 'grid',
            hero_count:Math.min(10,Math.max(1,Number.parseInt(value('aiHeroCount') || '6',10))),
            generation_rounds:Number.parseInt(value('aiGenerationRounds') || '0',10),
            round_strategy:value('aiRoundStrategy') || 'auto',
            auto_module_plan:checked('aiAutoModulePlan'),
            detail_modules:checked('aiAutoModulePlan') ? (!aiSelectedFiles.length && !aiSourceChanged && !aiDirty && aiResultData && aiResultData.generation_plan ? aiResultData.generation_plan.detail_module_plan : null) : collectModules(),
            selected_specs:selectedSpecs(), variant_references:variantReferences(),
            size_chart_enabled:checked('aiSizeChartModule'), size_chart_specs:collectSizeRows(),
            size_chart_only_selected:checked('aiSizeChartOnlySelected'),
            reference_count:aiSelectedFiles.length || (aiResultData && aiResultData.source_image_count) || 1,
            creative_density:value('aiCreativeDensity') || 'rich', model_scene:checked('aiModelScene'),
            output_size:value('aiImageSize') || '2048x2048',
        };
        return typeof window.aiAdjustPendingSettings==='function'?window.aiAdjustPendingSettings(configured):configured;
    }
    function refreshMode() {
        const url = value('aiImageApiUrl'), key = value('aiImageApiKey'), mode = value('aiReferenceMode') || 'auto';
        if ($('aiConnectionLabel')) $('aiConnectionLabel').textContent = url ? '本次服务已填写' : '本地演示';
        if ($('aiRunModeHint')) $('aiRunModeHint').textContent = url ? (mode === 'generation' ? '当前选择生成接口，不会发送实物照片，可能无法保持商品一致性。' : `本次参考照片会发送到所填图片服务商。密钥只保留在当前页面${key ? '，刷新后需重填' : '；尚未填写Key'}。`) : '当前没有填写图片 API；生成将使用离线演示，不代表当前商品的 AI 成品。';
    }
    function renderPlan(plan) {
        const main = Number(plan.main_count ?? plan.hero_count ?? 0), detail = Number(plan.detail_count ?? 0), spec = Number(plan.spec_count ?? 0);
        const requests = Number(plan.planned_image_requests ?? plan.provider_requests ?? plan.generation_rounds ?? 0);
        if ($('aiPlanSummary')) $('aiPlanSummary').textContent = `主图 ${main} 张 · 详情 ${detail} 张 · 规格 ${spec} 张`;
        if ($('aiPlanCost')) $('aiPlanCost').textContent = `预计 ${requests} 次图片请求；本地裁剪和精确尺码排版不计图片请求。金额以服务商账单为准。`;
        if ($('aiPlanStatus')) $('aiPlanStatus').textContent = plan.module_plan_source === 'prompt_ai' ? '提示词 AI 推荐' : checked('aiAutoModulePlan') ? '类目预估，识别后更新' : '自定义方案';
        const cap = Number.parseInt(value('aiMaxRequests') || '0',10);
        const warnings = Array.isArray(plan.warnings) ? plan.warnings.slice() : [];
        (plan.detail_module_plan||[]).filter(item=>item.note).forEach(item=>warnings.push(`${item.label||ROLE_LABELS[item.type]||item.type}：${item.note}`));
        if (cap > 0 && requests > cap) warnings.unshift(`方案需要 ${requests} 次请求，超过你设置的 ${cap} 次上限；请调整后再生成。`);
        if ($('aiPlanWarnings')) $('aiPlanWarnings').innerHTML = warnings.map(text => `<p>${esc(text)}</p>`).join('');
        if ($('aiPlanBatches')) $('aiPlanBatches').innerHTML = (plan.round_plan || []).map(batch => `<li>第 ${esc(batch.round)} 批：${esc((batch.modules || []).map(type => ROLE_LABELS[type] || type).join('、'))} · ${esc(batch.rows)}×${esc(batch.cols)}${batch.output_size ? ' · '+esc(batch.output_size) : ''}${batch.native_effective_min_edge?' · 裁后原生至少 '+esc(batch.native_effective_min_edge)+'px':''}</li>`).join('');
        if (checked('aiAutoModulePlan')) {
            const mapped = {scene:'lifestyle',proof:'feature_macro',usage:'steps',hook:'carousel_sell',benefit:'carousel_sell'};
            const totals = {};
            (plan.detail_module_plan || plan.module_plan || []).forEach(item => { const type=mapped[item.type] || item.type; totals[type]=(totals[type] || 0)+Number(item.count || 0); });
            document.querySelectorAll('#aiDetailModules .ai-module-choice').forEach(input => {
                if (input.id === 'aiSizeChartModule') return;
                input.checked = !!totals[input.dataset.module];
                const count = document.querySelector(`[data-module-count="${input.dataset.module}"]`);
                if (count && totals[input.dataset.module]) count.value = totals[input.dataset.module];
            });
        }
    }
    async function updatePlan() {
        const sequence = ++aiPlanSequence;
        if ($('aiPlanStatus')) $('aiPlanStatus').textContent = '正在计算';
        try {
            const response = await fetch('/api/ai/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings())});
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || '无法计算图片方案');
            if (sequence !== aiPlanSequence) return null;
            aiPlan = data.plan || data; aiPlanError = ''; renderPlan(aiPlan); return aiPlan;
        } catch (error) {
            if (sequence !== aiPlanSequence) return null;
            aiPlan = null; aiPlanError = error.message;
            if ($('aiPlanStatus')) $('aiPlanStatus').textContent = '方案暂不可用';
            if ($('aiPlanWarnings')) $('aiPlanWarnings').textContent = error.message;
            if ($('aiPlanSummary')) $('aiPlanSummary').textContent = '暂无法确定图片数量';
            return null;
        }
    }
    function schedulePlan() { clearTimeout(aiPlanTimer); aiPlanTimer=setTimeout(updatePlan,300); }
    function refreshModuleFields() {
        if ($('aiSizeChartFields')) $('aiSizeChartFields').style.display=checked('aiSizeChartModule') ? 'block' : 'none';
        renderSizeTable(); schedulePlan();
    }
    function setBusy(busy) {
        aiBusy = busy;
        if ($('aiGenerateButton')) { $('aiGenerateButton').disabled=busy; $('aiGenerateButton').textContent=busy ? '正在生成，请稍候' : '生成图片与商品资料'; }
        if ($('aiCancelTask')) $('aiCancelTask').hidden=!busy;
        ['aiUploadSection','aiPlanSection'].forEach(id => { if ($(id)) $(id).querySelectorAll('input,select,textarea,button').forEach(input => { input.disabled=busy; }); });
        document.querySelectorAll('.ai-regenerate-button').forEach(button => { button.disabled=busy || button.dataset.unavailable==='true'; });
        document.querySelectorAll('[data-action="recrop"]').forEach(button => { button.disabled=busy || button.dataset.unavailable==='true'; });
        if(typeof window.aiUpdatePrimaryAction==='function')window.aiUpdatePrimaryAction(busy);
    }
    function addProviderFields(fd) { PROVIDER_FIELDS.forEach(([field,id]) => { if (value(id)) fd.append(field,value(id)); }); }
    function requestId() { return typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `ai-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
    function syncEditor() {
        if (!aiResultData) return;
        aiResultData.title=value('aiTitle'); aiResultData.description=value('aiDescription');
        aiResultData.brand=value('aiBrand'); aiResultData.category=value('aiCategory');
        aiResultData.attrs=Object.fromEntries(aiAttrRows.filter(row=>row.name.trim()).map(row=>[row.name.trim(),row.value.trim()]));
        aiResultData.care_instructions=splitValues(value('aiCare'));
        if ($('aiAttrs')) $('aiAttrs').value=JSON.stringify(aiResultData.attrs,null,2);
        persistAiResult();
    }
    function rememberPrevious() {
        if (aiResultData) { syncEditor(); aiPreviousData=clone(aiResultData); storeJson(AI_PREVIOUS_STORAGE_KEY,aiPreviousData); }
    }
    function replaceResult(data) {
        aiResultData=data; aiResultData.status='done'; aiDirty=false; aiSourceChanged=false;
        if ($('aiDirtyNotice')) $('aiDirtyNotice').hidden=true;
        persistAiResult(); renderResult();
        if (data.plan || data.generation_plan) { aiPlan=data.plan || data.generation_plan; renderPlan(aiPlan); }
    }
    async function pollTask(taskId, token) {
        let errors=0;
        while (token === aiPollToken) {
            try {
                const response=await fetch(`/api/one-click/${encodeURIComponent(taskId)}`);
                const data=await response.json();
                if (response.status === 404) throw Object.assign(new Error('任务已过期或不存在；上一版成品仍保留。'),{terminal:true});
                if (!response.ok) throw new Error(data.detail || '任务状态读取失败');
                errors=0;
                setState(data.progress || (data.status === 'done' ? '生成完成' : '正在执行任务'));
                if (data.status === 'done') {
                    if(data.stage==='awaiting_confirmation' && typeof window.aiHandleProposal==='function'){
                        window.aiHandleProposal(data);localStorage.removeItem(AI_TASK_STORAGE_KEY);aiTaskId=null;
                        setState('识图与策划已完成；请检查规格、文案和每张提示词，确认后才会生图。');return data;
                    }
                    replaceResult(data); localStorage.removeItem(AI_TASK_STORAGE_KEY); aiTaskId=null;
                    setState('生成完成。文件检查与商品内容审核分开，请逐张核对后采用。'); notify('图片已就绪，请人工审核'); return data;
                }
                if (['error','failed','cancelled','interrupted','needs_attention','billing_unknown','needs_reconciliation'].includes(data.status)) {
                    if(data.proposal_id && typeof window.aiHandleProposalFailure==='function')window.aiHandleProposalFailure(data);
                    setState(data.message || data.error || `任务状态：${data.status}；上一版成品已保留。`);
                    if ($('aiResumeTask')) $('aiResumeTask').hidden=data.can_resume===false || !['interrupted','needs_attention','error','failed','cancelled'].includes(data.status);
                    return data;
                }
            } catch (error) {
                errors+=1;
                if (error.terminal || errors>=5) {
                    setState(error.message+' 可以稍后恢复任务，上一版成品仍保留。');
                    if ($('aiResumeTask')) $('aiResumeTask').hidden=false;
                    return {status:'error',message:error.message};
                }
                setState(`暂时无法读取任务，正在重连（${errors}/5）`);
            }
            await new Promise(resolve=>setTimeout(resolve,1200));
        }
        return {status:'cancelled'};
    }
    async function submitTask(url, fd) {
        if (aiBusy) return;
        rememberPrevious(); setBusy(true); ++aiPollToken;
        const token=aiPollToken;
        try {
            if(!fd.has('client_request_id'))fd.append('client_request_id',requestId());
            const response=await fetch(url,{method:'POST',body:fd});
            const data=await response.json();
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '提交失败，请检查配置');
            if (data.status === 'done' && !data.task_id) { replaceResult(data); return data; }
            if (!data.task_id) throw new Error('接口没有返回任务编号');
            aiTaskId=data.task_id;
            localStorage.setItem(AI_TASK_STORAGE_KEY,JSON.stringify({task_id:aiTaskId,started_at:Date.now()}));
            return await pollTask(aiTaskId,token);
        } catch (error) { setState('未完成：'+error.message+'；上一版成品仍保留。'); notify('本次任务未完成'); return {status:'error',message:error.message}; }
        finally { setBusy(false); }
    }
    window.runAiWorkflow = async function () {
        if (aiBusy || aiStarting) return;
        if (!aiSelectedFiles.length) { notify('请先选择或拖入商品照片'); $('aiDropZone').focus(); return; }
        aiStarting=true;
        if ($('aiGenerateButton')) $('aiGenerateButton').disabled=true;
        try {
        syncEditor();
        const plan=await updatePlan();
        if (!plan) { setState(aiPlanError || '出图计划不可用，请稍后重试'); return; }
        const required=Number(plan.planned_image_requests ?? plan.provider_requests ?? 0), explicitCap=Number.parseInt(value('aiMaxRequests') || '0',10);
        if (explicitCap>0 && required>explicitCap) { setState(`需要 ${required} 次图片请求，超过 ${explicitCap} 次预算上限。请减少图片或调整上限。`); $('aiMaxRequests').focus(); return; }
        const fd=new FormData(), config=settings();
        aiSelectedFiles.forEach(file=>fd.append('images',file));
        ['product_hint','style','rows','cols','strategy','generation_rounds','round_strategy','hero_count','creative_density'].forEach(field=>fd.append(field,String(config[field])));
        fd.append('auto_module_plan',config.auto_module_plan?'1':'0');
        fd.append('detail_modules_json',JSON.stringify(config.detail_modules));
        fd.append('selected_specs_json',JSON.stringify(config.selected_specs));
        fd.append('variant_references_json',JSON.stringify(config.variant_references));
        fd.append('size_chart_enabled',config.size_chart_enabled?'1':'0');
        fd.append('size_chart_only_selected',config.size_chart_only_selected?'1':'0');
        fd.append('size_chart_specs_json',JSON.stringify(config.size_chart_specs));
        fd.append('model_scene',config.model_scene?'1':'0'); fd.append('ad_copy',value('aiAdCopy'));
        fd.append('max_image_requests',String(explicitCap>0?explicitCap:required));
        fd.append('output_size',config.output_size);
        if (aiCreativeBriefOverride) fd.append('creative_brief_override',JSON.stringify(aiCreativeBriefOverride));
        addProviderFields(fd); setState('正在提交照片与已锁定出图计划');
        await submitTask('/api/ai/workflow',fd);
        } finally { aiStarting=false;if(!aiBusy && $('aiGenerateButton'))$('aiGenerateButton').disabled=false; }
    };
    window.aiCancelTask=async function () {
        if (!aiTaskId) return;
        try {
            const response=await fetch(`/api/ai/tasks/${encodeURIComponent(aiTaskId)}/cancel`,{method:'POST'});
            const data=await response.json(); if (!response.ok) throw new Error(data.detail || '取消失败');
            setState('已请求取消。已发出的请求可能仍在服务商执行；将停止后续未发送批次。');
        } catch(error) { setState(error.message); }
    };
    window.aiResumeTask=async function () {
        if (aiBusy || !aiTaskId) { notify('没有可恢复的任务'); return; }
        const fd=new FormData(); addProviderFields(fd); setState('正在恢复已保存任务，已完成模块不会重复执行');
        await submitTask(`/api/ai/tasks/${encodeURIComponent(aiTaskId)}/resume`,fd);
    };
    async function recoverTask() {
        let saved;
        try { saved=JSON.parse(localStorage.getItem(AI_TASK_STORAGE_KEY) || 'null'); } catch(_error) { saved=null; }
        if (!saved || !saved.task_id) {
            try {
                const response=await fetch('/api/ai/tasks/active'); if (!response.ok) return;
                const data=await response.json(); const tasks=Array.isArray(data)?data:(data.tasks || []);
                saved=tasks.find(task=>['running','pending','queued','interrupted','needs_attention','needs_reconciliation'].includes(task.status));
            } catch(_error) { return; }
        }
        if (!saved || !saved.task_id) return;
        aiTaskId=saved.task_id; setBusy(true); const token=++aiPollToken;
        await pollTask(aiTaskId,token); setBusy(false);
    }
    function normalizedOutputs() {
        if (!aiResultData) return [];
        if (Array.isArray(aiResultData.module_outputs) && aiResultData.module_outputs.length) return aiResultData.module_outputs;
        const outputs=[];
        [['main_images','hero'],['detail_images','detail']].forEach(([field,kind]) => (aiResultData[field] || []).forEach((path,index)=>outputs.push({id:`legacy-${kind}-${index}`,kind,path,label:`${KIND_LABELS[kind]} ${index+1}`,needs_review:true,prompt:'',legacy:true})));
        Object.entries(aiResultData.spec_images || {}).forEach(([spec_value,path],index)=>outputs.push({id:`legacy-spec-${index}`,kind:'spec',path,spec_value,label:`${spec_value}规格图`,needs_review:true,prompt:'',legacy:true}));
        aiResultData.module_outputs=outputs;
        return outputs;
    }
    function rebuildImageCollections() {
        const outputs=normalizedOutputs().filter(module=>module.adopted!==false && module.path);
        aiResultData.main_images=outputs.filter(module=>module.kind==='hero').map(module=>module.path);
        aiResultData.detail_images=outputs.filter(module=>module.kind==='detail').map(module=>module.path);
        const verified={};
        outputs.filter(module=>['spec','sku'].includes(module.kind) && module.spec_value && Array.isArray(module.reference_indices) && module.reference_indices.length).forEach(module=> { verified[module.spec_value]=module.path; });
        aiResultData.spec_images=verified;
    }
    function moduleById(id) { return normalizedOutputs().find(module=>module.id===id); }
    function cropDefaults(module) {
        const raw=module.crop_box || {},box=Array.isArray(raw)?{left:raw[0],top:raw[1],right:raw[2],bottom:raw[3]}:raw;
        const width=Number(module.source_width || module.sheet_width || (module.source_size||[])[0]),height=Number(module.source_height || module.sheet_height || (module.source_size||[])[1]);
        if(width>0 && height>0 && [box.left,box.top,box.right,box.bottom].every(item=>Number.isFinite(Number(item)))) {
            return {left:Number(box.left)/width,top:Number(box.top)/height,right:Number(box.right)/width,bottom:Number(box.bottom)/height};
        }
        return {left:0,top:0,right:1,bottom:1};
    }
    function imageCard(module,index) {
        const id=esc(module.id), kind=module.kind, label=module.label || ROLE_LABELS[module.module_type] || KIND_LABELS[kind] || '商品图片';
        const actualSource=module.generation_kind || aiResultData.generation_kind;
        const sourceLabel=actualSource==='ai'?'AI生成':actualSource==='verified_table' || module.render_source==='verified_table'?'实测数据排版':actualSource==='offline_demo'?'离线演示':actualSource==='demo_asset'?'演示素材':'请核对来源';
        const validRefs=Array.isArray(module.reference_indices)?module.reference_indices:[];
        const crop=cropDefaults(module);
        return `<article class="ai-image-card ${module.adopted===false?'is-excluded':''}" data-kind="${esc(kind)}" data-module-id="${id}" draggable="true"><div class="ai-image-card-header"><b>${index+1}. ${esc(label)}</b><span>${esc(sourceLabel)}</span></div>${module.path?`<img src="${esc(imageUrl(module.path))}" alt="${esc(label)}" data-open-image="${id}" loading="lazy">`:'<div class="ai-image-missing">待生成 / 待补充</div>'}<div class="ai-image-caption">${module.spec_value?`规格：${esc(module.spec_value)} · `:''}${validRefs.length?`参考照片 ${validRefs.join('、')}`:'请核对参考照片'}<span class="ai-review-tag">${module.adopted===false?'不采用':module.needs_review===false?'已人工核对':'待人工核对'}</span></div><div class="ai-image-card-actions"><button type="button" data-action="review" data-module="${id}">${module.needs_review===false?'标为待审核':'已核对'}</button>${kind==='hero'?`<button type="button" data-action="first" data-module="${id}">设为首图</button>`:''}<button type="button" data-action="up" data-module="${id}" aria-label="上移${esc(label)}">上移</button><button type="button" data-action="down" data-module="${id}" aria-label="下移${esc(label)}">下移</button><button type="button" data-action="toggle" data-module="${id}">${module.adopted===false?'恢复采用':'不采用'}</button><button type="button" data-action="compare" data-module="${id}">参考对照</button>${module.path?`<button type="button" data-action="download" data-module="${id}">下载</button>`:''}</div><details><summary>本图提示词与调整</summary><p>本图下次重生提示词</p><textarea data-module-prompt="${id}" aria-label="${esc(label)}提示词">${esc(module.prompt || '')}</textarea><details class="ai-actual-prompt"><summary>本批实际请求提示词</summary><pre>${esc(module.batch_prompt || module.prompt || '旧记录未保存实际提示词')}</pre></details><button type="button" class="ai-regenerate-button" data-action="regenerate" data-module="${id}" ${module.legacy?'disabled data-unavailable="true" title="旧结果没有模块记录，请先生成新商品包"':''}>仅重生本图（最多1次图片请求）</button><details class="ai-crop-controls"><summary>手动调整裁剪边界</summary>${module.source_sheet?`<img class="ai-crop-source" src="${esc(imageUrl(module.source_sheet))}" alt="原始生成画布，供调整裁剪范围">`:''}<p>相对${module.source_sheet?'上方原始画布':'当前图片'}填写，0为左 / 上边，1为右 / 下边。请确保范围只包含本图。</p><div class="ai-crop-grid">${[['left',0],['top',0],['right',1],['bottom',1]].map(([side,initial])=>`<label>${({left:'左',top:'上',right:'右',bottom:'下'})[side]}<input type="number" data-crop-module="${id}" data-crop-side="${side}" min="0" max="1" step="0.01" value="${Number(crop[side]).toFixed(4)}"></label>`).join('')}</div><button type="button" data-action="recrop" data-module="${id}" ${module.legacy?'disabled data-unavailable="true"':''}>应用裁剪（不调用生图API）</button></details></details></article>`;
    }
    function renderImages() {
        const outputs=normalizedOutputs();
        [['hero','aiMainPreview'],['detail','aiDetailPreview'],['spec','aiSpecPreview']].forEach(([kind,id])=> {
            if (!$(id)) return;
            const modules=outputs.filter(module=>module.kind===kind || (kind==='spec' && module.kind==='sku'));
            $(id).innerHTML=modules.length?modules.map(imageCard).join(''):'<p class="hint">本次没有该类图片。</p>';
        });
        const missing=aiResultData.spec_preview_missing || aiResultData.sku_preview_missing || [];
        if ($('aiSpecMissing')) $('aiSpecMissing').textContent=missing.length?'待补充对应实物参考：'+missing.join('、'):'';
        document.querySelectorAll('#aiResult [data-action]').forEach(button=>button.addEventListener('click',()=>imageAction(button.dataset.action,button.dataset.module)));
        document.querySelectorAll('#aiResult [data-open-image]').forEach(image=>image.addEventListener('click',()=> { if (typeof openLightbox==='function') openLightbox(image.src); }));
        document.querySelectorAll('#aiResult [data-module-prompt]').forEach(input=>input.addEventListener('change',()=>{ const module=moduleById(input.dataset.modulePrompt); if(module){module.prompt=input.value; persistAiResult();} }));
        let dragged=null;
        document.querySelectorAll('#aiResult .ai-image-card').forEach(card=> {
            card.addEventListener('dragstart',event=> { if (event.target.closest('textarea,input,button')) {event.preventDefault();return;} dragged=card.dataset.moduleId; event.dataTransfer.setData('text/plain',dragged); });
            card.addEventListener('dragover',event=>event.preventDefault());
            card.addEventListener('drop',event=> {event.preventDefault(); const target=moduleById(card.dataset.moduleId), origin=moduleById(dragged); if(!origin||!target||origin.kind!==target.kind)return; const array=normalizedOutputs();const from=array.indexOf(origin),to=array.indexOf(target);array.splice(from,1);array.splice(to,0,origin);rebuildImageCollections();persistAiResult();renderImages();});
        });
    }
    async function imageAction(action,id) {
        const module=moduleById(id); if(!module)return;
        if (action==='download') { if(typeof downloadSingleImage==='function') downloadSingleImage(encodeURIComponent(module.path));return; }
        if (action==='compare') {showReferenceComparison(module);return;}
        if (action==='regenerate' || action==='recrop') {
            if(aiBusy)return;
            syncEditor();
            const textarea=Array.from(document.querySelectorAll('[data-module-prompt]')).find(input=>input.dataset.modulePrompt===id);
            if(textarea)module.prompt=textarea.value;
            const fd=new FormData();fd.append('source_job_id',aiResultData.job_id);fd.append('module_id',id);fd.append('prompt',module.prompt||'');
            if(action==='recrop'){
                const box={};document.querySelectorAll('[data-crop-module]').forEach(input=>{if(input.dataset.cropModule===id)box[input.dataset.cropSide]=Number(input.value);});
                if(!Number.isFinite(box.left)||!Number.isFinite(box.top)||!Number.isFinite(box.right)||!Number.isFinite(box.bottom)||box.left<0||box.top<0||box.right>1||box.bottom>1||box.right<=box.left||box.bottom<=box.top){notify('裁剪范围需在0到1之间，右边大于左边、下边大于上边');return;}
                fd.append('box_json',JSON.stringify(box));
            }else{addProviderFields(fd);}
            setState(action==='recrop'?'正在调整本图裁剪边界，不调用生图API':'正在仅重生选中的图片，其他图片保留');
            const preserved=clone(aiResultData);
            const response=await submitTask(action==='recrop'?'/api/ai/recrop':'/api/ai/regenerate',fd);
            if(response && response.status==='done'){
                // A server revision is based on its saved manifest. Retain all
                // unsaved merchant edits and adoption/order from this page.
                const generated=moduleById(id);
                const returned=normalizedOutputs();
                const ordered=(preserved.module_outputs||[]).map(old=>{
                    const current=returned.find(item=>item.id===old.id);if(!current)return old;
                    return old.id===id?{...old,...current,adopted:old.adopted,needs_review:true}:{...current,...old,path:current.path};
                });
                aiResultData.module_outputs=ordered.length?ordered:returned;
                ['title','description','brand','category','attrs','specs','specs_confirmed','specs_user_modified','skus'].forEach(field=>{if(preserved[field]!==undefined)aiResultData[field]=preserved[field];});
                if(generated)generated.needs_review=true;
                rebuildImageCollections();persistAiResult();renderResult();
            }
            return;
        }
        if(action==='review')module.needs_review=module.needs_review===false;
        if(action==='toggle')module.adopted=module.adopted===false;
        if(['up','down','first'].includes(action)){
            const array=normalizedOutputs(),siblings=array.filter(item=>item.kind===module.kind),index=siblings.indexOf(module);
            let other=action==='first'?siblings[0]:siblings[index+(action==='up'?-1:1)];
            if(other&&other!==module){const from=array.indexOf(module),to=array.indexOf(other);array.splice(from,1);array.splice(to,0,module);}
        }
        rebuildImageCollections();persistAiResult();renderImages();
    }
    function showReferenceComparison(module) {
        let dialog=$('aiComparisonDialog');
        if(!dialog){dialog=document.createElement('dialog');dialog.id='aiComparisonDialog';dialog.className='ai-comparison-dialog';document.body.appendChild(dialog);}
        const refs=(module.reference_indices || []).map(index=>(aiResultData.source_images || aiResultData.input_images || [])[index-1]).filter(Boolean);
        dialog.innerHTML=`<div class="ai-dialog-heading"><h3>${esc(module.label||'图片')} · 实物参考对照</h3><button type="button" data-close-dialog>关闭</button></div><div class="ai-comparison-grid"><figure><figcaption>生成结果</figcaption><img src="${esc(imageUrl(module.path))}" alt="生成结果"></figure>${refs.map((path,index)=>`<figure><figcaption>实物参考 ${index+1}</figcaption><img src="${esc(imageUrl(path))}" alt="实物参考"></figure>`).join('')}</div><p>请人工核对颜色、结构、印花和文字；文件检查通过不代表商品内容正确。</p>`;
        dialog.querySelector('[data-close-dialog]').addEventListener('click',()=>dialog.close());
        if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');
    }
    window.aiShowMobilePreview=function(){
        if(!aiResultData)return;
        let dialog=$('aiMobileDialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='aiMobileDialog';dialog.className='ai-mobile-dialog';document.body.appendChild(dialog);}
        dialog.innerHTML=`<div class="ai-dialog-heading"><h3>手机详情预览</h3><button type="button" data-close-dialog>关闭</button></div><div class="ai-phone-preview"><h4>${esc(aiResultData.title||'商品标题')}</h4>${(aiResultData.detail_images||[]).map(path=>`<img src="${esc(imageUrl(path))}" alt="详情图">`).join('')}</div>`;
        dialog.querySelector('[data-close-dialog]').addEventListener('click',()=>dialog.close());dialog.showModal();
    };
    function skuSignature(dimensions,name) {
        return dimensions && Object.keys(dimensions).length ? JSON.stringify(Object.entries(dimensions).sort(([a],[b])=>a.localeCompare(b))) : 'legacy:'+String(name||'默认');
    }
    function mergeSkus(specs,existing) {
        if(typeof window.mergeSkusForSpecs==='function')return window.mergeSkusForSpecs(specs,existing||[]);
        const entries=Object.entries(specs||{}).filter(([,values])=>Array.isArray(values)&&values.length);
        const rows=[];
        const walk=(index,dimensions)=>{
            if(rows.length>=200)return;
            if(index===entries.length){
                const name=Object.values(dimensions).join(' ')||'默认',signature=skuSignature(dimensions,name);
                const old=(existing||[]).find(row=>row.id===signature||skuSignature(row.spec_values||row.dimensions,row.name)===signature||row.name===name);
                rows.push({...old,id:old&&old.id||signature,spec_values:dimensions,name,price:old?old.price:0,single_price:old?old.single_price:0,stock:old?old.stock:0,enabled:old?old.enabled!==false:true,spec_code:old?String(old.spec_code||''):String(rows.length+1)});return;
            }
            entries[index][1].forEach(v=>walk(index+1,{...dimensions,[entries[index][0]]:String(v)}));
        };walk(0,{});return rows;
    }
    function preserveListingEdits() {
        if(!aiResultData)return;
        if(typeof window.getListingProductPackage==='function') {
            const latest=window.getListingProductPackage();
            if(latest && String(latest.job_id||latest.id)===String(aiResultData.job_id||aiResultData.id)) {
                ['skus','specs','specs_confirmed','specs_user_modified'].forEach(key=>{if(latest[key]!==undefined)aiResultData[key]=clone(latest[key]);});
                return;
            }
        }
        const source=$('migrateUrl');
        if(source && source.value===`ai://${aiResultData.job_id}` && typeof skuData!=='undefined' && Array.isArray(skuData)) aiResultData.skus=clone(skuData);
    }
    function renderSpecEditor() {
        const box=$('aiSpecEditor');if(!box)return;
        const current=aiResultData.specs_user_modified ? aiResultData.specs||{} : aiResultData.specs_confirmed||{};
        const suggestions=aiResultData.spec_suggestions || (aiResultData.metadata||{}).spec_suggestions || {};
        const names=Array.from(new Set(['颜色','尺码',...Object.keys(current),...Object.keys(suggestions)]));
        box.innerHTML='<b>颜色 / 尺码与 SKU</b><p class="hint">候选值需按实物确认。变动只新增或移除组合，保留已有组合的价格与库存。</p>'+names.map((name,index)=>{
            const options=Array.from(new Set([...(current[name]||[]),...(suggestions[name]||[])])).map(String);
            return `<div class="ai-spec-dimension"><b>${esc(name)}</b><div>${options.map((v,i)=>`<label class="ai-spec-chip"><input type="checkbox" data-spec-name="${index}" data-spec-value="${i}" ${(current[name]||[]).includes(v)?'checked':''}>${esc(v)}</label>`).join('')}</div><div class="ai-spec-add"><input data-custom-dimension="${index}" aria-label="添加${esc(name)}" placeholder="添加实际${esc(name)}"><button type="button" data-add-dimension="${index}">添加</button></div></div>`;
        }).join('');
        box.querySelectorAll('[data-spec-name]').forEach(input=>input.addEventListener('change',()=>{
            const name=names[Number(input.dataset.specName)],options=Array.from(new Set([...(current[name]||[]),...(suggestions[name]||[])])).map(String),v=options[Number(input.dataset.specValue)];
            const specs=clone(current),next=new Set(specs[name]||[]);if(input.checked)next.add(v);else next.delete(v);if(next.size)specs[name]=Array.from(next);else delete specs[name];updateResultSpecs(specs);
        }));
        box.querySelectorAll('[data-add-dimension]').forEach(button=>button.addEventListener('click',()=>{
            const idx=Number(button.dataset.addDimension),name=names[idx],input=box.querySelector(`[data-custom-dimension="${idx}"]`);const vals=splitValues(input.value);if(!vals.length)return;const specs=clone(current);specs[name]=Array.from(new Set([...(specs[name]||[]),...vals]));updateResultSpecs(specs);
        }));
    }
    function updateResultSpecs(specs) {
        preserveListingEdits();aiResultData.specs=specs;aiResultData.specs_user_modified=true;aiResultData.skus=mergeSkus(specs,aiResultData.skus||[]);
        const colors=Object.entries(specs).find(([name])=>/色|款式|花色|配色/.test(name));
        const allowed=new Set(colors?colors[1]:[]);
        aiResultData.spec_images=Object.fromEntries(Object.entries(aiResultData.spec_images||{}).filter(([key])=>allowed.has(key)));
        if($('aiSelectedColors'))$('aiSelectedColors').value=colors?colors[1].join('、'):'';
        const sizes=Object.entries(specs).find(([name])=>/尺码|鞋码|尺寸|size/i.test(name));if($('aiSelectedSizes'))$('aiSelectedSizes').value=sizes?sizes[1].join('、'):'';
        renderSizeTable();persistAiResult();renderSpecEditor();schedulePlan();
        notify('规格已更新；已有 SKU 的价格、库存和编码已保留。尺码图片需按新规格重新生成。');
    }
    function renderAttributes() {
        const box=$('aiAttrsEditor');if(!box)return;
        box.innerHTML=aiAttrRows.map((row,index)=>`<div class="ai-attribute-row"><input data-attr-name="${index}" aria-label="属性名称" value="${esc(row.name)}" placeholder="属性名称"><input data-attr-value="${index}" aria-label="属性值" value="${esc(row.value)}" placeholder="属性值"><button type="button" data-attr-remove="${index}" aria-label="删除属性">×</button></div>`).join('');
        box.querySelectorAll('[data-attr-name]').forEach(input=>input.addEventListener('input',()=>{aiAttrRows[Number(input.dataset.attrName)].name=input.value;syncEditor();}));
        box.querySelectorAll('[data-attr-value]').forEach(input=>input.addEventListener('input',()=>{aiAttrRows[Number(input.dataset.attrValue)].value=input.value;syncEditor();}));
        box.querySelectorAll('[data-attr-remove]').forEach(button=>button.addEventListener('click',()=>{aiAttrRows.splice(Number(button.dataset.attrRemove),1);renderAttributes();syncEditor();}));
    }
    window.aiAddAttribute=function(){aiAttrRows.push({name:'',value:''});renderAttributes();const inputs=$('aiAttrsEditor').querySelectorAll('[data-attr-name]');if(inputs.length)inputs[inputs.length-1].focus();};
    function renderResult() {
        if(!aiResultData)return;
        if($('aiResult'))$('aiResult').style.display='block';
        const metadata=aiResultData.metadata||{};
        [['aiTitle','title'],['aiDescription','description'],['aiBrand','brand'],['aiCategory','category']].forEach(([id,field])=>{if($(id))$(id).value=aiResultData[field]||metadata[field]||'';});
        const attrs=aiResultData.attrs||metadata.attrs||{};aiAttrRows=Object.entries(attrs).map(([name,val])=>({name,value:String(val)}));renderAttributes();
        if($('aiAttrs'))$('aiAttrs').value=JSON.stringify(attrs,null,2);
        if($('aiCare'))$('aiCare').value=(aiResultData.care_instructions||metadata.care_instructions||[]).join('\n');
        const audit=aiResultData.quality_audit||{},main=audit.main||{},detail=audit.detail||{};
        const provenance=aiResultData.generation_kind==='ai'?'已调用真实图片服务':aiResultData.generation_kind==='gpt_builtin_demo'?'GPT演示通道':'离线 / 固定素材演示，不代表本商品AI成品';
        if($('aiCostNote'))$('aiCostNote').textContent=`${provenance}。本次实际图片请求 ${aiResultData.provider_calls??'-'} 次，缓存命中 ${aiResultData.cache_hits||0} 次。文件检查：${audit.status==='pass'?'通过':'需要核对'}（主图 ${main.count??(aiResultData.main_images||[]).length} 张，详情 ${detail.count??(aiResultData.detail_images||[]).length} 张）。商品外观和文字仅由人工审核。${aiResultData.cost_note||''}`;
        const nearCount=[audit.main,audit.detail,audit.spec].reduce((total,item)=>total+Number(item&&item.near_duplicate_count||0),0);
        if(nearCount && $('aiCostNote'))$('aiCostNote').textContent+=` 本地检测到${nearCount}组相似画面，请放大判断是否重复；不增加AI费用。`;
        if(metadata.prompt_ai_note && $('aiCostNote'))$('aiCostNote').textContent+=' '+metadata.prompt_ai_note+'。';
        const pack=aiResultData.prompt_pack||{};
        if($('aiPromptPack'))$('aiPromptPack').textContent=JSON.stringify({skill:aiResultData.skill||pack.skill,skill_source:aiResultData.skill_source||pack.skill_source,templates_used:pack.template_ids||[],installed_template_count:aiResultData.skill_template_count||pack.skill_template_count,round_plan:aiResultData.round_plan||[],prompt:pack.prompt||''},null,2);
        const selling=aiResultData.selling_points||metadata.selling_points||[],scenes=aiResultData.scene_suggestions||metadata.scene_suggestions||[],heads=aiResultData.ad_headlines||metadata.ad_headlines||[];
        if($('aiMeta'))$('aiMeta').innerHTML=`<b>场景与资料候选</b><label class="ai-field">卖点<textarea id="aiSellingPointsEdit" class="form-input" rows="2">${esc(selling.join('\n'))}</textarea></label><label class="ai-field">场景<textarea id="aiSceneSuggestionsEdit" class="form-input" rows="2">${esc(scenes.join('\n'))}</textarea></label><label class="ai-field">短标题<textarea id="aiAdHeadlinesEdit" class="form-input" rows="2">${esc(heads.join('\n'))}</textarea></label><button type="button" class="btn btn-secondary" onclick="aiUseCreativeBriefAndRegenerate()">按修改后的候选生成新版</button><p class="hint">商品可见证据：${esc((aiResultData.identity_evidence||metadata.identity_evidence||[]).join('；')||'请对照实物核对')}</p><p class="hint">待确认：${esc((aiResultData.uncertain_features||metadata.uncertain_features||[]).join('；')||'请检查所有生成图')}</p>`;
        renderSpecEditor();renderImages();
        if($('aiRestorePrevious'))$('aiRestorePrevious').hidden=!aiPreviousData;
        if($('aiDirtyNotice')){$('aiDirtyNotice').hidden=!aiDirty;if(aiDirty)$('aiDirtyNotice').textContent='配置已修改；当前仍显示上一版成品。';}
    }
    window.aiRestorePreviousVersion=function(){
        if(!aiPreviousData){notify('没有可恢复的上一版');return;}
        syncEditor();const current=aiResultData;aiResultData=clone(aiPreviousData);aiPreviousData=current?clone(current):null;aiDirty=false;aiSourceChanged=false;persistAiResult();renderResult();notify('已恢复上一版商品包');
    };
    window.aiUseCreativeBriefAndRegenerate=function(){
        const lines=id=>value(id).split(/[\r\n；;]+/).map(item=>item.trim()).filter(Boolean).slice(0,8);
        aiCreativeBriefOverride={selling_points:lines('aiSellingPointsEdit'),scene_suggestions:lines('aiSceneSuggestionsEdit'),ad_headlines:lines('aiAdHeadlinesEdit')};
        markDirty('候选文案已修改，点击生成新版后生效。');window.runAiWorkflow();
    };
    function handoffToListing() {
        if(!aiResultData){notify('请先生成或加载商品包');return false;}
        if(aiSourceChanged){notify('参考照片已更换；请先生成当前商品，或恢复上一版后再上架');return false;}
        syncEditor();preserveListingEdits();rebuildImageCollections();
        const specs=aiResultData.specs_user_modified?aiResultData.specs||{}:aiResultData.specs_confirmed||{};
        aiResultData.specs=specs;aiResultData.skus=mergeSkus(specs,aiResultData.skus||[]);persistAiResult();
        if(typeof window.applyProductPackageToListing==='function'){
            window.applyProductPackageToListing(clone(aiResultData));
        }else{
            scrapedSpecs=clone(specs);specImages=clone(aiResultData.spec_images||{});localMainImages=clone(aiResultData.main_images||[]);localDetailImages=clone(aiResultData.detail_images||[]);mainImageUrls=[];detailImageUrls=[];skuData=clone(aiResultData.skus||[]);
            $('productTitle').value=aiResultData.title||'';$('migrateUrl').value=`ai://${aiResultData.job_id}`;
            window.aiListingMeta={brand:aiResultData.brand||'',attrs:aiResultData.attrs||{},description:aiResultData.description||'',category:aiResultData.category||'',care_instructions:aiResultData.care_instructions||[]};
            ['scrapedSpecs','specImages','localMainImages','localDetailImages','skuData'].forEach(key=>localStorage.setItem(key,JSON.stringify(({scrapedSpecs,specImages,localMainImages,localDetailImages,skuData})[key])));
            if(typeof renderMainImages==='function')renderMainImages();if(typeof renderDetailImages==='function')renderDetailImages();if(typeof renderSkuTable==='function')renderSkuTable();
        }
        return true;
    }
    window.aiContinueToListing=function(){if(!handoffToListing())return;const nav=document.querySelector('.nav-item[data-tab="migrate"]');if(nav)nav.click();if($('skuSection'))$('skuSection').scrollIntoView({behavior:'smooth',block:'start'});};
    window.aiStartListing=async function(){if(!handoffToListing())return;const nav=document.querySelector('.nav-item[data-tab="migrate"]');if(nav)nav.click();if(typeof startListing==='function')await startListing();};
    window.aiGetProductPackage=function(){syncEditor();preserveListingEdits();return aiResultData?clone(aiResultData):null;};
    window.aiSetProductPackage=function(data){if(!data)return;rememberPrevious();replaceResult(clone(data));};
    window.aiSyncListingEdits=function(){preserveListingEdits();persistAiResult();};
    window.aiSaveDraft=async function(){if(!handoffToListing())return;if(typeof saveMigratedProduct==='function')await saveMigratedProduct();else notify('商品资料已保存在当前浏览器，可前往资料页保存');};
    window.onListingProductPackageChanged=function(data){
        if(!aiResultData || !data || String(data.job_id||data.id)!==String(aiResultData.job_id||aiResultData.id))return;
        ['skus','specs','specs_confirmed','specs_user_modified'].forEach(key=>{if(data[key]!==undefined)aiResultData[key]=clone(data[key]);});
        persistAiResult();renderSpecEditor();
    };
    window.aiTestProvider=async function(kind){
        const image=kind==='image';const button=$(image?'aiTestImageProvider':'aiTestPromptProvider');if(button)button.disabled=true;
        try{const response=await fetch('/api/ai/providers/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,url:value(image?'aiImageApiUrl':'aiPromptApiUrl'),api_key:value(image?'aiImageApiKey':'aiPromptApiKey'),model:value(image?'aiImageModel':'aiPromptModel')})});const data=await response.json();if(!response.ok)throw new Error(data.detail||'连接测试失败');notify(data.message||'连接可达。模型是否支持图片编辑仍以实际任务为准。');if($('aiProviderTestResult'))$('aiProviderTestResult').textContent=data.message||'连接可达；此检查不会生成图片。';}catch(error){if($('aiProviderTestResult'))$('aiProviderTestResult').textContent=error.message;notify(error.message);}finally{if(button)button.disabled=false;}
    };
    window.aiClearProviderKeys=function(){['aiImageApiKey','aiPromptApiKey','aiVisionApiKey'].forEach(id=>{if($(id))$(id).value='';});refreshMode();notify('已清除当前页面的 API Key');};
    window.aiBeginNewProduct=function(){
        if(aiBusy||aiStarting){notify('当前任务尚未结束，请先停止或等待完成');return;}
        rememberPrevious();aiResultData=null;aiSelectedFiles=[];aiFileBindings.clear();aiSizeRows.clear();aiCreativeBriefOverride=null;aiDirty=false;aiSourceChanged=false;
        localStorage.removeItem(AI_RESULT_STORAGE_KEY);
        if(typeof resetMigrateForm==='function')resetMigrateForm();
        ['aiSelectedColors','aiSelectedSizes','aiProductHint','aiAdCopy','aiSizeChartSpecs'].forEach(id=>{if($(id))$(id).value='';});
        if($('aiResult'))$('aiResult').style.display='none';if($('aiProgress'))$('aiProgress').style.display='none';
        if(typeof window.aiClearCreativeProposal==='function')window.aiClearCreativeProposal();
        renderFiles();renderSizeTable();schedulePlan();
        if($('aiRestoreLastProduct'))$('aiRestoreLastProduct').hidden=!aiPreviousData;
        notify('已切换到新商品；上一商品已保留，API设置继续使用');$('aiDropZone')?.focus();
    };
    window.aiRestoreLastProduct=function(){if(!aiPreviousData){notify('没有上一商品记录');return;}window.aiRestorePreviousVersion();if(typeof window.aiClearCreativeProposal==='function')window.aiClearCreativeProposal();};
    window.aiProposalAPI={settings,files:()=>aiSelectedFiles.slice(),referenceBindings:variantReferences,submitTask,addProviderFields,renderPlan,renderSizeTable,schedulePlan,markDirty,storeJson,setState,notify,isBusy:()=>aiBusy||aiStarting,
        pollExistingTask:async taskId=>{if(aiBusy)return;setBusy(true);aiTaskId=taskId;localStorage.setItem(AI_TASK_STORAGE_KEY,JSON.stringify({task_id:taskId}));const token=++aiPollToken;try{return await pollTask(taskId,token);}finally{setBusy(false);}},
        applyBindings:bindings=>{aiSelectedFiles.forEach((file,index)=>{if(!aiFileBindings.get(fileKey(file))){const names=Object.entries(bindings||{}).filter(([,indices])=>indices.includes(index+1)).map(([name])=>name);if(names.length)aiFileBindings.set(fileKey(file),names.join('、'));}});renderFiles();},
        applySizeRows:rows=>{aiSizeRows=new Map((rows||[]).map(row=>[row.size,row.measurements||{}]));renderSizeTable();}};
    function restoreResult() {
        try{
            const saved=JSON.parse(localStorage.getItem(AI_RESULT_STORAGE_KEY)||'null'),previous=JSON.parse(localStorage.getItem(AI_PREVIOUS_STORAGE_KEY)||'null');
            aiPreviousData=previous&&previous.job_id?previous:null;
            if(saved && saved.job_id && saved.generation_kind && Array.isArray(saved.detail_images)){
                aiResultData=saved;aiPreviousData=previous&&previous.job_id?previous:null;
                const plan=saved.generation_plan;
                if(plan){
                    if($('aiHeroCount'))$('aiHeroCount').value=String(plan.hero_count||saved.main_images.length);
                    if($('aiImageSize'))$('aiImageSize').value=plan.output_size||'2048x2048';
                    if($('aiStrategy'))$('aiStrategy').value=plan.strategy||'grid';
                    if($('aiStyle'))$('aiStyle').value=saved.style||'minimal';
                    if($('aiAutoModulePlan'))$('aiAutoModulePlan').checked=true;
                    renderPlan(plan);
                    if(plan.module_plan_source==='user' && $('aiAutoModulePlan'))$('aiAutoModulePlan').checked=false;
                    if($('aiSizeChartModule'))$('aiSizeChartModule').checked=(plan.detail_module_plan||[]).some(item=>item.type==='size_chart'&&item.count>0);
                    aiSizeRows=new Map((saved.size_chart_rows||[]).map(row=>[row.size,row.measurements||{}]));
                    if($('aiSizeColumns') && aiSizeRows.size)$('aiSizeColumns').value=[...new Set((saved.size_chart_rows||[]).flatMap(row=>Object.keys(row.measurements||{})))].join('、');
                    const specs=saved.specs_confirmed||saved.specs||{};
                    if($('aiSelectedColors'))$('aiSelectedColors').value=Object.entries(specs).filter(([key])=>/色|款式/.test(key)).flatMap(([,values])=>values).join('、');
                    if($('aiSelectedSizes'))$('aiSelectedSizes').value=Object.entries(specs).filter(([key])=>/尺码|尺寸|码数/.test(key)).flatMap(([,values])=>values).join('、');
                }
                renderResult();notify('已恢复商品包；实际图片内容仍需人工审核');
            }
        }catch(_error){notify('上次浏览器缓存无法恢复，可从商品列表重新加载');}
    }
    document.addEventListener('DOMContentLoaded',()=>{
        initUploader();restoreResult();refreshMode();refreshModuleFields();
        if($('aiRestoreLastProduct'))$('aiRestoreLastProduct').hidden=!aiPreviousData;
        ['aiProductHint','aiStyle','aiGrid','aiStrategy','aiGenerationRounds','aiRoundStrategy','aiAutoModulePlan','aiCreativeDensity','aiModelScene','aiAdCopy','aiHeroCount','aiMaxRequests','aiImageSize'].forEach(id=>{
            const input=$(id);if(input)input.addEventListener(input.tagName==='SELECT'||input.type==='checkbox'?'change':'input',()=>markDirty('出图设置已修改，当前成品保留至新版成功。'));
        });
        ['aiSelectedColors','aiSelectedSizes','aiSizeChartOnlySelected','aiSizeColumns','aiSizeChartSpecs'].forEach(id=>{const input=$(id);if(input)input.addEventListener('input',()=>{renderSizeTable();markDirty('实际规格或测量数据已修改，上一版图片仍保留。');});});
        document.querySelectorAll('#aiDetailModules .ai-module-choice,#aiDetailModules .ai-module-count').forEach(input=>input.addEventListener('change',()=>{if(input.id!=='aiSizeChartModule'&&$('aiAutoModulePlan'))$('aiAutoModulePlan').checked=false;refreshModuleFields();markDirty('图片模块已调整，生成新版后生效。');}));
        PROVIDER_FIELDS.forEach(([,id])=>{const input=$(id);if(input)input.addEventListener('input',()=>{refreshMode();markDirty('本次服务配置已修改；上一版不会被删除。',false,'provider');});});
        ['aiTitle','aiDescription','aiBrand','aiCategory','aiCare'].forEach(id=>{if($(id))$(id).addEventListener('input',syncEditor);});
        if($('aiAttrs'))$('aiAttrs').addEventListener('change',()=>{try{const attrs=JSON.parse(value('aiAttrs')||'{}');if(!attrs||typeof attrs!=='object'||Array.isArray(attrs))throw new Error();aiAttrRows=Object.entries(attrs).map(([name,val])=>({name,value:String(val)}));renderAttributes();syncEditor();}catch(_error){notify('属性格式不正确，请使用上方属性表格编辑');}});
        if($('aiMobilePreviewButton'))$('aiMobilePreviewButton').addEventListener('click',window.aiShowMobilePreview);
        if($('aiCancelTask'))$('aiCancelTask').addEventListener('click',window.aiCancelTask);
        if($('aiResumeTask'))$('aiResumeTask').addEventListener('click',window.aiResumeTask);
        if($('aiTestImageProvider'))$('aiTestImageProvider').addEventListener('click',()=>window.aiTestProvider('image'));
        if($('aiTestPromptProvider'))$('aiTestPromptProvider').addEventListener('click',()=>window.aiTestProvider('prompt'));
        if($('aiClearProviderKeys'))$('aiClearProviderKeys').addEventListener('click',window.aiClearProviderKeys);
        recoverTask();
    });
    window.addEventListener('beforeunload',releaseFileUrls);
})();



