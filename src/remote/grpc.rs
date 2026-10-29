//! 平台远程访问入口；控制流与各隧道独立，业务字节不解析、不记录。
use super::{
    hub::{FrameSender, Hub, LiveDevice, Peer, Pending},
    proto::*,
    relay::{self, send},
    response::ResponseStream,
    store::{DeviceStore, Grant, StoreError, uses_ssh_permission, valid_service},
};
use crate::{
    accounts::AccountRecord,
    client_auth::{ClientAuth, ClientAuthError},
};
use std::{
    pin::Pin,
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
use tokio::sync::{mpsc, oneshot};
use tokio_stream::{Stream, wrappers::ReceiverStream};
use tokio_util::sync::CancellationToken;
use tonic::{Request, Response, Status, Streaming};
use uuid::Uuid;
const QUEUE: usize = 8;
const FIRST: Duration = Duration::from_secs(10);
const ATTACH: Duration = Duration::from_secs(15);
type Output<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send>>;
#[derive(Clone)]
pub struct RemoteService {
    pub store: DeviceStore,
    pub clients: ClientAuth,
    pub hub: Hub,
    #[cfg(test)]
    pub(super) wait_for_open_frame_without_deadline: bool,
}
impl RemoteService {
    pub fn new(store: DeviceStore, clients: ClientAuth, hub: Hub) -> Self {
        Self {
            store,
            clients,
            hub,
            #[cfg(test)]
            wait_for_open_frame_without_deadline: false,
        }
    }

    pub(super) async fn first_open_frame(
        &self,
        input: &mut Streaming<TunnelFrame>,
    ) -> Result<TunnelFrame, Status> {
        // 撤权回归由测试外层期限约束，避免 SQLite 提交消耗生产首帧等待预算。
        #[cfg(test)]
        if self.wait_for_open_frame_without_deadline {
            return input
                .message()
                .await?
                .ok_or_else(|| Status::invalid_argument("必须提供首帧消息"));
        }
        first(input).await
    }
}
fn storage(error: StoreError) -> Status {
    match error {
        StoreError::Storage => Status::unavailable("设备存储暂不可用"),
        StoreError::Invalid(message) => Status::invalid_argument(message),
        _ => Status::permission_denied("设备凭据或访问授权无效"),
    }
}
fn client(error: ClientAuthError) -> Status {
    match error {
        ClientAuthError::Storage => Status::unavailable("账号存储暂不可用"),
        _ => Status::unauthenticated("上位机登录状态无效"),
    }
}
fn bearer<T>(request: &Request<T>) -> Result<String, &'static str> {
    let values: Vec<_> = request.metadata().get_all("authorization").iter().collect();
    if values.len() != 1 {
        return Err("必须提供唯一的访问凭据");
    }
    values[0]
        .to_str()
        .ok()
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|v| !v.is_empty() && v.len() <= 512 && !v.chars().any(char::is_whitespace))
        .map(str::to_owned)
        .ok_or("访问凭据格式不正确")
}
async fn first<T: prost::Message + Default>(input: &mut Streaming<T>) -> Result<T, Status> {
    tokio::time::timeout(FIRST, input.message())
        .await
        .map_err(|_| Status::deadline_exceeded("首帧等待超时"))??
        .ok_or_else(|| Status::invalid_argument("必须提供首帧消息"))
}
fn services(input: Vec<String>) -> Result<Vec<String>, &'static str> {
    if input.len() > 256 || input.iter().any(|s| !valid_service(s)) {
        return Err("设备服务标识无效或超过 256 项");
    }
    let mut input = input;
    input.sort();
    input.dedup();
    Ok(input)
}
#[tonic::async_trait]
impl device_access_service_server::DeviceAccessService for RemoteService {
    type ConnectDeviceStream = Output<PlatformMessage>;
    type AttachTunnelStream = Output<TunnelFrame>;
    async fn connect_device(
        &self,
        request: Request<Streaming<DeviceMessage>>,
    ) -> Result<Response<Self::ConnectDeviceStream>, Status> {
        let token = bearer(&request).map_err(Status::unauthenticated)?;
        let identity = self.store.authenticate(&token).await.map_err(|e| match e {
            StoreError::Storage => storage(e),
            _ => Status::unauthenticated("设备凭据无效"),
        })?;
        let mut inbound = request.into_inner();
        let hello = match first(&mut inbound).await?.payload {
            Some(device_message::Payload::Hello(hello)) => hello,
            _ => return Err(Status::invalid_argument("设备第一帧必须为 Hello")),
        };
        // 首帧等待期间凭据可能已被轮换或禁用，注册前再次验证。
        self.store.authenticate(&token).await.map_err(storage)?;
        if hello.device_id != identity.device_id {
            return Err(Status::permission_denied("设备身份与凭据不匹配"));
        }
        if hello.agent_version.len() > 128 || hello.agent_version.chars().any(char::is_control) {
            return Err(Status::invalid_argument("设备版本标识无效"));
        }
        let service_names = services(hello.services).map_err(Status::invalid_argument)?;
        let (tx, rx) = mpsc::channel(QUEUE);
        let (finish, terminal) = oneshot::channel();
        let cancel = CancellationToken::new();
        let connection_id = Uuid::new_v4().to_string();
        {
            let mut registry = self.hub.0.lock().await;
            if registry.devices.contains_key(&identity.device_id) {
                return Err(Status::already_exists("设备已经在线"));
            }
            if registry.devices.len() >= 10_000 {
                return Err(Status::resource_exhausted("在线设备数已达上限"));
            }
            registry.devices.insert(
                identity.device_id.clone(),
                LiveDevice {
                    connection_id: connection_id.clone(),
                    services: service_names,
                    agent_version: hello.agent_version,
                    outbound: tx.clone(),
                    cancel: cancel.clone(),
                },
            );
        }
        let service = self.clone();
        tokio::spawn(async move {
            tracing::info!(device_id=%identity.device_id,%connection_id,"设备控制连接已建立");
            let result = tokio::select! {
             result=service.control(&identity.device_id,&connection_id,&mut inbound,&tx,&cancel)=>result,
             error=service.monitor_device(&token)=>Err(error),
            };
            service
                .hub
                .remove_device(&identity.device_id, &connection_id)
                .await;
            if let Err(error) = &result {
                tracing::info!(device_id=%identity.device_id,code=?error.code(),"设备控制连接结束");
            }
            let _ = finish.send(result.err());
        });
        Ok(Response::new(Box::pin(ResponseStream::new(rx, terminal))))
    }
    async fn attach_tunnel(
        &self,
        request: Request<Streaming<TunnelFrame>>,
    ) -> Result<Response<Self::AttachTunnelStream>, Status> {
        let token = bearer(&request).map_err(Status::unauthenticated)?;
        let identity = self.store.authenticate(&token).await.map_err(|e| match e {
            StoreError::Storage => storage(e),
            _ => Status::unauthenticated("设备凭据无效"),
        })?;
        let mut inbound = request.into_inner();
        let attach = match first(&mut inbound).await?.payload {
            Some(tunnel_frame::Payload::Attach(attach)) => attach,
            _ => return Err(Status::invalid_argument("隧道第一帧必须为 attach")),
        };
        self.store.authenticate(&token).await.map_err(storage)?;
        let mut registry = self.hub.0.lock().await;
        let connection = registry
            .devices
            .get(&identity.device_id)
            .map(|d| d.connection_id.clone())
            .ok_or_else(|| Status::permission_denied("设备控制连接已失效"))?;
        let pending = registry
            .tunnels
            .get_mut(&attach.tunnel_id)
            .ok_or_else(|| Status::permission_denied("隧道票据无效"))?;
        if pending.device_id != identity.device_id
            || pending.connection_id != connection
            || pending.created.elapsed() > ATTACH
            || pending
                .ticket
                .as_bytes()
                .ct_eq(attach.attach_token.as_bytes())
                .unwrap_u8()
                != 1
        {
            return Err(Status::permission_denied("隧道票据无效"));
        }
        let sender = pending
            .attach
            .take()
            .ok_or_else(|| Status::permission_denied("隧道票据已消费"))?;
        let (tx, rx) = mpsc::channel(QUEUE);
        sender
            .send(Ok(Peer {
                inbound,
                outbound: tx,
                token,
            }))
            .map_err(|_| Status::cancelled("上位机已取消隧道"))?;
        Ok(Response::new(Box::pin(ReceiverStream::new(rx))))
    }
}
#[tonic::async_trait]
impl remote_access_service_server::RemoteAccessService for RemoteService {
    type OpenTunnelStream = Output<TunnelFrame>;
    async fn list_devices(
        &self,
        request: Request<ListDevicesRequest>,
    ) -> Result<Response<ListDevicesResponse>, Status> {
        let token = bearer(&request).map_err(Status::unauthenticated)?;
        let user = self.clients.authenticate(&token).await.map_err(client)?;
        let records = match user.account.role.as_str() {
            "admin" => self
                .store
                .list()
                .await
                .map_err(storage)?
                .into_iter()
                .filter(|device| device.enabled)
                .map(|device| {
                    (
                        device,
                        Grant {
                            account_id: user.account.id.clone(),
                            allow_grpc: true,
                            allow_ssh: true,
                        },
                    )
                })
                .collect(),
            "operator" => self
                .store
                .for_account(&user.account.id)
                .await
                .map_err(storage)?,
            _ => return Err(Status::permission_denied("上位机账号角色无效")),
        };
        let mut devices = Vec::with_capacity(records.len());
        for (record, grant) in records {
            let live = self.hub.snapshot(&record.device_id).await;
            let names = live
                .as_ref()
                .map(|d| {
                    d.services
                        .iter()
                        .filter(|s| {
                            if uses_ssh_permission(s) {
                                grant.allow_ssh
                            } else {
                                grant.allow_grpc
                            }
                        })
                        .cloned()
                        .collect()
                })
                .unwrap_or_default();
            devices.push(Device {
                device_id: record.device_id,
                display_name: record.display_name,
                online: live.is_some(),
                allow_grpc: grant.allow_grpc,
                allow_ssh: grant.allow_ssh,
                services: names,
                agent_version: live.map(|d| d.agent_version).unwrap_or_default(),
            });
        }
        Ok(Response::new(ListDevicesResponse { devices }))
    }
    async fn open_tunnel(
        &self,
        request: Request<Streaming<TunnelFrame>>,
    ) -> Result<Response<Self::OpenTunnelStream>, Status> {
        let token = bearer(&request).map_err(Status::unauthenticated)?;
        let user = self.clients.authenticate(&token).await.map_err(client)?;
        let mut inbound = request.into_inner();
        let open = match self.first_open_frame(&mut inbound).await?.payload {
            Some(tunnel_frame::Payload::Open(open)) => open,
            _ => return Err(Status::invalid_argument("隧道第一帧必须为 open")),
        };
        if !valid_service(&open.service) {
            return Err(Status::invalid_argument("服务名称必须为逻辑标识"));
        }
        // 首帧等待期间可能发生降级、退出或停用，查询设备状态前重新鉴权。
        let account = self
            .authorized(&open, &user.account.id, &token, None)
            .await?;
        tracing::info!(account_id=%account.id,role=%account.role,device_id=%open.device_id,service=%open.service,"上位机设备访问授权已通过");
        if open.service == "vertical-security" {
            tracing::info!(device_id=%open.device_id, "固定纵密通路已通过维护权限校验，准备转发管理连接");
        }
        let id = Uuid::new_v4().to_string();
        let ticket = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let (attach_tx, attach_rx) = oneshot::channel();
        let created = Instant::now();
        let deadline = tokio::time::Instant::now() + ATTACH;
        let live = {
            let mut registry = self.hub.0.lock().await;
            let live = registry
                .devices
                .get(&open.device_id)
                .cloned()
                .ok_or_else(|| Status::unavailable("设备当前离线"))?;
            if !live.services.contains(&open.service) {
                return Err(Status::not_found("设备未开放指定服务"));
            }
            if registry
                .tunnels
                .values()
                .filter(|t| t.device_id == open.device_id)
                .count()
                >= 16
                || registry.tunnels.len() >= 4096
            {
                return Err(Status::resource_exhausted("隧道数量已达上限"));
            }
            registry.tunnels.insert(
                id.clone(),
                Pending {
                    device_id: open.device_id.clone(),
                    connection_id: live.connection_id.clone(),
                    ticket: ticket.clone(),
                    created,
                    attach: Some(attach_tx),
                },
            );
            live
        };
        let (tx, rx) = mpsc::channel(QUEUE);
        let (finish, terminal) = oneshot::channel();
        let service = self.clone();
        tokio::spawn(async move {
            tracing::info!(device_id=%open.device_id,service=%open.service,tunnel_id=%id,"正在建立远程隧道");
            let work = async {
                let (upper, peer) = tokio::time::timeout_at(
                    deadline,
                    service.establish(&open, &id, &ticket, inbound, &tx, &live, attach_rx),
                )
                .await
                .map_err(|_| Status::deadline_exceeded("设备建立隧道超时"))??;
                service
                    .authorized(&open, &user.account.id, &token, Some(&peer.token))
                    .await?;
                relay::run(&id, upper, &tx, peer, &live.cancel).await
            };
            let result = tokio::select! {
                result = work => result,
                error = service.monitor_user(&open, &user.account.id, &token) => Err(error),
            };
            service.hub.0.lock().await.tunnels.remove(&id);
            if let Err(error) = &result {
                tracing::info!(device_id=%open.device_id,tunnel_id=%id,code=?error.code(),"远程隧道已关闭");
            } else {
                tracing::info!(device_id=%open.device_id,tunnel_id=%id,"远程隧道正常结束");
            }
            let _ = finish.send(result.err());
        });
        Ok(Response::new(Box::pin(ResponseStream::new(rx, terminal))))
    }
}
impl RemoteService {
    async fn monitor_device(&self, token: &str) -> Status {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tick.tick().await;
            if let Err(error) = self.store.authenticate(token).await {
                return storage(error);
            }
        }
    }
    async fn monitor_user(&self, open: &TunnelOpen, account: &str, token: &str) -> Status {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tick.tick().await;
            if let Err(error) = self.authorized(open, account, token, None).await {
                return error;
            }
        }
    }
    async fn control(
        &self,
        id: &str,
        connection: &str,
        inbound: &mut Streaming<DeviceMessage>,
        outbound: &mpsc::Sender<Result<PlatformMessage, Status>>,
        cancel: &CancellationToken,
    ) -> Result<(), Status> {
        send(
            outbound,
            PlatformMessage {
                payload: Some(platform_message::Payload::Welcome(DeviceWelcome {
                    connection_id: connection.to_owned(),
                    heartbeat_interval_seconds: 30,
                })),
            },
            cancel,
        )
        .await?;
        let heartbeat = tokio::time::sleep(Duration::from_secs(90));
        tokio::pin!(heartbeat);
        loop {
            tokio::select! {
                biased;
                _ = cancel.cancelled() => return Err(Status::permission_denied("设备已由管理端断开")),
                _ = outbound.closed() => return Ok(()),
                _ = &mut heartbeat => return Err(Status::deadline_exceeded("设备心跳超时")),
                message = inbound.message() => {
                    let Some(message) = message? else { return Ok(()) };
                    match message.payload {
                        Some(device_message::Payload::Heartbeat(beat)) => {
                            let names = services(beat.services).map_err(Status::invalid_argument)?;
                            if let Some(live) = self.hub.0.lock().await.devices.get_mut(id) {
                                if live.connection_id == connection { live.services = names; }
                            }
                            heartbeat.as_mut().reset(tokio::time::Instant::now() + Duration::from_secs(90));
                            send(outbound, PlatformMessage {
                                payload: Some(platform_message::Payload::HeartbeatAck(DeviceHeartbeatAck {})),
                            }, cancel).await?;
                        }
                        Some(device_message::Payload::Goodbye(_)) => return Ok(()),
                        Some(device_message::Payload::TunnelFailure(failure)) => {
                            self.hub.failure(id, connection, &failure.tunnel_id).await;
                        }
                        _ => return Err(Status::invalid_argument("设备控制消息格式不正确")),
                    }
                }
            }
        }
    }
    #[allow(clippy::too_many_arguments)]
    async fn establish(
        &self,
        open: &TunnelOpen,
        id: &str,
        ticket: &str,
        mut upper: Streaming<TunnelFrame>,
        outbound: &FrameSender,
        live: &LiveDevice,
        attached: oneshot::Receiver<Result<Peer, Status>>,
    ) -> Result<(Streaming<TunnelFrame>, Peer), Status> {
        let attachment = async {
            send(
                &live.outbound,
                PlatformMessage {
                    payload: Some(platform_message::Payload::OpenTunnel(TunnelRequest {
                        tunnel_id: id.to_owned(),
                        service: open.service.clone(),
                        attach_token: ticket.to_owned(),
                    })),
                },
                &live.cancel,
            )
            .await?;
            attached
                .await
                .map_err(|_| Status::unavailable("设备隧道建立失败"))?
        };
        let peer = tokio::select! {
            message = upper.message() => {
                return match message {
                    Err(error) => Err(error),
                    Ok(None) => Err(Status::cancelled("上位机已取消隧道")),
                    Ok(Some(_)) => Err(Status::invalid_argument("隧道 ready 之前不允许发送数据")),
                };
            }
            _ = outbound.closed() => return Err(Status::cancelled("上位机已取消隧道")),
            _ = live.cancel.cancelled() => return Err(Status::unavailable("设备控制连接已断开")),
            result = attachment => result?,
        };
        Ok((upper, peer))
    }
    async fn authorized(
        &self,
        open: &TunnelOpen,
        account: &str,
        token: &str,
        device_token: Option<&str>,
    ) -> Result<AccountRecord, Status> {
        let user = self.clients.authenticate(token).await.map_err(|e| {
            if matches!(e, ClientAuthError::Storage) {
                client(e)
            } else {
                Status::permission_denied("上位机会话授权已失效")
            }
        })?;
        if user.account.id != account {
            return Err(Status::permission_denied("上位机会话身份已变化"));
        }
        // 每次从有效会话读取最新角色，降级后立即恢复逐台授权检查。
        self.authorize_device(open, &user.account).await?;
        if let Some(token) = device_token {
            let device = self.store.authenticate(token).await.map_err(storage)?;
            if device.device_id != open.device_id {
                return Err(Status::permission_denied("设备凭据已失效"));
            }
        }
        Ok(user.account)
    }

    async fn authorize_device(
        &self,
        open: &TunnelOpen,
        account: &AccountRecord,
    ) -> Result<(), Status> {
        let result = match account.role.as_str() {
            "admin" => self.store.get(&open.device_id).await.and_then(|device| {
                if device.enabled {
                    Ok(())
                } else {
                    Err(StoreError::Denied)
                }
            }),
            "operator" => {
                self.store
                    .authorize(&open.device_id, &account.id, &open.service)
                    .await
            }
            _ => Err(StoreError::Denied),
        };
        result.map_err(|error| {
            tracing::warn!(account_id=%account.id,role=%account.role,device_id=%open.device_id,service=%open.service,"上位机设备访问授权被拒绝");
            storage(error)
        })
    }
}
