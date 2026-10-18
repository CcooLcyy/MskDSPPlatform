# Docker 发布与网页更新

## 平台自身 CI 更新

CI 由 GitLab 完成构建、测试、无域名依赖的程序包打包及平台上传。
上传任务使用 `PLATFORM_GRPC_ENDPOINT`、受保护且掩码的 `PLATFORM_UPLOAD_TOKEN`，
通过 `CreateUpload`、分块续传及 `CompleteUpload` 确认入库，不调用发布或安装接口。
默认 TLS；同宿主机受控 Docker 内网可显式配置 `PLATFORM_GRPC_PLAINTEXT=1`。
固定协议子模块必须包含软件上传接口，所有需要协议的作业均需初始化它。

首次部署使用已验证镜像引导平台接包能力，并将持久化 `deploy/updater.json` 切换为
`{"source":"platform","channel":"ci"}`。必须备份并保留平台运行目录、点表数据库和管理员配置。
此后由管理员在 `/update` 检查本地软件仓库中的候选包并决定升级；GitLab 不触发安装。
运行管理器和基础镜像变更仍单独维护；详见 [平台自身 CI 更新](docs/平台自身CI更新.md)。

2026-10-18 已完成验收：GitLab 流水线 #22 成功入库提交 `7e3742c`，管理员已在正式平台
手动升级并确认可用。上传与安装分开执行；后续 CI 包继续由管理员选择升级。

基础版本 `0.0.1`，目标平台 `linux/amd64`。Beta/Stable 流程保留，尚未首发。

`main` 的自动 CI 由 GitLab 执行，构建、测试、离线包和平台上传的配置见
[GitLab CI](docs/GitLabCI.md)。GitHub 的 CI 工作流仅做检查；GHCR、GitHub Release
及 R2 发布保留用于 Beta/Stable。历史 CI R2 目录已于 2026-10-18 清空，备份及执行记录见
[R2 历史 CI 清理](docs/R2历史CI清理.md)。

## 三个发布通道

| 通道 | 触发 | GHCR 标签 | 交付入口 | R2 清单 |
| --- | --- | --- | --- | --- |
| CI | GitLab 推送 main，或在 main 手动运行流水线 | 不推送 | 平台私有候选包、GitLab 作业产物 | 不更新 |
| Beta | 推送 beta/0.0.1，或手动指定 Beta 分支 | beta | beta-latest 预发布 | mskdsp-platform/beta/latest.json |
| Stable | 推送 v0.0.1，或手动指定已存在标签 | stable、latest | v0.0.1 正式发布 | mskdsp-platform/stable/latest.json |

镜像仓库 `ghcr.io/ccoolcyy/mskdsp-platform`。CI 离线归档和 Beta 镜像使用唯一标签，例如
`0.0.1-ci.20261013t080000.42.1-sha.0123456`；程序版本保留 SemVer 中的 `+sha.`。
PR 只检查，不发布。版本来源、配置、打包范围见 [发布方案](docs/发布方案.md)。

## GitHub 与 R2 配置

平台构建需要固定版本的 `platform-proto` 私有协议子模块。构建机须能访问内网 GitLab；GitLab 作业使用 `CI_JOB_TOKEN` 和协议项目 allowlist，GitHub 工作流使用只读 Secret `PLATFORM_PROTO_READ_TOKEN`。检出步骤见 `script/init_platform_proto.py`。
GitHub 托管 runner 默认不具备该内网连通能力，发布前须先准备可达的构建环境。平台自身 CI 包使用平台软件仓库，Beta/Stable 仍支持 R2 远程更新；产品和权限边界见 [软件仓库](docs/软件仓库.md)。

仓库 Actions Secrets：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`。
仅发布作业使用这些凭据，运行设备只访问公开 HTTPS 地址。

可选 Actions Variables：

- `R2_BUCKET`：默认 `mskdsp-update`。
- `R2_PREFIX`：默认 `mskdsp-platform`，不得复用其他项目的前缀。
- `R2_PUBLIC_BASE_URL`：默认 `https://update.clsclear.top`。

先校验所有版本资产，再推进 `latest.json`，验证入口后才精确清理当前通道旧对象。
正式版本拒绝不同提交或内容覆盖，Beta 拒绝过期分支构建覆盖滚动入口。
同一通道串行发布，不影响另一通道或其他项目。
GHCR 与 GitHub Release 使用 `GITHUB_TOKEN`；工作流明确授予发布所需权限。
若 GHCR 包为私有，可登录后拉取，也可直接下载公开的 R2 或 GitHub 离线镜像包。

## 默认部署：docker run

CI 离线包在 GitLab 手动运行 `offline-bundle` 后下载；Beta/Stable 从 GitHub Release
下载 ZIP、`latest.json` 和 `SHA256SUMS`。校验后解压：

```bash
sha256sum -c SHA256SUMS
unzip mskdsp-platform-*.zip -d platform
cd platform
sha256sum -c SHA256SUMS
docker load -i image.tar.gz
sh deploy/run.sh
```

脚本使用包内 `.env` 的唯一镜像标签，不需要 Compose 或在线拉取。
平台容器名为 `mskdsp-platform`；已有同名容器时脚本会报错，不自动接管。
默认 HTTP `127.0.0.1:8080`、gRPC `127.0.0.1:50000`，宿主机端口可用
`MSKDSP_HTTP_PORT`、`MSKDSP_GRPC_PORT` 调整，监听地址可用 `MSKDSP_BIND_ADDRESS` 调整。
宿主机工作目录默认 `/home/daniel/mskplt`，用 `MSKDSP_WORK_DIR` 可指定其他绝对目录。
脚本只在配置缺失时从交付包复制配置与更新源，运行时均只读挂载。
更新程序、状态、业务数据、日志与临时文件统一保存在工作目录的 `runtime` 子目录，
不要删除该目录。脚本为运行目录准备 UID 10001 写权限，容器根文件系统保持只读。
目录映射与旧容器兼容规则见 [平台工作目录](docs/平台工作目录.md)。

仓库 `compose.yaml` 只是可选模板，不属于默认离线交付方式；使用相同的宿主机工作目录。
首次使用 Compose 前须准备 `deploy` 配置和 `runtime/data`、`runtime/logs`、`runtime/tmp`
目录，并给予 UID 10001 写入运行目录的权限。
不同通道部署须使用匹配的镜像和 `deploy/updater.json` 更新源。CI 默认生成平台本地软件库来源，
Beta/Stable 交付包生成对应远程 URL。Compose 的构建参数 `UPDATE_MANIFEST_URL` 默认空，
仅在需要远程来源时显式设置 `MSKDSP_PLATFORM_UPDATE_URL`；已有持久化更新配置需单独核对。

## 简单更新页面

普通程序变更通过本节的平台自带更新功能安装。管理器、基础镜像或部署配置变更仍单独维护，
保留已有工作目录、管理员配置和端口。

登录后默认进入点表工作台。通过“程序更新”导航或 `http://127.0.0.1:8080/update`，查看当前版本、最新版本、更新通道和任务结果；页面不显示更新来源。
CI 点击“检查更新”查询平台本地软件库；Beta/Stable 的远程来源查询所配置的 R2 清单。
发现新版后由管理员点击“更新到最新版”。页面加载和状态轮询
只读取本地状态，不自动检查或安装。没有宿主机定时器、额外业务容器或 Docker Socket。

一个容器内常驻 Python 管理进程，运行可更新的 Rust 平台子进程。公开 HTTP 由管理
进程代理到内部 `127.0.0.1:8081`，gRPC 仍直接由 Rust 提供。下载时旧程序继续服务，
校验清单、SHA256、大小、ELF 架构和候选程序身份后才重启子进程。
HTTP 就绪、版本身份和真实 gRPC Hello/Welcome/Goodbye 握手均通过才提交新版状态。
失败恢复本地上一版；重启继续使用最后已提交版本，不被镜像种子覆盖。

更新会短暂断开 gRPC 长连接，Agent 需重连。现场配置保持不变，容器 ID 和镜像不变。
基础镜像、Python 管理器及系统库更新需由管理员单独升级，日常更新仅替换平台程序。
管理页面默认只在宿主回环地址暴露；远程访问通过受保护网关或 SSH 隧道。
动作头和 Origin 校验限制跨源浏览器请求，不替代管理身份认证。

## Beta 晋升 Stable

1. 在 main 完成修改、测试并提交，统一 Cargo.toml 与 Cargo.lock 基础版本。
2. 推送已确认的提交到 Beta：`git push origin <SHA>:refs/heads/beta/0.0.1`。
3. 等待 Beta 构建、打包校验和 R2 发布成功。
4. 在同一提交创建标签：`git tag -a v0.0.1 <SHA> -m 'Release v0.0.1'`。
5. 推送标签：`git push origin v0.0.1`，等待 Stable 发布完成。

正式标签必须与 Cargo 基础版本一致，且属于匹配版本的远端 Beta 分支。
创建 Beta 分支本身会自动发布 Beta，不能只为了满足正式发布检查而创建分支。
滚动 GitHub Release 的 `latest.json` 和更新清单中的 commit 表示当前构建来源。

## 验证

```bash
python -m unittest discover -s test -p 'test_*.py' -v
cargo fmt --all -- --check
cargo test --locked --all-targets
docker build --platform linux/amd64 -t mskdsp-platform:dev .
```

CI 自动运行工具测试、Rust 检查、镜像构建、程序包身份与摘要校验及平台入库。
容器运行和程序更新恢复烟测不在自动发布流程中；保留 `script/smoke_container.py`
和 `script/smoke_runtime.py` 供手动验证。
当前平台已提供管理员登录及 SQLite 点表库；原生 TLS、诊断上传及多用户权限仍待实现。点表旧 JSON 在事务迁移成功后清理；数据库备份、容量及旧程序回滚限制见 [Modbus 点表管理](docs/Modbus点表管理.md)。
