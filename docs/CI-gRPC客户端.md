# CI gRPC 客户端

## 当前用途

为 GitLab Runner 准备 Python gRPC 运行库、协议编译器和可重复生成的客户端绑定，并提供只读连通检查。
[平台自身 CI 更新](平台自身CI更新.md) 已使用专用上传客户端完成入库；只读 `probe` 本身仍不携带令牌、不上传包。

协议来源为父仓库固定的 `platform-proto/platform/v1/software.proto`。先运行现有 `script/init_platform_proto.py` 初始化协议；固定提交必须已包含该文件，本地未提交协议不能视为 CI 已可用。

## 生成与检查

安装包含 gRPC 依赖的新 CI 工具镜像后执行：

```sh
python3 -B script/platform_grpc_client.py generate --output-dir .ci-tmp/platform-grpc
python3 -B script/platform_grpc_client.py probe --target platform.example:443
```

默认 TLS 校验证书；私有 CA 使用 `--ca-file <CA文件>`。已确认的内部明文 gRPC 入口可显式使用 `--plaintext`，不自动降级，也不发送发布令牌。

生成目录仅包含 `software_pb2.py` 与 `software_pb2_grpc.py`，运行时需要同时将该目录加入 Python 模块搜索路径。以协议所在目录作为编译根，避免生成顶层 `platform` 包遮蔽 Python 标准库。生成物放在忽略的临时目录，不提交仓库或烘焙进公共工具镜像。

只读探测先等待 gRPC 通道就绪，再调用 `SoftwareCatalogService.ListPackages`，仅输出结果数量，不输出目录内容。通道可用但返回 `UNIMPLEMENTED`，表示尚未提供该目录接口，不能把端口开放当成上传服务可用。没有写入权限或令牌时，不以创建上传任务探测服务。

## 验收与边界

- 测试应实际编译协议、导入客户端，并使用随机回环端口验证成功响应与接口缺失；缺少依赖时明确跳过，不计作协议验证通过。
- 缺少协议、生成失败、连接超时或目录调用失败时返回非零状态，并给出中文错误。
- 工具镜像构建时检查依赖导入；新镜像须预加载到 Runner 后再切换 CI 镜像标签。
- 工作机只读探测与 Runner 上传分别验收；端口连通不能替代固定协议、令牌权限和真实包入库验证。

## 2026-10-18 当前验收

- 固定协议子模块已更新为 `df9727a0ffbe80042c6088c664f146551e26abaa`，包含软件上传接口及 `PackageMetadata.build_time = 14`、`channel = 15`。上传契约测试直接从该协议生成绑定，不再临时补充字段。
- GitLab 流水线 #22 完成检查、构建、打包及平台上传，提交 `7e3742c` 的包与平台入库摘要一致。Runner 使用同宿主机 Docker 网关内网入口，专用令牌通过请求元数据发送，不使用公网明文地址上传。
- 线上已部署软件仓库接口；管理员已在正式平台检查并手动升级至 `7e3742c`，确认可用。上传与安装保持分离。
- Linux Python 完整回归 167 项、Rust 85 项通过；真实 CI 包的隔离安装、持久化重启和失败回滚验证通过。平台配置及部署记录见 [平台自身 CI 更新](平台自身CI更新.md)。

## 历史：2026-10-18 接入前准备记录

以下记录描述接口部署和协议入库之前的现场状态，已由上述验收取代。

- Windows 隔离 Python 3.13 环境使用 grpcio/grpcio-tools 1.84.0，专项 7 项全部通过；完整 Python 回归最终运行 146 项，141 项通过、5 项因 POSIX shell/Linux 文件锁要求跳过。首次完整回归的一项既有 HTTP 测试曾出现 BadStatusLine，单独重跑及第二次完整回归通过。
- Runner 主机的 verify/tools v2 镜像已构建并预加载，两个镜像的真实 Debian 依赖环境各运行专项 7 项全部通过；本地 CI 配置已切换到 v2 标签，尚未触发真实发布流水线。
- 工作机与 Runner 主机 bridge 网络作业容器均能建立到 `60.208.15.90:50000` 的明文 gRPC 通道，但 `SoftwareCatalogService.ListPackages` 返回 `UNIMPLEMENTED`。工作机只读调用 `SoftwarePublishService.GetUpload` 同样返回 `UNIMPLEMENTED`，尚不能进行软件仓库接入验收。
- 验证期间未上传安装包、未调用发布或升级接口，未向该明文入口发送发布令牌。
- 当前协议子模块固定提交 `a928de609cbfb88cbfb38e5b22959f203f5282b2` 未包含软件协议；本地协议仍未提交。正式 CI 生成绑定前，需先完成协议提交并更新父仓库引用，本次保留该既有改动。
