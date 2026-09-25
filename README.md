# Studywell NCLEX Study Room

这是一个本地版的 Studywell NCLEX-RN 学习网站。它根据在线项目
`https://nclex-study-room.creamy-basil-3796.chatgpt.site/` 复制而来，目标是让项目文件保存在本机，并可以继续修改和运行。

## 项目位置

```text
/Users/wuyajing/Project/study
```

主要文件：

- `index.html`：页面入口和弹窗结构
- `style.css`：页面样式
- `app.js`：导航、模块、练习、进度和本地存储逻辑
- `import-material.js`：已弃用的旧浏览器端纯文本提取器，当前页面不再加载
- `vendor/jszip.min.js`：旧提取器的遗留依赖，当前页面不再加载
- `server.py`：本地静态服务器和本地 API 兼容层
- `document_extraction.py`：按页/幻灯片解析的结构化提取服务
- `requirements.txt`：PDF 与 Office 解析依赖

## 启动本地网站

项目内已经保存了一个可独立运行的 Python 3.12：

```text
/Users/wuyajing/Project/study/.python312/bin/python3
```

启动：

```bash
cd /Users/wuyajing/Project/study
.python312/bin/python3 server.py --port 4173
```

然后打开：

```text
http://localhost:4173/
```

也可以直接使用全局 `python3`。当前用户级 Python 默认配置指向同一个 Python 3.12 解释器。

## 当前功能

- Dashboard 仪表盘
- Study modules 学习模块
- Practice quiz 练习题
- My progress 学习进度
- My materials 学习材料
- 创建、编辑、删除学习模块
- 导入 DOCX、PPTX、TXT、MD 文件
- 浏览器本地保存模块和练习记录
- 内置 skin assessment 演示模块和演示题

数据默认保存在浏览器的 `localStorage` 中，键名为 `studywell-v1`。清理浏览器站点数据会清除本地模块和练习记录。

## 本地模式限制

在线网站的 AI 接口不会随网页源文件公开复制。本地 `server.py` 提供了兼容接口：

- `/api/material/format`：已弃用，返回 410；请使用 `/api/document/extract`
- `/api/quiz/demo`：返回内置演示题
- `/api/quiz/status`：报告 Hikari AI 题目生成配置状态
- `/api/quiz/generate`：调用 Hikari Responses API 生成 NCLEX 练习题，并严格校验格式与原文引用
- `/api/document/extract`：上传原始文件并返回经过 Pydantic 校验的章节结构

本地安装了 PyMuPDF、Pydantic、python-docx 和 python-pptx。PDF 页面会渲染为图像；DOCX 按段落、标题和表格行解析；PPTX 按幻灯片、形状位置和表格行解析。

本地结构化提取的 `key_terms` 使用 `extract_key_terms()` 进行源文档约束的术语清洗：优先完整医学短语，有限度地合并复数/词形，去除重复和泛化词，不从原文之外补充术语。视觉模型返回的结构仍需对照原始材料复核。

没有配置视觉 API 时，服务会明确返回 `local_structural` 模式和低置信度提示。配置以下环境变量后，PDF 页面图像和上传图片会发送给 OpenAI-compatible 视觉模型，并以严格 JSON Schema 输出；视觉请求每批最多处理 5 页：

```bash
export VISION_API_KEY="你的服务 API key"
export VISION_API_BASE_URL="https://api.openai.com/v1"
export VISION_MODEL="gpt-4o"
```

`VISION_API_BASE_URL` 可指向兼容的服务商端点。DOCX/PPTX 使用结构化 Office 解析并把嵌入图像提供给视觉模型；当前不会把整个 Office 文件渲染成逐页截图。不要把 API key 写入前端文件、README 或 Git 仓库。

## Hikari AI 临床题目生成

测验生成使用 Hikari Responses API。先在终端设置密钥和模型，再从同一终端启动服务：

```bash
export STUDYWELL_API_KEY="你的 Hikari API key"
export STUDYWELL_API_BASE_URL="https://hikariapi.xyz/v1"
export STUDYWELL_MODEL="gpt-5.6-sol"
.python312/bin/python3 server.py --port 4173
```

可选的题目生成调节变量（Render 上不设置也可以使用默认值）：

```text
QUIZ_MAX_COUNT=100
QUIZ_BATCH_SIZE=10
QUIZ_MAX_CONCURRENCY=4
QUIZ_BATCH_RETRIES=2
```

如果 Hikari API key 已设置在 `OPENAI_API_KEY`，服务也会读取该变量。题目要求 JSON Schema 输出，校验选项、单选题类别及逐字原文证据；任何引用与所选材料不匹配的结果都会被拒绝。所选学习材料会发送给 Hikari 生成题目。AI 题目仍应对照可靠护理教材复核，不替代权威 NCLEX 备考资料。

## Supabase shared modules

The shared workspace uses these server-only Render environment variables:

```text
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-server-secret-key
SUPABASE_STORAGE_BUCKET=study-materials
```

The browser never receives the service-role key. The Python backend stores module metadata in the Supabase `public.modules` table and uploads original documents to the configured private Storage bucket. Browser `localStorage` is only a temporary migration fallback when the shared API is unavailable; Supabase is the primary source of truth once the shared API loads successfully.

Because automatic table exposure was disabled during project setup, manually expose `public.modules` in Supabase under `Project Settings -> Data API -> Exposed schemas and tables` (or the equivalent `Data API` table exposure screen). Keep the Storage bucket private.

题目生成现在采用分块、批量 workflow：

1. 已有 `structuredSections` 的模块直接保留 section ID、页码、标题、段落、项目符号、术语和证据；纯文本模块由 `Extractor` 输出相同结构的 `chunks` JSON。原始文本不足以覆盖结构化结果时，会额外保留有界的原文块。
2. 后端按内容权重建立 coverage plan，限制单个章节最多约占 45%，再把题目分成每批约 10 题的请求。每批只接收自己负责的 chunks，不重复发送整份讲义。
3. Generator 批次在受控并行度（默认 4）下调用 Hikari。每批必须返回准确数量；Pydantic/schema、证据、选项、重复概念和引用校验失败时只重试该批。
4. 所有批次完成后进行全局近似去重；缺口只生成 replacement batches。响应中的 `generation` 字段包含批次数、并发度、章节 coverage 和警告。

系统支持 25、50 和 100 题，后端硬上限为 100。若材料没有足够的独立、可引用信息，系统会返回已通过检查的题目并明确说明缺口，不会为了凑数编造医学事实。AI 结果仍需由专业人员对照权威护理教材复核。

Extractor 的 Pydantic schema 校验失败时会自动重试 2 次；三次都失败才返回错误，不会把不合规 JSON 传给 Generator。视觉文档解析默认设有限额：每次最多 20 页、每页最多 8 MB 的图像载荷。可按需通过环境变量调整，但更高限制会增加 token 消耗：

```bash
export VISION_MAX_PAGES="20"
export VISION_MAX_PAGE_BYTES="8388608"
```

使用 `Multimodal vision extraction` 时，上传预览会提示图像可能产生模型 token 用量；超过限制的文件会在发送到视觉 API 前被拒绝。

## 更换 ChatGPT 账号后的恢复说明

旧浏览器中已经保存的模块只包含过去的文本结果，不包含原始文件。要让旧模块获得新的 sections、页码和表格结构，请重新上传原始 DOCX/PPTX/PDF/图片；不能从已压平的文本恢复原始版面。

把这个项目目录交给新的 ChatGPT/Codex 助手，并说明：

> 请先阅读 `/Users/wuyajing/Project/study/README.md`。这是 Studywell NCLEX 学习网站的本地项目。请保留现有界面和数据结构，先检查当前文件，再修改代码。不要删除 `.python312`，除非我明确要求。

新的助手应先检查：

1. `README.md`
2. `index.html`
3. `app.js`
4. `style.css`
5. `server.py`

如果只是运行项目，使用上面的本地启动命令即可。如果需要恢复在线版本背景，参考：

```text
在线地址：https://nclex-study-room.creamy-basil-3796.chatgpt.site/
原项目名称：studywell-nclex-rn-practice-sites-project
```

## 安全提醒

- 不要分享 `/Users/wuyajing/.codex/auth.json`。
- 不要把 ChatGPT 登录信息、API key、Cookie 或浏览器导出数据放进项目。
- `auth.json` 是账号凭据文件，与本项目无关，不应复制到项目目录。
