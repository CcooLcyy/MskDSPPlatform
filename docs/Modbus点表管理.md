# Modbus 点表管理

## 目标与边界

平台提供 `/modbus-templates` 点表库界面，支持导入上位机导出的 JSON 模板、新建、查看、逐点编辑、增删点位、保存、下载和删除。首页提供点表库入口，保留程序更新入口。本次仅修改平台，不新增上位机联机同步或设备下发功能。

使用 `MskDSPUpper/src/features/modbus-templates/model.ts` 的 `schema_version=1` 格式，RTU/TCP 共用；真实功能码为 1、3、4、5、6、16。工程量参数 scale/offset/deadband 保留十进制字符串。文件最大 2 MiB、每表 1 至 2000 点，字段与组合校验对齐上位机。下载的 JSON 可通过上位机的“导入模板”使用。网页未保存改动需提示，服务端返回中文校验错误。

上位机拒绝与内置模板或已有个人模板重复的模板 ID。平台保留上传文件的 ID；需要导入修改后的副本时，可使用“复制为新点表”，或修改模板 ID 后保存、下载。格式化下载也必须不超过 2 MiB。

## 接口与持久化

- `GET /api/v1/modbus-templates`：返回摘要数组，含平台 id、revision、name、manufacturer、models、protocol_version、address_base、point_count、created_at、updated_at。
- `POST /api/v1/modbus-templates`：请求体为模板对象，创建后返回 201 和完整记录。平台分配 UUID，模板自身 id 保留；不会按名称或模板 id 自动覆盖现有记录。
- `GET /api/v1/modbus-templates/{id}`：返回完整记录 `{id, revision, created_at, updated_at, template}`。
- `PUT /api/v1/modbus-templates/{id}`：模板对象替换，必须携带 `If-Match: <revision>`，成功返回完整记录；旧版本返回 409，缺少版本返回 428。
- `DELETE /api/v1/modbus-templates/{id}`：必须携带 `If-Match`，成功返回 204。
- `GET /api/v1/modbus-templates/{id}/download`：仅输出模板 JSON，下载文件名使用模板自身 id。

页面及全部点表接口接入平台现有管理员登录认证，未登录的页面访问跳转 `/login`，接口返回 401。写接口还需 `X-MskDSP-Action: modbus-template` 标记及同源请求；标记用于防跨源误操作，不能替代登录认证。

存储目录默认 `data/modbus-templates`，支持 `MSKDSP_PLATFORM_DATA_DIR` 覆盖数据根目录；容器使用现有持久卷 `/var/lib/mskdsp-platform/data`。每条记录独立 JSON；采用同目录临时文件、同步落盘和原子重命名后返回成功。串行化写入并检查 revision。最多保存 100 张点表。存储错误不报告成功，也不自动丢弃损坏记录。时间戳为 Unix 毫秒。

## 验收与异常场景

1. 上位机内置及个人模板导入后，字段完整，下载再通过上位机校验；科学计数法与精确十进制字符串不丢失。
2. 网页新建、修改点位及可选条件/枚举字段，保存后重新读取一致；失败时保留编辑内容。
3. 空表、重复标签/点位 id、越界地址、错误功能码/类型/寄存器组合、非法十进制、未知字段与未知版本被拒绝。
4. 服务重建存储实例后记录仍存在；并发旧版本覆盖/删除被拒绝；不存在记录返回 404；非法平台 id 不允许路径穿越。
5. HTTP 请求限 2 MiB，管理进程点表请求同步支持此限制；已有更新请求限制保持原值。
6. 页面状态和内容使用文本或表单赋值，导入内容不得作为 HTML 执行；请求期间禁用重复操作，错误以中文展示。

按文档先补校验、持久化、HTTP 合约与页面交互测试，再实现并完成本地构建与相关回归。发布与远端部署不在本次验收范围内。
