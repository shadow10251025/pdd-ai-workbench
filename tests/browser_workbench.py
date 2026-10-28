"""Real Chromium acceptance of the running local workbench, without paid APIs.

Run manually: python tests/browser_workbench.py
Creates an explicitly labelled offline sample, saves/reloads then removes its
draft. Leaves generated demo pictures for screenshot inspection.
"""
import io
import json
import os
import time
from pathlib import Path
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent.parent
BASE_URL=os.environ.get('PDD_BROWSER_TEST_URL','http://127.0.0.1:3000').rstrip('/')
OUTPUT=ROOT/'validation/browser'
OUTPUT.mkdir(parents=True,exist_ok=True)


def picture(color):
    image=Image.new('RGB',(700,700),'#f4f5f7'); draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((170,110,530,600),radius=40,fill=color)
    draw.rectangle((240,190,460,480),outline='white',width=12)
    result=io.BytesIO(); image.save(result,'JPEG'); return result.getvalue()


with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]; dialogs=[]; requests=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.accept()))
    page.on('request',lambda request:requests.append(request.url) if '/api/ai/confirm' in request.url else None)
    page.goto(BASE_URL+'/'); page.wait_for_load_state('networkidle')
    assert page.locator('.ai-module-example-button').count()==12
    page.locator('.ai-module-choice[data-module="feature_macro"]').locator('..').locator('.ai-module-example-button').click()
    assert page.locator('#moduleExampleDialog').is_visible()
    page.locator('[data-example-index="11"]').click()
    assert '尺码表' in page.locator('#moduleExampleDialog h3').inner_text()
    page.screenshot(path=str(OUTPUT/'module-examples.png'))
    page.locator('[data-example-close]').click()
    assert not page.locator('#aiSizeChartModule').is_checked()
    assert page.request.get(BASE_URL+'/static/examples/module-reference-atlas.png').status==200
    assert not requests
    page.locator('#aiProductImage').set_input_files([{'name':'offline-red.jpg','mimeType':'image/jpeg','buffer':picture('#b74c49')},{'name':'offline-blue.jpg','mimeType':'image/jpeg','buffer':picture('#4b76ae')}])
    assert page.locator('.ai-file-card').count()==2
    # A dropped third file goes through the actual drop handler.
    drop=page.evaluate_handle("""() => { const d=new DataTransfer();d.items.add(new File([new Uint8Array([1,2,3])],'remove-after-drop.jpg',{type:'image/jpeg'}));return d; }""")
    page.locator('#aiDropZone').dispatch_event('drop',{'dataTransfer':drop})
    assert page.locator('.ai-file-card').count()==3
    page.locator('[data-remove-file="2"]').click(); assert page.locator('.ai-file-card').count()==2
    page.locator('#aiProductHint').fill('离线流程验收样例，非AI质量样张')
    page.locator('#aiSelectedColors').fill('红色')
    page.locator('#aiSelectedSizes').fill('S、M')
    page.locator('[data-reference-index="0"]').fill('红色');page.locator('[data-reference-index="0"]').blur()
    page.locator('#aiHeroCount').fill('2');page.locator('#aiHeroCount').blur()
    for choice in page.locator('.ai-module-choice').all():
        choice.uncheck()
    page.wait_for_timeout(500)
    assert '详情 0 张' in page.locator('#aiPlanSummary').inner_text()
    page.locator('.ai-module-choice[data-module="lifestyle"]').check()
    page.locator('.ai-module-count[data-module-count="lifestyle"]').fill('1');page.locator('.ai-module-count[data-module-count="lifestyle"]').blur()
    page.wait_for_timeout(500)
    page.locator('#aiGenerateButton').click()
    page.wait_for_selector('#aiProposalModules [data-proposal-prompt]',state='attached',timeout=30000)
    assert page.evaluate('window.aiGetProductPackage()') is None
    assert not requests
    page.locator('#aiProposalTitle').fill('离线流程验收样例 - 请勿发布')
    page.locator('#aiProposalModules .ai-proposal-module').first.locator('summary').click()
    page.locator('[data-proposal-prompt]').first.fill('MERCHANT CONFIRMED PROMPT: clean standalone product hero, exact photographed product.')
    page.locator('#aiProposal').scroll_into_view_if_needed();page.screenshot(path=str(OUTPUT/'proposal-confirmation.png'))
    page.reload();page.wait_for_load_state('networkidle')
    page.wait_for_selector('#aiProposalModules [data-proposal-prompt]',state='attached')
    assert page.locator('[data-proposal-prompt]').first.input_value().startswith('MERCHANT CONFIRMED PROMPT:')
    assert page.locator('#aiProposalTitle').input_value()=='离线流程验收样例 - 请勿发布'
    assert '主图 2 张' in page.locator('#aiPlanSummary').inner_text()
    assert not requests
    page.locator('#aiGenerateButton').click()
    page.wait_for_function("() => { const r=window.aiGetProductPackage();return r && r.module_outputs && r.module_outputs.length===4; }",timeout=30000)
    package=page.evaluate('window.aiGetProductPackage()')
    assert package['generation_kind']=='offline_demo'
    assert package['confirmed_before_generation'] is True
    assert package['module_outputs'][0]['prompt'].startswith('MERCHANT CONFIRMED PROMPT:')
    assert len(package['main_images'])==2 and len(package['detail_images'])==1 and len(package['spec_images'])==1
    assert len(requests)==1 and all(sku['stock']==0 for sku in package['skus'])
    page.locator('#aiTitle').fill('离线流程验收样例 - 请勿发布')
    first=page.locator('#aiMainPreview .ai-image-card').first
    first.locator('[data-action="review"]').click()
    first.locator('img[data-open-image]').click()
    assert page.locator('#lightbox').is_visible()
    page.evaluate('closeLightbox()')
    first.locator('details').first.locator('summary').first.click()
    assert first.locator('textarea[data-module-prompt]').input_value()
    page.locator('#aiResult').scroll_into_view_if_needed();page.screenshot(path=str(OUTPUT/'review-desktop.png'))
    # Configure a real SKU value through the listing editor, then save/reload.
    page.evaluate('aiContinueToListing()');page.wait_for_timeout(200)
    page.locator('#batchPrice').fill('19.90');page.locator('#batchSinglePrice').fill('29.90');page.locator('#batchStock').fill('0')
    page.evaluate('batchSetSkuPrices()')
    page.evaluate('saveMigratedProduct()');page.wait_for_timeout(1200)
    persisted=page.evaluate("JSON.parse(localStorage.getItem('currentProductPackage'))")
    assert persisted and persisted['title']=='离线流程验收样例 - 请勿发布'
    assert all(row['stock']==0 and float(row['price'])==19.9 for row in persisted['sku'])
    products=page.evaluate("fetch('/api/migrated-products').then(r=>r.json())")
    saved=next(item for item in products if item['title']=='离线流程验收样例 - 请勿发布')
    page.reload(); page.wait_for_load_state('networkidle')
    restored=page.evaluate("JSON.parse(localStorage.getItem('currentProductPackage'))")
    assert restored['sku'][0]['stock']==0
    page.set_viewport_size({'width':390,'height':844});page.locator('.nav-item[data-tab="ai"]').click();page.wait_for_timeout(150)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth+2')
    page.screenshot(path=str(OUTPUT/'workbench-mobile.png'))
    # Remove only the acceptance draft created above, preserving all real drafts.
    response=page.evaluate("id=>fetch('/api/migrated-products/'+id,{method:'DELETE'}).then(r=>r.json())",saved['id'])
    assert response['success']
    assert not errors,errors
    (OUTPUT/'report.json').write_text(json.dumps({'status':'passed','paid_image_requests':0,'uploaded_files':2,'confirmed_image_submissions':len(requests),'output_count':4,'checks':['12-module-examples','example-dialog-and-size-table','multi-select','drag-drop','cancel-all-details','planning-does-not-generate-images','edit-prompts-before-confirmation','bound-SKU-photo','exact-counts','actual-prompts','large-image-review','complete-draft-save','reload-stock-zero','mobile-layout','no-js-errors'],'dialogs':dialogs},ensure_ascii=False,indent=2),'utf-8')
    browser.close()
print('Browser acceptance passed; paid requests: 0')

