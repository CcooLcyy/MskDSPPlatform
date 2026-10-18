# MskDSPPlatform

MskDSP 的服务端平台，面向上位机、下位机和后续网页管理端提供统一的服务能力。

## 规划范围

- 现场和设备管理
- 诊断信息与日志接收
- 上下位机软件更新
- 设备运行状态和数据上报
- 用户权限与操作审计
- 网页管理接口

## 相关仓库

- `MskDSPUpper`：Windows 上位机
- `mskdsp`：Linux 下位机
- `MskDSPProto`：上位机与下位机之间的 Protocol Buffers 定义
- `MskDSPPlatformProto`：平台对外的 Protocol Buffers 定义，通过 `platform-proto/` 子模块引用

## 平台协议子模块

克隆本仓库后，在仓库根目录初始化平台协议子模块：

```bash
git submodule update --init --recursive
```

子模块来源为内网 GitLab 的 `mskdsp/mskdspplatformproto`，需要相应的仓库读取权限。
父仓库固定引用具体提交，初始化时不会自动跟随协议仓库的最新版本。
软件仓库发布和查询接口由 `platform-proto/platform/v1/software.proto` 提供，
现有 Agent 协议仍由 `proto/platform/v1/agent.proto` 提供。

## 当前状态

当前基础版本为 `0.0.1`，已实现 Agent 握手、心跳、退出通知，以及 HTTP 健康、就绪和版本接口。
在线状态暂存在内存；已提供管理员登录、更新管理及 SQLite 点表库，诊断上传、设备业务持久化和设备身份认证仍待实现。

新增 [上下位机软件仓库](docs/软件仓库.md)：PubMgr 可通过独立认证的 gRPC 服务上传、校验入库并发布安装包。
管理员在 `/software` 查看版本、发布与撤回；公开 HTTP 提供上下位机兼容清单和流式下载。
安装包及版本记录保存在业务数据目录的 `software/`，不会因平台程序更新或重建容器而清空。
PubMgr、上位机调用逻辑尚未迁移；平台自身 CI 更新使用独立的只上传和管理员升级流程。

平台自身 [CI 更新](docs/平台自身CI更新.md)：GitLab 构建测试后通过 gRPC 上传完整程序包，
平台校验入库并保存历史版本；管理员在 `/update` 检查并选择升级。
上传不发布、不安装、不重启。更新管理进程直接读取持久化的软件仓库并缓存校验候选包，
保留既有健康检查和失败回滚，日常 CI 更新不访问 R2。

## Docker 发布

CI 通过 GitLab 生成 Linux amd64 镜像和程序包并上传平台；已有 Beta/Stable 发布保留，不在此次接入范围。
推送 main 发布 CI，推送 beta 版本分支发布 Beta，推送正式版本标签发布 Stable。
首轮只发布 CI。部署和版本晋升步骤见 [RELEASING.md](RELEASING.md)，
发布范围与验收要求见 [发布方案](docs/发布方案.md)。

默认用 `docker run` 部署。使用 `root` 管理员登录后，首页默认显示点表工作台；从“程序更新”导航进入 `/update`，可检查并安装更新，
常规 CI 更新从平台本地软件仓库读取程序包，校验后仅重启平台子进程，
失败回滚，不自动检查或安装。配置与程序版本状态持久化保留。
管理员登录接口、会话和密码配置见 [管理员登录](docs/管理员登录.md)。平台重启或更新后需要重新登录。

登录、程序更新和 Modbus 点表库页面支持深色模式。页面顶部的“外观”可选择跟随系统、浅色或深色，
默认跟随系统；选择会在当前浏览器保存，并在页面跳转、刷新及同源标签页之间保持一致。

## 开发原则

平台提供 [Modbus 点表管理](docs/Modbus点表管理.md) 页面 `/modbus-templates`，可保存上位机导出的 JSON 点表模板，
支持新建、逐点编辑、导入、下载及回收站恢复，最多 1000 张、每张 10000 点及 10 MiB。
删除后在 SQLite 回收站保留 90 天，到期不可恢复并定时清理；回收站不占正常点表库名额。
默认使用 `data/modbus-templates/templates.sqlite3` SQLite 数据库，旧 JSON 自动导入后清理原文件及迁移备份，
通过 `MSKDSP_PLATFORM_DATA_DIR` 设置数据根目录；未设置时优先使用 `MSKDSP_RUNTIME_DIR/data`，兼容旧容器更新。
Docker 部署的宿主机工作目录统一为 `/home/daniel/mskplt`，配置与所有平台运行文件保存在其子目录内，程序更新和容器重建后仍保留。详见 [平台工作目录](docs/平台工作目录.md)。

- 上位机与平台之间使用带 TLS 的 gRPC 通信。
- 大文件使用流式上传，并在服务端校验 SHA256。
- 现场日志和配置中的敏感信息必须脱敏。
- 运行数据、诊断文件和网页管理接口按功能模块拆分。
