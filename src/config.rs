use anyhow::{Context, Result};
use serde::Deserialize;
use std::{env, fs, net::SocketAddr, path::Path};

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    pub server: ServerConfig,
    pub runtime: RuntimeConfig,
    pub auth: crate::auth::AuthConfig,
    pub software: SoftwareConfig,
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
pub struct SoftwareConfig {
    pub public_base_url: String,
    pub publisher_token_sha256: String,
    pub ci_upload_token_sha256: String,
    pub max_package_bytes: u64,
    pub upload_retention_seconds: u64,
}

impl Default for SoftwareConfig {
    fn default() -> Self {
        Self {
            public_base_url: String::new(),
            publisher_token_sha256: String::new(),
            ci_upload_token_sha256: String::new(),
            max_package_bytes: 8 * 1024 * 1024 * 1024,
            upload_retention_seconds: 86400,
        }
    }
}

impl SoftwareConfig {
    pub fn validate(&self) -> Result<()> {
        if !self.public_base_url.is_empty() {
            let url =
                url::Url::parse(&self.public_base_url).context("软件仓库公开下载地址格式不正确")?;
            anyhow::ensure!(
                matches!(url.scheme(), "http" | "https")
                    && url.host_str().is_some()
                    && url.username().is_empty()
                    && url.password().is_none()
                    && url.query().is_none()
                    && url.fragment().is_none(),
                "软件仓库公开下载地址必须为无凭据、查询参数或片段的 HTTP/HTTPS 地址"
            );
        }
        anyhow::ensure!(
            self.publisher_token_sha256.is_empty()
                || (self.publisher_token_sha256.len() == 64
                    && self
                        .publisher_token_sha256
                        .bytes()
                        .all(|c| c.is_ascii_hexdigit())),
            "软件仓库发布令牌摘要必须为空或 64 位十六进制 SHA256"
        );
        anyhow::ensure!(
            self.ci_upload_token_sha256.is_empty()
                || (self.ci_upload_token_sha256.len() == 64
                    && self
                        .ci_upload_token_sha256
                        .bytes()
                        .all(|c| c.is_ascii_hexdigit())),
            "平台 CI 上传令牌摘要必须为空或 64 位十六进制 SHA256"
        );
        anyhow::ensure!(
            self.ci_upload_token_sha256.is_empty()
                || !self
                    .ci_upload_token_sha256
                    .eq_ignore_ascii_case(&self.publisher_token_sha256),
            "平台 CI 上传令牌必须与软件发布令牌分别配置"
        );
        anyhow::ensure!(
            (1..=8 * 1024 * 1024 * 1024).contains(&self.max_package_bytes),
            "软件安装包大小上限必须为 1 字节到 8 GiB"
        );
        anyhow::ensure!(
            (60..=7 * 86400).contains(&self.upload_retention_seconds),
            "软件上传任务保留时间必须为 60 秒到 7 天"
        );
        Ok(())
    }
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            server: ServerConfig::default(),
            runtime: RuntimeConfig::default(),
            auth: crate::auth::AuthConfig::default(),
            software: SoftwareConfig::default(),
        }
    }
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            http_addr: "127.0.0.1:8080"
                .parse()
                .expect("valid default HTTP address"),
            grpc_addr: "127.0.0.1:50000"
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

impl AppConfig {
    pub fn load() -> Result<Self> {
        let path = env::var("MSKDSP_PLATFORM_CONFIG").unwrap_or_else(|_| "config.toml".into());
        let mut config = if Path::new(&path).exists() {
            let content =
                fs::read_to_string(&path).with_context(|| format!("读取配置文件失败: {path}"))?;
            toml::from_str(&content).with_context(|| format!("解析配置文件失败: {path}"))?
        } else {
            Self::default()
        };
        // 常驻管理进程仅覆盖子进程 HTTP 地址，原配置文件保持不变。
        if let Ok(address) = env::var("MSKDSP_PLATFORM_HTTP_ADDR") {
            config.server.http_addr = address.parse().context("子进程 HTTP 地址格式不正确")?;
        }
        config.software.validate()?;
        Ok(config)
    }
}

#[cfg(test)]
mod software_tests {
    use super::*;

    // 已退出使用的旧证书字段作为未知配置忽略，不影响统一明文平台启动。
    #[tokio::test]
    async fn legacy_certificate_settings_do_not_affect_plaintext_runtime() {
        for enabled in [false, true] {
            let text = format!(
                "[tls]\nenabled={enabled}\nserver_certificate='不存在的旧证书.pem'\nserver_private_key='不存在的旧私钥.pem'\n"
            );
            let config: AppConfig = toml::from_str(&text).unwrap();
            crate::runtime::PlatformRuntime::new(config).await.unwrap();
        }
    }

    // 旧配置未提供软件仓库配置时仍可启动，发布令牌保持禁用。
    #[test]
    fn old_configuration_keeps_software_publication_disabled() {
        let config: AppConfig = toml::from_str("[server]\nhttp_addr='127.0.0.1:8080'\n").unwrap();
        assert!(config.software.public_base_url.is_empty());
        assert!(config.software.publisher_token_sha256.is_empty());
        assert!(config.software.ci_upload_token_sha256.is_empty());
        config.software.validate().unwrap();
    }

    // 下载地址拒绝凭据、查询、片段和非 HTTP 协议，避免生成歧义或泄密清单。
    #[test]
    fn invalid_public_download_origins_are_rejected() {
        for value in [
            "file:///tmp/pkg",
            "https://user:password@example.com",
            "https://example.com?x=1",
            "https://example.com/#x",
        ] {
            let config = SoftwareConfig {
                public_base_url: value.into(),
                ..SoftwareConfig::default()
            };
            assert!(config.validate().is_err(), "{value}");
        }
        for value in ["http://127.0.0.1:8080", "https://example.com/platform"] {
            let config = SoftwareConfig {
                public_base_url: value.into(),
                ..SoftwareConfig::default()
            };
            config.validate().unwrap();
        }
    }

    // 限额和令牌摘要配置错误必须在监听之前失败。
    #[test]
    fn invalid_limits_and_publisher_digest_are_rejected() {
        let mut config = SoftwareConfig {
            max_package_bytes: 0,
            ..SoftwareConfig::default()
        };
        assert!(config.validate().is_err());
        config.max_package_bytes = 1024;
        config.publisher_token_sha256 = "not-a-sha256".into();
        assert!(config.validate().is_err());
        config.publisher_token_sha256 = "ab".repeat(32);
        config.upload_retention_seconds = 0;
        assert!(config.validate().is_err());
        config.upload_retention_seconds = 86400;
        config.validate().unwrap();
    }

    // CI 令牌可独立启用，但不得使用明文或与发布令牌复用。
    #[test]
    fn ci_upload_digest_is_independent_and_must_not_reuse_publisher() {
        let mut config = SoftwareConfig {
            ci_upload_token_sha256: "明文令牌".into(),
            ..Default::default()
        };
        assert!(config.validate().is_err());
        config.ci_upload_token_sha256 = "ab".repeat(32);
        config.validate().unwrap();
        config.publisher_token_sha256 = "AB".repeat(32);
        assert!(config.validate().is_err());
        config.publisher_token_sha256 = "cd".repeat(32);
        config.validate().unwrap();
    }
}
