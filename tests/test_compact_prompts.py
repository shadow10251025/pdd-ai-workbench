import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import ai_workflow as w
from creative_proposal import prepare_proposal, batch_prompt
from image_prompting import make_pack, module_prompt, skill_brief


class CompactPromptTests(unittest.TestCase):
    def setUp(self):
        self.image = io.BytesIO()
        Image.new('RGB', (700, 700), 'red').save(self.image, 'JPEG')

    def test_auto_visual_direction_is_product_specific_and_not_a_preset(self):
        for hint, visual in [('衬衫', 'Warm linen editorial with an urban street setting.'),
                             ('耳机', 'Dark immersive music photography with restrained violet highlights.'),
                             ('收纳盒', 'Practical family-home photography in a lived-in room.')]:
            def response(url, payload, key, timeout):
                if '你负责识图' in payload['messages'][0]['content']:
                    result = {'title': hint, 'identity_evidence': ['visible product']}
                else:
                    request = json.loads(payload['messages'][1]['content'])
                    self.assertNotIn('skill_guidance', request)
                    self.assertNotIn('manual_prompts', str(request))
                    self.assertLess(len(payload['messages'][1]['content']), 6000)
                    result = {'visual_direction': {'summary': visual, 'reason': '按商品与用途选择'},
                              'module_prompts': [{'id': 'hero-01', 'prompt': 'A clear full product photograph.'},
                                                 {'id': 'detail-scene-01', 'prompt': 'Natural use of the product in the suggested setting.', 'headline': '日常好搭'}]}
                return {'choices': [{'message': {'content': json.dumps(result)}}]}
            with tempfile.TemporaryDirectory() as directory, patch.object(w, '_http_json', side_effect=response), patch.object(w, 'generate_sheet') as image:
                result = prepare_proposal(Path(directory), [self.image.getvalue()],
                    {'product_hint': hint, 'hero_count': 1, 'style': 'auto', 'model_scene': True,
                     'detail_modules': [{'type': 'scene', 'count': 1}], 'output_size': '2048x2048'},
                    {'prompt_api_url': 'https://8.8.8.8/v1/chat/completions', 'prompt_api_key': 'test-only-key'})
                image.assert_not_called()
            self.assertIn(visual, result['prompt_pack']['style_lock'])
            self.assertEqual(result['prompt_pack']['visual_direction']['mode'], 'auto')
            self.assertEqual(result['module_outputs'][0]['template_id'], 'hero-image')
            scene = result['module_outputs'][1]
            self.assertEqual(scene['template_id'], 'model-showcase')
            self.assertIn('visible face', scene['prompt'])
            self.assertNotIn('No people', scene['prompt'])
            self.assertIn('「日常好搭」', scene['prompt'])
            self.assertTrue(all(len(entry['prompt']) < 1800 for entry in result['module_outputs']))
            actual = result['batch_prompts'][0]['prompt']
            self.assertEqual(actual.count('Campaign Style Lock:'), 1)
            self.assertEqual(actual.count(visual), 1)
            self.assertNotIn('8K', actual)
            self.assertIn('6% clear margins', actual)

    def test_requested_preset_wins_and_missing_template_values_are_omitted(self):
        pack = make_pack('tech', 'Pastel cottage illustration.')
        self.assertNotIn('Pastel cottage', pack['style_lock'])
        brief = skill_brief({'kind': 'detail', 'module_type': 'scene'}, False)
        self.assertEqual(brief['id'], 'lifestyle-scene')
        self.assertNotIn('{', json.dumps(brief)[1:])
        self.assertNotIn('photographed product evidence for', str(brief))

    def test_sku_uses_only_bound_variant_and_batch_mapping_is_explicit(self):
        entry = {'id': 'spec-blue', 'kind': 'spec', 'module_type': 'sku_preview',
                 'spec_value': 'blue', 'reference_indices': [2], 'render_source': 'provider'}
        pack = make_pack()
        prompt = module_prompt(w, entry, 0, pack, {'title': 'Red shirt', 'identity_evidence': ['red cotton']}, 'Red shirt', True, '', [])
        self.assertNotIn('Red shirt', prompt)
        self.assertNotIn('red cotton', prompt)
        self.assertIn('SKU「blue」', prompt)
        self.assertIn('No people', prompt)
        entry['prompt'] = prompt
        batch = {'rows': 1, 'cols': 1, 'output_size': '1024x1024'}
        actual = batch_prompt(pack, batch, [entry])
        self.assertIn('input 1=original photo 2', actual)
        self.assertEqual(actual.count('Campaign Style Lock:'), 1)
        # Merchant edits are never truncated or rewritten by the batch composer.
        entry['prompt'] = 'MANUAL COPY: '+('detail ' * 400)
        self.assertIn(entry['prompt'], batch_prompt(pack, batch, [entry]))

    def test_rich_detail_copy_reaches_the_actual_image_prompt(self):
        entry = {'id': 'detail-scene-01', 'kind': 'detail', 'module_type': 'scene',
                 'reference_indices': [1], 'render_source': 'provider'}
        direction = {
            'prompt': 'A warm cafe commute scene with layered ecommerce callouts.',
            'headline': '从通勤到周末都好搭',
            'subheadline': '自然融入日常衣橱，轻松完成多种场合穿搭',
            'labels': ['利落立领', '顺滑拉链', '双侧口袋'],
            'cta': '查看颜色',
        }
        pack = make_pack('auto', 'Warm urban fashion photography.')
        pack['creative_density'] = 'rich'
        prompt = module_prompt(w, entry, 0, pack, {'title': '灰色拉链外套'}, '灰色外套', True, '', [], direction)
        self.assertIn('headline 「从通勤到周末都好搭」', prompt)
        self.assertIn('subheadline 「自然融入日常衣橱，轻松完成多种场合穿搭」', prompt)
        self.assertIn('「利落立领」、「顺滑拉链」、「双侧口袋」', prompt)
        self.assertIn('CTA 「查看颜色」', prompt)
        self.assertIn('do not reduce the design to one four-character slogan', prompt)
        self.assertNotIn('no repeated boxes or dense text', prompt)

        pack['creative_density'] = 'conservative'
        conservative = module_prompt(w, entry, 0, pack, {'title': '灰色拉链外套'}, '灰色外套', True, '', [], direction)
        self.assertIn('one headline and 1-2 evidence-bound labels', conservative)
        self.assertNotIn('「双侧口袋」', conservative)

        white = {'id': 'hero-01', 'kind': 'hero', 'module_type': 'white_pack',
                 'reference_indices': [1], 'render_source': 'provider'}
        white_prompt = module_prompt(w, white, 0, pack, {'title': '灰色拉链外套'}, '灰色外套', True, '', [], direction)
        self.assertIn('No people, props, typography', white_prompt)
        self.assertNotIn('从通勤到周末都好搭', white_prompt)

    def test_style_does_not_change_pixel_budget_counts_or_cropping_geometry(self):
        options = {'product_hint': 'test product', 'hero_count': 5, 'rows': 3, 'cols': 3,
                   'detail_modules': [{'type': 'scene', 'count': 20}], 'output_size': '2048x2048'}
        baseline = w.build_generation_plan(**options, style='minimal')
        for style in ('auto', 'fresh', 'luxury', 'tech'):
            plan = w.build_generation_plan(**options, style=style)
            self.assertEqual(plan['round_plan'], baseline['round_plan'])
            self.assertEqual([entry['id'] for entry in plan['module_outputs']], [entry['id'] for entry in baseline['module_outputs']])
            self.assertEqual(plan['detail_count'], 20)
            self.assertEqual(plan['planned_image_requests'], baseline['planned_image_requests'])


if __name__ == '__main__':
    unittest.main()
