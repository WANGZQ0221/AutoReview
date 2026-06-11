# AutoReview

AutoReview 用于把 Android 应用的“打包、提交、查询、审核协作”流程自动化。

当前版本目标很明确：**给一个 APK，复用已配置好的应用材料，完成 OPPO 简单提交；也支持批量打包和批量提交。**

## 当前状态

### 已实现

- OPPO 提交主流程：已支持获取 token、签名、上传 APK/材料、提交审核、查询任务状态、查询应用状态。
- 只给 APK 提交：已支持 `--apk` 覆盖本次 APK，其他图标、截图、版权证明等材料可用 `--reuse-remote-materials` 从 OPPO 平台已有版本复用。
- 批量提交：已支持 `batch-submit`，通过 `config/oppo_batch.json` 批量提交多个 APK/配置。
- 批量打包入口：已支持 `package-apk` 和 `batch-package`，包装旧的 `package.js` 流程。
- 飞书长连接机器人：已支持飞书长连接收消息、回复消息、接收文本/图片/文件。
- OCR 与驳回分析：已支持图片 OCR，OPPO 驳回截图可自动分析，并生成整改清单。
- 飞书配置编辑：已支持查看配置、暂存修改、确认保存、自动备份；密钥字段不展示、不允许通过飞书改。
- 飞书上传材料绑定：已支持上传 APK、图标、截图、版权证明、ICP 证明并绑定到本地配置；当前主流程不依赖这个能力。
- 应用商店竞品搜索与月度记录：已支持通过飞书搜索公开应用商店结果，并把竞品公开下载/评分指标按月份写入会话状态。
- 多渠道配置模板：已增加 OPPO、小米、荣耀、vivo、华为配置模板，字段结构统一。
- 配置文件化：已取消环境变量读取，AutoReview 配置全部从 JSON 文件读取。

### 部分实现 / 依赖外部条件

- 自动打包：AutoReview 包装器已实现，但真实打包依赖安卓项目迁移、`packlist.xls`、`jksconfig.txt`、`app/build.gradle`、Gradle 环境和 Node 依赖。
- 自动提交跑通：流程已跑通到 OPPO 任务阶段；之前失败原因是 APK 自身 `targetSdkVersion < 30`，不是提交流程问题。
- 批量打包 + 批量提交串联：两段能力都有，但还没做“一条命令打包后自动提交”的编排。

### 未实现

- 小米 / 荣耀 / vivo / 华为 API 提交：目前只有配置模板，还没接各平台真实 API。
- 自动撤回 / 取消审核：目前提交成功后需要到 OPPO 后台手动撤回。
- 飞书一键自动提交 OPPO：目前飞书主要用于协作、查询、配置、OCR 分析，不直接触发最终提交。
- 审核意见中的视频解析：OCR 主要处理截图/图片文字，视频类审核意见还没做解析。
- image2 生成 APP 宣传图：image2 当前只是预留/辅助接口，没有实现宣传图生成流程。
- 基于竞品搜索自动生成产品改进方案：目前已能搜索和记录公开指标，还没接入自动改进建议生成。
- 自动修 APK 合规问题：比如 targetSdkVersion 低、马甲包相似度高、权限问题等，目前只能识别/提示，不能自动改 APK。

## 目录

- `main.py`：命令行入口。
- `package.js`：旧打包工具源码，AutoReview 通过 CLI 包装调用。
- `autoreview/oppo/`：OPPO 提交、查询、批量提交逻辑。
- `autoreview/packaging/`：APK 批量打包包装器。
- `autoreview/feishu/`：飞书长连接、消息下载、回复。
- `autoreview/agent/`：飞书聊天指令、会话状态、配置编辑。
- `autoreview/market/`：应用商店公开搜索、竞品指标快照。
- `config/oppo_submission.example.json`：OPPO 提交配置模板。
- `config/llm_config.example.json`：共享大模型配置模板，可被各厂商配置复用。
- `config/oppo_batch.example.json`：OPPO 批量提交配置模板。
- `config/package_batch.example.json`：批量打包配置模板。
- `config/xiaomi_submission.example.json`：小米配置模板。
- `config/honor_submission.example.json`：荣耀配置模板。
- `config/vivo_submission.example.json`：vivo 配置模板。
- `config/huawei_submission.example.json`：华为配置模板。
- `tests/`：单元测试。

## 第一次配置

复制 OPPO 配置：

```powershell
cd D:\development_sercer\AutoReview
Copy-Item config\oppo_submission.example.json config\oppo_submission.json
```

在 `config\oppo_submission.json` 填写：

```json
{
  "credentials": {
    "client_id": "OPPO 开放平台 client_id",
    "client_secret": "OPPO 开放平台 client_secret"
  },
  "submission": {
    "pkg_name": "应用包名",
    "version_code": "版本号",
    "version_name": "版本名称",
    "apk_url": {
      "path": "../release/app-release.apk",
      "cpu_code": 0
    }
  }
}
```

图标、截图、版权证明、联系人等字段也在同一个 JSON 文件里配置。AutoReview 不读取环境变量。

## 最短跑通流程

### 1. 只有 APK，复用 OPPO 已有材料提交

把 APK 放到本地，例如：

```powershell
$Apk = "D:\development_sercer\AutoReview\release\app-release.apk"
Test-Path $Apk
```

提交：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json submit `
  --apk "$Apk" `
  --reuse-remote-materials `
  --wait-task `
  --force
```

说明：

- `--apk` 只覆盖本次命令，不会改写配置文件。
- `--reuse-remote-materials` 会先查询 OPPO 平台已有版本的 `app_info`，复用图标、截图、版权证明等 URL。
- `--wait-task` 只等 OPPO 提交任务完成，不等待完整人工审核。
- `--force` 用于绕过本地“最近驳回不建议原包重提”的保护，只应在确认要测试/提交时使用。

如果本次提交的新版本号和复用材料的旧版本号不同：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json submit `
  --apk "$Apk" `
  --version-code 65 `
  --version-name 3.1400.34.6 `
  --reuse-remote-materials `
  --reuse-version-code 64 `
  --wait-task `
  --force
```

### 2. 查询状态

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json status
```

指定版本号：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json status --version-code 65
```

### 3. 校验本地配置

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json validate
```

注意：如果你提交时使用 `--reuse-remote-materials`，本地可以没有图标/截图/版权证明文件；这些材料会在提交时从 OPPO 平台现有版本复用。

## 批量提交

复制示例：

```powershell
Copy-Item config\oppo_batch.example.json config\oppo_batch.json
```

示例结构：

```json
{
  "defaults": {
    "config": "oppo_submission.json"
  },
  "items": [
    {
      "name": "英语四级单词",
      "apk": "../release/app-release.apk",
      "version_code": "10002",
      "version_name": "1.0.2"
    }
  ]
}
```

执行：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json batch-submit `
  --batch-file config\oppo_batch.json `
  --wait-task
```

失败后继续后续任务：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json batch-submit `
  --batch-file config\oppo_batch.json `
  --wait-task `
  --continue-on-error
```

批量任务也支持复用 OPPO 远程材料：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json batch-submit `
  --batch-file config\oppo_batch.json `
  --reuse-remote-materials `
  --wait-task
```

## 批量打包 APK

AutoReview 复用 `package.js` 的旧打包流程。目标 Android 项目目录需要有：

```text
packlist.xls
jksconfig.txt
app/build.gradle
gradlew 或 gradlew.bat
```

如果目标项目缺 Node 依赖：

```powershell
cd D:\你的Android项目目录
npm install node-xlsx iconv-lite
cd D:\development_sercer\AutoReview
```

先 dry-run：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py package-apk `
  --project-dir D:\你的Android项目目录 `
  --channels book1400 book1401 `
  --script D:\development_sercer\AutoReview\package.js `
  --dry-run
```

实际打包：

```powershell
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py package-apk `
  --project-dir D:\你的Android项目目录 `
  --channels book1400 book1401 `
  --script D:\development_sercer\AutoReview\package.js
```

默认会跳过 `package.js` 结束后的 `start.bat` 自动执行，只做打包。如果要保留旧逻辑：

```powershell
--run-start
```

找最新 APK：

```powershell
$Project = "D:\你的Android项目目录"
$Apk = Get-ChildItem "$Project\app\build\outputs\apk" -Recurse -Filter *.apk |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName
$Apk
```

批量打包清单：

```powershell
Copy-Item config\package_batch.example.json config\package_batch.json
D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py batch-package --batch-file config\package_batch.json
```

打包包装器会写目标项目的 `packconfig.txt`，如果原文件存在，会先备份到目标项目的 `backups/` 目录。

## 打包后直接提交

```powershell
cd D:\development_sercer\AutoReview

$Project = "D:\你的Android项目目录"
$Channel = "book1400"

D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py package-apk `
  --project-dir "$Project" `
  --channels $Channel `
  --script D:\development_sercer\AutoReview\package.js

$Apk = Get-ChildItem "$Project\app\build\outputs\apk" -Recurse -Filter *.apk |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName

D:\development_sercer\AutoReview\.venv\Scripts\python.exe main.py -c config\oppo_submission.json submit `
  --apk "$Apk" `
  --reuse-remote-materials `
  --wait-task `
  --force
```

## 飞书机器人

飞书入口是协作辅助，不是当前最短提交路径的必需项。

### 启动 / 停止飞书长连接

进入项目目录后启动：

```powershell
cd D:\development_sercer\AutoReview
.\start_feishu_ws.ps1
```

指定配置文件和日志级别启动：

```powershell
cd D:\development_sercer\AutoReview
.\start_feishu_ws.ps1 -Config "config\oppo_submission.json" -LogLevel INFO
```

`-LogLevel` 可选：`DEBUG`、`INFO`、`WARN`、`ERROR`。如果已经启动过，脚本会读取 `data\feishu_ws.pid`，发现进程还在时不会重复启动。

停止长连接：

```powershell
cd D:\development_sercer\AutoReview
.\stop_feishu_ws.ps1
```

### 设置开机自动启动

可以用 Windows 任务计划程序让飞书机器人开机后自动启动。

1. 打开“任务计划程序”。
2. 点击右侧“创建任务”，不要选“创建基本任务”。
3. “常规”页填写：

```text
名称：AutoReview Feishu Bot
勾选：不管用户是否登录都要运行
勾选：使用最高权限运行
配置：Windows 10 或 Windows 11
```

4. “触发器”页点击“新建”：

```text
开始任务：启动时
可选：延迟任务时间 30 秒
```

5. “操作”页点击“新建”：

```text
操作：启动程序
程序或脚本：powershell.exe
添加参数：
-NoProfile -ExecutionPolicy Bypass -File "D:\development_sercer\AutoReview\start_feishu_ws.ps1"
起始于：
D:\development_sercer\AutoReview
```

6. “条件”页按需取消“只有在计算机使用交流电源时才启动此任务”。
7. “设置”页建议勾选：

```text
允许按需运行任务
如果任务失败，按以下频率重新启动：1 分钟
尝试重新启动次数：3 次
如果此任务已经运行，以下规则适用：请勿启动新实例
```

8. 保存任务后，在任务计划程序里右键 `AutoReview Feishu Bot`，选择“运行”测试。

启动后检查：

```powershell
Test-Path D:\development_sercer\AutoReview\data\feishu_ws.pid
Get-Content D:\development_sercer\AutoReview\logs\feishu-ws.out.log -Tail 50
Get-Content D:\development_sercer\AutoReview\logs\feishu-ws.err.log -Tail 50
```

启动后日志会追加写入，不会清空历史日志：

```text
logs\feishu-ws.out.log
logs\feishu-ws.err.log
```

后台进程 PID 保存在：

```text
data\feishu_ws.pid
```

如果 PowerShell 提示脚本执行策略禁止运行，可以先在当前窗口临时放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

主要指令：

```text
帮助
状态
分析驳回：OPPO 后台驳回原因
分析这张图
整改清单
提交检查
查询审核状态
查询审核状态：10001
查看提交配置
设置提交配置：submission.version_code=10002
确认保存配置
取消保存配置
绑定材料：APK
绑定材料：图标
绑定材料：截图1
绑定材料：版权证明
绑定材料：ICP证明
搜索竞品：英语四级单词
记录竞品下载：英语四级单词
清空当前记录
清空所有记录
```

竞品分析也支持轻量自然语言表达，例如：

```text
帮我看看有哪些类似的背单词软件
帮我找英语四级单词的对标产品
把竞品下载量记录一下
保存本月同类 APP 下载数据
```

其他飞书能力也支持轻量自然语言表达，例如：

```text
你能做什么？
现在进度怎么样？
帮我分析一下最近这张截图
接下来怎么整改？
帮我查审核状态 10001
帮我检查一下现在能不能提交
帮我看看当前提交配置
把 submission.version_code=10002 暂存一下
确认保存刚才的配置修改
取消这次配置修改
把刚才上传的 APK 绑定成材料
清空当前会话记录
```

语义识别是本地规则，不依赖大模型。固定指令仍然最稳定；自然表达会尽量识别意图，缺少必要内容时会提示补充。

### 大模型兜底对话

飞书 agent 支持接入 OpenAI-compatible 大模型，用于更开放的小需求聊天、意图识别和会话记忆。默认关闭；未配置时仍使用固定指令和本地语义规则。

共享配置在 `config/llm_config.json`，各厂商提交配置通过 `llm_config_path` 复用同一份大模型配置。

先复制模板：

```powershell
Copy-Item config\llm_config.example.json config\llm_config.json
```

然后编辑 `config/llm_config.json`：

```json
{
  "enabled": true,
  "base_url": "https://api.openai.com/v1",
  "api_key": "填写大模型 API Key",
  "model": "gpt-4.1-mini",
  "timeout_seconds": 30,
  "temperature": 0.2,
  "max_tokens": 800
}
```

OPPO、小米、荣耀、vivo、华为等提交配置里只需要引用：

```json
{
  "llm_config_path": "llm_config.json"
}
```

修改后重启飞书机器人：

```powershell
cd D:\development_sercer\AutoReview
.\stop_feishu_ws.ps1
.\start_feishu_ws.ps1
```

启用后，处理顺序是：

```text
精确指令
本地语义规则
大模型结构化意图识别 / 普通聊天建议
本地工具执行
会话记忆更新
```

大模型不会直接改文件、提交审核或上传材料；它只输出结构化意图，真正执行仍由本地 Python 工具完成。配置修改仍然只会先暂存，需要再发送“确认保存配置”才会写入文件。

可以这样聊：

```text
以后这个应用默认按英语四级单词处理
这个版本审核风险大不大？
帮我想想怎么降低马甲包嫌疑
研究一下这个赛道有哪些产品
下个版本号先改到 10003
```

会话记忆保存在 `data/review_agent_state.json` 的 `agent_memory` 中。

飞书配置在 `config/oppo_submission.json`：

```json
{
  "feishu": {
    "app_id": "飞书自建应用 app_id",
    "app_secret": "飞书自建应用 app_secret",
    "verification_token": "飞书事件订阅 verification token",
    "encrypt_key": "",
    "state_path": "../data/review_agent_state.json",
    "image_analysis": {
      "image2_url": "",
      "ocr_url": "http://127.0.0.1:5000/ocr",
      "ocr_api_key": "OCR 接口 API Key，可留空",
      "timeout_seconds": 120
    }
  }
}
```

说明：

- OCR 用于识别审核意见截图。
- `image2_url` 当前是预留/辅助能力，后续可用于宣传图或图片相关能力。
- 飞书下载图片/文件需要开放消息资源、图片资源、文件资源读取权限。
- 会话状态保存在 `data/review_agent_state.json`。
- 上传到飞书的原始文件会保存到 `data/feishu_uploads/`。
- `记录竞品下载` 会在会话状态的 `market_download_snapshots` 中按 `YYYY-MM` 保存月度快照。多数商店不公开精确下载量；AutoReview 只记录公开可见的下载量文本、评分和评分数，不公开的字段会留空。

## 多渠道配置模板

已提供：

- `config/oppo_submission.example.json`
- `config/xiaomi_submission.example.json`
- `config/honor_submission.example.json`
- `config/vivo_submission.example.json`
- `config/huawei_submission.example.json`

这些模板使用统一 `submission` 字段：

```text
pkg_name
version_code
version_name
app_id
apk_url
app_name
summary
detail_desc
update_desc
privacy_source_url
icon_url
pic_url
copyright_url
icp_url
special_url
business_username
business_email
business_mobile
age_level
adaptive_equipment
```

目前只有 OPPO API 已接入；其他渠道模板用于后续扩展。

## 测试

```powershell
cd D:\development_sercer\AutoReview
D:\development_sercer\AutoReview\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

当前测试覆盖：

- OPPO 签名、上传参数、提交流程
- OPPO 审核状态分类
- 批量提交配置解析
- 远程材料复用
- 飞书消息解析、图片/文件下载记录
- 飞书配置编辑和材料绑定
- 批量打包包装器
- JSON 配置文件解析

## 注意事项

- agent 不能绕过各应用商店审核规则，只能自动化打包、上传、提交、查询和风险提示。
- OPPO 如果返回 targetSdkVersion、包体合规、版本号等错误，需要换 APK 或改版本后重提。
- `submission.last_rejection_reason` 只用于本地风险判断，不会提交给 OPPO。
- 密钥类字段只写在配置文件中，不通过飞书展示，也不允许通过飞书修改。
- 当前自动撤回/取消审核未接入。
