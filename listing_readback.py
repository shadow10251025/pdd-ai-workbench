"""Conservative, read-only listing evidence. Unknown fields never pass."""
import json


def identity_matches(expected, actual):
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    identifier = expected.get('mallId') or expected.get('id')
    if identifier:
        return str(identifier) == str(actual.get('mallId') or actual.get('id') or '')
    return bool(expected.get('name') and expected.get('name') == actual.get('name') and expected.get('account', '') == actual.get('account', ''))


async def verify_form(cdp, payload, *, warnings=None):
    """Only exact visible row matches count; virtualized/changed DOM needs review."""
    script = r'''(() => {
      const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
      const title=[...document.querySelectorAll('input,textarea')].find(e=>((e.placeholder||'')+' '+(e.getAttribute('aria-label')||'')).includes('标题'));
      const images = selector => {
        const inputs=[...document.querySelectorAll('input[data-tracking-click-viewid]')].filter(e=>selector.test(e.getAttribute('data-tracking-click-viewid')));
        if(inputs.length!==1) return null;
        let container=inputs[0].closest('[class*="upload"]');
        for(let i=0;i<3&&container;i++,container=container.parentElement){
          const count=container.querySelectorAll('[class*="delete"],[class*="remove"]');
          if(count.length) return count.length;
        }
        return null;
      };
      const rows=[...document.querySelectorAll('tr,[role="row"]')].map(e=>({text:clean(e.innerText),cells:[...e.querySelectorAll('td,[role="cell"]')].map(c=>clean(c.innerText)),values:[...e.querySelectorAll('input')].filter(i=>i.type!=='checkbox'&&i.type!=='file').map(i=>i.value)}));
      return JSON.stringify({title:title?title.value:null,main_count:images(/carousel_img_localfile_upload/),detail_count:images(/detail.*localfile|detail.*upload/),rows});
    })()'''
    try:
        raw = await cdp.js(script)
        evidence = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(evidence, dict):
            raise ValueError('unknown response')
    except Exception:
        return {'verified': False, 'status': 'needs_review', 'unverified_fields': ['title', 'images', 'skus'], 'message': '未能回读商品页，请人工核对后发布'}
    missing = []
    if evidence.get('title') != payload.get('title'):
        missing.append('title')
    if evidence.get('main_count') != len(payload.get('main_images', [])):
        missing.append('main_images')
    if payload.get('detail_images') and evidence.get('detail_count') != len(payload['detail_images']):
        missing.append('detail_images')
    # The existing uploader cannot establish the mapping of every preview image
    # by reading the platform DOM. Keep that limitation visible.
    if payload.get('spec_images'):
        missing.append('spec_images')
    rows = evidence.get('rows') or []
    used = set()
    from decimal import Decimal, InvalidOperation
    def number(value):
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return None
    for sku in payload.get('skus', []):
        if not sku.get('enabled', True):
            continue
        label = ' '.join(str(sku.get('name') or '').split())
        specs=sku.get('spec_values')
        def row_matches(row):
            cells=row.get('cells') or []
            if isinstance(specs,dict) and specs:
                return all(str(value) in cells for value in specs.values())
            return label in cells if cells else row.get('text','') == label
        matches = [(i, row) for i, row in enumerate(rows) if label and row_matches(row) and i not in used]
        if len(matches) != 1:
            missing.append('sku:' + str(sku.get('id') or label)); continue
        index, row = matches[0]
        values = [number(value) for value in row.get('values', [])]
        expected = [number(sku.get(key)) for key in ('price', 'single_price', 'stock')]
        # Require a distinct input for every expected value, including equal prices.
        for item in expected:
            if item is None or item not in values:
                missing.append('sku:' + str(sku.get('id') or label)); break
            values.remove(item)
        used.add(index)
    if warnings:
        missing.append('reported_warnings')
    return {'verified': not missing, 'status': 'filled_verified' if not missing else 'needs_review', 'unverified_fields': missing, 'evidence': evidence,
            'message': '已回读核对填入资料，请商家确认后手动发布' if not missing else '填表流程已结束，部分资料需要人工核对；未计上架额度'}
