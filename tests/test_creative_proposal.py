import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image,ImageDraw
import ai_workflow as w
from ai_api import install_ai_routes
from creative_proposal import batch_prompt,load_proposal


class CreativeProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        app=FastAPI();self.runner=install_ai_routes(app,self.root,lambda:None)
        self.client=TestClient(app);self.addCleanup(self.client.close)
        self.env=patch.dict(os.environ,{'PDD_AI_PROVIDER':'mock','PDD_AI_DEMO':'','PDD_IMAGE_API_URL':'','PDD_IMAGE_API_KEY':'','PDD_PROMPT_API_URL':'','PDD_PROMPT_API_KEY':'','PDD_AI_IMAGE_CACHE':'0'})
        self.env.start();self.addCleanup(self.env.stop)
        image=Image.new('RGB',(700,700),'red');buffer=io.BytesIO();image.save(buffer,'JPEG');self.raw=buffer.getvalue()
        self.options={'hero_count':2,'detail_modules':[],'auto_module_plan':False,'selected_specs':{},'rows':2,'cols':3,'strategy':'grid','output_size':'2048x2048','product_hint':'sample product','model_scene':False}

    def wait(self,tid):
        for _ in range(150):
            data=self.client.get('/api/ai/tasks/'+tid).json()
            if data['status'] not in {'queued','running','cancel_requested'}:
                for _ in range(20):
                    if not self.runner.busy:break
                    time.sleep(.01)
                return data
            time.sleep(.02)
        self.fail('task timed out')

    def metadata(self,url,payload,key,timeout):
        system=payload['messages'][0]['content']
        if '你负责识图' in system:
            result={'title':'Red shirt','specs':{'颜色':['red']},'spec_suggestions':{'尺码':['S','M','L']},'identity_evidence':['red fabric visible'],'reference_bindings':{'red':[1]}}
        else:
            self.assertIsInstance(payload['messages'][1]['content'],str)
            result={'title':'Red shirt draft','module_recommendations':[{'type':'scene','count':2,'reason':'show how to wear'}],
                    'module_prompts':[{'id':'hero-01','prompt':'A white studio hero of the red shirt.'},{'id':'hero-02','prompt':'A side angle of the red shirt.'},{'type':'sku_preview','prompt':'The red variant only.'}]}
        return {'choices':[{'message':{'content':json.dumps(result)}}]}

    def sheet(self,config,prompt,input_image,out_path,rows,cols,progress,trace,refs):
        image=Image.new('RGB',(cols*540,rows*540),'white');draw=ImageDraw.Draw(image)
        for index in range(rows*cols):
            x,y=index%cols*540,index//cols*540;draw.rectangle((x+24,y+24,x+516,y+516),fill=(index*40%255,100,150))
        Path(out_path).parent.mkdir(parents=True,exist_ok=True);image.save(out_path)
        trace.update(mode='offline',reference_sent=False,endpoint_kind='none')
        return image

    def prepare(self,source='',with_photo=True,providers=True):
        fields={'options_json':json.dumps(self.options),'source_proposal_id':source}
        if providers:fields.update(prompt_api_url='https://8.8.8.8/v1/chat/completions',prompt_api_key='planner-secret',prompt_model='vision-capable')
        response=self.client.post('/api/ai/prepare',data=fields,files=[('images',('red.jpg',self.raw,'image/jpeg'))] if with_photo else None)
        self.assertEqual(response.status_code,200,response.text)
        result=self.wait(response.json()['task_id']);self.assertEqual(result['status'],'done',str(result));return result

    def test_prepare_uses_two_text_roles_and_zero_image_requests(self):
        with patch.object(w,'_http_json',side_effect=self.metadata) as text,patch.object(w,'generate_sheet') as image:
            proposal=self.prepare()
            self.assertEqual(text.call_count,2);image.assert_not_called()
        self.assertEqual(proposal['stage'],'awaiting_confirmation');self.assertEqual(proposal['provider_calls'],0)
        self.assertEqual(proposal['metadata']['specs_confirmed'],{'颜色':['red']})
        self.assertNotIn('尺码',proposal['metadata']['specs_confirmed'])
        self.assertEqual(proposal['metadata']['spec_suggestions']['尺码'],['S','M','L'])
        self.assertEqual(proposal['generation_plan']['detail_count'],0)
        self.assertTrue(all(module['prompt'] for module in proposal['module_outputs']))
        self.assertEqual(self.runner.store.entitlement('local')['image_reserved'],0)
        for file in self.root.rglob('*.json'):
            self.assertNotIn('planner-secret',file.read_text('utf-8'))

    def test_confirmation_uses_exact_edited_prompts_and_does_not_repeat_text_ai(self):
        with patch.object(w,'_http_json',side_effect=self.metadata):proposal=self.prepare()
        module=proposal['module_outputs'][0];edited='USER CONFIRMED: clean hero with the photographed red shirt.'
        with patch.object(w,'_http_json') as text,patch.object(w,'generate_metadata') as legacy,patch.object(w,'generate_sheet',side_effect=self.sheet) as image:
            response=self.client.post('/api/ai/confirm',data={'proposal_id':proposal['proposal_id'],'client_request_id':'approved-once','edits_json':json.dumps({'metadata':{'title':'My final title'},'module_prompts':{module['id']:edited}})})
            self.assertEqual(response.status_code,200,response.text);result=self.wait(response.json()['task_id'])
            self.assertEqual(result['status'],'done',str(result));self.assertTrue(result['confirmed_before_generation'])
            self.assertEqual(result['title'],'My final title');text.assert_not_called();legacy.assert_not_called()
            target=next(item for item in result['module_outputs'] if item['id']==module['id'])
            self.assertEqual(target['prompt'],edited)
            self.assertIn(edited,image.call_args_list[0].args[1])
            confirmed=copy.deepcopy(proposal);confirmed['module_outputs'][0]['prompt']=edited
            first=proposal['generation_plan']['round_plan'][0]
            members=[next(item for item in confirmed['module_outputs'] if item['id']==mid) for mid in first['module_ids']]
            self.assertEqual(image.call_args_list[0].args[1],batch_prompt(proposal['prompt_pack'],first,members))
            repeated=self.client.post('/api/ai/confirm',data={'proposal_id':proposal['proposal_id'],'client_request_id':'approved-once','edits_json':json.dumps({'metadata':{'title':'My final title'},'module_prompts':{module['id']:edited}})})
            self.assertTrue(repeated.json()['duplicate'])

    def test_replanning_reuses_recognition_and_new_prompts_precede_generation(self):
        with patch.object(w,'_http_json',side_effect=self.metadata) as text,patch.object(w,'generate_sheet') as image:
            original=self.prepare();self.options['style']='fresh'
            revised=self.prepare(original['proposal_id'],with_photo=False)
            self.assertEqual(text.call_count,3);image.assert_not_called()
            self.assertTrue(revised['text_requests']['recognition_cache_hit'])
            self.assertEqual(revised['text_requests']['recognition'],0)
            self.assertNotEqual(original['module_outputs'][0]['prompt'],revised['module_outputs'][0]['prompt'])

    def test_offline_proposal_does_not_invent_uniform_size_and_confirmation_validates_modules(self):
        with patch.object(w,'generate_sheet') as image:
            proposal=self.prepare(providers=False);image.assert_not_called()
        self.assertEqual(proposal['metadata']['specs_confirmed'],{})
        self.assertEqual(proposal['recognition']['recognition_status'],'not_configured')
        for data in ({'proposal_id':'../private'},{'proposal_id':proposal['proposal_id'],'edits_json':json.dumps({'module_prompts':{'fake-id':'bad'}})}):
            self.assertEqual(self.client.post('/api/ai/confirm',data=data).status_code,422)

    def test_failed_planning_preserves_recognition_for_retry_without_image_calls(self):
        calls=[]
        def fail_planner_once(url,payload,key,timeout):
            calls.append(payload['messages'][0]['content'])
            if len(calls)==2:raise w.WorkflowError('planning connection unavailable')
            return self.metadata(url,payload,key,timeout)
        with patch.object(w,'_http_json',side_effect=fail_planner_once),patch.object(w,'generate_sheet') as image:
            response=self.client.post('/api/ai/prepare',data={'options_json':json.dumps(self.options),'prompt_api_url':'https://8.8.8.8/v1/chat/completions','prompt_api_key':'planner-secret','prompt_model':'vision-capable'},files=[('images',('red.jpg',self.raw,'image/jpeg'))])
            self.assertEqual(response.status_code,200,response.text)
            failed=self.wait(response.json()['task_id'])
            self.assertEqual(failed['status'],'error');self.assertEqual(failed['error_code'],'planning_failed')
            self.assertTrue(failed['proposal_id'])
            recovered=self.prepare(failed['proposal_id'],with_photo=False)
            self.assertEqual(recovered['text_requests']['recognition'],0)
            self.assertEqual(len(calls),3);image.assert_not_called()

    def test_explicit_empty_specs_and_reference_bindings_are_not_reinvented(self):
        with patch.object(w,'_http_json',side_effect=self.metadata):
            first=self.prepare()
            self.options.update(specs_user_modified=True,selected_specs={},variant_references={},variant_references_user_modified=True)
            cleared=self.prepare(first['proposal_id'],with_photo=False)
            self.assertEqual(cleared['metadata']['specs_confirmed'],{})
            self.assertEqual(cleared['generation_plan']['spec_count'],0)
            self.options['selected_specs']={'颜色':['red']}
            missing=self.prepare(cleared['proposal_id'],with_photo=False)
            self.assertEqual(missing['generation_plan']['spec_preview_missing'],['red'])
            self.assertEqual(missing['generation_plan']['spec_count'],0)

    def test_manual_metadata_and_unchanged_module_prompt_survive_replan(self):
        with patch.object(w,'_http_json',side_effect=self.metadata):
            first=self.prepare()
            original_module=first['module_outputs'][0]
            self.options.update(hero_count=3,merchant_metadata={'title':'Merchant title','description':'Merchant description','brand':''},manual_prompts={original_module['id']:'A merchant-edited hero direction.'})
            more=self.prepare(first['proposal_id'],with_photo=False)
            retained=next(item for item in more['module_outputs'] if item['id']==original_module['id'])
            self.assertEqual(retained['prompt'],'A merchant-edited hero direction.')
            self.assertTrue(retained['manual_prompt_retained'])
            self.assertEqual(more['metadata']['title'],'Merchant title')
            self.assertEqual(more['source_images'],first['source_images'])
            self.assertFalse(list((self.root/'data/ai_jobs'/more['proposal_id']).glob('input_*')))
            self.options['style']='luxury'
            changed=self.prepare(more['proposal_id'],with_photo=False)
            revised=next(item for item in changed['module_outputs'] if item['id']==original_module['id'])
            self.assertNotEqual(revised['prompt'],'A merchant-edited hero direction.')
            self.assertEqual(revised['previous_manual_prompt'],'A merchant-edited hero direction.')
            self.assertNotEqual(revised['context_signature'],retained['context_signature'])

    def test_one_generic_ai_direction_does_not_pretend_to_cover_three_scenes(self):
        def one_direction(url,payload,key,timeout):
            response=self.metadata(url,payload,key,timeout)
            if '你负责识图' not in payload['messages'][0]['content']:
                response={'choices':[{'message':{'content':json.dumps({'title':'Red shirt','module_prompts':[{'type':'scene','prompt':'One distinct scene concept.'}]})}}]}
            return response
        self.options['detail_modules']=[{'type':'scene','count':3}]
        with patch.object(w,'_http_json',side_effect=one_direction):proposal=self.prepare()
        scenes=[item for item in proposal['module_outputs'] if item['module_type']=='scene']
        self.assertEqual(len(scenes),3)
        self.assertEqual(sum(item['prompt_author']=='planner_ai' for item in scenes),1)
        self.assertEqual(len({item['prompt'] for item in scenes}),3)
        self.assertTrue(any('缺少策划AI专属提示词' in item for item in proposal['warnings']))

    def test_bad_options_are_rejected_before_text_calls_and_lookup_is_private(self):
        with patch.object(w,'_http_json') as text:
            for changes in ({'hero_count':{}},{'merchant_metadata':['title']},{'manual_prompts':{'hero-01':''}},{'max_image_requests':1000}):
                result=self.client.post('/api/ai/prepare',data={'options_json':json.dumps({**self.options,**changes})},files=[('images',('photo.jpg',self.raw,'image/jpeg'))])
                self.assertEqual(result.status_code,422,result.text)
            text.assert_not_called()
        task,_=self.runner.store.create_task('local','workflow',{'title':'private product'},'known-request')
        found=self.client.get('/api/ai/requests/known-request').json()
        self.assertEqual(found['task_id'],task['id']);self.assertNotIn('private product',str(found))
        self.runner.owner='another-owner'
        self.assertFalse(self.client.get('/api/ai/requests/known-request').json()['found'])


if __name__=='__main__':unittest.main()
