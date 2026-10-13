pub mod agent {
    tonic::include_proto!("mskdsp.platform.v1");
}

use crate::{
    error::PlatformError,
    state::{AgentSession, SessionRegistry},
};
use agent::agent_service_server::AgentService;
use agent::{
    AgentMessage, HeartbeatAck, ProtocolError, ServerMessage, Welcome, agent_message,
    server_message,
};
use std::{
    pin::Pin,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::mpsc;
use tokio_stream::{Stream, StreamExt, wrappers::ReceiverStream};
use tonic::{Request, Response, Status, Streaming};
use uuid::Uuid;

pub use agent::agent_service_server::AgentServiceServer;

/// 管理进程通过真实双向流握手检查 gRPC 服务，检查后主动清理临时会话。
pub async fn probe(address: &str, protocol_version: &str) -> anyhow::Result<()> {
    let check = async {
        let mut client =
            agent::agent_service_client::AgentServiceClient::connect(format!("http://{address}"))
                .await?;
        let (sender, receiver) = mpsc::channel(2);
        sender
            .send(AgentMessage {
                payload: Some(agent_message::Payload::Hello(agent::Hello {
                    agent_id: format!("update-probe-{}", Uuid::new_v4()),
                    site_id: String::new(),
                    agent_version: crate::build_info::BuildInfo::current().version.into(),
                    protocol_version: protocol_version.into(),
                    capabilities: Vec::new(),
                })),
            })
            .await?;
        let mut response = client
            .connect_agent(ReceiverStream::new(receiver))
            .await?
            .into_inner();
        let first = response
            .message()
            .await?
            .ok_or_else(|| anyhow::anyhow!("gRPC 握手未返回 Welcome"))?;
        match first.payload {
            Some(server_message::Payload::Welcome(welcome))
                if welcome.protocol_version == protocol_version => {}
            _ => anyhow::bail!("gRPC 握手响应协议不匹配"),
        }
        sender
            .send(AgentMessage {
                payload: Some(agent_message::Payload::Goodbye(agent::Goodbye {
                    reason: "更新健康检查完成".into(),
                })),
            })
            .await?;
        drop(sender);
        if response.message().await?.is_some() {
            anyhow::bail!("gRPC 检查会话未正常关闭");
        }
        Ok(())
    };
    tokio::time::timeout(std::time::Duration::from_secs(5), check)
        .await
        .map_err(|_| anyhow::anyhow!("gRPC 握手检查超时"))?
}

#[derive(Clone)]
pub struct AgentGrpcService {
    sessions: SessionRegistry,
    protocol_version: Arc<str>,
}

impl AgentGrpcService {
    pub fn new(sessions: SessionRegistry, protocol_version: Arc<str>) -> Self {
        Self {
            sessions,
            protocol_version,
        }
    }
}

type ResponseStream = Pin<Box<dyn Stream<Item = Result<ServerMessage, Status>> + Send>>;

#[tonic::async_trait]
impl AgentService for AgentGrpcService {
    type ConnectAgentStream = ResponseStream;

    async fn connect_agent(
        &self,
        request: Request<Streaming<AgentMessage>>,
    ) -> Result<Response<Self::ConnectAgentStream>, Status> {
        let mut inbound = request.into_inner();
        let (outbound_tx, outbound_rx) = mpsc::channel(16);
        let sessions = self.sessions.clone();
        let protocol_version = self.protocol_version.clone();

        tokio::spawn(async move {
            if let Err(error) = handle_agent_stream(
                &mut inbound,
                outbound_tx.clone(),
                sessions,
                protocol_version,
            )
            .await
            {
                let _ = outbound_tx.send(Err(error)).await;
            }
        });

        Ok(Response::new(Box::pin(ReceiverStream::new(outbound_rx))))
    }
}

async fn handle_agent_stream(
    inbound: &mut Streaming<AgentMessage>,
    outbound: mpsc::Sender<Result<ServerMessage, Status>>,
    sessions: SessionRegistry,
    protocol_version: Arc<str>,
) -> Result<(), Status> {
    let first = inbound
        .next()
        .await
        .ok_or_else(|| Status::invalid_argument("Agent 必须先发送 Hello"))??;
    let hello = match first.payload {
        Some(agent_message::Payload::Hello(hello)) => hello,
        _ => return Err(Status::invalid_argument("Agent 第一条消息必须是 Hello")),
    };

    if hello.agent_id.trim().is_empty() {
        return Err(Status::invalid_argument(
            PlatformError::EmptyAgentId.to_string(),
        ));
    }
    if hello.protocol_version != protocol_version.as_ref() {
        return Err(Status::failed_precondition(
            PlatformError::UnsupportedProtocol(hello.protocol_version).to_string(),
        ));
    }

    let connection_id = Uuid::new_v4().to_string();
    sessions
        .register(AgentSession {
            connection_id: connection_id.clone(),
            agent_id: hello.agent_id.clone(),
            site_id: hello.site_id,
            agent_version: hello.agent_version,
        })
        .await;

    outbound
        .send(Ok(ServerMessage {
            payload: Some(server_message::Payload::Welcome(Welcome {
                connection_id: connection_id.clone(),
                protocol_version: protocol_version.to_string(),
                server_time_unix_ms: now_unix_ms(),
            })),
        }))
        .await
        .map_err(|_| Status::unavailable("Agent 响应流已关闭"))?;

    while let Some(message) = inbound.next().await {
        let message = message?;
        match message.payload {
            Some(agent_message::Payload::Heartbeat(_)) => {
                outbound
                    .send(Ok(ServerMessage {
                        payload: Some(server_message::Payload::HeartbeatAck(HeartbeatAck {
                            server_time_unix_ms: now_unix_ms(),
                        })),
                    }))
                    .await
                    .map_err(|_| Status::unavailable("Agent 响应流已关闭"))?;
            }
            Some(agent_message::Payload::Goodbye(_)) => break,
            Some(agent_message::Payload::Hello(_)) | None => {
                outbound
                    .send(Ok(ServerMessage {
                        payload: Some(server_message::Payload::Error(ProtocolError {
                            code: "INVALID_MESSAGE".into(),
                            message: "连接建立后不允许重复发送 Hello".into(),
                            retryable: false,
                        })),
                    }))
                    .await
                    .map_err(|_| Status::unavailable("Agent 响应流已关闭"))?;
            }
        }
    }

    sessions.remove(&hello.agent_id, &connection_id).await;
    Ok(())
}

fn now_unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or_default()
}
