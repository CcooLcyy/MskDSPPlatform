# GitLab CI

入口为 `.gitlab-ci.yml`，作业定义放在 `.gitlab/ci/ci.yml`。推送 `main` 或在
`main` 手动运行流水线，执行检查、构建、打包及平台 CI 包入库；合并请求仅执行检查。
发布版本、镜像标签、离线 ZIP 和程序热更新包沿用现有发布工具的格式。

## 作业与产物

1. `verify`：Rust 格式检查、编译、Rust 测试，以及 Python/Node 相关测试。
   先构建调试程序，再执行 Python 测试，确保依赖真实平台程序的测试可以运行。
   点表 API 测试按单调时钟最多等待平台 HTTP 就绪 30 秒，健康探测单次最多 0.5 秒；
   进程提前退出立即失败，就绪后只登录一次。每次启动（包括重启）的完整合并输出分别
   保存在 `package/logs/platform-startup/*.log`，测试子进程固定开启平台 INFO 日志，
   随现有检查日志归档，成功或失败均保留
   14 天。启动失败同时报告耗时、退出码、端口、最后探测状态及最多 8 KiB 日志尾部，
   不额外归档测试配置、数据库或登录响应。正常业务请求仍沿用 5 秒超时。
   Python 命令单独设置 `MSKDSP_PLATFORM_TEST_TMPDIR=/dev/shm`，只让点表与账号的
   12 个真实进程夹具在容器内有界 tmpfs 建立独立数据目录，并把该子进程的 `TMPDIR`
   指向同一夹具；其他测试和启动日志继续使用原路径。默认 64 MiB `/dev/shm` 足够：
   全部夹具本机实测最高为 10000 点往返的 3.58 MiB，创建前要求至少 32 MiB 可用空间，
   不足或目录无效时明确失败，不回退到拥塞磁盘。测试结束清理配置、数据库和临时文件，
   启动日志仍保存在上述产物目录。该设置不修改 Runner、宿主机或平台部署。
2. `metadata`：冻结提交、UTC 构建时间和唯一版本。流水线 IID 与作业 ID
   共同区分不同构建和重试，通过 dotenv 将构建身份交给后续作业。
3. `build-image`：使用独立 BuildKit 构建 Linux x64 镜像并导出 Docker 离线归档。
   Rust 编译缓存保存在构建服务的数据卷内。
4. `package`：从镜像归档提取实际平台程序，核对完整构建身份，生成域名无关的
   `package/binary.tar.gz`、镜像压缩包、上传元数据和镜像身份记录，归档保留 14 天。
   `build-image` 的原始 `package/image.tar` 也保留 14 天，供首次引导部署使用。
5. `upload-platform`：检查任务来源仍为当前 main，通过 gRPC 创建上传任务、按偏移
   分块续传并确认完整入库，不调用发布或安装接口，失败保留 GitLab 产物。作业串行执行。
6. `offline-bundle`：需要离线部署包时手动运行，从程序包及镜像归档生成
   部署 ZIP、程序包和 SHA256SUMS，归档 `release/`，保留 14 天。
   作业重新校验摘要及身份；未运行或失败均不阻断 `upload-platform`。

GitLab CI 不依赖 GHCR 登录；离线归档保留原镜像仓库和唯一标签，部署时使用
`docker load`。上传到平台只形成私有候选包，由管理员决定是否安装。

## GitLab 变量

实例级、群组级或项目级均可提供以下凭据；实例级变量供所有仓库继承：

| 变量 | 含义 |
| --- | --- |
| `PLATFORM_UPLOAD_TOKEN` | 仅允许平台自身 CI 包上传的令牌，开启掩码 |
| `PLATFORM_GRPC_ENDPOINT` | 可达的 gRPC host:port，默认 TLS |
| `PLATFORM_GRPC_CA_FILE` | 可选私有 CA 文件路径 |
| `PLATFORM_GRPC_PLAINTEXT` | 可选值 1，仅显式内网/回环 IP 允许明文 |

凭据使用 Variable 类型，开启保护与日志掩码，关闭变量引用展开。发布分支
`main` 必须受保护。不要在日志中打印凭据，也不要将凭据写入版本库。

以上上传配置使用 `ci` 环境范围。平台只保存令牌 SHA256，不将原文写入仓库、
日志或命令参数。当前同宿主机 Runner 使用 Docker 网关内网入口，公网发布端应走 TLS。
CI 不再读取 R2 凭据或更新公开 R2 清单；平台管理器使用本地软件仓库来源。

项目还需要 `ci-build` 环境范围内的受保护变量：`BUILDKIT_HOST`、
`BUILDKIT_CA_B64`、`BUILDKIT_CERT_B64`、`BUILDKIT_KEY_B64`。后三项是 mTLS
CA、客户端证书和私钥的 Base64 文本；私钥必须开启掩码。构建作业将其解码到
临时目录，结束后清理。合并请求不会运行镜像构建或发布作业。

Git 默认只获取最近 20 层历史，并跳过子模块；`verify`、`build-image` 和 `upload-platform`
启用协议子模块拉取，为协议依赖保留准备。子模块通过 `CI_JOB_TOKEN` 获取，协议项目的
Job token allowlist 必须允许平台项目。`metadata`、`package` 和
`offline-bundle` 不拉子模块。上传前的分支检查以 `--depth=1` 获取最新 main，
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
| `PLATFORM_VERIFY_IMAGE` | `mskdsp-platform-ci-verify:rust1.94.1-v2` | Rust、rustfmt、Python gRPC、Node、protoc、Git 和 CA，供检查测试使用 |
| `PLATFORM_TOOLS_IMAGE` | `mskdsp-platform-ci-tools:bookworm-v2` | Python gRPC、Git、AWS CLI 和 CA，供元数据、打包、上传及离线包使用 |

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

推送后确认五个自动作业均成功，检查测试日志和 `package` 的域名无关归档。
在平台软件库核对完成入库包的版本、提交、大小及 SHA256 与流水线一致，
确认上传前后运行身份不变，由管理员在 `/update` 检查并决定升级。需要离线交付时，再手动运行
`offline-bundle` 并下载其 `release/` 归档验证部署包。

2026-10-18 已完成首次实际链路验收：流水线 #22 自动作业成功，提交 `7e3742c`
的包在平台完整入库且摘要一致；管理员已手动升级并确认可用。上传与安装分别执行，
后续包继续等待管理员决定是否升级。
