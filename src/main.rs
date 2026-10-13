mod auth;
mod build_info;
mod config;
mod error;
mod grpc;
mod http;
mod modbus_templates;
mod runtime;
mod state;
mod template_http;

use anyhow::Result;
use config::AppConfig;
use runtime::PlatformRuntime;
use tracing::info;

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [] => {}
        [flag] if flag == "--version-json" => {
            println!(
                "{}",
                serde_json::to_string(&build_info::BuildInfo::current())?
            );
            return Ok(());
        }
        [flag] if flag == "--hash-admin-password" => {
            use std::io::Read;
            let mut password = String::new();
            std::io::stdin().take(1025).read_to_string(&mut password)?;
            let password = password.trim_end_matches(['\r', '\n']);
            anyhow::ensure!(
                !password.is_empty() && password.len() <= 1024,
                "密码长度必须为 1～1024 字节"
            );
            println!("{}", auth::hash_password(password)?);
            return Ok(());
        }
        [flag, address] if flag == "--probe-grpc" => {
            let protocol_version = AppConfig::load()?.runtime.protocol_version;
            grpc::probe(address, &protocol_version).await?;
            println!("gRPC 握手检查通过");
            return Ok(());
        }
        _ => anyhow::bail!(
            "参数不支持；可使用 --version-json、--hash-admin-password 或 --probe-grpc 主机:端口"
        ),
    }
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
