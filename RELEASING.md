# Docker 发布与网页更新

基础版本 `0.0.1`，目标平台 `linux/amd64`。首次只发布 CI，Beta/Stable 流程保留。

## 三个发布通道

| 通道 | 触发 | GHCR 标签 | GitHub Release | R2 清单 |
| --- | --- | --- | --- | --- |
| CI | 推送 main，或手动运行 CI | ci | ci-latest 预发布 | mskdsp-platform/ci/latest.json |
| Beta | 推送 beta/0.0.1，或手动指定 Beta 分支 | beta | beta-latest 预发布 | mskdsp-platform/beta/latest.json |
| Stable | 推送 v0.0.1，或手动指定已存在标签 | stable、latest | v0.0.1 正式发布 | mskdsp-platform/stable/latest.json |

镜像仓库 `ghcr.io/ccoolcyy/mskdsp-platform`。CI/Beta 另有唯一镜像标签，例如
`0.0.1-ci.20261013t080000.42.1-sha.0123456`；程序版本保留 SemVer 中的 `+sha.`。
PR 只检查，不发布。版本来源、配置、打包范围见 [发布方案](docs/发布方案.md)。

## GitHub 与 R2 配置

仓库 Actions Secrets：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`。
仅发布作业使用这些凭据，运行设备只访问公开 HTTPS 地址。

可选 Actions Variables：

- `R2_BUCKET`：默认 `mskdsp-update`。
- `R2_PREFIX`：默认 `mskdsp-platform`，不得复用其他项目的前缀。
- `R2_PUBLIC_BASE_URL`：默认 `https://update.clsclear.top`。

先校验所有版本资产，再推进 `latest.json`，验证入口后才精确清理当前通道旧对象。
正式版本拒绝不同提交或内容覆盖，CI/Beta 拒绝过期分支构建覆盖滚动入口。
同一通道串行发布，不影响另一通道或其他项目。
GHCR 与 GitHub Release 使用 `GITHUB_TOKEN`；工作流明确授予发布所需权限。
若 GHCR 包为私有，可登录后拉取，也可直接下载公开的 R2 或 GitHub 离线镜像包。

## 默认部署：docker run

从 GitHub Release 下载本通道 ZIP、`latest.json` 和 `SHA256SUMS`，校验后解压：

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
不同通道部署须使用匹配的镜像和 `deploy/updater.json` 更新源；交付包会自动生成对应 URL。

## 简单更新页面

当前调试阶段，在对应 workflow 成功后可重建平台容器以同步管理器与部署配置，保留已有工作目录、管理员配置和端口。相对稳定后的常规部署使用本节的平台自带更新功能，不再通过容器重建部署普通程序变更。

打开 `http://127.0.0.1:8080/` 或 `/update`，查看当前版本、最新版本、更新通道、来源和结果。
点击“检查更新”查询 R2；发现新版后点击“更新到最新版”。页面加载和状态轮询
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

CI 自动运行工具测试、Rust 检查、镜像构建、交付包身份与摘要校验及发布。
容器运行和程序更新恢复烟测不在自动发布流程中；保留 `script/smoke_container.py`
和 `script/smoke_runtime.py` 供手动验证。
当前平台已提供管理员登录及 SQLite 点表库；原生 TLS、诊断上传及多用户权限仍待实现。点表旧 JSON 在事务迁移成功后清理；数据库备份、容量及旧程序回滚限制见 [Modbus 点表管理](docs/Modbus点表管理.md)。
