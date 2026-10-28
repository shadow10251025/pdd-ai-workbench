# PDD AI 商品图片与上架工作台

面向商家自有商品照片的本地开发项目。主流程为：**多图上传 → 识图 → 策划与提示词 → 商家确认 → 分批生图 → 人工审核 → 商品草稿与上架资料**。

这是开发交接版本。真实商户API、真实商品成图质量与当前拼多多页面还需要联合验收。租赁服务后端已有基础实现，商户网站、本地助手配对与公网部署尚未完整交付。

## 本地启动

已验证环境为 Windows、Python 3.13.9；建议使用独立虚拟环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$env:PDD_AI_PROVIDER = "mock"
.\.venv\Scripts\python.exe run_dev.py
```

打开 http://127.0.0.1:3000/ 。该开发入口无需生产私钥，支持图片工作台、规格与草稿编辑；授权管理、店铺浏览器操作和实际填表接口被禁用。端口冲突可运行 `python run_dev.py --port 3001`。

未配置API时，图片会明确标为“离线演示”，用于验证流程，不能作为生图质量样张。真实测试时在界面填写自己服务商的识图/策划和生图地址、模型与API Key；生成前核对请求上限。临时密钥不保存到浏览器草稿，刷新后需重新填写。

`run_local.py` 是原有正式桌面管理端入口，需要业主持有的授权凭据。仓库不包含这些生产凭据；开发时使用 `run_dev.py`。不要将两个本地入口开放到公网。

## 当前能力

- 拖拽、多选参考照片；颜色/款式与对应实物照片绑定。
- 识图与策划分开，可共用兼容视觉的文字API；先审核标题、建议规格、模块与实际提示词，再确认生图。
- 主图、12类详情模块、SKU预览和可选实测尺码表；数量可配置，支持分轮生成与拼批。
- 优先控制请求成本，按原生像素预算安排批次；不以无限放大代替清晰度。仅本地文件检查，内容由人工审核。
- 提示词逐图查看与修改、模块示例、大图检查、单图重生、重裁与历史版本。
- 稳定SKU编号、价格库存与编码合并、完整商品草稿；库存0可保留。
- 持久化任务、取消/恢复、请求幂等与缓存；提交响应丢失后查询原任务。
- 租赁后端的租户隔离、角色、会话撤销、配额与安全出站请求；部署边界见文档。

## 开发者从这里开始

- [DEVELOPMENT.md](DEVELOPMENT.md)：代码地图、接口、启动、测试和开发方式。
- [HANDOFF_STATUS.md](HANDOFF_STATUS.md)：最新改动、验收范围、已知缺口和下一步。
- [WORKFLOW_20261028.md](WORKFLOW_20261028.md)：两阶段工作流与模块示例。
- [server/RENTAL_DEPLOYMENT.md](server/RENTAL_DEPLOYMENT.md)：租赁服务与公网部署边界。
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：Skill来源、固定版本与第三方许可情况。

```powershell
python -m unittest discover -s tests -p "test_*.py"
# 先在另一个终端运行 run_dev.py，再做真实页面离线验收：
python tests/browser_workbench.py
```

仓库只交付源码、固定模板、原创模块示例和测试。生产签名私钥、资产种子、API Key、账号/店铺登录状态、租户台账、商品照片、任务和旧安装包均不包含。`assets_src/` 是供开发者修改的浏览器脚本源码，属于私有项目代码，不是密钥；不要分发到商户安装包。历史链接抓取组件作为兼容参考保留，不是当前产品主入口。
