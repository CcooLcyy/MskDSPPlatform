mod config;
mod error;
mod grpc;
mod http;
mod runtime;
mod state;

use anyhow::Result;
use config::AppConfig;
use runtime::PlatformRuntime;
use tracing::info;

#[tokio::main]
async fn main() -> Result<()> {
    init_logging();

    let config = AppConfig::load()?;
    let runtime = PlatformRuntime::new(config).await?;

    info!("MskDSPPlatform 基础服务启动");
    runtime.run().await
}

fn init_logging() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mskdsp_platform=info,tower_http=info".into()),
        )
        .with_target(false)
        .compact()
        .init();
}
