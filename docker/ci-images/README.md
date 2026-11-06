# GitLab CI 工具镜像

在 Linux x64 Runner 宿主机执行 `sh docker/ci-images/build.sh`，预加载：

- `mskdsp-platform-ci-verify:rust1.94.1-v2`：Rust 1.94.1、rustfmt、Python、Python gRPC、Node、protoc、Git 和 CA。
- `mskdsp-platform-ci-tools:bookworm-v2`：Python、Python gRPC、Git、AWS CLI、CA 和程序身份检查所需的运行库。

两个镜像都通过 Debian bookworm 的 APT 软件包安装 `python3-grpcio`、
`python3-grpc-tools`、`python3-protobuf` 和 `python3-pkg-resources`，
不向系统 Python 全局执行 pip 安装。`python3-pkg-resources` 提供
`grpc_tools.protoc` 运行时需要的 `pkg_resources`；使用 `--no-install-recommends`
时显式安装，避免镜像构建后缺少该模块。
包信息见 Debian 官方的 [grpcio](https://packages.debian.org/bookworm/python3-grpcio)、
[grpc-tools](https://packages.debian.org/bookworm/python3-grpc-tools)、
[protobuf](https://packages.debian.org/bookworm/python3-protobuf) 和
[pkg-resources](https://packages.debian.org/bookworm/python3-pkg-resources) 页面。
每个镜像构建时都会检查 `import grpc, grpc_tools.protoc, google.protobuf`；
导入失败会直接终止构建。

作业使用 `if-not-present`，不再逐次安装软件。工具镜像只含公共依赖，不含源代码、
生成的协议绑定、GitLab 令牌、R2 凭据或 BuildKit 证书。主程序的 Dockerfile 和构建缓存独立维护。

新标签必须在每台相关 Runner 主机完成构建或导入，并通过依赖导入检查后，
再将 `PLATFORM_VERIFY_IMAGE` / `PLATFORM_TOOLS_IMAGE` 切换到以上 v2 标签。
增加 Runner 主机前需构建或导入这两个镜像；
未预加载时任务会因无法获取镜像失败。Runner 必须允许 `if-not-present` 拉取策略。
若未来使用共享镜像仓库，应将相应镜像变量切换到该仓库中固定版本或摘要的地址。

升级依赖或刷新安全更新时，用新的标签构建，再修改 `.gitlab-ci.yml` 中的
`PLATFORM_VERIFY_IMAGE` / `PLATFORM_TOOLS_IMAGE`；不要覆盖正在使用的版本标签。
构建脚本输出本地镜像 ID，供迁移时核对。APT 源仍执行软件包签名校验。

平台协议源文件由仓库固定版本的 `platform-proto` 子模块提供。作业检出所需子模块后，
在作业工作区生成 Python 绑定：

```sh
python3 -B script/platform_grpc_client.py generate --output-dir .ci-tmp/platform-grpc
```

生成器默认使用 `platform-proto`，也可用 `--proto-root` 显式选择协议根目录。
所需 `.proto` 必须已提交到固定子模块版本；本地未跟踪文件不会出现在干净检出中。
绑定仅作为作业临时文件，更新协议后重新生成，不复制进工具镜像。
镜像依赖准备与绑定生成本身不执行平台软件上传。当前 GitLab 的 `upload-platform`
作业另行使用这些依赖完成平台 CI 包入库；Stable 的远程发布能力保留。

2026-10-18：两个 v2 镜像已在现有 Linux x64 Runner 主机构建并预加载，
依赖导入检查通过，各自在只读作业容器运行 7 项客户端专项测试全部通过。
`.gitlab-ci.yml` 默认镜像标签为 v2，GitLab 流水线 #22 已完成真实平台 CI 包上传。
提交 `7e3742c` 已由管理员在正式平台手动升级并确认可用。
协议及接入验收详见 [CI gRPC 客户端](../../docs/CI-gRPC客户端.md)。
