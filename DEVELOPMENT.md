# 开发交接

## 入口与运行方式

使用README中的虚拟环境和锁定依赖，执行 `python run_dev.py`。本入口仅监听127.0.0.1，保留Host、Origin、会话令牌和上传大小校验；支持AI工作台和本地草稿，不开启发码、店铺操作和实际填表。

`run_dev.py` 的限制只属于显式启动的开发进程，不改变 `run_local.py`、桌面授权判断或 `rental_server.py` 的授权。正式店铺联调需要另行配置隔离测试环境与专用授权，不能要求业主把生产签名私钥放入GitHub。`assets_src/` 从2026-10-01原始交接包恢复，仅提取可编辑脚本和清单，没有提取私钥、种子和台账。当前工作目录原先只保留了授权后的密文资产，恢复源码是为了让开发者能继续修改填表逻辑。正式资产打包前须复核该源码与实际部署版本。

AI Key由商家在页面临时填写；不需要复制业主的 `.env`。`.env.example` 为无凭据配置示例，程序不会自动加载其中设置。离线测试应显式设置 `PDD_AI_PROVIDER=mock`，并保持真实API输入框为空。真实生图测试须明确照片和小额请求预算。

## 代码地图

| 文件/目录 | 职责 |
|---|---|
| `web/index.html`、`web/static/style.css` | 工作区布局和样式 |
| `web/static/ai_workflow.js` | 上传、图片审核、规格、结果及上架资料衔接 |
| `web/static/creative_workbench.js` | 识图策划、确认、编辑恢复和重复提交处理 |
| `web/static/module_examples.js` | 12类本地模块示例，无API请求 |
| `web/app.py` | 本地接口、完整草稿、店铺选择及填表前校验 |
| `creative_proposal.py` | 识图与策划结果、模板提示词、商家编辑和方案确认 |
| `core_planner.py`、`workflow_engine.py` | 数量、稳定模块编号、像素预算、批次和执行 |
| `ai_api.py`、`task_store.py` | API、持久化任务、幂等、取消/恢复及请求记录 |
| `ai_workflow.py`、`skill_templates.py` | 生图适配、参考图、缓存和锁定Skill模板 |
| `ai_review.py`、`image_recrop.py`、`size_chart.py` | 文件检查、重裁和真实尺码表排版 |
| `product_package.py`、`local_drafts.py` | 商品包、稳定SKU和SQLite草稿 |
| `one_click_list.py`、`listing_readback.py`、`assets_src/` | PDD填表、店铺身份和结果回读；需要真实页面联调 |
| `rental_server.py` | 租赁服务认证、租户、角色、额度和网关 |
| `safe_http.py`、`resource_limits.py`、`local_security.py` | 出站、上传资源与本地访问边界 |
| `maintenance.py`、`tools/release_workbench.py` | 引用保护、备份清理与本地代码发布 |
| `server/main.py` | 旧授权服务；与新租赁入口不同 |
| `browser/`、`extractor/`、`publisher/`、`utils/`、`one_click.py` | 历史兼容源码，部分依赖已缺失的旧 `config.py`；不作为当前入口 |

## 核心接口

1. `POST /api/ai/plan`：本地规则估算，不调用收费接口。
2. `POST /api/ai/prepare`：识图与策划；可能产生文字/视觉API费用，尚不生图。
3. `POST /api/ai/confirm`：提交确认内容并执行锁定生图计划。
4. `GET /api/ai/tasks/{id}`：查询持久化进度与结果。
5. `GET /api/ai/requests/{client_request_id}`：响应丢失后定位原提交，避免新建重复任务。
6. `POST /api/ai/regenerate`、`POST /api/ai/recrop`：仅修改指定模块。
7. `/api/migrated-products`：兼容接口名下保存完整商品草稿。
8. `POST /api/listing/preflight`：校验资料；实际填表在正式入口完成。

本地写请求先调用 `GET /api/session`，再带 `X-PDD-Session`。页面公共请求封装会自动处理；重启服务后刷新页面获取新令牌。线上租赁服务使用其独立认证及CSRF约定，不能直接复用桌面权限。

商品和图片使用 `ProductPackage`、稳定SKU及模块编号关联。`detail_modules=[]` 是主动取消全部，不能被当成未填写。价格、库存0、编码和启用状态按SKU编号合并。缺少颜色实物参考时不允许拿普通详情图补位。

## 测试和交付

自动测试不调用用户真实收费接口。`tests/browser_workbench.py` 对启动中的本地页面进行离线检查，会新建明确标记的验收草稿并只删除该草稿。生成的验收图片保留在本地数据目录，截图和报告写到被忽略的 `validation/`。

发布使用 `tools/release_workbench.py` 只复制代码和静态资源，不复制keys或业务数据。GitHub交接使用 `tools/export_developer.py` 的白名单导出；它不会覆盖已有目的目录，包含敏感文件与高置信度密钥检查。不要使用历史 `tools/pack_handover.py`，该旧脚本的目标是包含生产私钥的整包，已从本次开发仓库排除。

`SOURCE_MANIFEST.json` 记录本次初始交付文件的SHA-256；后续开发修改后清单会自然过期，应重新生成或通过Git提交记录追踪。新增商业许可并非本次交接任务，项目自有代码未授予公众开源许可。
