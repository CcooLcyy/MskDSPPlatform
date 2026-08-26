use anyhow::{Context, Result};
use serde::Deserialize;
use std::{env, fs, net::SocketAddr, path::Path};

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    pub server: ServerConfig,
    pub runtime: RuntimeConfig,
    pub tls: TlsConfig,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct ServerConfig {
    pub http_addr: SocketAddr,
    pub grpc_addr: SocketAddr,
    pub shutdown_timeout_seconds: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct RuntimeConfig {
    pub protocol_version: String,
    pub environment: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct TlsConfig {
    pub enabled: bool,
    pub server_certificate: String,
    pub server_private_key: String,
    pub client_ca_certificate: String,
    pub require_client_certificate: bool,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            server: ServerConfig::default(),
            runtime: RuntimeConfig::default(),
            tls: TlsConfig::default(),
        }
    }
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            http_addr: "127.0.0.1:8080"
                .parse()
                .expect("valid default HTTP address"),
            grpc_addr: "127.0.0.1:50051"
                .parse()
                .expect("valid default gRPC address"),
            shutdown_timeout_seconds: 10,
        }
    }
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            protocol_version: "platform.v1".to_string(),
            environment: "development".to_string(),
        }
    }
}

impl Default for TlsConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            server_certificate: String::new(),
            server_private_key: String::new(),
            client_ca_certificate: String::new(),
            require_client_certificate: false,
        }
    }
}

impl AppConfig {
    pub fn load() -> Result<Self> {
        let path = env::var("MSKDSP_PLATFORM_CONFIG").unwrap_or_else(|_| "config.toml".into());
        if !Path::new(&path).exists() {
            return Ok(Self::default());
        }

        let content =
            fs::read_to_string(&path).with_context(|| format!("读取配置文件失败: {path}"))?;
        toml::from_str(&content).with_context(|| format!("解析配置文件失败: {path}"))
    }
}
