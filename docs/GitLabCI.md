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
4. `package`：从镜像归档提取实际平台程序，执行版本身份检查，生成部署 ZIP、
   镜像压缩包、程序更新包、清单和 SHA256SUMS。GitLab 归档保留 14 天，
   中间镜像归档保留 1 天。
5. `publish-r2`：读取 R2 凭据，先检查当前 GitLab `main`，上传并校验版本资产，
   在提交 `latest.json` 前再次检查 `main`。入口读回验证通过后，按原规则清理
   本项目 CI 通道的旧对象。发布作业通过 resource group 串行执行。

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

协议子模块通过 `CI_JOB_TOKEN` 获取，协议项目的 Job token allowlist
必须允许平台项目。项目作业归档大小限制应能容纳镜像归档及交付包，当前设为 1024 MiB。

## Runner 与缓存

作业标签为 `linux`、`amd64`、`docker`，使用实例级 Docker Runner。
Runner 全局并发 6，由所有使用它的仓库共享；单个作业上限 8 CPU、16 GiB。
Rust 检查作业按 Cargo.lock 使用 GitLab cache，缓存 `.cargo/registry`、
`.cargo/git` 和 `target`；受保护与非保护分支缓存由 GitLab 隔离。
Runner 宿主缓存目录为 `/home/daniel/gitlab-runner-cache`。

镜像构建服务部署模板为 `docker/gitlab-buildkit/compose.yml`，服务器目录为
`/home/daniel/mskdsp-platform-ci-buildkit`。客户端只通过 mTLS 连接构建服务，
作业容器不挂载 Docker Socket。服务端证书、CA 和客户端私钥均不进入版本库；
证书到期前需轮换服务器文件和 GitLab 构建变量。构建状态放在独立命名卷。

此服务器使用 Ubuntu 自带且已加载的 `rootlesskit` AppArmor profile，
允许构建容器的用户命名空间；模板仅对这个容器使用该 profile 和
`seccomp=unconfined`，BuildKit 仍以 UID 1000 运行。宿主机用户命名空间限制
保持开启，通用 Runner 无需特权模式。迁移服务器时需先确认同名 profile 存在。

## 验收

推送后确认五个作业均成功，检查测试日志的实际执行数量，并下载 GitLab
归档验证交付包。最后读取公开 R2 `ci/latest.json`，核对版本和提交与流水线一致，
确认程序更新包的 SHA256、大小和下载地址通过发布工具校验。
