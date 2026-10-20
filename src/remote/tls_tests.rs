use super::*;
// TLS 配置读取必须检查证书与私钥路径，不能无声回落到明文监听。
#[tokio::test]
async fn enabled_tls_requires_readable_certificate_and_key() {
    let config = crate::config::TlsConfig {
        enabled: true,
        ..Default::default()
    };
    assert!(server_tls(&config).await.is_err());
    let disabled = crate::config::TlsConfig::default();
    assert!(server_tls(&disabled).await.unwrap().is_none());
}

// 可读文件不代表合法证书；tonic 必须拒绝无效 PEM，不得打开明文服务。
#[tokio::test]
async fn malformed_tls_identity_is_rejected_by_transport() {
    let dir = tempfile::tempdir().unwrap();
    let certificate = dir.path().join("server.pem");
    let key = dir.path().join("server.key");
    std::fs::write(&certificate, "无效证书").unwrap();
    std::fs::write(&key, "无效私钥").unwrap();
    let config = crate::config::TlsConfig {
        enabled: true,
        server_certificate: certificate.to_string_lossy().into_owned(),
        server_private_key: key.to_string_lossy().into_owned(),
        ..Default::default()
    };
    let tls = server_tls(&config).await.unwrap().unwrap();
    assert!(tonic::transport::Server::builder().tls_config(tls).is_err());
}
