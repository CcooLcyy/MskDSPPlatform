pub mod agent {
    tonic::include_proto!("mskdsp.platform.v1");
}

use crate::{
    error::PlatformError,
    identities::{IdentityError, IdentityStore},
    state::{AgentSession, SessionRegistry},
};
use agent::agent_service_server::AgentService;
use agent::{AgentMessage, HeartbeatAck, ServerMessage, Welcome, agent_message, server_message};
use std::{
    pin::Pin,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::mpsc;
use tokio_stream::{Stream, wrappers::ReceiverStream};
use tokio_util::sync::CancellationToken;
use tonic::{Request, Response, Status, Streaming};
use uuid::Uuid;

pub use agent::agent_service_server::AgentServiceServer;

/// 管理进程使用只读 RPC 检查 gRPC 服务，不占用上位机身份或注册临时会话。
pub async fn probe(address: &str, protocol_version: &str) -> anyhow::Result<()> {
    let check = async {
        let mut client =
            agent::agent_service_client::AgentServiceClient::connect(format!("http://{address}"))
                .await?;
        let response = client
            .check_health(agent::HealthRequest {})
            .await?
            .into_inner();
        if response.protocol_version != protocol_version {
            anyhow::bail!("gRPC 健康检查响应协议不匹配");
        }
        Ok(())
    };
    tokio::time::timeout(Duration::from_secs(5), check)
        .await
        .map_err(|_| anyhow::anyhow!("gRPC 健康检查超时"))?
}

#[derive(Clone)]
pub struct AgentGrpcService {
    sessions: SessionRegistry,
    protocol_version: Arc<str>,
    identities: IdentityStore,
}

impl AgentGrpcService {
    pub fn new(
        sessions: SessionRegistry,
        protocol_version: Arc<str>,
        identities: IdentityStore,
    ) -> Self {
        Self {
            sessions,
            protocol_version,
            identities,
        }
    }
}

type ResponseStream = Pin<Box<dyn Stream<Item = Result<ServerMessage, Status>> + Send>>;

#[tonic::async_trait]
impl AgentService for AgentGrpcService {
    type ConnectAgentStream = ResponseStream;

    async fn check_health(
        &self,
        _request: Request<agent::HealthRequest>,
    ) -> Result<Response<agent::HealthResponse>, Status> {
        Ok(Response::new(agent::HealthResponse {
            protocol_version: self.protocol_version.to_string(),
        }))
    }

    async fn connect_agent(
        &self,
        request: Request<Streaming<AgentMessage>>,
    ) -> Result<Response<Self::ConnectAgentStream>, Status> {
        let mut values = request.metadata().get_all("authorization").iter();
        let value = values
            .next()
            .ok_or_else(|| Status::unauthenticated("缺少上位机接入凭据"))?;
        if values.next().is_some() {
            return Err(Status::unauthenticated("接入凭据只能提供一次"));
        }
        let token = value
            .to_str()
            .ok()
            .and_then(|value| value.strip_prefix("Bearer "))
            .filter(|token| {
                !token.is_empty() && token.len() <= 512 && !token.chars().any(char::is_whitespace)
            })
            .ok_or_else(|| Status::unauthenticated("上位机接入凭据格式错误"))?
            .to_owned();
        let mut inbound = request.into_inner();
        let (outbound_tx, outbound_rx) = mpsc::channel(16);
        let service = self.clone();
        tokio::spawn(async move {
            if let Err(error) = service
                .handle_agent_stream(&mut inbound, &outbound_tx, &token)
                .await
            {
                tracing::warn!(code = ?error.code(), "上位机连接被终止");
                let _ = outbound_tx.try_send(Err(error));
            }
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(outbound_rx))))
    }
}

impl AgentGrpcService {
    async fn handle_agent_stream(
        &self,
        inbound: &mut Streaming<AgentMessage>,
        outbound: &mpsc::Sender<Result<ServerMessage, Status>>,
        token: &str,
    ) -> Result<(), Status> {
        let first = tokio::select! {
            _ = outbound.closed() => return Ok(()),
            first = tokio::time::timeout(Duration::from_secs(10), inbound.message()) => {
                first.map_err(|_| Status::deadline_exceeded("上位机 Hello 握手超时"))??
                    .ok_or_else(|| Status::invalid_argument("上位机必须先发送 Hello"))?
            }
        };
        let hello = match first.payload {
            Some(agent_message::Payload::Hello(hello)) => hello,
            _ => return Err(Status::invalid_argument("上位机第一条消息必须是 Hello")),
        };
        if hello.agent_id.trim().is_empty() {
            return Err(Status::invalid_argument(
                PlatformError::EmptyAgentId.to_string(),
            ));
        }
        if hello.protocol_version != self.protocol_version.as_ref() {
            return Err(Status::failed_precondition(
                PlatformError::UnsupportedProtocol(hello.protocol_version).to_string(),
            ));
        }
        if hello.agent_id.len() > 128
            || hello.site_id.len() > 128
            || hello.agent_version.len() > 128
        {
            return Err(Status::invalid_argument("上位机身份、现场或版本信息过长"));
        }
        let identity = self
            .identities
            .authenticate(&hello.agent_id, token, &hello.site_id, &hello.agent_version)
            .await
            .map_err(|error| identity_status(error, false))?;
        let connection_id = Uuid::new_v4().to_string();
        let session = AgentSession {
            connection_id: connection_id.clone(),
            agent_id: identity.id.clone(),
            site_id: identity.site_id,
            agent_version: hello.agent_version,
        };
        let cancellation = self
            .sessions
            .register(session.clone())
            .await
            .map_err(|_| Status::already_exists("该上位机已经在线"))?;
        tracing::info!(agent_id = %identity.id, %connection_id, "上位机已通过接入认证");
        // 注册后的所有退出路径均经此处清理，旧连接不能删除后来注册的新连接。
        let result = self
            .run_session(
                inbound,
                outbound,
                &session,
                identity.credential_version,
                &cancellation,
            )
            .await;
        self.sessions.remove(&identity.id, &connection_id).await;
        tracing::info!(agent_id = %identity.id, %connection_id, "上位机会话已清理");
        result
    }

    async fn run_session(
        &self,
        inbound: &mut Streaming<AgentMessage>,
        outbound: &mpsc::Sender<Result<ServerMessage, Status>>,
        session: &AgentSession,
        credential_version: i64,
        cancellation: &CancellationToken,
    ) -> Result<(), Status> {
        let agent_id = session.agent_id.as_str();
        self.identities
            .connected(agent_id, credential_version, &session.agent_version)
            .await
            .map_err(|error| identity_status(error, true))?;
        send_message(
            outbound,
            ServerMessage {
                payload: Some(server_message::Payload::Welcome(Welcome {
                    connection_id: session.connection_id.clone(),
                    protocol_version: self.protocol_version.to_string(),
                    server_time_unix_ms: now_unix_ms(),
                })),
            },
            cancellation,
        )
        .await?;
        let heartbeat_timeout = tokio::time::sleep(Duration::from_secs(60));
        tokio::pin!(heartbeat_timeout);
        let mut authorization_check = tokio::time::interval(Duration::from_secs(1));
        authorization_check.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                _ = cancellation.cancelled() => return Err(Status::permission_denied("上位机会话已被管理端断开")),
                _ = outbound.closed() => return Ok(()),
                _ = &mut heartbeat_timeout => return Err(Status::deadline_exceeded("上位机心跳超时")),
                _ = authorization_check.tick() => {
                    self.identities.check_session(agent_id, credential_version).await
                        .map_err(|error| identity_status(error, true))?;
                }
                message = inbound.message() => {
                    let Some(message) = message? else { return Ok(()) };
                    match message.payload {
                        Some(agent_message::Payload::Heartbeat(_)) => {
                            self.identities.heartbeat(agent_id, credential_version).await
                                .map_err(|error| identity_status(error, true))?;
                            heartbeat_timeout.as_mut().reset(tokio::time::Instant::now() + Duration::from_secs(60));
                            send_message(outbound, ServerMessage {
                                payload: Some(server_message::Payload::HeartbeatAck(HeartbeatAck { server_time_unix_ms: now_unix_ms() })),
                            }, cancellation).await?;
                        }
                        Some(agent_message::Payload::Goodbye(_)) => return Ok(()),
                        Some(agent_message::Payload::Hello(_)) => return Err(Status::invalid_argument("连接建立后不允许重复发送 Hello")),
                        None => return Err(Status::invalid_argument("上位机消息不能为空")),
                    }
                }
            }
        }
    }
}

fn identity_status(error: IdentityError, authenticated: bool) -> Status {
    match error {
        IdentityError::Storage => Status::unavailable("上位机身份存储暂不可用"),
        IdentityError::Invalid(message) => Status::invalid_argument(message),
        _ if authenticated => Status::permission_denied("上位机会话授权已失效"),
        _ => Status::unauthenticated("上位机身份或接入凭据无效"),
    }
}

async fn send_message(
    outbound: &mpsc::Sender<Result<ServerMessage, Status>>,
    message: ServerMessage,
    cancellation: &CancellationToken,
) -> Result<(), Status> {
    tokio::select! {
        biased;
        _ = cancellation.cancelled() => Err(Status::permission_denied("上位机会话已被管理端断开")),
        result = tokio::time::timeout(Duration::from_secs(10), outbound.send(Ok(message))) => {
            result.map_err(|_| Status::deadline_exceeded("上位机响应流阻塞超时"))?
                .map_err(|_| Status::unavailable("上位机响应流已关闭"))
        }
    }
}

fn now_unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or_default()
}

#[cfg(test)]
#[path = "grpc_identity_tests.rs"]
mod tests;
