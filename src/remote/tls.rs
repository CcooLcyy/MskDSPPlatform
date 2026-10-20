//! gRPC 服务端 TLS 配置；启用后任何证书错误均阻止监听。
use crate::config::TlsConfig;
use anyhow::{Context, Result};
use tonic::transport::{Certificate, Identity, ServerTlsConfig};
pub async fn server_tls(config: &TlsConfig) -> Result<Option<ServerTlsConfig>> {
    if !config.enabled {
        return Ok(None);
    }
    anyhow::ensure!(
        !config.server_certificate.is_empty() && !config.server_private_key.is_empty(),
        "启用 TLS 必须配置服务端证书和私钥"
    );
    let certificate = tokio::fs::read(&config.server_certificate)
        .await
        .context("读取 gRPC 服务端证书失败")?;
    let key = tokio::fs::read(&config.server_private_key)
        .await
        .context("读取 gRPC 服务端私钥失败")?;
    let mut tls = ServerTlsConfig::new().identity(Identity::from_pem(certificate, key));
    if config.require_client_certificate {
        anyhow::ensure!(
            !config.client_ca_certificate.is_empty(),
            "启用双向 TLS 必须配置客户端 CA"
        );
        let ca = tokio::fs::read(&config.client_ca_certificate)
            .await
            .context("读取客户端 CA 证书失败")?;
        tls = tls.client_ca_root(Certificate::from_pem(ca));
    }
    Ok(Some(tls))
}
#[cfg(test)]
#[path = "tls_tests.rs"]
mod tests;
