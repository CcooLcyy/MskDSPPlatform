# gRPC 端口分段管理

## 目标和边界

平台服务器将既有 TCP 50000 端口 MSS 限制持久化，并通过管理员网页管理。下位机程序、拨号参数、路由和固件均不修改。仅限制平台发给客户端的数据分段，默认推荐 MSS 1240；不修改反向 SYN-ACK。

Linux 宿主机独立服务管理 `ip mskdsp_grpc_mss` 表；不得恢复或清空全防火墙，不改其他服务端口。匹配固定外部接口、Docker bridge、DNAT 的原始/转发目标端口和初始 SYN，只降低原本更大的 MSS。端口固定为 50000，接口由 root 部署配置确定，网页仅能修改启用状态及 MSS。

## 管理接口

平台管理进程通过运行目录的 `network-control/control.sock` 访问宿主机服务；主容器不增加权限、不挂载 Docker socket。宿主机服务仅接受 root 和平台 UID 的本地 Unix socket 请求。套接字目录由 root 创建，设置文件位于 root 管理目录。

网页 `/network` 与 `/api/network/status`、`/api/network/settings` 均要求现有管理员会话。写操作沿用同源校验，要求 JSON 和 `X-MskDSP-Action: network`。请求只允许 `enabled`、`mss`、`revision`；MSS 为 536 至 1460 整数，拒绝布尔数值、额外字段及过期 revision。服务不可用时返回清晰错误，不显示已生效。

状态应区分保存配置与实际应用结果，显示固定端口、MSS、revision、持久化与命中计数；设置仅影响之后新建连接，保存不强制断开现有业务连接。

## 持久化与回退

宿主机 systemd 服务开机读取保存配置，按需应用独有 nft 表。只替换本服务自己的表；保存失败时恢复前一个规则。停用只删除自己的表；服务停止也清理自己的表。配置损坏、nft 不可用或执行失败时明确报告失败，不宣称已生效。

安装器只安装本服务的代码、配置与 unit；保留现有配置，重复执行不得覆盖管理员设置。卸载/回退通过停止独立服务删除其规则，保留配置以便恢复。宿主机升级或 Docker 重启后需检查服务和新连接分段；接口或发布端口迁移需要同步 root 配置。

## 验收

1. 单元/HTTP 测试验证严格输入、鉴权、同源保护、并发 revision、失败回退和停止清理。
2. 验证只操作独有表，规则方向正确、重复应用不累积。
3. 真服务器检查开机启用、服务重启恢复配置、管理员 API 和网页可达；不重启整台服务器。
4. 真机默认 TCP 参数下验证 256 KiB 前缀 SHA256、最大 IP1280 和 PPP 接收错误，再验证已有业务连接保持。
5. 已有完整包实测：50,072,993 字节，SHA256 `4faf3b2e674e621f06f048f42ae2d9b06136675b236a8200d416e66529aae148`，163.222 秒，EOF 与 gRPC 状态 0，PPP 接收错误增量 0；此结果不等同远程 SSH 安装闭环验收。

## 部署与操作

Linux 宿主机需已安装 nftables、Python 3 和 systemd；确认平台容器 UID/GID 均为 10001。源码部署：

```sh
sudo python3 script/install_grpc_network.py --work-dir /home/daniel/mskplt --external-interface enp7s0 --bridge-interface docker0 --enable
systemctl is-enabled mskdsp-platform-grpc-network.service
systemctl status mskdsp-platform-grpc-network.service
```

离线包包含同目录的 `deploy/network/install_grpc_network.py` 与 `grpc_network_host.py`，可使用前者完成同一安装。首次安装加 `--enable`；重复安装保留已有配置，不覆盖管理员保存的启停状态。安装器需要实际状态通过持久化与规则核验后才报告成功。

安装文件：`/etc/mskdsp-platform/grpc-network.json`、`/usr/local/lib/mskdsp-platform/grpc_network_host.py`、`/etc/systemd/system/mskdsp-platform-grpc-network.service`。运行目录中仅暴露本地控制 socket，不保存 root 凭据。

平台镜像必须同时更新 `runtime.py`、`grpc_network.py`、`network.html` 与 `update.html`；Dockerfile 已包含这些文件。仅安装新的 Rust 二进制无法带入这项管理功能。已有平台运行目录 bind mount 无需新增挂载或权限。

管理员登录后，从“平台维护 → gRPC 网络兼容”进入 `/network`。保存设置后仅新建 TCP 连接采用新值。停用可在网页保存 `enabled=false`，仍保留设置和管理服务；完整停止管理及其规则可执行：

```sh
sudo systemctl disable --now mskdsp-platform-grpc-network.service
```

不需要重启宿主机来验证持久化：先保存设置，再重启此独立服务，核对保存修订号及 applied/persistent 均为 true；开机启用由 systemd 的 `multi-user.target` 管理。不得为了验证而重启无关业务服务。
