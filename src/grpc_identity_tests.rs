use super::*;
use crate::identities::{AgentRecord, AgentUpdate, IssuedCredential, NewAgent};
use agent::agent_service_client::AgentServiceClient;
use tempfile::TempDir;
use tokio::{net::TcpListener, sync::oneshot, task::JoinHandle};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{
    Code,
    transport::{Channel, Server},
};

struct TestServer {
    _directory: TempDir,
    identities: IdentityStore,
    sessions: SessionRegistry,
    address: String,
    client: AgentServiceClient<Channel>,
    shutdown: Option<oneshot::Sender<()>>,
    task: JoinHandle<()>,
}

impl TestServer {
    async fn start() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let identities = IdentityStore::open(directory.path().join("identities.sqlite3"))
            .await
            .unwrap();
        let sessions = SessionRegistry::default();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let service = AgentGrpcService::new(sessions.clone(), "1".into(), identities.clone());
        let (shutdown, stopped) = oneshot::channel();
        let task = tokio::spawn(async move {
            Server::builder()
                .add_service(AgentServiceServer::new(service))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                    let _ = stopped.await;
                })
                .await
                .unwrap();
        });
        let client = AgentServiceClient::connect(format!("http://{address}"))
            .await
            .unwrap();
        Self {
            _directory: directory,
            identities,
            sessions,
            address,
            client,
            shutdown: Some(shutdown),
            task,
        }
    }

    async fn register(&self) -> IssuedCredential {
        self.identities
            .register(
                NewAgent {
                    name: "现场一号".into(),
                    site_id: "site-1".into(),
                    notes: "集成测试".into(),
                },
                "admin",
            )
            .await
            .unwrap()
    }

    async fn connect(
        &mut self,
        id: &str,
        token: Option<&str>,
        site_id: &str,
    ) -> Result<(mpsc::Sender<AgentMessage>, Streaming<ServerMessage>), Status> {
        self.connect_version(id, token, site_id, "0.7.0").await
    }

    async fn connect_version(
        &mut self,
        id: &str,
        token: Option<&str>,
        site_id: &str,
        version: &str,
    ) -> Result<(mpsc::Sender<AgentMessage>, Streaming<ServerMessage>), Status> {
        let (sender, receiver) = mpsc::channel(4);
        sender
            .send(AgentMessage {
                payload: Some(agent_message::Payload::Hello(agent::Hello {
                    agent_id: id.into(),
                    site_id: site_id.into(),
                    agent_version: version.into(),
                    protocol_version: "1".into(),
                    capabilities: vec![],
                })),
            })
            .await
            .unwrap();
        let mut request = Request::new(ReceiverStream::new(receiver));
        if let Some(token) = token {
            request
                .metadata_mut()
                .insert("authorization", format!("Bearer {token}").parse().unwrap());
        }
        let response = self.client.connect_agent(request).await?.into_inner();
        Ok((sender, response))
    }

    async fn online(
        &mut self,
        issued: &IssuedCredential,
    ) -> (mpsc::Sender<AgentMessage>, Streaming<ServerMessage>) {
        let (sender, mut response) = self
            .connect(&issued.agent.id, Some(&issued.token), &issued.agent.site_id)
            .await
            .unwrap();
        assert!(matches!(
            response.message().await.unwrap().unwrap().payload,
            Some(server_message::Payload::Welcome(_))
        ));
        assert_eq!(self.sessions.count().await, 1);
        (sender, response)
    }

    async fn denied(&mut self, id: &str, token: Option<&str>, site: &str, expected: Code) {
        let error = match self.connect(id, token, site).await {
            Err(error) => error,
            Ok((_sender, mut stream)) => stream.message().await.unwrap_err(),
        };
        assert_eq!(error.code(), expected);
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.shutdown.take();
        self.task.abort();
    }
}

async fn wait_offline(sessions: &SessionRegistry) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while sessions.count().await != 0 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("断流后必须及时清理会话");
}

fn changed(record: &AgentRecord, enabled: bool) -> AgentUpdate {
    AgentUpdate {
        name: record.name.clone(),
        site_id: record.site_id.clone(),
        notes: record.notes.clone(),
        enabled,
    }
}

// 接入必须验证唯一凭据以及登记的现场身份；未认证请求不能占用会话。
#[tokio::test]
async fn credentials_and_site_binding_are_required() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    server
        .denied(&issued.agent.id, None, "site-1", Code::Unauthenticated)
        .await;
    server
        .denied(
            &issued.agent.id,
            Some("wrong-token"),
            "site-1",
            Code::Unauthenticated,
        )
        .await;
    server
        .denied(
            &issued.agent.id,
            Some(&issued.token),
            "different-site",
            Code::Unauthenticated,
        )
        .await;
    server
        .denied(
            "update-probe-no-bypass",
            Some(&issued.token),
            "site-1",
            Code::Unauthenticated,
        )
        .await;
    assert_eq!(server.sessions.count().await, 0);
}

// 有效身份获得 Welcome 和心跳确认，客户端 Goodbye 后清理会话。
#[tokio::test]
async fn authenticated_connection_heartbeat_and_goodbye() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (sender, mut stream) = server.online(&issued).await;
    let session = server.sessions.get(&issued.agent.id).await.unwrap();
    assert_eq!(session.site_id, "site-1");
    assert_eq!(session.agent_version, "0.7.0");
    sender
        .send(AgentMessage {
            payload: Some(agent_message::Payload::Heartbeat(agent::Heartbeat {
                sent_at_unix_ms: 0,
            })),
        })
        .await
        .unwrap();
    assert!(matches!(
        stream.message().await.unwrap().unwrap().payload,
        Some(server_message::Payload::HeartbeatAck(_))
    ));
    sender
        .send(AgentMessage {
            payload: Some(agent_message::Payload::Goodbye(agent::Goodbye {
                reason: "结束".into(),
            })),
        })
        .await
        .unwrap();
    assert!(stream.message().await.unwrap().is_none());
    wait_offline(&server.sessions).await;
}

// 同一个身份不可同时上线，拒绝第二个连接不能清除第一个连接。
#[tokio::test]
async fn duplicate_connection_preserves_first_session() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (sender, stream) = server.online(&issued).await;
    let before = server.identities.get(&issued.agent.id).await.unwrap();
    assert!(before.last_seen_at.is_some());
    assert_eq!(before.agent_version, "0.7.0");
    tokio::time::sleep(Duration::from_millis(5)).await;
    let (_duplicate_sender, mut duplicate_stream) = server
        .connect_version(
            &issued.agent.id,
            Some(&issued.token),
            "site-1",
            "9.9.9-rejected",
        )
        .await
        .unwrap();
    assert_eq!(
        duplicate_stream.message().await.unwrap_err().code(),
        Code::AlreadyExists
    );
    let after = server.identities.get(&issued.agent.id).await.unwrap();
    assert_eq!(after.agent_version, before.agent_version);
    assert_eq!(after.last_seen_at, before.last_seen_at);
    assert_eq!(server.sessions.count().await, 1);
    drop(stream);
    drop(sender);
    wait_offline(&server.sessions).await;
}

// 上传流关闭与完整双向 RPC 取消均必须清理在线记录。
#[tokio::test]
async fn request_eof_and_response_cancellation_cleanup() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (sender, mut stream) = server.online(&issued).await;
    drop(sender);
    assert!(stream.message().await.unwrap().is_none());
    wait_offline(&server.sessions).await;
    let (sender, stream) = server.online(&issued).await;
    drop(stream);
    drop(sender);
    wait_offline(&server.sessions).await;
}

// 管理端断开后旧连接不能继续，立即重新连接仍有独立租约。
#[tokio::test]
async fn administrator_disconnect_removes_session() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (_sender, mut stream) = server.online(&issued).await;
    server.sessions.disconnect(&issued.agent.id).await;
    let error = tokio::time::timeout(Duration::from_secs(3), stream.message())
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(error.code(), Code::PermissionDenied);
    wait_offline(&server.sessions).await;
    let (_sender2, _stream2) = server.online(&issued).await;
}

// 禁用既拒绝新连接，也在无心跳时主动终止现有连接。
#[tokio::test]
async fn disabled_identity_is_disconnected_and_cannot_reconnect() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (_sender, mut stream) = server.online(&issued).await;
    server
        .identities
        .update(
            &issued.agent.id,
            issued.agent.revision,
            changed(&issued.agent, false),
            "admin",
        )
        .await
        .unwrap();
    let error = tokio::time::timeout(Duration::from_secs(3), stream.message())
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(error.code(), Code::PermissionDenied);
    wait_offline(&server.sessions).await;
    server
        .denied(
            &issued.agent.id,
            Some(&issued.token),
            "site-1",
            Code::Unauthenticated,
        )
        .await;
}

// 轮换撤销旧凭据和旧会话，新凭据可连接；撤销后也不能再连接。
#[tokio::test]
async fn rotation_and_revocation_invalidate_live_sessions() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (_sender, mut stream) = server.online(&issued).await;
    let rotated = server
        .identities
        .rotate(&issued.agent.id, issued.agent.revision, "admin")
        .await
        .unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(3), stream.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    wait_offline(&server.sessions).await;
    server
        .denied(
            &issued.agent.id,
            Some(&issued.token),
            "site-1",
            Code::Unauthenticated,
        )
        .await;
    let (_sender2, mut stream2) = server.online(&rotated).await;
    server
        .identities
        .revoke(&rotated.agent.id, rotated.agent.revision, "admin")
        .await
        .unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(3), stream2.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    wait_offline(&server.sessions).await;
    server
        .denied(
            &rotated.agent.id,
            Some(&rotated.token),
            "site-1",
            Code::Unauthenticated,
        )
        .await;
}

// 健康检查无需上位机凭据，验证协议但不产生在线记录。
#[tokio::test]
async fn health_probe_is_read_only_and_checks_protocol() {
    let server = TestServer::start().await;
    probe(&server.address, "1").await.unwrap();
    assert!(probe(&server.address, "different").await.is_err());
    assert_eq!(server.sessions.count().await, 0);
}

// 重复认证头不能产生歧义，第一条消息必须为 Hello。
#[tokio::test]
async fn ambiguous_credentials_and_invalid_handshakes_are_rejected() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (_sender, receiver) = mpsc::channel(1);
    let mut request = Request::new(ReceiverStream::new(receiver));
    request.metadata_mut().append(
        "authorization",
        format!("Bearer {}", issued.token).parse().unwrap(),
    );
    request
        .metadata_mut()
        .append("authorization", "Bearer wrong".parse().unwrap());
    assert_eq!(
        server
            .client
            .connect_agent(request)
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    let (sender, receiver) = mpsc::channel(1);
    sender
        .send(AgentMessage {
            payload: Some(agent_message::Payload::Heartbeat(agent::Heartbeat {
                sent_at_unix_ms: 0,
            })),
        })
        .await
        .unwrap();
    let mut request = Request::new(ReceiverStream::new(receiver));
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {}", issued.token).parse().unwrap(),
    );
    let mut stream = server
        .client
        .connect_agent(request)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        stream.message().await.unwrap_err().code(),
        Code::InvalidArgument
    );
    assert_eq!(server.sessions.count().await, 0);
}

// 现场变更使现有连接失效，并要求下次 Hello 携带新的现场编号。
#[tokio::test]
async fn changed_site_invalidates_previous_connection() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let (_sender, mut stream) = server.online(&issued).await;
    let mut update = changed(&issued.agent, true);
    update.site_id = "site-2".into();
    server
        .identities
        .update(&issued.agent.id, issued.agent.revision, update, "admin")
        .await
        .unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(3), stream.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    wait_offline(&server.sessions).await;
    server
        .denied(
            &issued.agent.id,
            Some(&issued.token),
            "site-1",
            Code::Unauthenticated,
        )
        .await;
    let (_sender, mut stream) = server
        .connect(&issued.agent.id, Some(&issued.token), "site-2")
        .await
        .unwrap();
    assert!(matches!(
        stream.message().await.unwrap().unwrap().payload,
        Some(server_message::Payload::Welcome(_))
    ));
}

// 传输层连接突然中断时，即使应用仍持有发送端也必须清理服务端租约。
#[tokio::test]
async fn interrupted_transport_cleans_up_live_session() {
    let mut server = TestServer::start().await;
    let issued = server.register().await;
    let proxy = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy_address = proxy.local_addr().unwrap();
    let address = server.address.clone();
    let relay = tokio::spawn(async move {
        let (mut downstream, _) = proxy.accept().await.unwrap();
        let mut upstream = tokio::net::TcpStream::connect(address).await.unwrap();
        let _ = tokio::io::copy_bidirectional(&mut downstream, &mut upstream).await;
    });
    server.client = AgentServiceClient::connect(format!("http://{proxy_address}"))
        .await
        .unwrap();
    let (_sender, mut stream) = server.online(&issued).await;
    relay.abort();
    let _ = relay.await;
    wait_offline(&server.sessions).await;
    assert!(
        tokio::time::timeout(Duration::from_secs(3), stream.message())
            .await
            .unwrap()
            .is_err()
    );
}
