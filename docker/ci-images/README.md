# GitLab CI 工具镜像

在 Linux x64 Runner 宿主机执行 `sh docker/ci-images/build.sh`，预加载：

- `mskdsp-platform-ci-verify:rust1.94.1-v1`：Rust 1.94.1、rustfmt、Python、Node、protoc、Git 和 CA。
- `mskdsp-platform-ci-tools:bookworm-v1`：Python、Git、AWS CLI、CA 和程序身份检查所需的运行库。

作业使用 `if-not-present`，不再逐次安装软件。工具镜像只含公共依赖，不含源代码、
GitLab 令牌、R2 凭据或 BuildKit 证书。主程序的 Dockerfile 和构建缓存独立维护。

当前镜像预加载在现有 Runner 主机。增加 Runner 主机前需构建或导入这两个镜像；
未预加载时任务会因无法获取镜像失败。Runner 必须允许 `if-not-present` 拉取策略。
若未来使用共享镜像仓库，应将相应镜像变量切换到该仓库中固定版本或摘要的地址。

升级依赖或刷新安全更新时，用新的标签构建，再修改 `.gitlab-ci.yml` 中的
`PLATFORM_VERIFY_IMAGE` / `PLATFORM_TOOLS_IMAGE`；不要覆盖正在使用的版本标签。
构建脚本输出本地镜像 ID，供迁移时核对。APT 源仍执行软件包签名校验。
