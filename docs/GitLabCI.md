# GitLab CI

入口为 `.gitlab-ci.yml`，作业定义放在 `.gitlab/ci/ci.yml`。推送 `main` 或在
`main` 手动运行流水线，执行检查、构建、打包及 R2 CI 发布；合并请求仅执行检查。
发布版本、镜像标签、离线 ZIP 和程序热更新包沿用现有发布工具的格式。

## 作业与产物

1. `verify`：Rust 格式检查、编译、Rust 测试，以及 Python/Node 相关测试。
   先构建调试程序，再执行 Python 测试，确保依赖真实平台程序的测试可以运行。
2. `metadata`：冻结提交、UTC 构建时间和唯一版本。流水线 IID 与作业 ID
   共同区分不同构建和重试，通过 dotenv 将构建身份交给后续作业。
3. `build-image`：使用独立 BuildKit 构建 Linux x64 镜像并导出 Docker 离线归档。
   Rust 编译缓存保存在构建服务的数据卷内。
4. `package`：从镜像归档提取实际平台程序，执行版本身份检查，生成 R2 所需的
   镜像压缩包、程序更新包、清单和 SHA256SUMS。自动流程只归档 `r2/` 和元数据，
   保留 14 天；中间镜像归档保留 1 天，不额外生成并传递含相同镜像的离线 ZIP。
5. `publish-r2`：读取 R2 凭据，先检查当前 GitLab `main`，上传并校验版本资产，
   在提交 `latest.json` 前再次检查 `main`。入口读回验证通过后，按原规则清理
   本项目 CI 通道的旧对象。发布作业通过 resource group 串行执行。
6. `offline-bundle`：需要离线部署包时手动运行，从 `package` 的 R2 资产生成
   原格式部署 ZIP、程序包和 SHA256SUMS，归档 `release/`，保留 14 天。
   作业重新校验资产摘要和镜像身份，不上传 R2；必须在上游归档过期前运行。
   这是允许失败的可选作业，未运行或失败均不阻断 `publish-r2`。

GitLab CI 不依赖 GHCR 登录；离线归档保留原镜像仓库和唯一标签，部署时使用
`docker load`。发布到 R2 不会自动安装到运行中的平台容器。

## GitLab 变量

实例级、群组级或项目级均可提供以下凭据；实例级变量供所有仓库继承：

| 变量 | 含义 |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare 账户 ID |
| `R2_ACCESS_KEY_ID` | R2 S3 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | R2 S3 Secret Access Key |

凭据使用 Variable 类型，开启保护与日志掩码，关闭变量引用展开。发布分支
`main` 必须受保护。不要在日志中打印凭据，也不要将凭据写入版本库。

非敏感配置在 `.gitlab-ci.yml` 中给出默认值：`R2_BUCKET=mskdsp-update`、
`R2_PREFIX=mskdsp-platform`、`R2_PUBLIC_BASE_URL=https://update.clsclear.top`。
不同项目必须使用各自的前缀。平台 CI 更新地址为
`https://update.clsclear.top/mskdsp-platform/ci/latest.json`。

项目还需要 `ci-build` 环境范围内的受保护变量：`BUILDKIT_HOST`、
`BUILDKIT_CA_B64`、`BUILDKIT_CERT_B64`、`BUILDKIT_KEY_B64`。后三项是 mTLS
CA、客户端证书和私钥的 Base64 文本；私钥必须开启掩码。构建作业将其解码到
临时目录，结束后清理。合并请求不会运行镜像构建或发布作业。

Git 默认只获取最近 20 层历史，并跳过子模块；仅 `verify` 和 `build-image`
启用协议子模块拉取，为协议依赖保留准备。子模块通过 `CI_JOB_TOKEN` 获取，协议项目的
Job token allowlist 必须允许平台项目。`metadata`、`package`、`publish-r2` 和
`offline-bundle` 不拉子模块。发布前的两次分支检查各以 `--depth=1` 获取最新 main，
无需完整历史。项目作业归档大小限制应能容纳镜像归档及交付包，当前设为 1024 MiB。

## Runner 与缓存

作业标签为 `linux`、`amd64`、`docker`，使用实例级 Docker Runner。
Runner 全局并发 6，由所有使用它的仓库共享；单个作业上限 8 CPU、16 GiB。
Rust 检查作业按 Cargo.lock 使用 GitLab cache，缓存 `.cargo/registry`、
`.cargo/git` 和 `target`；受保护与非保护分支缓存由 GitLab 隔离。
Runner 宿主缓存目录为 `/home/daniel/gitlab-runner-cache`。检查失败也会保存编译缓存，
测试临时目录位于构建卷中的 `.ci-tmp`，避免容器 overlay 的额外写入。

任务工具预装在两类镜像中，作业启动时不再重复执行 APT 安装和 rustfmt 下载：

| 变量 | 默认镜像 | 用途 |
| --- | --- | --- |
| `PLATFORM_VERIFY_IMAGE` | `mskdsp-platform-ci-verify:rust1.94.1-v1` | Rust 1.94.1、rustfmt、Python、Node、protoc、git 和 CA，供检查测试使用 |
| `PLATFORM_TOOLS_IMAGE` | `mskdsp-platform-ci-tools:bookworm-v1` | Bookworm 的 Python、git、awscli 和 CA，供元数据、打包、发布及离线包使用 |

镜像构建定义见 `docker/ci-images/Dockerfile`。当前单 Runner 主机预先构建并加载这两个
本地镜像，作业显式使用 `if-not-present`；新增 Runner 主机前必须加载相同版本镜像。
更新工具时应构建并使用新版本标签，避免本地旧镜像被继续复用。也可以将镜像发布到
可访问的镜像仓库，再修改上述变量。`PLATFORM_RUST_IMAGE` 和 `PLATFORM_RUNTIME_IMAGE`
仍用于构建产品镜像，不作为这两类任务工具镜像。

镜像构建服务部署模板为 `docker/gitlab-buildkit/compose.yml`，服务器目录为
`/home/daniel/mskdsp-platform-ci-buildkit`。客户端只通过 mTLS 连接构建服务，
作业容器不挂载 Docker Socket。服务端证书、CA 和客户端私钥均不进入版本库；
证书到期前需轮换服务器文件和 GitLab 构建变量。构建状态放在独立命名卷。

此服务器使用 Ubuntu 自带且已加载的 `rootlesskit` AppArmor profile，
允许构建容器的用户命名空间；模板仅对这个容器使用该 profile 和
`seccomp=unconfined`，BuildKit 仍以 UID 1000 运行。宿主机用户命名空间限制
保持开启，通用 Runner 无需特权模式。迁移服务器时需先确认同名 profile 存在。
构建快照使用已实测可用的 rootless `overlayfs`，避免 `native` 在每个步骤复制
整个工具链文件系统。运行时依赖安装位于版本标签之前，版本变化不会使该层缓存失效。
APT 保留 Debian 默认软件源，关闭 HTTP 请求流水线，并设置 30 秒网络等待与三次重试，
避免单次软件源请求长时间挂起；仍执行正常的软件包签名及完整性校验。

## 验收

推送后确认五个自动作业均成功，检查测试日志的实际执行数量及 `package` 的 R2 归档。
最后读取公开 R2 `ci/latest.json`，核对版本和提交与流水线一致，确认程序更新包的
SHA256、大小和下载地址通过发布工具校验。需要离线交付时，再手动运行
`offline-bundle` 并下载其 `release/` 归档验证部署包。
