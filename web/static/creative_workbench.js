/* Editable planning drafts and idempotent, explicitly confirmed generation. */
(() => {
    'use strict';
    const $=id=>document.getElementById(id),api=window.aiProposalAPI;
    const esc=text=>String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const value=id=>String($(id)?.value||'').trim();
    const split=text=>Array.from(new Set(String(text||'').split(/[、,，\n]+/).map(x=>x.trim()).filter(Boolean)));
    const clone=data=>JSON.parse(JSON.stringify(data));
    const savedKey='aiCreativeProposalV1',pendingKey='aiCreativeConfirmationV2';
    const colorDim=name=>/色|款式/.test(name),sizeDim=name=>/尺码|鞋码|码数|尺寸|大小/.test(name);
    let proposal=null,stale=false,applying=false,preparing=false,confirming=false,timer,retrySource='';
    let specsEdited=false,refsEdited=false,photosChanged=false,pendingBindings=null,otherSpecs=[],manualMeta={},manualPrompts={},pending=null;
    let capabilities={image_mode:'offline_demo'};

    function rememberPending(){try{pending?api.storeJson(pendingKey,pending):localStorage.removeItem(pendingKey);}catch(_){}}
    function otherMap(){return Object.fromEntries(otherSpecs.filter(row=>row.name.trim()&&split(row.values).length).map(row=>[row.name.trim(),split(row.values)]));}
    window.aiAdjustPendingSettings=settings=>{
        const output={...settings,selected_specs:{...otherMap(),...settings.selected_specs},specs_user_modified:specsEdited,variant_references_user_modified:refsEdited};
        if(pendingBindings!==null)output.variant_references=clone(pendingBindings);
        else if(proposal&&!api.files().length&&!photosChanged)output.variant_references=proposal.options.variant_references||{};
        if(proposal&&!api.files().length&&!photosChanged){
            output.reference_count=proposal.source_images.length;
            if(!stale)output.detail_modules=proposal.generation_plan.detail_module_plan;
        }
        return output;
    };
    function persist(){
        if(!proposal)return;
        api.storeJson(savedKey,{...proposal,_stale:stale,_photos_changed:photosChanged,_pending_options:stale?api.settings():null,
            _specs_edited:specsEdited,_refs_edited:refsEdited,_pending_bindings:pendingBindings,_other_specs:otherSpecs,
            _manual_metadata:manualMeta,_manual_prompts:manualPrompts,_size_columns:value('aiSizeColumns')});
    }
    function imageMode(){return value('aiImageApiUrl')||capabilities.image_mode==='ai'?'api':capabilities.image_mode==='demo_asset'?'fixed':'offline';}
    function renderMode(){
        const mode=imageMode();
        if($('aiGenerationMode')){
            $('aiGenerationMode').dataset.mode=mode;
            $('aiGenerationMode').textContent=mode==='api'?'确认后将调用图片API；请核对批次数、实物参考和提示词。':mode==='fixed'?'当前是固定素材演示，不代表按你的商品照片生成的AI成品。':'尚未配置图片API：确认后仅生成离线演示，用于验证流程，不是AI商品成品。';
        }
    }
    function label(busy){
        const button=$('aiGenerateButton');if(!button)return;
        if(busy){button.textContent=preparing?'正在识图与策划…':'正在处理，请稍候';return;}
        button.textContent=!proposal?'识图并策划方案':stale?(photosChanged&&!api.files().length?'重新选择照片并策划':'更新策划后再生成'):
            pending?.proposal_id===proposal.proposal_id?'查询上次生成任务':imageMode()==='api'?'确认方案并生成图片':'确认方案，生成演示';
        if($('aiReplanButton'))$('aiReplanButton').disabled=api.isBusy();
        renderMode();
    }
    window.aiUpdatePrimaryAction=label;
    window.aiHandleProposalFailure=data=>{retrySource=data.proposal_id;try{localStorage.setItem('aiRecognitionRetry',retrySource);}catch(_){}};
    window.aiProposalSettingsChanged=(reason,sourceChanged,changeType)=>{
        if(applying)return;
        if(changeType==='provider'){renderMode();label(api.isBusy());return;}
        if(sourceChanged){photosChanged=true;pendingBindings=api.referenceBindings();}
        if(changeType==='reference'){refsEdited=true;pendingBindings=api.referenceBindings();}
        if(proposal){stale=true;persist();renderStatus();}
        label(false);clearTimeout(timer);
        if(sourceChanged&&$('aiAutoRecognize')?.checked&&(value('aiPromptApiUrl')||value('aiVisionApiUrl'))){
            timer=setTimeout(()=>{if(!api.isBusy()&&api.files().length)window.aiPrepareProposal();},1100);
        }
    };
    function changedSpecs(){specsEdited=true;api.markDirty('规格已修改；更新策划后再生图。',false,'spec');persist();renderSuggestions();}
    function renderStatus(){
        if(!proposal)return;
        const facts=proposal.recognition||{},usage=proposal.text_requests||{};
        const prefix=stale?(photosChanged&&!api.files().length?'新照片还未完成策划，刷新后请重新选择照片。':'配置已修改，当前显示上一版策划。请更新后再生成。'):'策划已就绪，确认前不会生图。';
        $('aiProposalStatus').textContent=`${prefix} ${facts.recognition_note||''}。${proposal.planning_note||''}。本轮策划：识图${usage.recognition||0}次、策划${usage.planning||0}次${usage.recognition_cache_hit?'（复用识图）':''}，图片请求0次。${(proposal.warnings||[]).join('；')}`;
    }
    function renderBatchPrompts(){
        if(!proposal||!$('aiProposalBatches'))return;
        const prompts=Object.fromEntries(proposal.module_outputs.map(module=>[module.id,module.prompt]));
        $('aiProposalBatches').innerHTML=(proposal.generation_plan.round_plan||[]).map(batch=>{
            const header=proposal.prompt_pack.style_lock+`\nCreate exactly one ${batch.rows} by ${batch.cols} contact sheet. Equal-sized cells, thin #FFFFFF gutters, no content crossing cells. Output size ${batch.output_size}. Preserve each module ID's role and match each provided reference. Unused cells blank.\n`;
            const text=header+batch.module_ids.map((id,index)=>`CELL ${index+1}:\n${prompts[id]||''}`).join('\n\n');
            return `<details><summary>第${batch.round}批 · ${batch.module_ids.length}张 · ${esc(batch.output_size)} · 1次图片请求</summary><pre>${esc(text)}</pre></details>`;
        }).join('');
    }
    function renderOtherSpecs(){
        const node=$('aiOtherSpecsEditor');if(!node)return;
        node.innerHTML='<div class="ai-other-spec-heading"><b>其他销售规格</b><button type="button" id="aiAddOtherSpec" class="btn btn-secondary">添加规格</button></div><p class="hint">容量、型号、套装数量等可在这里填写；不会自动当成颜色。</p>'+otherSpecs.map((row,index)=>
            `<div class="ai-attribute-row"><input data-other-name="${index}" aria-label="其他规格名称" value="${esc(row.name)}" placeholder="例如：容量"><input data-other-values="${index}" aria-label="其他规格值" value="${esc(row.values)}" placeholder="例如：350 mL、500 mL"><button type="button" data-other-remove="${index}" aria-label="删除该规格">×</button></div>`).join('');
        $('aiAddOtherSpec').onclick=()=>{otherSpecs.push({name:'',values:''});renderOtherSpecs();node.querySelector('[data-other-name="'+(otherSpecs.length-1)+'"]').focus();};
        node.querySelectorAll('[data-other-name]').forEach(input=>input.oninput=()=>{otherSpecs[Number(input.dataset.otherName)].name=input.value;changedSpecs();});
        node.querySelectorAll('[data-other-values]').forEach(input=>input.oninput=()=>{otherSpecs[Number(input.dataset.otherValues)].values=input.value;changedSpecs();});
        node.querySelectorAll('[data-other-remove]').forEach(button=>button.onclick=()=>{otherSpecs.splice(Number(button.dataset.otherRemove),1);renderOtherSpecs();changedSpecs();});
    }
    function renderReferences(){
        const node=$('aiProposalReferences');if(!node||!proposal)return;
        const bindings=pendingBindings??proposal.options.variant_references??{};
        node.innerHTML='<b>本方案的实物参考与规格绑定</b><div class="ai-proposal-photo-grid">'+proposal.source_images.map((path,index)=>{
            const names=Object.entries(bindings).filter(([,indices])=>indices.includes(index+1)).map(([name])=>name).join('、');
            const url=typeof toImgUrl==='function'?toImgUrl(path):'/'+path;
            return `<figure><img data-proposal-photo="${index}" src="${esc(url)}" alt="实物参考照片${index+1}"><figcaption>照片${index+1}<input data-proposal-binding="${index+1}" value="${esc(names)}" aria-label="方案照片${index+1}对应规格" placeholder="对应颜色或款式，可留空"></figcaption></figure>`;
        }).join('')+'</div>';
        node.querySelectorAll('[data-proposal-photo]').forEach(image=>image.onclick=()=>{if(typeof openLightbox==='function')openLightbox(image.src);});
        node.querySelectorAll('[data-proposal-binding]').forEach(input=>input.oninput=()=>{
            pendingBindings={};node.querySelectorAll('[data-proposal-binding]').forEach(item=>split(item.value).forEach(name=>(pendingBindings[name]||=[]).push(Number(item.dataset.proposalBinding))));
            refsEdited=true;api.markDirty('参考照片绑定已修改，请更新策划。',false,'bindings');persist();
        });
    }
    function renderSuggestions(){
        if(!proposal||!$('aiProposalSuggestions'))return;
        const suggestions=Object.entries(proposal.metadata.spec_suggestions||{}),selected=api.settings().selected_specs;
        $('aiProposalSuggestions').innerHTML='<b>建议规格（点击加入或移除，按实际出售情况选择）</b>'+(suggestions.length?suggestions.map(([name,values])=>
            `<div><span>${esc(name)}：</span>${values.map(item=>`<button type="button" class="ai-spec-chip" aria-pressed="${(selected[name]||[]).includes(item)}" data-proposal-spec="${esc(item)}" data-spec-dim="${esc(name)}">${esc(item)}</button>`).join('')}</div>`).join(''):'<p class="hint">暂无可确认的规格建议，可先补充实物资料或在上方填写。</p>');
        document.querySelectorAll('[data-proposal-spec]').forEach(button=>button.onclick=()=>{
            const name=button.dataset.specDim,item=button.dataset.proposalSpec;
            const toggle=values=>values.includes(item)?values.filter(value=>value!==item):[...values,item];
            if(colorDim(name)||sizeDim(name)){
                const id=colorDim(name)?'aiSelectedColors':'aiSelectedSizes';$(id).value=toggle(split(value(id))).join('、');
            }else{
                let row=otherSpecs.find(row=>row.name===name);if(!row){row={name,values:''};otherSpecs.push(row);}row.values=toggle(split(row.values)).join('、');renderOtherSpecs();
            }
            changedSpecs();api.renderSizeTable();
        });
    }
    function renderProposal(){
        if(!proposal)return;
        $('aiProposal').style.display='block';renderStatus();renderMode();
        const facts=proposal.recognition||{},metadata=proposal.metadata||{};
        $('aiRecognitionFacts').innerHTML=`<b>照片可见信息</b><p>${esc((facts.identity_evidence||[]).join('；')||'暂无AI识图证据，请配置识图服务或人工核对。')}</p><b>待确认</b><p>${esc((facts.uncertain_features||[]).join('；')||'请核对品牌、规格、材质与卖点')}</p>`;
        [['aiProposalTitle','title'],['aiProposalBrand','brand'],['aiProposalCategory','category'],['aiProposalDescription','description']].forEach(([id,key])=>{
            $(id).value=metadata[key]||'';$(id).oninput=()=>{proposal.metadata[key]=$(id).value;manualMeta[key]=$(id).value;persist();};
        });
        renderReferences();renderOtherSpecs();renderSuggestions();
        $('aiProposalModules').innerHTML=proposal.module_outputs.map(module=>`<article class="ai-proposal-module"><div><b>${esc(module.label)}</b><span>${module.render_source!=='provider'?'本地精确排版':module.prompt_author==='merchant'?'保留手动提示词':module.prompt_author==='planner_ai'?'策划AI + Skill':'Skill规则提示词'}</span></div><p>${module.spec_value?'对应规格：'+esc(module.spec_value)+' · ':''}参考照片 ${(module.reference_indices||[]).join('、')}${module.scene?' · '+esc(module.scene):''}</p>${module.headline?`<p>建议广告词：${esc(module.headline)}</p>`:''}<details><summary>查看与修改本图提示词</summary><textarea data-proposal-prompt="${esc(module.id)}" aria-label="${esc(module.label)}生成前提示词">${esc(module.prompt)}</textarea></details>${module.previous_manual_prompt?`<details class="ai-old-manual-prompt"><summary>查看上次手动修改</summary><p>${esc(module.manual_prompt_note)}</p><pre>${esc(module.previous_manual_prompt)}</pre><button type="button" class="btn btn-secondary" data-use-old-prompt="${esc(module.id)}">用这份手动内容替换本图</button></details>`:''}</article>`).join('');
        document.querySelectorAll('[data-proposal-prompt]').forEach(input=>input.oninput=()=>{
            const module=proposal.module_outputs.find(item=>item.id===input.dataset.proposalPrompt);module.prompt=input.value;manualPrompts[module.id]=input.value;persist();renderBatchPrompts();
        });
        document.querySelectorAll('[data-use-old-prompt]').forEach(button=>button.onclick=()=>{
            const module=proposal.module_outputs.find(item=>item.id===button.dataset.useOldPrompt);module.prompt=module.previous_manual_prompt;manualPrompts[module.id]=module.prompt;persist();renderProposal();
        });
        renderBatchPrompts();renderPending();label(false);
    }
    function applyControls(options,plan){
        const selected=options.selected_specs||{};
        [['aiProductHint','product_hint'],['aiStyle','style'],['aiHeroCount','hero_count'],['aiStrategy','strategy'],['aiImageSize','output_size'],['aiMaxRequests','max_image_requests'],['aiCreativeDensity','creative_density'],['aiAdCopy','ad_copy'],['aiGenerationRounds','generation_rounds'],['aiRoundStrategy','round_strategy']].forEach(([id,key])=>{if($(id)&&options[key]!==undefined)$(id).value=String(options[key]);});
        const grid=String(options.rows||2)+'x'+String(options.cols||3);
        if($('aiGrid')){if(![...$('aiGrid').options].some(option=>option.value===grid)){const option=document.createElement('option');option.value=grid;option.textContent=grid.replace('x',' × ')+'（已保存）';$('aiGrid').appendChild(option);}$('aiGrid').value=grid;}
        $('aiModelScene').checked=!!options.model_scene;
        $('aiSelectedColors').value=Object.entries(selected).filter(([name])=>colorDim(name)).flatMap(([,values])=>values).join('、');
        $('aiSelectedSizes').value=Object.entries(selected).filter(([name])=>sizeDim(name)).flatMap(([,values])=>values).join('、');
        otherSpecs=Object.entries(selected).filter(([name])=>!colorDim(name)&&!sizeDim(name)).map(([name,values])=>({name,values:values.join('、')}));
        const modules=options.detail_modules===null||options.detail_modules===undefined?plan.detail_module_plan:options.detail_modules;
        $('aiAutoModulePlan').checked=true;api.renderPlan({...plan,detail_module_plan:modules});$('aiAutoModulePlan').checked=options.auto_module_plan!==false;
        $('aiSizeChartModule').checked=!!options.size_chart_enabled;$('aiSizeChartOnlySelected').checked=options.size_chart_only_selected!==false;
        if($('aiSizeChartFields'))$('aiSizeChartFields').style.display=options.size_chart_enabled?'block':'none';
        const rows=options.size_chart_specs||[];
        const columns=[...new Set((Array.isArray(rows)?rows:[]).flatMap(row=>Object.keys(row.measurements||{})))];
        if(columns.length)$('aiSizeColumns').value=columns.join('、');api.applySizeRows(Array.isArray(rows)?rows:[]);api.applyBindings(options.variant_references||{});
    }
    window.aiHandleProposal=data=>{
        applying=true;proposal=data;stale=false;photosChanged=false;retrySource='';localStorage.removeItem('aiRecognitionRetry');
        specsEdited=!!data.options.specs_user_modified;refsEdited=!!data.options.variant_references_user_modified;pendingBindings=clone(data.options.variant_references||{});
        manualMeta=clone(data.options.merchant_metadata||{});manualPrompts=Object.fromEntries(data.module_outputs.filter(item=>item.prompt_author==='merchant').map(item=>[item.id,item.prompt]));
        applyControls(data.options,data.generation_plan);renderProposal();persist();applying=false;$('aiProposal').scrollIntoView({behavior:'smooth',block:'start'});
    };
    function validateOtherSpecs(){
        const names=otherSpecs.map(row=>row.name.trim()).filter(Boolean);
        if(new Set(names).size!==names.length||names.some(name=>colorDim(name)||sizeDim(name))){api.notify('其他规格名称不能重复，颜色和尺码请使用上方专用输入栏。');$('aiOtherSpecsEditor').scrollIntoView({block:'center'});return false;}
        return true;
    }
    window.aiPrepareProposal=async()=>{
        if(api.isBusy()||preparing||confirming||!validateOtherSpecs())return;
        if(!api.files().length&&(photosChanged||(!proposal&&!retrySource))){api.notify('请重新选择当前商品照片；不会用上一商品照片代替。');$('aiDropZone').focus();return;}
        preparing=true;clearTimeout(timer);
        const fd=new FormData(),options={...api.settings(),merchant_metadata:{...manualMeta},manual_prompts:{...manualPrompts}};
        api.files().forEach(file=>fd.append('images',file));fd.append('options_json',JSON.stringify(options));
        if(retrySource||proposal)fd.append('source_proposal_id',retrySource||proposal.proposal_id);
        api.addProviderFields(fd);api.setState('正在识图与策划；本阶段不调用生图接口。');
        try{await api.submitTask('/api/ai/prepare',fd);}finally{preparing=false;label(false);}
    };
    function renderPending(message=''){
        const node=$('aiPendingSubmission');if(!node)return;
        const active=!!pending&&pending.proposal_id===proposal?.proposal_id;node.hidden=!active;if(!active)return;
        node.innerHTML=`<b>上次确认提交尚未核对完成</b><p>${esc(message||'点击主要按钮会先查询上次任务，不会直接再次生图。当前修改的提示词仍保留。')}</p>${pending.not_found?'<button type="button" class="btn btn-secondary" id="aiRetryOriginalConfirmation">重试原提交（沿用同一编号）</button>':''}`;
        if($('aiRetryOriginalConfirmation'))$('aiRetryOriginalConfirmation').onclick=()=>submitConfirmation(true);
    }
    function finishConfirmation(outcome){
        if(outcome?.status==='done'||(outcome?.error_code&&outcome.billing_status!=='unknown'&&Number(outcome.provider_calls||0)===0&&outcome.error_code!=='failed_charged')){
            pending=null;rememberPending();renderPending();
        }else if(pending){pending.status=outcome?.status||'unknown';rememberPending();renderPending(outcome?.message||'暂时不能确认任务结果，请先查询上次任务。');}
    }
    async function submitConfirmation(retryOriginal=false){
        if(api.isBusy()||preparing||confirming||!proposal)return;
        confirming=true;clearTimeout(timer);
        try{
            if(pending?.proposal_id===proposal.proposal_id&&!retryOriginal){
                const response=await fetch('/api/ai/requests/'+encodeURIComponent(pending.id));const found=await response.json();
                if(!response.ok)throw new Error(found.detail||'任务查询失败，请稍后再试');
                if(!found.found){pending.not_found=true;rememberPending();renderPending('暂未找到上次任务。可以重试原提交；沿用同一编号，避免网络恢复后重复执行。');return;}
                api.setState('正在查询上次生成任务，当前修改已保留。');finishConfirmation(await api.pollExistingTask(found.task_id));return;
            }
            if(!retryOriginal){
                const metadata={title:value('aiProposalTitle'),brand:value('aiProposalBrand'),category:value('aiProposalCategory'),description:value('aiProposalDescription')};
                if(!metadata.title){api.notify('请先确认商品标题');$('aiProposalTitle').focus();return;}
                const edits={metadata,module_prompts:Object.fromEntries(proposal.module_outputs.map(module=>[module.id,module.prompt]))};
                pending={id:typeof crypto.randomUUID==='function'?crypto.randomUUID():`confirm-${Date.now()}-${Math.random().toString(36).slice(2)}`,proposal_id:proposal.proposal_id,edits_json:JSON.stringify(edits),created:Date.now()};rememberPending();
            }
            const fd=new FormData();fd.set('proposal_id',pending.proposal_id);fd.set('edits_json',pending.edits_json);fd.set('client_request_id',pending.id);api.addProviderFields(fd);
            api.setState(imageMode()==='api'?'正在执行已确认的图片方案，不再调用识图与策划。':'正在生成离线演示；未调用真实生图服务。');
            finishConfirmation(await api.submitTask('/api/ai/confirm',fd));
        }catch(error){renderPending(error.message);api.setState(error.message+'；保留原提交编号，可稍后查询。');}
        finally{confirming=false;label(false);}
    }
    window.runAiWorkflow=async()=>{if(api.isBusy()||preparing||confirming)return;if(!proposal||stale)return window.aiPrepareProposal();await submitConfirmation();};
    window.aiClearCreativeProposal=()=>{
        clearTimeout(timer);proposal=null;stale=false;photosChanged=false;retrySource='';manualMeta={};manualPrompts={};pendingBindings=null;otherSpecs=[];specsEdited=false;refsEdited=false;
        localStorage.removeItem(savedKey);localStorage.removeItem('aiRecognitionRetry');$('aiProposal').style.display='none';if($('aiDirtyNotice'))$('aiDirtyNotice').hidden=true;label(false);
    };
    window.aiExportProposal=()=>{
        if(!proposal){api.notify('请先完成策划');return;}
        const lines=[`# ${value('aiProposalTitle')||proposal.metadata.title}`,`\n图片请求预算：${proposal.generation_plan.planned_image_requests}次`,stale?'\n配置已修改，下方是上一版方案。':'\n状态：待人工确认',...proposal.module_outputs.flatMap(module=>[`\n## ${module.label} (${module.id})`,`参考照片：${(module.reference_indices||[]).join('、')}`,module.prompt])];
        let text=lines.join('\n');['aiImageApiKey','aiPromptApiKey','aiVisionApiKey'].map(value).filter(key=>key.length>3).forEach(key=>{text=text.split(key).join('[已隐藏]');});
        const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'})),link=document.createElement('a');link.href=url;link.download=`商品图片策划_${proposal.proposal_id}.md`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    };
    document.addEventListener('DOMContentLoaded',()=>{
        try{retrySource=localStorage.getItem('aiRecognitionRetry')||'';pending=JSON.parse(localStorage.getItem(pendingKey)||'null');}catch(_){}
        try{
            const saved=JSON.parse(localStorage.getItem(savedKey)||'null');
            if(saved?.proposal_id){
                applying=true;proposal=saved;stale=!!saved._stale;photosChanged=!!saved._photos_changed;specsEdited=!!saved._specs_edited;refsEdited=!!saved._refs_edited;
                manualMeta=saved._manual_metadata||{};manualPrompts=saved._manual_prompts||{};pendingBindings=saved._pending_bindings??null;
                applyControls(stale&&saved._pending_options?saved._pending_options:saved.options,saved.generation_plan);
                if(saved._other_specs)otherSpecs=saved._other_specs;if(saved._size_columns)$('aiSizeColumns').value=saved._size_columns;
                renderProposal();applying=false;
            }else renderOtherSpecs();
        }catch(_){applying=false;api.notify('上次策划缓存无法完整恢复，可从原照片重新策划。');}
        ['aiSelectedColors','aiSelectedSizes'].forEach(id=>$(id)?.addEventListener('input',()=>{specsEdited=true;persist();renderSuggestions();}));
        $('aiSizeColumns')?.addEventListener('input',persist);
        fetch('/api/ai/capabilities').then(response=>response.ok?response.json():null).then(data=>{if(data){capabilities=data;label(api.isBusy());}}).catch(()=>{});
        label(false);
    });
})();
