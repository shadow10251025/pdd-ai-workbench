/* Static original example art: browsing never calls a model or uploads photos. */
(() => {
    const examples=[
        ['carousel_sell','首屏卖点','第一眼说明商品值得看的地方','完整商品、一个短主标题、2—3个有依据的卖点，利用留白自然排版。','适合详情开场和支持文案的副图；白底首图另用干净商品照。'],
        ['angle','多角度展示','让用户看清外观','正面、侧面、背面使用不同镜头，背景与整套图片协调。','只能展示参考照片支持的结构，看不到的背面不要凭空补。'],
        ['lifestyle','人物 / 生活场景','展示真实使用语境','自然人物与环境互动，适当留白承载短文案，商品保持清楚。','人物为合成通用形象，场景依据商品用途选择。'],
        ['feature_macro','材质 / 细节特写','让用户放大看细节','织纹、边缘、缝线等真实细节搭配简短标注，避免整齐重复的方框。','强调照片能证明的外观，不从纹理猜成分或功效。'],
        ['mechanism','结构 / 功能说明','解释可见部件与用法','完整商品配细引导线或局部特写，信息沿商品结构展开。','不添加不存在的口袋、接口、内部结构或功能。'],
        ['steps','使用步骤','降低理解或使用门槛','2—3个清楚步骤或搭配方式，配少量序号与短文字。','穿搭、安装、开合等可用；没有说明书时不猜安全要求。'],
        ['comparison','对比说明','帮助用户选择本商品的不同选项','同款不同颜色或已确认款式并列，保持相同光线和尺度。','比较自家已知选项，不编造竞品差评或虚假效果对比。'],
        ['packaging','包装 / 随附物','说明实际会收到什么','包装、商品和真实随附物做自然平铺，避免过多装饰。','没有包装照片就补充实拍，不能把示例配件算进销售承诺。'],
        ['sku_preview','颜色 / 款式预览','对应规格，避免选错款','各颜色独立清晰展示，白底与尺度统一，可另做整组总览。','每个实际SKU需要对应参考照片；示例里的颜色不是本商品库存。'],
        ['trust_faq','常见问题','解决选购时的疑问','商品图与2—3个真实问答自然组合，短标题清晰易扫读。','答案来自已确认资料，不能捏造售后承诺、评价或认证。'],
        ['closing_cta','收尾 / 选购提示','收束整套详情并提醒选择','有氛围的商品画面、简短收尾标题、自然光影与留白。','不编造低价、销量、限时优惠或其他交易条件。'],
        ['size_chart','尺码表（实测）','准确呈现实际规格','按所选尺码与实测项目排表，单位明确，过多行列分页。','示例数值仅说明排版；实际生成使用商家填写的测量数据。']
    ];
    const atlas='/static/examples/module-reference-atlas.png';
    const escape=text=>String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function position(index){return `${index%4*100/3}% ${Math.floor(index/4)*50}%`;}
    function open(index=0){
        let dialog=document.getElementById('moduleExampleDialog');
        if(!dialog){dialog=document.createElement('dialog');dialog.id='moduleExampleDialog';dialog.className='module-example-dialog';document.body.appendChild(dialog);}
        const [key,name,purpose,composition,note]=examples[index];
        dialog.innerHTML=`<div class="ai-dialog-heading"><h3>模块示例 · ${escape(name)}</h3><button type="button" data-example-close>关闭</button></div><p class="hint">原创通用参考图，仅用于理解构图与排版，不是你的商品成品。查看不会调用AI。</p><div class="module-example-layout"><nav aria-label="示例类型">${examples.map((item,i)=>`<button type="button" data-example-index="${i}" aria-current="${i===index?'true':'false'}">${escape(item[1])}</button>`).join('')}</nav><figure><div class="module-example-art" role="img" aria-label="${escape(name)}构图示例" style="background-image:url('${atlas}');background-position:${position(index)}"></div><figcaption><b>${escape(purpose)}</b><p>${escape(composition)}</p><p>${escape(note)}</p><button type="button" class="btn btn-primary" data-example-use="${key}">选择这个模块</button></figcaption></figure></div>`;
        dialog.querySelector('[data-example-close]').onclick=()=>dialog.close();
        dialog.querySelectorAll('[data-example-index]').forEach(button=>button.onclick=()=>open(Number(button.dataset.exampleIndex)));
        dialog.querySelector('[data-example-use]').onclick=()=>{
            const input=document.querySelector(`.ai-module-choice[data-module="${key}"]`);
            if(input&&!input.checked){input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));}
            dialog.close();
        };
        if(!dialog.open)dialog.showModal();
    }
    document.addEventListener('DOMContentLoaded',()=>{
        const grid=document.getElementById('aiDetailModules');if(!grid)return;
        const bar=document.createElement('div');bar.className='module-example-gallery-bar';bar.innerHTML='<span>不确定选哪类图片？先看构图参考</span><button type="button" class="btn btn-secondary">查看全部12类示例</button>';bar.querySelector('button').onclick=()=>open(0);grid.before(bar);
        examples.forEach(([key,name],index)=>{
            const input=grid.querySelector(`[data-module="${key}"]`);if(!input)return;
            const button=document.createElement('button');button.type='button';button.className='ai-module-example-button';button.setAttribute('aria-label','查看'+name+'示例');
            button.innerHTML=`<span class="ai-module-example-thumb" style="background-image:url('${atlas}');background-position:${position(index)}"></span><span>查看${escape(name)}示例 ↗</span>`;
            button.onclick=event=>{event.preventDefault();event.stopPropagation();open(index);};input.closest('label').appendChild(button);
        });
        const image=new Image();image.onload=()=>document.documentElement.style.setProperty('--example-aspect',String(image.naturalWidth*3/(image.naturalHeight*4)));image.src=atlas;
    });
})();
