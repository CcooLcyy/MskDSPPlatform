use crate::{
    auth::AdminAuth,
    config::AppConfig,
    grpc::{AgentGrpcService, AgentServiceServer},
    http::{self, HttpState},
    state::SessionRegistry,
};
use anyhow::{Context, Result};
use std::{
    ffi::OsString,
    future::Future,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{net::TcpListener, sync::watch, time::timeout};
use tonic::transport::Server;
use tracing::{info, warn};

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
            auth: AdminAuth::new(self.config.auth.clone())?,
        };
        let working_directory = std::env::current_dir().context("读取平台工作目录失败")?;
        let data_root = resolve_data_root(
            std::env::var_os("MSKDSP_PLATFORM_DATA_DIR"),
            std::env::var_os("MSKDSP_RUNTIME_DIR"),
            &working_directory,
        );
        let templates_directory = data_root.join("modbus-templates");
        info!(
            data_directory = %data_root.display(),
            templates_directory = %templates_directory.display(),
            "平台数据存储目录已确定"
        );
        let templates = crate::modbus_templates::TemplateStore::new(templates_directory.clone())
            .with_context(|| format!("初始化点表存储失败：{}", templates_directory.display()))?;
        templates
            .initialize()
            .await
            .with_context(|| format!("初始化点表数据库失败：{}", templates_directory.display()))?;
        let http_router = http::router(http_state.clone())
            .merge(crate::template_http::router(http_state, templates.clone()));
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
            axum::serve(http_listener, http_router)
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
        let mut maintenance = tokio::task::JoinSet::new();
        let cleanup_shutdown = shutdown_tx.subscribe();
        maintenance.spawn(run_trash_maintenance(
            move || {
                let templates = templates.clone();
                async move { templates.purge_expired().await }
            },
            Duration::from_secs(3600),
            cleanup_shutdown,
        ));
        let result = run_services(
            http_service,
            grpc_service,
            shutdown_signal(),
            shutdown_tx,
            Duration::from_secs(self.config.server.shutdown_timeout_seconds),
        )
        .await;
        // 无论服务正常结束、失败还是任务被取消，后台清理都随所属运行时结束。
        maintenance.abort_all();
        while maintenance.join_next().await.is_some() {}
        result?;
        info!("MskDSPPlatform 基础服务已停止");
        Ok(())
    }
}

async fn run_trash_maintenance<P, F>(
    mut purge: P,
    period: Duration,
    shutdown: watch::Receiver<bool>,
) where
    P: FnMut() -> F,
    F: Future<Output = Result<usize, crate::modbus_templates::StoreError>>,
{
    let mut interval = tokio::time::interval(period);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let stopping = wait_for_shutdown(shutdown);
    tokio::pin!(stopping);
    loop {
        tokio::select! {
            biased;
            _ = &mut stopping => break,
            _ = interval.tick() => {
                match purge().await {
                    Ok(count) if count > 0 => info!(count, "点表回收站过期记录已清理"),
                    Ok(_) => {},
                    Err(error) => warn!(error = %error, "点表回收站定时清理失败，将在下次定时任务重试"),
                }
            }
        }
    }
}

fn resolve_data_root(
    explicit_directory: Option<OsString>,
    runtime_directory: Option<OsString>,
    working_directory: &Path,
) -> PathBuf {
    let directory = explicit_directory
        .filter(|directory| !directory.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            runtime_directory
                .filter(|directory| !directory.is_empty())
                .map(|directory| PathBuf::from(directory).join("data"))
        })
        .unwrap_or_else(|| PathBuf::from("data"));
    if directory.is_absolute() {
        directory
    } else {
        working_directory.join(directory)
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
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use tokio::sync::{Notify, oneshot, watch};

    #[test]
    fn data_root_explicit_directory_takes_priority() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        let explicit = std::env::temp_dir().join("mskplt-data");
        let runtime = std::env::temp_dir().join("mskplt-runtime");
        assert_eq!(
            resolve_data_root(
                Some(explicit.clone().into_os_string()),
                Some(runtime.into_os_string()),
                &working_directory,
            ),
            explicit,
        );
    }

    #[test]
    fn data_root_legacy_container_uses_runtime_directory() {
        let working_directory = std::env::temp_dir().join("mskplt-readonly-app");
        let runtime = std::env::temp_dir().join("mskplt-runtime");
        assert_eq!(
            resolve_data_root(
                None,
                Some(runtime.clone().into_os_string()),
                &working_directory
            ),
            runtime.join("data"),
        );
    }

    #[test]
    fn data_root_local_fallback_is_absolute() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        let resolved = resolve_data_root(None, None, &working_directory);
        assert_eq!(resolved, working_directory.join("data"));
        assert!(resolved.is_absolute());
    }

    #[test]
    fn data_root_relative_explicit_directory_is_resolved_from_working_directory() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        assert_eq!(
            resolve_data_root(
                Some("custom-data".into()),
                Some("runtime".into()),
                &working_directory
            ),
            working_directory.join("custom-data"),
        );
    }

    #[test]
    fn data_root_relative_runtime_directory_is_resolved_from_working_directory() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        assert_eq!(
            resolve_data_root(None, Some("runtime".into()), &working_directory),
            working_directory.join("runtime").join("data"),
        );
    }

    #[test]
    fn data_root_empty_explicit_directory_uses_runtime_directory() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        assert_eq!(
            resolve_data_root(Some("".into()), Some("runtime".into()), &working_directory),
            working_directory.join("runtime").join("data"),
        );
    }

    #[test]
    fn data_root_empty_runtime_directory_uses_local_fallback() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        assert_eq!(
            resolve_data_root(None, Some("".into()), &working_directory),
            working_directory.join("data"),
        );
    }

    #[test]
    fn data_root_empty_directories_use_local_fallback() {
        let working_directory = std::env::temp_dir().join("mskplt-app");
        assert_eq!(
            resolve_data_root(Some("".into()), Some("".into()), &working_directory),
            working_directory.join("data"),
        );
    }

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

    // 定时清理一次失败后仍继续重试，退出信号终止任务，不能留下后台循环。
    #[tokio::test]
    async fn trash_maintenance_retries_errors_and_stops_on_shutdown() {
        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let calls = Arc::new(AtomicUsize::new(0));
        let retried = Arc::new(Notify::new());
        let observed_calls = calls.clone();
        let observed_retry = retried.clone();
        let task = tokio::spawn(run_trash_maintenance(
            move || {
                let count = observed_calls.fetch_add(1, Ordering::SeqCst);
                let retried = observed_retry.clone();
                async move {
                    if count == 0 {
                        Err(crate::modbus_templates::StoreError::Storage(
                            "模拟存储失败".into(),
                        ))
                    } else {
                        retried.notify_one();
                        Ok(1)
                    }
                }
            },
            Duration::from_millis(5),
            shutdown_rx,
        ));
        timeout(Duration::from_secs(1), retried.notified())
            .await
            .unwrap();
        shutdown_tx.send(true).unwrap();
        timeout(Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap();
        assert!(calls.load(Ordering::SeqCst) >= 2);
    }

    // 已收到停止通知时不得先执行新的清理事务。
    #[tokio::test]
    async fn trash_maintenance_skips_work_after_shutdown() {
        let (_, shutdown_rx) = watch::channel(true);
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        run_trash_maintenance(
            move || {
                observed.fetch_add(1, Ordering::SeqCst);
                async { Ok(0) }
            },
            Duration::from_secs(3600),
            shutdown_rx,
        )
        .await;
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
}
