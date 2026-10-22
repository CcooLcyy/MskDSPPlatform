//! 真实环回 gRPC 服务替换：保持登录令牌、重新握手，并验证退出后不能复活。
use super::{AgentGrpcService, AgentServiceServer, agent};
use crate::{
    accounts::{AccountStore, NewAccount},
    client_auth::ClientAuth,
    state::SessionRegistry,
};
use agent::agent_service_client::AgentServiceClient;
use std::{net::SocketAddr, path::Path, time::Duration};
use tokio::{
    net::TcpListener,
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};
use tonic::{
    Code, Request, Streaming,
    transport::{Channel, Server},
};

struct RestartServer {
    address: SocketAddr,
    shutdown: Option<oneshot::Sender<()>>,
    task: JoinHandle<()>,
}

impl RestartServer {
    async fn start(database: &Path, address: SocketAddr, lifetime: u64) -> Self {
        // 每次启动重新打开同一数据库，不复用 ClientAuth 或 SessionRegistry。
        let accounts = AccountStore::open(database.to_path_buf()).await.unwrap();
        let service = AgentGrpcService::new(
            SessionRegistry::default(),
            "1".into(),
            ClientAuth::new(accounts, lifetime),
        );
        let listener = TcpListener::bind(address).await.unwrap();
        let address = listener.local_addr().unwrap();
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
        Self {
            address,
            shutdown: Some(shutdown),
            task,
        }
    }

    async fn stop(mut self) {
        self.shutdown.take().unwrap().send(()).unwrap();
        match tokio::time::timeout(Duration::from_secs(3), &mut self.task).await {
            Ok(result) => result.unwrap(),
            Err(_) => {
                self.task.abort();
                let _ = (&mut self.task).await;
            }
        }
    }
}

impl Drop for RestartServer {
    fn drop(&mut self) {
        self.shutdown.take();
        self.task.abort();
    }
}

fn authorized<T>(body: T, token: &str) -> Request<T> {
    let mut request = Request::new(body);
    request
        .metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    request
}

async fn connect(
    client: &mut AgentServiceClient<Channel>,
    token: &str,
) -> Result<
    (
        mpsc::Sender<agent::AgentMessage>,
        Streaming<agent::ServerMessage>,
    ),
    tonic::Status,
> {
    let (sender, receiver) = mpsc::channel(4);
    sender
        .send(agent::AgentMessage {
            payload: Some(agent::agent_message::Payload::Hello(agent::Hello {
                agent_id: "same-installation-after-update".into(),
                protocol_version: "1".into(),
                agent_version: "test".into(),
                ..Default::default()
            })),
        })
        .await
        .unwrap();
    let stream = client
        .connect_agent(authorized(ReceiverStream::new(receiver), token))
        .await?
        .into_inner();
    Ok((sender, stream))
}

async fn welcome(client: &mut AgentServiceClient<Channel>, token: &str) -> String {
    let (sender, mut stream) = connect(client, token).await.unwrap();
    let message = tokio::time::timeout(Duration::from_secs(3), stream.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let Some(agent::server_message::Payload::Welcome(welcome)) = message.payload else {
        panic!("重新连接必须收到 Welcome");
    };
    assert_eq!(welcome.protocol_version, "1");
    assert!(!welcome.connection_id.is_empty());
    drop(sender);
    drop(stream);
    welcome.connection_id
}

#[tokio::test]
async fn same_token_reconnects_after_grpc_service_restart_and_logout_stays_revoked() {
    let directory = tempfile::tempdir().unwrap();
    let database = directory.path().join("accounts.sqlite3");
    let accounts = AccountStore::open(database.clone()).await.unwrap();
    let account: NewAccount = serde_json::from_value(serde_json::json!({
        "username": "restart-client", "display_name": "更新会话测试", "password": "field-password-123"
    })).unwrap();
    let account = accounts.create(account).await.unwrap();
    drop(accounts);

    let server = RestartServer::start(&database, "127.0.0.1:0".parse().unwrap(), 300).await;
    let address = server.address;
    let mut client = AgentServiceClient::connect(format!("http://{address}"))
        .await
        .unwrap();
    let login = client
        .login(agent::LoginRequest {
            username: "restart-client".into(),
            password: "field-password-123".into(),
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(login.token_type, "Bearer");
    assert_eq!(login.expires_in, 300);
    assert_eq!(login.account.as_ref().unwrap().id, account.id);
    let before = client
        .get_session(authorized(agent::SessionRequest {}, &login.access_token))
        .await
        .unwrap()
        .into_inner();
    let first_connection = welcome(&mut client, &login.access_token).await;
    drop(client);
    server.stop().await;
    // 跨过整秒，避免仅检查 <= 时漏掉按原始 TTL 重新计时的实现。
    tokio::time::sleep(Duration::from_millis(1100)).await;

    // 更新后的配置允许新登录更长有效期，原登录仍必须沿用原绝对到期时间。
    let restarted = RestartServer::start(&database, address, 3600).await;
    let mut client = AgentServiceClient::connect(format!("http://{address}"))
        .await
        .unwrap();
    let after = client
        .get_session(authorized(agent::SessionRequest {}, &login.access_token))
        .await
        .unwrap()
        .into_inner();
    assert!(after.access_token.is_empty());
    assert_eq!(after.token_type, "Bearer");
    assert_eq!(after.account.as_ref().unwrap().id, account.id);
    assert!(after.expires_in > 0);
    assert!(
        after.expires_in < before.expires_in,
        "更新不得延长原会话有效期"
    );
    let next_connection = welcome(&mut client, &login.access_token).await;
    assert_ne!(first_connection, next_connection);
    client
        .logout(authorized(agent::SessionRequest {}, &login.access_token))
        .await
        .unwrap();
    drop(client);
    restarted.stop().await;

    let _restarted_again = RestartServer::start(&database, address, 3600).await;
    let mut client = AgentServiceClient::connect(format!("http://{address}"))
        .await
        .unwrap();
    assert_eq!(
        client
            .get_session(authorized(agent::SessionRequest {}, &login.access_token))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    let error = match connect(&mut client, &login.access_token).await {
        Err(error) => error,
        Ok((_sender, mut stream)) => stream.message().await.unwrap_err(),
    };
    assert_eq!(error.code(), Code::Unauthenticated);
}
