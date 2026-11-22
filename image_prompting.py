"""Small, role-specific Skill briefs; pixel geometry belongs to core_planner."""
from __future__ import annotations

POLICY = 'skill-compact-v1'
ROLE_TEMPLATES = {
    'white_pack': 'hero-image', 'angle': 'hero-image', 'proof': 'detail-macro',
    'flat_lay': 'flat-lay', 'scene': 'lifestyle-scene', 'packaging': 'packaging',
    'sku_preview': 'hero-image', 'size_chart': 'size-spec',
}
PURPOSES = {
    'white_pack': 'One complete product centered on #FFFFFF; a clean first carousel image.',
    'angle': 'Show a distinct angle supported by the photos; keep the whole product recognizable.',
    'proof': 'A close photograph of visible texture or workmanship; show the detail clearly.',
    'flat_lay': 'A natural overhead arrangement of the photographed product.',
    'scene': 'Show the product naturally in one suitable real-life setting.',
    'hook': 'A strong opening image with a clear product promise, supporting explanation and visible evidence.',
    'carousel_sell': 'Show one visible selling point in an attractive supporting product image with a complete ecommerce copy hierarchy.',
    'pain_point': 'Show a plausible usage need without exaggerated problems or results.',
    'mechanism': 'Explain visible construction with layered callouts and supporting detail crops; no invented hidden parts.',
    'benefit': 'Show one supported benefit through photography, a supporting explanation and readable feature labels.',
    'usage': 'Explain supported usage with a few clear steps.',
    'comparison': 'Compare only confirmed variants or visible details of this product.',
    'packaging': 'Show only packaging and included items confirmed in the references.',
    'trust_faq': 'Answer a simple purchase question using confirmed product facts.',
    'closing_cta': 'An appealing closing product image with a neutral invitation to choose options.',
}
PRESETS = {
    'minimal': 'white #FFFFFF and neutral gray, soft diffused studio light, restrained composition',
    'fresh': 'airy natural light, product-matched soft colors, relaxed everyday photography',
    'luxury': 'rich editorial contrast, product-matched dark or ivory tones, controlled highlights',
    'tech': 'precise product photography, cool directional light, restrained technical visual cues',
}


def short(value, limit=600):
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split()).strip()
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0].rstrip(' ,;')


def style_direction(style='auto', summary=''):
    if style in PRESETS:
        direction = PRESETS[style] + '.'
    else:
        direction = short(summary, 420) or 'Choose product-appropriate photography from the references and each image purpose.'
    return ('Campaign Style Lock: ' + direction +
            ' Keep the product identity and typography coherent; scenes, backgrounds and layouts may vary naturally. '
            'White hero and SKU backgrounds remain #FFFFFF.')


def make_pack(style='auto', summary='', reason=''):
    import ai_workflow as w
    lock = style_direction(style, summary)
    return {'skill': 'ecom-details-image', 'skill_source': w._skill_template_root()[1],
            'skill_template_count': 25, 'prompt_policy': POLICY, 'style': style,
            'style_lock': lock, 'prompt': lock, 'template_ids': [],
            'visual_direction': {'summary': short(summary, 420) if style == 'auto' else PRESETS.get(style, ''),
                                 'reason': short(reason, 240), 'mode': style}}


def template_id(entry, model_scene=False):
    if entry['kind'] == 'spec':
        return 'hero-image'
    role = entry['module_type']
    if role == 'scene' and model_scene:
        return 'model-showcase'
    return ROLE_TEMPLATES.get(role, 'infographic')


def skill_brief(entry, model_scene=False):
    import ai_workflow as w
    from skill_templates import brief
    return brief(w._skill_template_root()[0], template_id(entry, model_scene))


def module_prompt(w, entry, index, pack, metadata, product_hint, model_scene, ad_copy, chart_rows, direction=None):
    """One task, one template, one optional art direction; no parallel blueprints."""
    role = entry['module_type']
    white = role == 'white_pack' or entry['kind'] == 'spec'
    local = entry.get('render_source', 'provider') != 'provider'
    if local:
        return 'Local verified data layout; no image-provider request.'
    brief = skill_brief(entry, bool(model_scene) and not white)
    entry['template_id'] = brief['id']
    if brief['id'] not in pack.setdefault('template_ids', []):
        pack['template_ids'].append(brief['id'])
    art = direction or {}
    density = str(pack.get('creative_density') or metadata.get('creative_density') or 'rich').strip().lower()
    rich_copy = density not in {'conservative', '少文字', 'light'}
    scene = short(art.get('scene'), 180)
    camera = short(art.get('camera'), 120)
    concept = short(art.get('prompt'), 800)
    product = ('the photographed variant' if entry['kind'] == 'spec' else
               short(metadata.get('title') or product_hint, 100) or 'the photographed product')
    parts = [pack['style_lock'], f"Product: {product}. Match the reference photos' shape, color, print and proportions."]
    if entry['kind'] == 'spec':
        parts.append(f"SKU「{entry['spec_value']}」: one complete product on #FFFFFF, using only this variant's bound photos; never recolor it.")
    else:
        parts.append(PURPOSES.get(role, 'One clear ecommerce product image.'))
    # Planner owns the scene and camera; the template only fills missing choices.
    if concept:
        parts.append('Art direction: ' + concept)
        if role == 'scene' and scene and scene.lower() not in concept.lower():
            parts.append('Setting: '+scene)
        if camera and camera.lower() not in concept.lower():
            parts.append('Camera: '+camera)
    else:
        scenes = metadata.get('scene_plan') or []
        fallback = scenes[index % len(scenes)] if scenes else {}
        scene = scene or short(fallback.get('setting'), 180)
        camera = camera or short(fallback.get('camera'), 120)
        if role == 'scene' and scene:
            parts.append('Setting: ' + scene)
        cues = [camera or brief.get('camera', ''), brief.get('lighting', '')]
        parts.append(' '.join(cue for cue in cues if cue))
    if white:
        parts.append('Full product, generous clear margins. No people, props, typography, inset photos or collage.')
    else:
        if role == 'scene' and model_scene:
            parts.append('One generic synthetic adult naturally using or wearing the product; visible face and natural skin texture, without copying a reference person.')
        else:
            parts.append('No people or hands.')
        allows_copy = entry['kind'] == 'detail' or role == 'scene'
        if allows_copy:
            blocks = metadata.get('creative_blocks') or []
            matching = [block for block in blocks if block.get('role') == role]
            block = matching[index % len(matching)] if matching else (blocks[index % len(blocks)] if blocks else {})
            headline = short(ad_copy if role == 'scene' and ad_copy else art.get('headline') or block.get('headline'), 80)
            subheadline = short(art.get('subheadline') or block.get('subheadline'), 80)
            raw_labels = art.get('labels') if isinstance(art.get('labels'), (list, tuple)) else block.get('labels') or []
            labels = [short(text, 18) for text in raw_labels[:4 if rich_copy else 2] if short(text, 18)]
            cta = short(art.get('cta') or block.get('cta'), 12)
            copy_parts = []
            if headline:
                copy_parts.append('headline 「' + headline + '」')
            if subheadline:
                copy_parts.append('subheadline 「' + subheadline + '」')
            if labels:
                copy_parts.append('feature labels ' + '、'.join('「'+text+'」' for text in labels))
            if cta:
                copy_parts.append('CTA 「' + cta + '」')
            if copy_parts:
                parts.append('Render this exact Chinese copy with correct characters: ' + '; '.join(copy_parts) + '.')
            if rich_copy:
                parts.append('Use a rich but readable ecommerce hierarchy: main headline, explanatory subheadline and 2-4 evidence-bound feature callouts. Integrate text with the photography using callout lines, detail crops, layered captions, comparison columns or small icon labels as appropriate; do not reduce the design to one four-character slogan with empty space. Keep all copy together under about 50 Chinese characters.')
            else:
                parts.append('Use a restrained readable ecommerce hierarchy with one headline and 1-2 evidence-bound labels in natural scene whitespace.')
        else:
            parts.append('No advertising text.')
        parts.append(f'Framing variation {index+1}: one focal point, clear margins; a distinct crop from neighboring images.')
    parts.append('Avoid watermarks, fake logos, altered product parts, unsupported claims, measurements or accessories. No care panel.')
    return '\n'.join(part for part in parts if part)


def batch_header(pack, batch, members):
    indices = sorted({i for entry in members for i in entry.get('reference_indices', [])})
    mapping = ', '.join(f'input {i+1}=original photo {original}' for i, original in enumerate(indices))
    return (pack['style_lock'] +
            f"\nCreate exactly one image at {batch['output_size']}, arranged in {batch['rows']} rows by {batch['cols']} columns. "
            'Equal cells in left-to-right, top-to-bottom order. Thin #FFFFFF gutters; keep all content inside its cell with 6% clear margins. '
            'Leave unused cells blank. Create new photography and layouts; references are product evidence, never inset source-photo copies.\n' +
            (f'Reference mapping: {mapping}.\n' if mapping else ''))


def batch_prompt(pack, batch, members):
    if pack.get('prompt_policy') != POLICY:
        header = pack['style_lock']+f"\nCreate exactly one {batch['rows']} by {batch['cols']} contact sheet. Equal-sized cells, thin #FFFFFF gutters, no content crossing cells. Output size {batch['output_size']}. Preserve each module ID's role and match each provided reference. Unused cells blank.\n"
        return header+'\n\n'.join(f"CELL {index+1}:\n{entry['prompt']}" for index,entry in enumerate(members))
    lock = pack['style_lock']+'\n'
    def body(entry):
        prompt = entry['prompt']
        return prompt[len(lock):] if prompt.startswith(lock) else prompt
    return batch_header(pack, batch, members)+'\n\n'.join(
        f"CELL {index+1} [{entry['id']}], original reference photos {entry.get('reference_indices', [])}:\n{body(entry)}"
        for index,entry in enumerate(members))
