"""Draft migration and listing guard regressions; no provider/browser calls."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from product_package import (merge_skus_for_specs, normalize_product_package,
                             preflight_listing, sku_id_for_specs, validate_listing_payload)


class ProductPackageTests(unittest.TestCase):
    def valid(self):
        return {"title": "纯棉衬衣", "main_images": ["data/main.jpg"], "detail_images": [],
                "skus": [{"name": "默认", "price": 12.30, "single_price": 15, "stock": 0,
                          "enabled": True, "spec_code": "001"}], "shops": [9222]}

    def test_sku_merge_preserves_zero_price_inventory_code_and_disabled(self):
        specs = {"颜色": ["奶油 白", "黑色"], "尺码": ["M", "L"]}
        rows = merge_skus_for_specs(specs)
        rows[0].update(price=19.8, single_price=25, stock=0, spec_code="1001", enabled=False)
        changed = {"尺码": ["L", "M", "XL"], "颜色": ["黑色", "奶油 白"]}
        merged = merge_skus_for_specs(changed, rows)
        old = next(row for row in merged if row["id"] == rows[0]["id"])
        self.assertEqual((old["price"], old["single_price"], old["stock"], old["spec_code"], old["enabled"]), (19.8, 25, 0, "1001", False))
        self.assertEqual(len(merged), 6)
        self.assertEqual(len({row["spec_code"] for row in merged}), 6)
        self.assertEqual([row["stock"] for row in merged if row["spec_values"]["尺码"] == "XL"], [0, 0])

    def test_legacy_exact_name_adapter_and_ambiguous_names(self):
        rows = merge_skus_for_specs({"颜色": ["红 色"], "尺码": ["L"]}, [{"name": "红 色 L", "stock": 0, "price": 5, "single_price": 6}])
        self.assertEqual(rows[0]["price"], 5)
        ambiguous = merge_skus_for_specs({"一": ["a b", "a"], "二": ["c", "b c"]}, [{"name": "a b c", "price": 99}])
        self.assertTrue(all(row["price"] == 0 for row in ambiguous if row["name"] == "a b c"))

    def test_more_than_200_combinations_is_explicit(self):
        with self.assertRaises(ValueError):
            merge_skus_for_specs({"颜色": list(map(str, range(21))), "尺码": list(map(str, range(10)))})

    def test_complete_round_trip_and_explicit_empty_specs(self):
        draft = {"id": "a", "job_id": "j", "title": "衣服", "brand": "商家", "attrs": {"材质": "棉"},
                 "description": "描述", "category": "服装", "specs": {"尺码": ["L"]},
                 "sku": [{"name": "L", "price": 22, "single_price": 30, "stock": 0, "spec_code": "abc"}],
                 "main_images": ["data/b.jpg", "data/a.jpg"], "detail_images": ["data/detail.jpg"],
                 "module_outputs": [{"module_id": "hero-1", "review": "approved", "prompt": "自然光"}],
                 "review": {"hero-1": "approved"}, "version_history": [{"version": 1}],
                 "provider_overrides": {"api_key": "never-save"}}
        restored = normalize_product_package(json.loads(json.dumps(normalize_product_package(draft))))
        self.assertEqual(restored["sku"][0]["stock"], 0)
        for key in ("brand", "attrs", "description", "category", "main_images", "detail_images", "module_outputs", "review", "version_history"):
            self.assertEqual(restored[key], draft[key])
        self.assertNotIn("never-save", json.dumps(restored))
        empty = normalize_product_package({"specs": {}, "specs_confirmed": {"尺码": ["XL"]}, "sku": []})
        self.assertEqual(empty["specs"], {})
        self.assertEqual(empty["sku"], [])

    def test_invalid_prices_and_missing_inventory_block_before_listing(self):
        self.assertTrue(preflight_listing(self.valid())["ok"])
        for field, value in (("price", float("nan")), ("price", float("inf")), ("price", -1), ("price", 1.234), ("single_price", 1), ("stock", -1), ("stock", 0.5), ("stock", None), ("stock", True)):
            draft = self.valid()
            draft["skus"][0][field] = value
            self.assertFalse(preflight_listing(draft)["ok"], (field, value))
        draft = self.valid()
        del draft["skus"][0]["stock"]
        self.assertTrue(any(row["field"].endswith(".stock") for row in validate_listing_payload(draft)))

    def test_empty_main_and_uncertain_shop_block(self):
        for field, value in (("main_images", []), ("main_images", [""]), ("shops", []), ("shops", [True]), ("shops", [80]), ("title", "")):
            draft = self.valid()
            draft[field] = value
            self.assertFalse(preflight_listing(draft)["ok"], field)

    def test_local_image_paths_are_contained_and_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data" / "main.jpg").write_bytes(b"fixture")
            draft = self.valid()
            self.assertTrue(preflight_listing(draft, project_dir=root)["ok"])
            draft["main_images"] = ["/data/main.jpg"]
            self.assertTrue(preflight_listing(draft, project_dir=root)["ok"])
            for path in ("data/missing.jpg", "../outside.jpg", str(root / "outside.jpg")):
                draft["main_images"] = [path]
                self.assertFalse(preflight_listing(draft, project_dir=root)["ok"])

    @unittest.skipUnless(shutil.which("node"), "Node.js required for actual frontend behaviour")
    def test_frontend_draft_merge_save_refresh_switch_and_preflight(self):
        source = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
        expected_id = sku_id_for_specs({"颜色": "奶油 白", "尺码": "L"})
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
function boot(seed={}){
 const store=new Map(Object.entries(seed)),elements=new Map(),alerts=[],requests=[];
 const el=id=>{if(!elements.has(id))elements.set(id,{value:'',style:{},innerHTML:'',textContent:'',addEventListener(){},classList:{add(){},remove(){},toggle(){}}});return elements.get(id)};
 const box={TextEncoder,Uint8Array,Uint32Array,DataView,console,JSON,URL,setInterval:()=>1,clearInterval(){},setTimeout:()=>1,
 document:{getElementById:el,addEventListener(){},querySelectorAll:()=>[],querySelector:()=>null},
 localStorage:{getItem:key=>store.get(key)||null,setItem:(key,value)=>store.set(key,value),removeItem:key=>store.delete(key)},
 alert:value=>alerts.push(value),confirm:()=>true,
 fetch:async(url,options={})=>{requests.push({url,options});return {ok:true,json:async()=>url==='/api/migrated-products'&&options.method==='POST'?{success:true,id:'saved-a'}:url==='/api/listing/preflight'?{ok:false,errors:[{message:'图片缺失'}]}:url==='/api/migrated-products'?[]:{shops:[]}}}};
 box.window=box;vm.createContext(box);vm.runInContext(source,box);return {box,store,el,alerts,requests,run:code=>vm.runInContext(code,box)};
}
(async()=>{
 const page=boot();
 assert.equal(page.run(`productSkuId({'颜色':'奶油 白','尺码':'L'})`),process.argv[2]);
 page.run(`applyProductPackageToListing({id:'a',job_id:'job-a',title:'衣服',brand:'品牌A',attrs:{材质:'棉'},description:'描述A',category:'服装',specs:{颜色:['奶油 白'],尺码:['L']},main_images:['data/hero.jpg'],detail_images:['data/d2.jpg','data/d1.jpg'],module_outputs:[{module_id:'hero-1',prompt:'自然光'}],review:{'hero-1':'approved'},version_history:[{version:1}]});updateSku(0,'price','19.8');updateSku(0,'single_price','29');updateSku(0,'stock','0');updateSku(0,'spec_code','custom-code');updateSku(0,'enabled',false);`);
 page.run(`applyProductPackageToListing({id:'a',job_id:'job-a',title:'衣服',brand:'品牌A',attrs:{材质:'棉'},description:'描述A',category:'服装',specs:{颜色:['奶油 白'],尺码:['L','XL']},main_images:['data/hero.jpg'],detail_images:['data/d2.jpg','data/d1.jpg'],module_outputs:[{module_id:'hero-1',prompt:'自然光'}],review:{'hero-1':'approved'},version_history:[{version:1}]});`);
 const kept=page.box.getListingProductPackage();
 assert.equal(kept.sku[0].price,19.8);assert.equal(kept.sku[0].stock,0);assert.equal(kept.sku[0].enabled,false);assert.equal(kept.sku[0].spec_code,'custom-code');assert.equal(kept.sku[1].stock,0);
 await page.run(`_saveMigratedProductRequest('衣服')`);
 const saved=JSON.parse(page.requests.find(r=>r.options.method==='POST'&&r.url==='/api/migrated-products').options.body);
 assert.equal(saved.brand,'品牌A');assert.equal(saved.description,'描述A');assert.equal(saved.review['hero-1'],'approved');assert.equal(saved.module_outputs[0].prompt,'自然光');assert.deepEqual(saved.detail_images,['data/d2.jpg','data/d1.jpg']);
 const refresh=boot(Object.fromEntries(page.store));refresh.run(`applyProductPackageToListing(currentDraftPackage)`);
 assert.equal(refresh.box.getListingProductPackage().sku[0].stock,0);assert.equal(refresh.box.getListingProductPackage().brand,'品牌A');
 refresh.run(`state.migratedProducts=[{id:'b',title:'杯子',sku:[{name:'默认',price:10,single_price:12,stock:0}],main_images:['data/cup.jpg']}];loadMigratedProduct('b');`);
 const other=refresh.box.getListingProductPackage();assert.equal(other.brand,'');assert.deepEqual(other.attrs,{});assert.equal(other.description,'');assert.equal(other.module_outputs,undefined);assert.equal(other.sku[0].stock,0);
 refresh.el('batchPrice').value='12';refresh.el('batchSinglePrice').value='15';refresh.el('batchStock').value='0';refresh.run('batchSetSkuPrices()');assert.equal(refresh.box.getListingProductPackage().sku[0].stock,0);
 const good={title:'杯子',main_images:['data/cup.jpg'],skus:[{name:'默认',price:10,single_price:12,stock:0}],shops:[9222]};assert.equal(refresh.box.listingValidationErrors(good).length,0);
 for(const stock of [-1,.5,null,undefined,'',true])assert(refresh.box.listingValidationErrors({...good,skus:[{...good.skus[0],stock}]}).length);
 await refresh.run('startListing()');assert(!refresh.requests.some(r=>r.url==='/api/start-listing'));assert(refresh.alerts.some(t=>String(t).includes('目标店铺')));
 const attempt=boot();attempt.run(`applyProductPackageToListing({id:'x',title:'杯子',main_images:['data/main.jpg'],sku:[{name:'默认',price:10,single_price:12,stock:0}]})`);await attempt.run('_doStartListing([9222])');assert(attempt.requests.some(r=>r.url==='/api/listing/preflight'));assert(!attempt.requests.some(r=>r.url==='/api/start-listing'));
 page.run(`state.migratedProducts=[{id:"x' onclick='evil",title:'<img src=x onerror=evil>',main_images:['data/x.jpg']}];renderProducts()`);assert(!page.el('productList').innerHTML.includes('<img src=x'));assert(page.el('productList').innerHTML.includes('&lt;img'));
 console.log('frontend draft, SKU, save, refresh, switch and listing guards passed');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(source), expected_id], text=True, capture_output=True, timeout=30, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
