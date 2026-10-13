use crate::{
    config::AppConfig,
    grpc::{AgentGrpcService, AgentServiceServer},
    http::{self, HttpState},
    state::SessionRegistry,
};
use anyhow::{Context, Result};
use std::{future::Future, sync::Arc, time::Duration};
use tokio::{net::TcpListener, sync::watch, time::timeout};
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

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let http_shutdown = shutdown_rx.clone();
        let http_service = async move {
            axum::serve(http_listener, http::router(http_state))
                .with_graceful_shutdown(wait_for_shutdown(http_shutdown))
                .await
                .context("HTTP 服务异常退出")
        };
        let grpc_service = async move {
            Server::builder()
                .add_service(AgentServiceServer::new(grpc_service))
                .serve_with_shutdown(grpc_addr, wait_for_shutdown(shutdown_rx))
                .await
                .context("gRPC 服务异常退出")
        };
        run_services(
            http_service,
            grpc_service,
            shutdown_signal(),
            shutdown_tx,
            Duration::from_secs(self.config.server.shutdown_timeout_seconds),
        )
        .await?;
        info!("MskDSPPlatform 基础服务已停止");
        Ok(())
    }
}

async fn wait_for_shutdown(mut receiver: watch::Receiver<bool>) {
    while !*receiver.borrow_and_update() {
        if receiver.changed().await.is_err() {
            break;
        }
    }
}

async fn run_services<H, G, S>(
    http: H,
    grpc: G,
    signal: S,
    shutdown: watch::Sender<bool>,
    duration: Duration,
) -> Result<()>
where
    H: Future<Output = Result<()>>,
    G: Future<Output = Result<()>>,
    S: Future<Output = ()>,
{
    // 两个服务都结束才算完成；任一服务报错则立即退出并释放另一个服务。
    let services = async {
        tokio::try_join!(http, grpc)?;
        Ok(())
    };
    tokio::pin!(services);
    tokio::select! {
        result = &mut services => result,
        _ = signal => {
            let _ = shutdown.send(true);
            timeout(duration, &mut services).await.context("停止服务超时，终止未完成连接")?
        }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use tokio::sync::{Notify, oneshot, watch};

    // 收到退出通知后必须等待 HTTP 与 gRPC 两个服务完成，不能只等待其中之一。
    #[tokio::test]
    async fn shutdown_waits_for_both_services() {
        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let (signal_tx, signal_rx) = oneshot::channel();
        let (grpc_done_tx, grpc_done_rx) = oneshot::channel();
        let http_done = Arc::new(AtomicBool::new(false));
        let http_notify = Arc::new(Notify::new());
        let notify = http_notify.clone();
        let done = http_done.clone();
        let http = async move {
            wait_for_shutdown(shutdown_rx).await;
            done.store(true, Ordering::SeqCst);
            notify.notify_one();
            Ok(())
        };
        let grpc = async move {
            let _ = grpc_done_rx.await;
            Ok(())
        };
        let task = tokio::spawn(run_services(
            http,
            grpc,
            async move {
                let _ = signal_rx.await;
            },
            shutdown_tx,
            Duration::from_secs(2),
        ));
        signal_tx.send(()).unwrap();
        timeout(Duration::from_secs(1), http_notify.notified())
            .await
            .unwrap();
        assert!(http_done.load(Ordering::SeqCst));
        assert!(!task.is_finished());
        grpc_done_tx.send(()).unwrap();
        task.await.unwrap().unwrap();
    }

    // 服务无法排空时退出必须受配置超时限制。
    #[tokio::test]
    async fn shutdown_timeout_is_enforced() {
        let (shutdown_tx, _) = watch::channel(false);
        let result = run_services(
            std::future::pending::<Result<()>>(),
            std::future::pending::<Result<()>>(),
            async {},
            shutdown_tx,
            Duration::ZERO,
        )
        .await;
        assert!(result.unwrap_err().to_string().contains("停止服务超时"));
    }
}
