use super::{grpc::RemoteService, hub::Hub, proto::*, store::*};
use crate::{
    accounts::{AccountStore, NewAccount},
    client_auth::ClientAuth,
};
use device_access_service_client::DeviceAccessServiceClient;
use remote_access_service_client::RemoteAccessServiceClient;
use tokio::{
    net::TcpListener,
    sync::{mpsc, oneshot},
};
use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};
use tonic::{
    Code, Request, Streaming,
    transport::{Channel, Server},
};
struct Fixture {
    _dir: tempfile::TempDir,
    store: DeviceStore,
    accounts: AccountStore,
    auth: ClientAuth,
    hub: Hub,
    device: DeviceAccessServiceClient<Channel>,
    upper: RemoteAccessServiceClient<Channel>,
    token: String,
    user: String,
    account: String,
    stop: Option<oneshot::Sender<()>>,
    task: tokio::task::JoinHandle<()>,
}
fn request<T>(body: T, token: &str) -> Request<T> {
    let mut req = Request::new(body);
    req.metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    req
}
fn frame(payload: tunnel_frame::Payload) -> TunnelFrame {
    TunnelFrame {
        payload: Some(payload),
    }
}
impl Fixture {
    async fn start() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let store = DeviceStore::open(dir.path().join("devices.sqlite3"))
            .await
            .unwrap();
        let created = store
            .create(
                NewDevice {
                    device_id: "field".into(),
                    display_name: "现场".into(),
                },
                vec![],
            )
            .await
            .unwrap();
        let accounts = AccountStore::open(dir.path().join("accounts.sqlite3"))
            .await
            .unwrap();
        let account = accounts
            .create(NewAccount {
                role: "operator".into(),
                username: "operator".into(),
                password: "field-password-123".into(),
                display_name: "操作员".into(),
            })
            .await
            .unwrap();
        store
            .set_grants(
                "field",
                vec![Grant {
                    account_id: account.id.clone(),
                    allow_grpc: true,
                    allow_ssh: true,
                }],
            )
            .await
            .unwrap();
        let auth = ClientAuth::new(accounts.clone(), 3600);
        let user = auth
            .login("operator", "field-password-123")
            .await
            .unwrap()
            .access_token;
        let hub = Hub::default();
        let service = RemoteService::new(store.clone(), auth.clone(), hub.clone());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = format!("http://{}", listener.local_addr().unwrap());
        let (stop, stopped) = oneshot::channel();
        let task = tokio::spawn(async move {
            Server::builder()
                .add_service(
                    device_access_service_server::DeviceAccessServiceServer::new(service.clone()),
                )
                .add_service(remote_access_service_server::RemoteAccessServiceServer::new(service))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                    let _ = stopped.await;
                })
                .await
                .unwrap();
        });
        Self {
            _dir: dir,
            store,
            accounts,
            auth,
            hub,
            device: DeviceAccessServiceClient::connect(address.clone())
                .await
                .unwrap(),
            upper: RemoteAccessServiceClient::connect(address).await.unwrap(),
            token: created.token,
            user,
            account: account.id,
            stop: Some(stop),
            task,
        }
    }
    async fn role(&self, role: &str) {
        let account = self.accounts.get(&self.account).await.unwrap();
        self.accounts
            .update(
                &account.id,
                account.revision,
                crate::accounts::AccountUpdate {
                    display_name: account.display_name,
                    enabled: true,
                    role: Some(role.into()),
                },
            )
            .await
            .unwrap();
    }
    async fn online(&mut self) -> (mpsc::Sender<DeviceMessage>, Streaming<PlatformMessage>) {
        let (tx, rx) = mpsc::channel(8);
        tx.send(DeviceMessage {
            payload: Some(device_message::Payload::Hello(DeviceHello {
                device_id: "field".into(),
                agent_version: "test".into(),
                services: vec!["ModuleManager".into(), "ssh".into()],
            })),
        })
        .await
        .unwrap();
        let mut output = self
            .device
            .connect_device(request(ReceiverStream::new(rx), &self.token))
            .await
            .unwrap()
            .into_inner();
        assert!(matches!(
            output.message().await.unwrap().unwrap().payload,
            Some(platform_message::Payload::Welcome(_))
        ));
        (tx, output)
    }
    async fn open(&mut self) -> (mpsc::Sender<TunnelFrame>, Streaming<TunnelFrame>) {
        self.open_service("ssh").await
    }
    async fn open_service(
        &mut self,
        service: &str,
    ) -> (mpsc::Sender<TunnelFrame>, Streaming<TunnelFrame>) {
        let (tx, rx) = mpsc::channel(8);
        tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
            device_id: "field".into(),
            service: service.into(),
        })))
        .await
        .unwrap();
        let out = self
            .upper
            .open_tunnel(request(ReceiverStream::new(rx), &self.user))
            .await
            .unwrap()
            .into_inner();
        (tx, out)
    }
    async fn attach(
        &mut self,
        control: &mut Streaming<PlatformMessage>,
    ) -> (
        mpsc::Sender<TunnelFrame>,
        Streaming<TunnelFrame>,
        TunnelRequest,
    ) {
        let message = control.message().await.unwrap().unwrap();
        let Some(platform_message::Payload::OpenTunnel(open)) = message.payload else {
            panic!("应收到隧道请求")
        };
        let (tx, rx) = mpsc::channel(8);
        tx.send(frame(tunnel_frame::Payload::Attach(TunnelAttach {
            tunnel_id: open.tunnel_id.clone(),
            attach_token: open.attach_token.clone(),
        })))
        .await
        .unwrap();
        let out = self
            .device
            .attach_tunnel(request(ReceiverStream::new(rx), &self.token))
            .await
            .unwrap()
            .into_inner();
        (tx, out, open)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop.take();
        self.task.abort();
    }
}

// 真实 localhost gRPC 两端转发二进制数据，半关闭后仍接收尾部，票据不能复用。
#[tokio::test]
async fn binary_relay_preserves_half_close_and_rejects_reused_ticket() {
    let mut f = Fixture::start().await;
    let (_device_tx, mut control) = f.online().await;
    let (upper_tx, mut upper) = f.open().await;
    let (device_tx, mut device, ticket) = f.attach(&mut control).await;
    assert!(matches!(
        upper.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::Ready(_))
    ));
    assert!(matches!(
        device.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::Ready(_))
    ));
    upper_tx
        .send(frame(tunnel_frame::Payload::Data(vec![0, 255, 1, 128])))
        .await
        .unwrap();
    assert_eq!(
        device.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::Data(vec![0, 255, 1, 128]))
    );
    upper_tx
        .send(frame(tunnel_frame::Payload::HalfClose(TunnelHalfClose {})))
        .await
        .unwrap();
    assert!(matches!(
        device.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::HalfClose(_))
    ));
    device_tx
        .send(frame(tunnel_frame::Payload::Data(b"tail".to_vec())))
        .await
        .unwrap();
    assert_eq!(
        upper.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::Data(b"tail".to_vec()))
    );
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Attach(TunnelAttach {
        tunnel_id: ticket.tunnel_id,
        attach_token: ticket.attach_token,
    })))
    .await
    .unwrap();
    let err = f
        .device
        .attach_tunnel(request(ReceiverStream::new(rx), &f.token))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Code::PermissionDenied);
    device_tx
        .send(frame(tunnel_frame::Payload::HalfClose(TunnelHalfClose {})))
        .await
        .unwrap();
    assert!(matches!(
        upper.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::HalfClose(_))
    ));
}

// 设备离线与未授权请求明确失败；设备上线后仅列出授权可用服务。
#[tokio::test]
async fn device_listing_offline_and_service_permissions() {
    let mut f = Fixture::start().await;
    f.store
        .create(
            NewDevice {
                device_id: "unassigned".into(),
                display_name: "其他账号的设备".into(),
            },
            vec![],
        )
        .await
        .unwrap();
    let mut list = f
        .upper
        .list_devices(request(ListDevicesRequest {}, &f.user))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(list.devices.len(), 1);
    assert_eq!(list.devices[0].device_id, "field");
    assert!(!list.devices.remove(0).online);
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
        device_id: "field".into(),
        service: "ssh".into(),
    })))
    .await
    .unwrap();
    let err = f
        .upper
        .open_tunnel(request(ReceiverStream::new(rx), &f.user))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Code::Unavailable);
    let (_tx, _control) = f.online().await;
    let list = f
        .upper
        .list_devices(request(ListDevicesRequest {}, &f.user))
        .await
        .unwrap()
        .into_inner();
    assert!(list.devices[0].online);
    assert_eq!(list.devices[0].services.len(), 2);
    f.store
        .set_grants(
            "field",
            vec![Grant {
                account_id: f.account.clone(),
                allow_grpc: true,
                allow_ssh: false,
            }],
        )
        .await
        .unwrap();
    let filtered = f
        .upper
        .list_devices(request(ListDevicesRequest {}, &f.user))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(filtered.devices.len(), 1);
    assert!(filtered.devices[0].allow_grpc);
    assert!(!filtered.devices[0].allow_ssh);
    assert_eq!(filtered.devices[0].services, ["ModuleManager"]);
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
        device_id: "field".into(),
        service: "ssh".into(),
    })))
    .await
    .unwrap();
    let err = f
        .upper
        .open_tunnel(request(ReceiverStream::new(rx), &f.user))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Code::PermissionDenied);
}

// 未授权账号看不到设备，即使用户名为 admin、知道设备编号也不能打开任何服务。
#[tokio::test]
async fn ungranted_operator_cannot_see_or_open_known_devices_even_when_named_admin() {
    let mut f = Fixture::start().await;
    let named_admin = f
        .accounts
        .create(NewAccount {
            role: "operator".into(),
            username: "admin".into(),
            password: "field-password-123".into(),
            display_name: "普通账号".into(),
        })
        .await
        .unwrap();
    f.user = f
        .auth
        .login(&named_admin.username, "field-password-123")
        .await
        .unwrap()
        .access_token;
    let (_device_tx, _control) = f.online().await;
    assert!(
        f.upper
            .list_devices(request(ListDevicesRequest {}, &f.user))
            .await
            .unwrap()
            .into_inner()
            .devices
            .is_empty()
    );
    for service in ["ssh", "ModuleManager"] {
        let (tx, rx) = mpsc::channel(1);
        tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
            device_id: "field".into(),
            service: service.into(),
        })))
        .await
        .unwrap();
        assert_eq!(
            f.upper
                .open_tunnel(request(ReceiverStream::new(rx), &f.user))
                .await
                .unwrap_err()
                .code(),
            Code::PermissionDenied
        );
    }
}

// 当前会话升为管理员后立即覆盖全部及后建设备，不依赖授权行，禁用设备仍不可见。
#[tokio::test]
async fn admin_role_sees_all_enabled_devices_and_opens_services_without_grants() {
    let mut f = Fixture::start().await;
    f.store.set_grants("field", vec![]).await.unwrap();
    assert!(
        f.upper
            .list_devices(request(ListDevicesRequest {}, &f.user))
            .await
            .unwrap()
            .into_inner()
            .devices
            .is_empty()
    );
    f.role("admin").await;
    for id in ["later", "disabled"] {
        f.store
            .create(
                NewDevice {
                    device_id: id.into(),
                    display_name: id.into(),
                },
                vec![],
            )
            .await
            .unwrap();
    }
    f.store
        .update(
            "disabled",
            DeviceUpdate {
                display_name: "停用设备".into(),
                enabled: false,
            },
        )
        .await
        .unwrap();
    let (_device_tx, mut control) = f.online().await;
    let list = f
        .upper
        .list_devices(request(ListDevicesRequest {}, &f.user))
        .await
        .unwrap()
        .into_inner();
    let mut ids: Vec<_> = list.devices.iter().map(|d| d.device_id.as_str()).collect();
    ids.sort();
    assert_eq!(ids, ["field", "later"]);
    assert!(list.devices.iter().all(|d| d.allow_grpc && d.allow_ssh));
    let field = list
        .devices
        .iter()
        .find(|d| d.device_id == "field")
        .unwrap();
    assert_eq!(field.services, ["ModuleManager", "ssh"]);
    for service in ["ssh", "ModuleManager"] {
        let (upper_tx, mut upper) = f.open_service(service).await;
        let (_device_tx, mut device, _) = f.attach(&mut control).await;
        assert!(matches!(
            upper.message().await.unwrap().unwrap().payload,
            Some(tunnel_frame::Payload::Ready(_))
        ));
        device.message().await.unwrap();
        upper_tx
            .send(frame(tunnel_frame::Payload::Data(vec![1, 0, 255])))
            .await
            .unwrap();
        assert_eq!(
            device.message().await.unwrap().unwrap().payload,
            Some(tunnel_frame::Payload::Data(vec![1, 0, 255]))
        );
    }
    assert!(f.store.grants("field").await.unwrap().is_empty());
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
        device_id: "disabled".into(),
        service: "ssh".into(),
    })))
    .await
    .unwrap();
    assert_eq!(
        f.upper
            .open_tunnel(request(ReceiverStream::new(rx), &f.user))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

// 首次读取请求体说明服务已完成初始认证，可确定性复现首帧等待期间的撤权。
struct PausedRequestBody {
    read_started: Option<oneshot::Sender<()>>,
    frames: mpsc::Receiver<axum::body::Bytes>,
}
impl tokio_stream::Stream for PausedRequestBody {
    type Item = Result<axum::body::Bytes, std::convert::Infallible>;
    fn poll_next(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Self::Item>> {
        if let Some(started) = self.read_started.take() {
            let _ = started.send(());
        }
        self.frames.poll_recv(cx).map(|frame| frame.map(Ok))
    }
}

// 测试专用的等待开关不能改变默认入口的十秒首帧期限。
#[tokio::test]
async fn default_open_tunnel_first_frame_deadline_is_ten_seconds() {
    use tonic::codec::{Codec, ProstCodec};

    let f = Fixture::start().await;
    let service = RemoteService::new(f.store.clone(), f.auth.clone(), f.hub.clone());
    let (started, read_started) = oneshot::channel();
    let (_frames, rx) = mpsc::channel(1);
    let body = axum::body::Body::from_stream(PausedRequestBody {
        read_started: Some(started),
        frames: rx,
    });
    let mut codec = ProstCodec::<TunnelFrame, TunnelFrame>::default();
    let mut input = Streaming::new_request(codec.decoder(), body, None, None);
    // 仅验证入口的首帧读取，虚拟时钟内不执行数据库或认证 I/O。
    tokio::time::pause();
    let pending = tokio::spawn(async move { service.first_open_frame(&mut input).await });
    tokio::time::timeout(std::time::Duration::from_secs(30), read_started)
        .await
        .expect("隧道入口未开始读取首帧")
        .unwrap();
    tokio::time::advance(std::time::Duration::from_secs(9)).await;
    assert!(!pending.is_finished(), "首帧期限在十秒之前触发");
    tokio::time::advance(std::time::Duration::from_secs(1)).await;
    let error = match pending.await.unwrap() {
        Err(error) => error,
        Ok(_) => panic!("缺少首帧时必须返回超时"),
    };
    assert_eq!(error.code(), Code::DeadlineExceeded);
    assert_eq!(error.message(), "首帧等待超时");
}

// 首帧等待期间降级、退出或停用都必须在查询在线状态、通知下位机之前重新拒绝。
#[tokio::test]
async fn authorization_changes_during_first_frame_wait_are_checked_before_device_access() {
    use prost::Message;
    use remote_access_service_server::RemoteAccessService;
    use tonic::codec::{Codec, ProstCodec};

    for online in [false, true] {
        for change in ["demote", "logout", "disable"] {
            let mut f = Fixture::start().await;
            f.role("admin").await;
            f.store.set_grants("field", vec![]).await.unwrap();
            let mut device_control = if online { Some(f.online().await) } else { None };
            let (started, read_started) = oneshot::channel();
            let (frames, rx) = mpsc::channel(1);
            let body = axum::body::Body::from_stream(PausedRequestBody {
                read_started: Some(started),
                frames: rx,
            });
            let mut codec = ProstCodec::<TunnelFrame, TunnelFrame>::default();
            let input = Streaming::new_request(codec.decoder(), body, None, None);
            let mut service = RemoteService::new(f.store.clone(), f.auth.clone(), f.hub.clone());
            service.wait_for_open_frame_without_deadline = true;
            let token = f.user.clone();
            let pending = tokio::spawn(async move {
                RemoteAccessService::open_tunnel(&service, request(input, &token)).await
            });
            tokio::time::timeout(std::time::Duration::from_secs(30), read_started)
                .await
                .unwrap()
                .unwrap();
            // 明确跨过生产十秒期限，再恢复真实时钟执行 SQLite 撤权。
            tokio::time::pause();
            tokio::time::advance(std::time::Duration::from_secs(11)).await;
            assert!(
                !pending.is_finished(),
                "撤权测试专用首帧等待不应在十秒后结束"
            );
            tokio::time::resume();
            let result = tokio::time::timeout(std::time::Duration::from_secs(30), async {
                match change {
                    "demote" => f.role("operator").await,
                    "logout" => f.auth.logout(&f.user).await.unwrap(),
                    "disable" => {
                        let account = f.accounts.get(&f.account).await.unwrap();
                        f.accounts
                            .update(
                                &account.id,
                                account.revision,
                                crate::accounts::AccountUpdate {
                                    display_name: account.display_name,
                                    enabled: false,
                                    role: None,
                                },
                            )
                            .await
                            .unwrap();
                    }
                    _ => unreachable!(),
                }
                let open = frame(tunnel_frame::Payload::Open(TunnelOpen {
                    device_id: "field".into(),
                    service: "ssh".into(),
                }))
                .encode_to_vec();
                let mut wire = vec![0];
                wire.extend_from_slice(&(open.len() as u32).to_be_bytes());
                wire.extend_from_slice(&open);
                if let Err(send_error) = frames.send(wire.into()).await {
                    let outcome = match pending.await {
                        Ok(Err(status)) => format!("gRPC 状态={status:?}"),
                        Ok(Ok(_)) => "意外收到成功的隧道响应".to_owned(),
                        Err(error) => format!("请求任务失败：{error}"),
                    };
                    panic!("首帧发送失败：{send_error}；{outcome}；撤权={change}，在线={online}");
                }
                pending.await.expect("隧道请求任务失败")
            })
            .await
            .unwrap_or_else(|_| panic!("撤权及首帧响应超过三十秒：撤权={change}，在线={online}"));
            let error = match result {
                Err(error) => error,
                Ok(_) => panic!("首帧等待期间 {change} 后不应建立隧道，online={online}"),
            };
            assert_eq!(
                error.code(),
                Code::PermissionDenied,
                "change={change}, online={online}"
            );
            assert!(f.hub.0.lock().await.tunnels.is_empty());
            if let Some((_, control)) = &mut device_control {
                assert!(
                    tokio::time::timeout(std::time::Duration::from_millis(50), control.message())
                        .await
                        .is_err(),
                    "撤权后的请求不能通知下位机"
                );
            }
        }
    }
}

// 管理员降级后的现有隧道按最新角色及明确授权判断：有授权继续，无授权终止。
#[tokio::test]
async fn admin_demotion_preserves_explicit_grant_and_closes_ungranted_tunnels() {
    let mut f = Fixture::start().await;
    f.role("admin").await;
    let (_device_tx, mut control) = f.online().await;
    let (upper_tx, mut upper) = f.open().await;
    let (_device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    f.role("operator").await;
    tokio::time::sleep(std::time::Duration::from_millis(1200)).await;
    upper_tx
        .send(frame(tunnel_frame::Payload::Data(vec![5])))
        .await
        .unwrap();
    assert_eq!(
        device.message().await.unwrap().unwrap().payload,
        Some(tunnel_frame::Payload::Data(vec![5]))
    );
    drop(upper_tx);
    drop(upper);
    drop(device);

    f.role("admin").await;
    f.store.set_grants("field", vec![]).await.unwrap();
    let (_upper_tx, mut upper) = f.open().await;
    let (_device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    f.role("operator").await;
    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_secs(3), upper.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert!(
        f.upper
            .list_devices(request(ListDevicesRequest {}, &f.user))
            .await
            .unwrap()
            .into_inner()
            .devices
            .is_empty()
    );
}

// 管理员设备权限不能绕过当前登录会话过期，已有隧道必须终止。
#[tokio::test]
async fn admin_tunnel_still_closes_when_session_expires() {
    let mut f = Fixture::start().await;
    f.role("admin").await;
    f.store.set_grants("field", vec![]).await.unwrap();
    let (_device_tx, mut control) = f.online().await;
    let (_upper_tx, mut upper) = f.open().await;
    let (_device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    assert!(f.auth.expire_session_for_test(&f.user).await);
    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_secs(3), upper.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

// 删除账号使已有隧道和旧会话失效，同名重建不能看到或访问原账号的授权设备。
#[tokio::test]
async fn deleted_account_loses_tunnel_and_recreated_username_does_not_inherit_grants() {
    let mut f = Fixture::start().await;
    let (_control_tx, mut control) = f.online().await;
    let (_upper_tx, mut upper) = f.open().await;
    let (_device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    f.accounts.delete(&f.account, 1).await.unwrap();
    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_secs(3), upper.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert_eq!(
        f.upper
            .list_devices(request(ListDevicesRequest {}, &f.user))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    let replacement = f
        .accounts
        .create(NewAccount {
            role: "operator".into(),
            username: "operator".into(),
            password: "field-password-123".into(),
            display_name: "重建账号".into(),
        })
        .await
        .unwrap();
    assert_ne!(replacement.id, f.account);
    f.user = f
        .auth
        .login("operator", "field-password-123")
        .await
        .unwrap()
        .access_token;
    assert!(
        f.upper
            .list_devices(request(ListDevicesRequest {}, &f.user))
            .await
            .unwrap()
            .into_inner()
            .devices
            .is_empty()
    );
    for service in ["ssh", "ModuleManager"] {
        let (tx, rx) = mpsc::channel(1);
        tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
            device_id: "field".into(),
            service: service.into(),
        })))
        .await
        .unwrap();
        assert_eq!(
            f.upper
                .open_tunnel(request(ReceiverStream::new(rx), &f.user))
                .await
                .unwrap_err()
                .code(),
            Code::PermissionDenied
        );
    }
    assert!(
        f.hub.snapshot("field").await.is_some(),
        "删除账号不应断开设备本身"
    );
}

// 账号退出会关闭现有隧道，响应流取消后清理隧道，设备禁用关闭控制流。
#[tokio::test]
async fn revocation_and_cancellation_release_sessions() {
    let mut f = Fixture::start().await;
    let (_device_tx, mut control) = f.online().await;
    let (_tx, mut upper) = f.open().await;
    let (_device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    f.auth.logout(&f.user).await.unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(3), upper.message())
        .await
        .unwrap();
    assert_eq!(result.unwrap_err().code(), Code::PermissionDenied);
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert!(f.hub.0.lock().await.tunnels.is_empty());
    f.store
        .update(
            "field",
            DeviceUpdate {
                display_name: "现场".into(),
                enabled: false,
            },
        )
        .await
        .unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(3), control.message())
        .await
        .unwrap();
    assert_eq!(result.unwrap_err().code(), Code::PermissionDenied);
}

// 票据必须正确且绑定设备，取消未附着请求后释放额度。
#[tokio::test]
async fn wrong_ticket_and_cancelled_pending_tunnel_are_rejected() {
    let mut f = Fixture::start().await;
    let (_device_tx, mut control) = f.online().await;
    let (upper_tx, upper) = f.open().await;
    let command = control.message().await.unwrap().unwrap();
    let Some(platform_message::Payload::OpenTunnel(command)) = command.payload else {
        panic!("应收到打开请求")
    };
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Attach(TunnelAttach {
        tunnel_id: command.tunnel_id.clone(),
        attach_token: "incorrect".into(),
    })))
    .await
    .unwrap();
    let err = f
        .device
        .attach_tunnel(request(ReceiverStream::new(rx), &f.token))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Code::PermissionDenied);
    drop(upper);
    drop(upper_tx);
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        loop {
            if f.hub.0.lock().await.tunnels.is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Attach(TunnelAttach {
        tunnel_id: command.tunnel_id,
        attach_token: command.attach_token,
    })))
    .await
    .unwrap();
    assert_eq!(
        f.device
            .attach_tunnel(request(ReceiverStream::new(rx), &f.token))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

// 已建立隧道拒绝空帧及半关闭之后的写入，终止双方并释放会话。
#[tokio::test]
async fn malformed_data_terminates_tunnel() {
    let mut f = Fixture::start().await;
    let (_device_tx, mut control) = f.online().await;
    let (upper_tx, mut upper) = f.open().await;
    let (_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    upper_tx
        .send(frame(tunnel_frame::Payload::Data(vec![])))
        .await
        .unwrap();
    assert_eq!(
        upper.message().await.unwrap_err().code(),
        Code::InvalidArgument
    );
}

// 接收方不读取且转发被背压阻塞时，撤权仍在独立监视器的一个周期内结束隧道。
#[tokio::test]
async fn grant_revocation_interrupts_backpressure() {
    let mut f = Fixture::start().await;
    let (_device_tx, mut control) = f.online().await;
    let (_upper_tx, mut upper) = f.open().await;
    let (device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    let writer = tokio::spawn(async move {
        for _ in 0..512 {
            if device_tx
                .send(frame(tunnel_frame::Payload::Data(vec![7; 65536])))
                .await
                .is_err()
            {
                break;
            }
        }
    });
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    f.store.set_grants("field", vec![]).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            if f.hub.0.lock().await.tunnels.is_empty() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    writer.abort();
}

// 下位机本机目标连接失败后主动通知，平台立即报告失败而非等待附着超时。
#[tokio::test]
async fn local_target_failure_is_reported() {
    let mut f = Fixture::start().await;
    let (device_tx, mut control) = f.online().await;
    let (_tx, mut upper) = f.open().await;
    let Some(platform_message::Payload::OpenTunnel(open)) =
        control.message().await.unwrap().unwrap().payload
    else {
        panic!("应收到打开请求")
    };
    device_tx
        .send(DeviceMessage {
            payload: Some(device_message::Payload::TunnelFailure(TunnelFailure {
                tunnel_id: open.tunnel_id,
                message: "本机连接失败".into(),
            })),
        })
        .await
        .unwrap();
    assert_eq!(upper.message().await.unwrap_err().code(), Code::Unavailable);
}

// 重复身份无法抢占设备，旧设备令牌轮换后原控制流失效。
#[tokio::test]
async fn duplicate_device_and_rotated_credentials() {
    let mut f = Fixture::start().await;
    let (_tx, mut control) = f.online().await;
    let (tx, rx) = mpsc::channel(1);
    tx.send(DeviceMessage {
        payload: Some(device_message::Payload::Hello(DeviceHello {
            device_id: "field".into(),
            agent_version: "test".into(),
            services: vec![],
        })),
    })
    .await
    .unwrap();
    assert_eq!(
        f.device
            .connect_device(request(ReceiverStream::new(rx), &f.token))
            .await
            .unwrap_err()
            .code(),
        Code::AlreadyExists
    );
    f.store.rotate("field").await.unwrap();
    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_secs(3), control.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

// 上传背压时反向与心跳仍活跃，取消后清理隧道且复用控制连接重新传输。
#[tokio::test]
async fn blocked_upload_keeps_reverse_heartbeat_and_reopen_available() {
    let mut f = Fixture::start().await;
    let (control_tx, mut control) = f.online().await;
    let (upper_tx, mut upper) = f.open().await;
    let (device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    let writer = tokio::spawn(async move {
        for _ in 0..1024 {
            if upper_tx
                .send(frame(tunnel_frame::Payload::Data(vec![1; 65536])))
                .await
                .is_err()
            {
                break;
            }
        }
    });
    // 下位机不读取上传流，使上位机至下位机方向触发背压。
    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    assert!(!writer.is_finished(), "上传发送必须仍被背压阻塞");
    device_tx
        .send(frame(tunnel_frame::Payload::Data(
            b"reverse-remains-live".to_vec(),
        )))
        .await
        .unwrap();
    let response = tokio::time::timeout(std::time::Duration::from_secs(3), upper.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(
        response.payload,
        Some(tunnel_frame::Payload::Data(
            b"reverse-remains-live".to_vec()
        ))
    );
    control_tx
        .send(DeviceMessage {
            payload: Some(device_message::Payload::Heartbeat(DeviceHeartbeat {
                services: vec!["ssh".into()],
            })),
        })
        .await
        .unwrap();
    assert!(matches!(
        tokio::time::timeout(std::time::Duration::from_secs(3), control.message())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
            .payload,
        Some(platform_message::Payload::HeartbeatAck(_))
    ));
    drop(upper);
    writer.abort();
    let _ = writer.await;
    drop(device);
    drop(device_tx);
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while !f.hub.0.lock().await.tunnels.is_empty() {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert!(f.hub.snapshot("field").await.is_some());
    let (retry_tx, mut retry) = f.open().await;
    let (_device_tx, mut device, _) = f.attach(&mut control).await;
    retry.message().await.unwrap();
    device.message().await.unwrap();
    retry_tx
        .send(frame(tunnel_frame::Payload::Data(
            "重试成功".as_bytes().to_vec(),
        )))
        .await
        .unwrap();
    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_secs(3), device.message())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
            .payload,
        Some(tunnel_frame::Payload::Data("重试成功".as_bytes().to_vec()))
    );
    drop(retry);
    drop(retry_tx);
    drop(device);
    drop(_device_tx);
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while !f.hub.0.lock().await.tunnels.is_empty() {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
}

// 控制通知入队受阻也纳入同一十五秒打开期限，过期会话清理并拒绝晚附着。
#[tokio::test]
async fn open_deadline_includes_blocked_control_notification() {
    let mut f = Fixture::start().await;
    let (_device_tx, _control) = f.online().await;
    let (tx, _held) = mpsc::channel(1);
    tx.send(Ok(PlatformMessage {
        payload: Some(platform_message::Payload::HeartbeatAck(
            DeviceHeartbeatAck {},
        )),
    }))
    .await
    .unwrap();
    f.hub
        .0
        .lock()
        .await
        .devices
        .get_mut("field")
        .unwrap()
        .outbound = tx;
    let start = std::time::Instant::now();
    let (_tx, mut upper) = f.open().await;
    let result = tokio::time::timeout(std::time::Duration::from_secs(17), upper.message())
        .await
        .unwrap();
    assert_eq!(result.unwrap_err().code(), Code::DeadlineExceeded);
    assert!(start.elapsed() < std::time::Duration::from_secs(17));
    assert!(f.hub.0.lock().await.tunnels.is_empty());
}

// 真实本机 TCP 服务接收 50 MiB 上传后才回写，隧道传递 EOF 并保留全部尾部。
#[tokio::test]
async fn tcp_echo_roundtrip_preserves_binary_tail() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let echo = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut bytes = Vec::new();
        socket.read_to_end(&mut bytes).await.unwrap();
        socket.write_all(&bytes).await.unwrap();
        socket.write_all(b"tail").await.unwrap();
        socket.shutdown().await.unwrap();
    });
    let mut f = Fixture::start().await;
    let (_device_tx, mut control) = f.online().await;
    let (upper_tx, mut upper) = f.open().await;
    let (device_tx, mut device, _) = f.attach(&mut control).await;
    upper.message().await.unwrap();
    device.message().await.unwrap();
    let socket = tokio::net::TcpStream::connect(address).await.unwrap();
    let (mut reader, mut writer) = socket.into_split();
    let write = tokio::spawn(async move {
        while let Some(frame) = device.message().await.unwrap() {
            match frame.payload {
                Some(tunnel_frame::Payload::Data(data)) => writer.write_all(&data).await.unwrap(),
                Some(tunnel_frame::Payload::HalfClose(_)) => {
                    // 仅结束 TCP 写方向，接收流继续存活，等待 TCP 回传尾部。
                    writer.shutdown().await.unwrap();
                }
                _ => panic!("非法转发帧"),
            }
        }
    });
    let read = tokio::spawn(async move {
        let mut buffer = vec![0; 65536];
        loop {
            let size = reader.read(&mut buffer).await.unwrap();
            if size == 0 {
                device_tx
                    .send(frame(tunnel_frame::Payload::HalfClose(TunnelHalfClose {})))
                    .await
                    .unwrap();
                break;
            }
            device_tx
                .send(frame(tunnel_frame::Payload::Data(buffer[..size].to_vec())))
                .await
                .unwrap();
        }
    });
    let payload: Vec<u8> = (0..50 * 1024 * 1024).map(|n| (n % 251) as u8).collect();
    for chunk in payload.chunks(65536) {
        upper_tx
            .send(frame(tunnel_frame::Payload::Data(chunk.to_vec())))
            .await
            .unwrap();
    }
    upper_tx
        .send(frame(tunnel_frame::Payload::HalfClose(TunnelHalfClose {})))
        .await
        .unwrap();
    let mut received = Vec::new();
    while let Some(frame) = tokio::time::timeout(std::time::Duration::from_secs(5), upper.message())
        .await
        .unwrap()
        .unwrap()
    {
        match frame.payload {
            Some(tunnel_frame::Payload::Data(data)) => received.extend(data),
            Some(tunnel_frame::Payload::HalfClose(_)) => break,
            _ => panic!("非法响应帧"),
        }
    }
    let mut expected = payload;
    expected.extend_from_slice(b"tail");
    assert_eq!(received, expected);
    write.await.unwrap();
    read.await.unwrap();
    echo.await.unwrap();
}

// 每设备十六条并发额度同时包含待附着及已建立的隧道。
#[tokio::test]
async fn per_device_tunnel_limit_counts_pending_requests() {
    let mut f = Fixture::start().await;
    let (_device_tx, _control) = f.online().await;
    let mut pending = Vec::new();
    for _ in 0..16 {
        pending.push(f.open().await);
    }
    let (tx, rx) = mpsc::channel(1);
    tx.send(frame(tunnel_frame::Payload::Open(TunnelOpen {
        device_id: "field".into(),
        service: "ssh".into(),
    })))
    .await
    .unwrap();
    let err = f
        .upper
        .open_tunnel(request(ReceiverStream::new(rx), &f.user))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Code::ResourceExhausted);
    drop(pending);
}
