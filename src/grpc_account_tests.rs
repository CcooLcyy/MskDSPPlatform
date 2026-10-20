use super::*;
use crate::accounts::{AccountRecord, AccountStore, AccountUpdate, NewAccount};
use crate::client_auth::LoginResponse;
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
    accounts: AccountStore,
    auth: ClientAuth,
    sessions: SessionRegistry,
    address: String,
    client: AgentServiceClient<Channel>,
    shutdown: Option<oneshot::Sender<()>>,
    task: JoinHandle<()>,
}

impl TestServer {
    async fn start() -> Self {
        Self::with_expiry(28_800).await
    }

    async fn with_expiry(seconds: u64) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let accounts = AccountStore::open(directory.path().join("accounts.sqlite3"))
            .await
            .unwrap();
        let auth = ClientAuth::new(accounts.clone(), seconds);
        let sessions = SessionRegistry::default();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let service = AgentGrpcService::new(sessions.clone(), "1".into(), auth.clone());
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
            accounts,
            auth,
            sessions,
            address,
            client,
            shutdown: Some(shutdown),
            task,
        }
    }

    async fn register(&self, username: &str) -> LoginResponse {
        self.accounts
            .create(NewAccount {
                username: username.into(),
                display_name: "现场账号".into(),
                password: "field-password-123".into(),
            })
            .await
            .unwrap();
        self.auth
            .login(username, "field-password-123")
            .await
            .unwrap()
    }

    async fn connect(
        &mut self,
        instance: &str,
        token: Option<&str>,
        site: &str,
    ) -> Result<(mpsc::Sender<AgentMessage>, Streaming<ServerMessage>), Status> {
        let (sender, receiver) = mpsc::channel(4);
        sender
            .send(AgentMessage {
                payload: Some(agent_message::Payload::Hello(agent::Hello {
                    agent_id: instance.into(),
                    site_id: site.into(),
                    agent_version: "0.7.0".into(),
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
        login: &LoginResponse,
        instance: &str,
    ) -> (mpsc::Sender<AgentMessage>, Streaming<ServerMessage>) {
        let (sender, mut stream) = self
            .connect(instance, Some(&login.access_token), "compatibility-only")
            .await
            .unwrap();
        assert!(matches!(
            stream.message().await.unwrap().unwrap().payload,
            Some(server_message::Payload::Welcome(_))
        ));
        (sender, stream)
    }

    async fn denied(&mut self, instance: &str, token: Option<&str>, expected: Code) {
        let error = match self.connect(instance, token, "any-site").await {
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

async fn wait_count(sessions: &SessionRegistry, count: usize) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while sessions.count().await != count {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("连接退出后必须及时清理会话");
}

async fn revoked(stream: &mut Streaming<ServerMessage>) {
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(3), stream.message())
            .await
            .unwrap()
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

fn changed(account: &AccountRecord, enabled: bool) -> AccountUpdate {
    AccountUpdate {
        display_name: account.display_name.clone(),
        enabled,
    }
}

#[tokio::test]
async fn account_login_is_required_and_legacy_credentials_do_not_work() {
    let mut server = TestServer::start().await;
    server.denied("instance", None, Code::Unauthenticated).await;
    server
        .denied(
            "instance",
            Some("legacy-manual-instance-token"),
            Code::Unauthenticated,
        )
        .await;
    server
        .denied("instance", Some(&"a".repeat(64)), Code::Unauthenticated)
        .await;
    assert_eq!(server.sessions.count().await, 0);
}

#[tokio::test]
async fn same_account_multiple_instances_and_cross_account_same_instance_can_connect() {
    let mut server = TestServer::start().await;
    let first = server.register("field-one").await;
    let second = server.register("field-two").await;
    let (_sender1, _stream1) = server.online(&first, "computer-1").await;
    let (_sender2, _stream2) = server.online(&first, "computer-2").await;
    let (_sender3, _stream3) = server.online(&second, "computer-1").await;
    assert_eq!(server.sessions.count().await, 3);
    assert_eq!(
        server.sessions.count_for_account(&first.account.id).await,
        2
    );
    assert_eq!(
        server.sessions.count_for_account(&second.account.id).await,
        1
    );
    server
        .denied("computer-1", Some(&first.access_token), Code::AlreadyExists)
        .await;
    assert_eq!(server.sessions.count().await, 3);
    let (_sender4, _stream4) = server.online(&first, "").await;
    let (_sender5, _stream5) = server.online(&first, "").await;
    assert_eq!(server.sessions.count().await, 5);
}

#[tokio::test]
async fn heartbeat_goodbye_and_response_cancellation_clean_up() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
    let (sender, mut stream) = server.online(&login, "computer-1").await;
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
                reason: "退出".into(),
            })),
        })
        .await
        .unwrap();
    assert!(stream.message().await.unwrap().is_none());
    wait_count(&server.sessions, 0).await;
    let (sender, mut stream) = server.online(&login, "computer-1").await;
    drop(sender);
    assert!(stream.message().await.unwrap().is_none());
    wait_count(&server.sessions, 0).await;
    let (sender, stream) = server.online(&login, "computer-1").await;
    drop(stream);
    drop(sender);
    wait_count(&server.sessions, 0).await;
}

#[tokio::test]
async fn disabled_account_disconnects_existing_connections_and_rejects_reconnect() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
    let (_sender, mut stream) = server.online(&login, "computer-1").await;
    server
        .accounts
        .update(
            &login.account.id,
            login.account.revision,
            changed(&login.account, false),
        )
        .await
        .unwrap();
    revoked(&mut stream).await;
    wait_count(&server.sessions, 0).await;
    server
        .denied(
            "computer-1",
            Some(&login.access_token),
            Code::Unauthenticated,
        )
        .await;
    assert!(matches!(
        server.auth.login("field-one", "field-password-123").await,
        Err(ClientAuthError::Unauthorized)
    ));
}

#[tokio::test]
async fn password_reset_and_logout_revoke_existing_connections() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
    let (_sender, mut stream) = server.online(&login, "computer-1").await;
    server
        .accounts
        .reset_password(
            &login.account.id,
            login.account.revision,
            "new-password-123".into(),
        )
        .await
        .unwrap();
    revoked(&mut stream).await;
    wait_count(&server.sessions, 0).await;
    server
        .denied(
            "computer-1",
            Some(&login.access_token),
            Code::Unauthenticated,
        )
        .await;
    let fresh = server
        .auth
        .login("field-one", "new-password-123")
        .await
        .unwrap();
    let (_sender2, mut stream2) = server.online(&fresh, "computer-1").await;
    server.auth.logout(&fresh.access_token).await;
    revoked(&mut stream2).await;
    wait_count(&server.sessions, 0).await;
    server
        .denied(
            "computer-1",
            Some(&fresh.access_token),
            Code::Unauthenticated,
        )
        .await;
}

#[tokio::test]
async fn expired_login_disconnects_without_waiting_for_heartbeat() {
    let mut server = TestServer::with_expiry(1).await;
    let login = server.register("field-one").await;
    let (_sender, mut stream) = server.online(&login, "computer-1").await;
    revoked(&mut stream).await;
    wait_count(&server.sessions, 0).await;
    server
        .denied(
            "computer-1",
            Some(&login.access_token),
            Code::Unauthenticated,
        )
        .await;
}

#[tokio::test]
async fn health_probe_is_read_only_and_checks_protocol() {
    let server = TestServer::start().await;
    probe(&server.address, "1").await.unwrap();
    assert!(probe(&server.address, "different").await.is_err());
    assert_eq!(server.sessions.count().await, 0);
}

#[tokio::test]
async fn ambiguous_bearer_headers_and_invalid_handshakes_are_rejected() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
    for credentials in [
        vec![
            format!("Bearer {}", login.access_token),
            "Bearer wrong".into(),
        ],
        vec![format!("bearer {}", login.access_token)],
        vec![format!("Bearer {} extra", login.access_token)],
    ] {
        let (_sender, receiver) = mpsc::channel(1);
        let mut request = Request::new(ReceiverStream::new(receiver));
        for value in credentials {
            request
                .metadata_mut()
                .append("authorization", value.parse().unwrap());
        }
        assert_eq!(
            server
                .client
                .connect_agent(request)
                .await
                .unwrap_err()
                .code(),
            Code::Unauthenticated
        );
    }
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
        format!("Bearer {}", login.access_token).parse().unwrap(),
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

#[tokio::test]
async fn interrupted_transport_cleans_up_live_session() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
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
    let (_sender, mut stream) = server.online(&login, "computer-1").await;
    relay.abort();
    let _ = relay.await;
    wait_count(&server.sessions, 0).await;
    assert!(
        tokio::time::timeout(Duration::from_secs(3), stream.message())
            .await
            .unwrap()
            .is_err()
    );
}

#[tokio::test]
async fn admin_disconnect_and_stale_cleanup_preserve_new_connection() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
    let (_sender, mut stream) = server.online(&login, "computer-1").await;
    let key = format!("{}:computer-1", login.account.id);
    let before = server.sessions.get(&key).await.unwrap();
    server.sessions.disconnect(&key).await;
    let (_sender2, _stream2) = server.online(&login, "computer-1").await;
    server.sessions.remove(&key, &before.connection_id).await;
    assert_eq!(
        server.sessions.count_for_account(&login.account.id).await,
        1
    );
    revoked(&mut stream).await;
}

#[tokio::test]
async fn account_disconnect_cancels_all_its_connections_and_preserves_other_accounts() {
    let mut server = TestServer::start().await;
    let login = server.register("field-one").await;
    let other = server.register("field-two").await;
    let (_sender1, mut stream1) = server.online(&login, "computer-1").await;
    let (_sender2, mut stream2) = server.online(&login, "computer-2").await;
    let (_sender3, _stream3) = server.online(&other, "computer-1").await;
    server.sessions.disconnect_account(&login.account.id).await;
    revoked(&mut stream1).await;
    revoked(&mut stream2).await;
    assert_eq!(server.sessions.count().await, 1);
    assert_eq!(
        server.sessions.count_for_account(&other.account.id).await,
        1
    );
}
