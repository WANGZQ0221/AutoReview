# AutoReview -> OpenClaw 迁移方案存档（2026-06-18）

## 结论

建议后续迁移时不要重写 AutoReview 的业务内核，而是在新机器上新建一个 OpenClaw 版“应用发布运营 agent”外壳。

目标形态：

- AutoReview core 继续负责真实业务能力：打包、OPPO 提交、状态查询、OCR、材料绑定、竞品搜索。
- OpenClaw 负责上层能力：账号授权、对话、工具路由、记忆、飞书通道、后续定时任务。
- 业务代码仓库和 OpenClaw workspace 分离，参考 longxia 运营体系的形态。

一句话方案：

> 新机器上做成“OpenClaw 运营 agent + AutoReview core 工具服务”双层架构：OpenClaw 负责账号授权、对话、记忆和通道，AutoReview 继续负责打包、提交、OCR、材料绑定和竞品搜索。

## 背景

当前 AutoReview 已经有一套 Python 业务实现，包含：

- OPPO 提交、状态查询、批量提交。
- APK 打包包装器。
- 飞书长连接机器人。
- OCR/image2 接口。
- OPPO 驳回分析和整改清单。
- 配置查看、暂存、确认保存。
- 上传材料绑定。
- 应用商店竞品搜索。
- LLM intent 和 ToolRegistry 雏形。

迁移到 OpenClaw 的主要动机不是业务代码重构，而是：

- 使用 OpenClaw 的账号授权能力，减少自己维护 API Key 的压力。
- 让 OpenClaw 承担运营 agent、通道、记忆、定时任务等上层职责。
- 后续扩展为更完整的应用发布/运营 agent。

## 推荐架构

### 目录分层

新机器建议按 Linux/WSL 方式组织：

```text
/home/admin/auto_review_core
/home/admin/.openclaw/workspace
/home/admin/.openclaw/cron
/home/admin/.openclaw/agents
/home/admin/.openclaw/credentials
```

职责边界：

- `/home/admin/auto_review_core`
  - 当前 AutoReview Python 项目。
  - 保留 `main.py`、`autoreview/`、`config/`、`tests/`。
  - 继续提供 CLI 或本地 HTTP 工具服务。
- `/home/admin/.openclaw/workspace`
  - OpenClaw 运营 agent 工作区。
  - 放 skills、references、memory、ops_requests。
- `/home/admin/.openclaw/cron`
  - 后续定时任务，例如每日审核状态巡检、提交检查、竞品月报。
- `/home/admin/.openclaw/credentials`
  - OpenClaw 账号授权、飞书通道等运行态。
  - 不写入交接文档，不外发密钥。

## OpenClaw 运营 Agent 形态

参考 longxia 的 `yunying-ops`，建议给 AutoReview 建一个总入口 skill：

```text
skills/app-release-ops/SKILL.md
```

它是“应用发布运营 agent”的总路由入口。

### 建议 Skill 拆分

```text
skills/app-release-ops/SKILL.md
skills/package-apk/SKILL.md
skills/oppo-submission/SKILL.md
skills/review-analysis/SKILL.md
skills/market-research/SKILL.md
skills/material-binding/SKILL.md
skills/config-edit/SKILL.md
```

### 总入口职责

`app-release-ops` 负责理解用户需求并路由到子 skill 或本地工具，例如：

- “帮我打包八年级语文下册”
- “查一下 OPPO 审核状态”
- “分析这张驳回截图”
- “把刚上传的文件绑定成版权证明”
- “搜索一下抖音在 OPPO 的下载量”
- “先把 version_code 改成 101，别保存”

### Workspace 建议结构

```text
workspace/
  skills/
    app-release-ops/
      SKILL.md
      references/
        current-architecture.md
        command-cheatsheet.md
        config-boundaries.md
    package-apk/
      SKILL.md
    oppo-submission/
      SKILL.md
    review-analysis/
      SKILL.md
    market-research/
      SKILL.md
    material-binding/
      SKILL.md
    config-edit/
      SKILL.md

  memory/
    apps/
      default_app.json
    preferences/
      store_scope.json
    sessions/
      latest_context.json

  ops_requests/
    pending/
    done/

  references/
    deployment.md
    machine-layout.md
    cutover.md
```

## 工具接入方案

当前 AutoReview 已经有 ToolRegistry 和本地业务方法，迁移时不要重复实现业务逻辑。

### 方案 A：OpenClaw 调 CLI

第一阶段推荐先使用 CLI。

示例：

```bash
python main.py -c config/oppo_submission.json status
python main.py package-apk --project-dir ...
python main.py submit --apk ...
```

优点：

- 复用现有代码最多。
- 最快落地。
- 调试简单。

缺点：

- 输出偏命令行文本。
- 后续做复杂 agent 编排不如 JSON API 稳。

### 方案 B：OpenClaw 调本地 HTTP Agent API

第二阶段建议切到本地 API。

当前项目已有：

```text
autoreview/agent_app/server.py
```

目标形态：

```text
Feishu / Chat
  -> OpenClaw agent
  -> OpenClaw 使用账号授权调模型
  -> 调 AutoReview 本地 HTTP 工具
  -> AutoReview 返回 JSON
  -> OpenClaw 生成最终回复
```

建议端口：

```text
127.0.0.1:8090
```

## 第一版 Agent 能力范围

### V1 要做

- 文本对话路由。
- 调用本地 AutoReview 工具。
- 读取/写入会话记忆。
- 飞书消息收发。
- 使用 OpenClaw 账号授权。
- 支持核心动作：
  - 单个 APK 打包。
  - 批量打包。
  - OPPO 状态查询。
  - 提交前检查。
  - 驳回分析。
  - 最近图片 OCR 分析。
  - 材料绑定。
  - 查看/暂存配置。
  - 竞品搜索。

### V1 不做

- 自动正式提交到所有商店。
- 多机调度。
- 定时批量运营报告。
- 视频审核意见解析。
- image2 宣传图生产链路。
- 自动修 APK 合规问题，例如 targetSdkVersion、马甲包相似度、权限问题。

## 新机器部署建议

如果新机器是 Windows，建议使用 WSL2 Debian 作为主运行环境。

理由：

- OpenClaw workspace、cron、gateway、systemd 在 Linux/WSL 形态更顺。
- 后续要加通道、后台服务、定时任务，Linux 路径和服务管理更稳定。
- longxia 迁移已经验证过类似形态。

### 推荐形态

- WSL2 Debian。
- `admin` 用户。
- OpenClaw 安装在 WSL。
- AutoReview core 放在 WSL。
- Android 打包如必须依赖 Windows SDK，可后续单独做打包服务或 WSL 调 Windows 命令。

## 账号授权

迁移到 OpenClaw 的 LLM 认证建议：

- OpenClaw 使用 OpenAI/ChatGPT 账号授权。
- AutoReview core 不再直接负责 OpenAI API Key。
- AutoReview core 只暴露本地工具能力。
- 模型调用、对话总结、上下文记忆交给 OpenClaw agent。

不要使用抓 ChatGPT 网页 cookie/session token 的方式。

## 迁移步骤草案

### 阶段 1：新机器基础环境

1. 安装 WSL2 Debian。
2. 安装 Python、Node、OpenClaw。
3. 用 OpenClaw 完成 OpenAI 账号授权。
4. 初始化一个空 workspace。

### 阶段 2：迁移 AutoReview core

1. 同步当前 AutoReview 仓库到新机器。
2. 创建虚拟环境并安装依赖。
3. 跑单元测试。
4. 验证 CLI：
   - `status`
   - `package-apk`
   - `validate`
   - `agent-app`
5. 确认本地能力都能独立工作。

### 阶段 3：搭 OpenClaw 运营 agent

1. 创建 `app-release-ops` 总 skill。
2. 创建子 skill。
3. 接入 CLI 或本地 HTTP 工具。
4. 先在 OpenClaw CLI/本地 chat 里调试。

### 阶段 4：接飞书

1. 先只接测试机器人。
2. OpenClaw 收消息。
3. 路由到 `app-release-ops`。
4. 调 AutoReview 工具。
5. 回复飞书。

### 阶段 5：再考虑定时任务

后续可加：

- 每日提交检查。
- 审核状态巡检。
- 驳回汇总。
- 竞品月报。
- 批量打包报告。

## 新旧并行与 Cutover 边界

### 新旧并行期

- 现有 AutoReview 飞书机器人可以先保留。
- OpenClaw 版先只接测试通道。
- 不要两个机器人同时对同一个群或同一批用户提供正式服务。
- 不要两个入口都能触发正式提交。

### 切换时

1. 先关旧的飞书长连接机器人。
2. 再启 OpenClaw 飞书通道。
3. 提交能力先保持人工确认，不自动放开。
4. 观察日志和回复质量。
5. 稳定后再启用更高风险能力。

## 风险与注意事项

- 不要把 `client_secret`、`api_key`、飞书密钥、OpenClaw credential 写进文档。
- 配置修改默认只暂存，确认后才写文件。
- 正式提交、撤回、批量提交属于高风险动作，必须保留人工确认。
- 新旧机器人不要同时响应同一个飞书群。
- 打包机/多机器协同后续单独设计，不放进 V1。
- Android 项目、Gradle、JDK、Node 依赖仍是打包链路的关键外部条件。

## 第一批工具映射表

| 用户意图 | OpenClaw Skill | AutoReview 能力 |
| --- | --- | --- |
| 查状态 | `oppo-submission` | `main.py status` / agent tool `oppo_status` |
| 提交前检查 | `oppo-submission` | agent tool `submission_check` |
| 单包打包 | `package-apk` | `main.py package-apk` / agent tool `package_apk` |
| 批量打包 | `package-apk` | `main.py batch-package` / agent tool `batch_package` |
| 查应用名对应包 | `package-apk` | agent tool `package_lookup` |
| 分析驳回文本 | `review-analysis` | agent tool `analyze_rejection` |
| 分析最近图片 | `review-analysis` | OCR + agent tool `analyze_last_image` |
| 整改清单 | `review-analysis` | agent tool `remediation_checklist` |
| 查看配置 | `config-edit` | agent tool `view_submission_config` |
| 暂存配置修改 | `config-edit` | agent tool `stage_config_update` |
| 确认配置修改 | `config-edit` | agent tool `confirm_config_update` |
| 绑定上传材料 | `material-binding` | agent tool `bind_material` |
| 搜索竞品/下载量 | `market-research` | agent tool `market_search` |
| 记录竞品月度数据 | `market-research` | agent tool `market_download_snapshot` |

## 后续落地建议

下一步正式迁移时，建议按以下顺序做：

1. 在新机器安装 OpenClaw 并完成账号授权。
2. 同步 AutoReview core 并跑完整测试。
3. 起本地 `agent_app` 服务，验证本地 JSON 调用。
4. 创建 OpenClaw workspace 和 `app-release-ops` skill。
5. 先接 CLI 工具，再切 HTTP 工具。
6. 接测试飞书机器人。
7. 测试 3 类真实任务：
   - 打包一个真实 APK。
   - 查一次 OPPO 审核状态。
   - 发一张驳回截图并生成整改清单。
8. 确认稳定后再切换正式飞书入口。
