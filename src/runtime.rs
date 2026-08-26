use crate::{
    config::AppConfig,
    grpc::{AgentGrpcService, AgentServiceServer},
    http::{self, HttpState},
    state::SessionRegistry,
};
use anyhow::{Context, Result};
use std::{sync::Arc, time::Duration};
use tokio::{net::TcpListener, time::timeout};
use tonic::transport::Server;
use tracing::info;

pub struct PlatformRuntime {
    config: AppConfig,
    sessions: SessionRegistry,
}

impl PlatformRuntime {
    pub async fn new(config: AppConfig) -> Result<Self> {
        if config.tls.enabled {
            anyhow::bail!("当前基础版本尚未启用 TLS 监听，请先关闭 tls.enabled");
        }
        Ok(Self {
            config,
            sessions: SessionRegistry::default(),
        })
    }

    pub async fn run(self) -> Result<()> {
        let protocol_version: Arc<str> = self.config.runtime.protocol_version.clone().into();
        let http_state = HttpState {
            sessions: self.sessions.clone(),
            protocol_version: protocol_version.clone(),
        };
        let http_listener = TcpListener::bind(self.config.server.http_addr)
            .await
            .with_context(|| format!("绑定 HTTP 地址失败: {}", self.config.server.http_addr))?;
        let grpc_addr = self.config.server.grpc_addr;
        let grpc_service = AgentGrpcService::new(self.sessions.clone(), protocol_version);

        info!(address = %http_listener.local_addr()?, "HTTP 服务监听完成");
        info!(address = %grpc_addr, "gRPC 服务准备监听");

        let http_task = tokio::spawn(async move {
            axum::serve(http_listener, http::router(http_state))
                .with_graceful_shutdown(shutdown_signal())
                .await
                .context("HTTP 服务异常退出")
        });
        let grpc_task = tokio::spawn(async move {
            Server::builder()
                .add_service(AgentServiceServer::new(grpc_service))
                .serve_with_shutdown(grpc_addr, shutdown_signal())
                .await
                .context("gRPC 服务异常退出")
        });

        tokio::select! {
            result = http_task => result??,
            result = grpc_task => result??,
        }

        let timeout_duration = Duration::from_secs(self.config.server.shutdown_timeout_seconds);
        timeout(timeout_duration, async {}).await.ok();
        info!("MskDSPPlatform 基础服务已停止");
        Ok(())
    }
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("注册 Ctrl+C 信号处理失败");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("注册 SIGTERM 信号处理失败")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => info!("收到 Ctrl+C，开始停止服务"),
        _ = terminate => info!("收到 SIGTERM，开始停止服务"),
    }
}
